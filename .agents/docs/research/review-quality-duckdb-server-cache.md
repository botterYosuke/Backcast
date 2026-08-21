# Quality Review: duckdb-server-cache

Review date: 2026-08-21 JST  
Scope: all 22 changed files in `.agents/logs/review-diff-duckdb-server-cache.patch`  
Verdict: **Not ready to merge/cut over**

## Summary

- Critical: **0**
- High: **5**
- Medium: **3**
- Low: **1**
- Focused validation: **89 Python tests passed**, **9 Node tests passed**.
- The happy-path locking, generation-safe compound repository API, normal
  same-stem download coalescing, ContextVar request isolation, operation
  handshake, and frontend supersession logic are substantially sound.
- Merge/cutover is blocked by one reproduced exception-time fail-closed bug,
  one server availability-classification bug, the loss of the distinct
  disk-full policy, incomplete cutover documentation, and the still-undeployed
  live listing endpoint.

## Critical / High Findings

### [High] Reconciliation can raise again and bypass fail-closed marking

- **File:** `src/tickreplay/cache_commit.py:268`
- **Invariant:** Every non-process-killing failure after the live-file replace
  must either reconcile fully or mark the stem unavailable before releasing
  `symbol_lock`.
- **Failing scenario:** Step 5 (`publish_generation`) raises. The `except`
  block calls `reconcile_stem`, which calls the same failing publish operation
  again. That second exception escapes the handler before
  `mark_unavailable()` and before the promised `CommitFailedError` are reached.
  The lock is then released by the context manager while the stem was never
  failed closed.
- **Evidence:** A minimal reproduction replaced `publish_generation` with a
  raising implementation. Output was `RuntimeError publish failure`,
  `live True`, `sidecar True`, `unavailable None`. Tests only cover a
  reconciliation result of `action == "failed"`; they do not cover the
  reconciliation helper itself raising.
- **Recommended fix:** Guard the reconciliation call itself. Convert every
  reconciliation exception into a failed outcome, mark the stem unavailable
  while the symbol lock is still held, then raise `CommitFailedError`. Add
  failpoints for `publish_generation`, hashing/unlinking, and startup sidecar
  replacement. Also make `reconcile_all_at_startup()` reject or fail closed on
  a `failed` outcome instead of ignoring it at `cache_commit.py:176`.

### [High] An unavailable listing directory becomes an authoritative empty listing

- **File:** `cloud-run/main.py:51`
- **Invariant:** 404 is allowed only after a successful authoritative listing
  proves a stem absent; server/storage availability failures must remain 503
  or another retryable 5xx condition.
- **Failing scenario:** The data volume is not mounted, permissions are lost,
  or `os.listdir()` otherwise raises `OSError`. The endpoint catches the error
  and returns HTTP 200 with `{"stems": []}`. The client treats that as a live,
  authoritative listing, so every requested stem becomes 404 instead of 503.
- **Evidence:** `tests/test_cloud_run_main.py:78` explicitly pins the incorrect
  missing-directory-as-empty behavior. `TickRepository._confirm_stem_known()`
  correctly distinguishes live from unavailable listings, but cannot recover
  after the server has mislabeled the failure as success.
- **Recommended fix:** Return a retryable 5xx (preferably 503) when the listing
  directory is missing or unreadable; reserve 200 + empty list for a directory
  that was opened successfully and is genuinely empty. Replace the current
  test and add a permission/read-failure case.

### [High] Disk-full is silently treated as an offline stale-cache fallback

- **File:** `src/tickreplay/repository.py:442`
- **Invariant:** `DiskFullError` must remain distinct from server
  unavailability for the offline fallback policy.
- **Failing scenario:** A conditional refresh writes to a full cache volume
  while an old live file exists. `_download_and_commit()` catches the entire
  `DownloadError` hierarchy and returns normally solely because
  `conditional=True` and the live file exists. The operation becomes
  `stale-served`; the UI message says the server is unreachable, hiding the
  actionable local storage failure.
- **Evidence:** A minimal injected `DiskFullError` reproduction returned
  normally. The approved plan lines 227-230 explicitly require the opposite.
  Existing tests prove only that staging raises `DiskFullError`, not that the
  repository/server preserve it.
- **Recommended fix:** Permit stale fallback only for the explicitly accepted
  network/server-unavailability errors. Propagate disk-full/local-write
  failures to a distinct terminal outcome and display their real message. Add
  a repository/server integration test with an existing live cache.

### [High] The immediate config cutover is undocumented and the shipped docs instruct a broken setup

- **File:** `docs/tick-replay.md:27`
- **Invariant:** Step 9 requires the new environment variables, cutover note,
  cache lifecycle, no-resume/single-process/authenticity trade-offs, and the
  ordered rollback procedure before rollout.
- **Failing scenario:** An operator follows the current document, sets only
  `BACKCAST_JQUANTS_DUCKDB_ROOT`, and starts the new release. That resolver has
  been deleted, so lifespan startup fails because
  `BACKCAST_DUCKDB_CACHE_DIR` is absent. There is no root `.env.example` to
  correct the instruction and no post-cutover rollback order preserving the
  old environment value.
- **Evidence:** `docs/tick-replay.md:27-29` and `:102` still document only the
  removed setting. Root `.env.example` does not exist; the changed
  `cloud-run/.env.example` configures the file server and is not a client
  replacement.
- **Recommended fix:** Complete Step 9 before merge: add the root example,
  update the guide, record the cutover warning and accepted cache policies,
  and include the approved client-first post-cutover rollback procedure.

### [High] The mandatory server-first deployment gate still fails live

- **File:** `src/tickreplay/config.py:24`
- **Invariant:** The configured default server must expose the listing route
  before any cut-over client depends on it.
- **Failing scenario:** A client starts with the default URL, requests
  `/api/stocks-trades`, and receives 404 because the local route in
  `cloud-run/main.py:40` has not been deployed. With no persisted listing or
  cache, all symbol existence is indeterminate and the app cannot load data.
- **Evidence:** Read-only live probe at 2026-08-21 20:17 JST:
  `GET http://backcast.i234.me:8080/api/stocks-trades` returned
  `HTTP/1.1 404 NOT FOUND` from gunicorn. This independently reconfirms the
  earlier handoff audit.
- **Recommended fix:** Deploy the server endpoint first and run the complete
  live 200/206/304/404/416 and matching/mismatching `If-Range` matrix. Only
  then stage the client cutover/canary. This is an operational blocker, not a
  defect in the local route implementation.

## Medium / Low Findings

### [Medium] Lifespan transition can overlap an old close with a new construction

- **File:** `src/tickreplay/server.py:224`
- **Scenario:** The last lifespan detaches `_repository` under
  `_construction_lock`, then closes DuckDB/HTTP handles after releasing that
  lock. A concurrent lifespan can observe `None`, construct a second
  repository for the same cache, and run reconciliation before the old pool
  is closed. Background operation threads are also not joined or cancelled on
  shutdown (`server.py:129`). On Windows this can recreate the open-handle
  condition the commit coordinator was designed to prevent.
- **Recommended fix:** Add an explicit closing state/condition so a new
  acquisition waits for close completion, and coordinate tracker workers with
  repository shutdown. Add concurrent exit/entry and shutdown-during-download
  tests; current tests cover nested and sequential re-entry only.

### [Medium] The global coalescer key is only the stem

- **File:** `src/tickreplay/cache.py:399`
- **Scenario:** If two repository instances overlap (possible through the
  lifecycle race above) and use different cache directories or clients, a
  same-stem follower receives the leader's `StagingResult`, including the
  leader's `.part` path, rather than staging into its own cache. The normal
  singleton path is safe, but the global key silently assumes it.
- **Recommended fix:** Scope the coalescer to a repository/cache manager or
  key by immutable cache identity plus stem. Add a retry-after-failure test and
  a cross-cache isolation test.

### [Medium] Polling has no total/no-progress deadline

- **File:** `src/tickreplay/static/request-coordinator.mjs:215`
- **Scenario:** Repeated successful snapshots with the expected operation ID
  but no increasing terminal revision poll forever; `maxPollFailures` applies
  only to rejected fetches. A hung worker or a terminal-publication bug leaves
  the UI permanently busy.
- **Recommended fix:** Add a bounded total duration or no-progress deadline,
  surface a retryable timeout, and test it with repeated unchanged snapshots.

### [Low] Deleted resolver names remain in production comments and fail the literal grep gate

- **File:** `src/tickreplay/config.py:6`
- **Evidence:** The old env var and resolver identifiers remain at lines 6-7
  and 80. They are not executable compatibility paths, but the approved
  grep-based removal check is not clean and the wording suggests a resolver
  that no longer exists.
- **Recommended fix:** Rewrite the comments in terms of the legacy local-root
  configuration without retaining executable identifier names, or narrow the
  gate deliberately to imports/calls.

## Independently Verified Invariants

- **Atomic ownership / eviction window:** Only `cache_commit.py` replaces live
  DuckDB files. Reads and commits use the same per-stem lock, and pool access
  remains inside that lock. The step-2 barrier test meaningfully pins the
  eviction-to-replace window.
- **Generation-safe repository API:** `resolve_and_load_session()` performs
  metadata resolution, session search, and load under one symbol-lock span;
  the obsolete split public API is gone.
- **Operation identity:** `OperationTracker` allocates a globally monotonic
  process-local operation ID under a lock and resets revision per operation.
  The frontend guard resets revision on each handshake and the second-operation
  Node test passes.
- **DownloadCoalescer normal path:** Same-stem callers share the same Future;
  leader success and failure are propagated to followers and the key is
  released afterward. Both focused tests pass.
- **ContextVar propagation:** Observation is installed and reset in the worker
  thread containing repository/staging work; each caller records its own
  returned shared result/error. The server stale-network integration test
  confirms the intended request outcome and once-per-process behavior.
- **Status/start handshake and frontend races:** The pending response carries
  the exact server epoch and operation ID; polling accepts only exact identity
  plus increasing revision. Superseded requests are aborted and checked again
  after each fetch. All nine Node race tests pass.
- **Stale network behavior:** A true transport failure with an existing cache
  becomes `stale-served`, is terminal in the UI, and refresh is attempted only
  once per process. The exception-class boundary is the defect described
  above.
- **Lifespan reference count:** Nested and sequential lifespans reuse/close the
  repository correctly in the tested paths. The untested concurrent boundary
  is reported separately.

## Validation Performed

```text
uv run pytest tests/test_cloud_run_main.py tests/test_tickreplay_config.py \
  tests/test_tickreplay_cache.py tests/test_tickreplay_cache_commit.py \
  tests/test_tickreplay_repository.py tests/test_tickreplay_server.py -q
=> 89 passed, 1 Starlette deprecation warning

node --test src/tickreplay/static/request-coordinator.test.mjs
=> 9 passed

Minimal commit failure reproduction
=> RuntimeError publish failure; live=True; sidecar=True; unavailable=None

Minimal conditional DiskFullError reproduction
=> returned normally

Live listing probe
=> HTTP/1.1 404 NOT FOUND
```

Full-project pytest, Ruff, formatting, and ty were not rerun in this reviewer
turn because the lead requested immediate finalization. The prior handoff audit
records targeted Ruff/format/ty passing before the final frontend files were
added; Node syntax/behavior is covered by the passing test run above.

## Codex Consultation

Mandatory Codex consultation did **not** produce review findings:

1. First wrapper call returned `ok: true`, but Codex received only the heading
   `# Objective` and answered with a 35-character request for more objective
   text. The archived wrapper prompt itself was complete, so wrapper exit 0
   was not accepted as substantive success.
2. The one permitted retry changed the same prompt's first line to instruct
   Codex to open and read that full prompt file. It ran for about six minutes
   but left a 0-byte response file. It was interrupted when the lead requested
   immediate finalization.

There were therefore **no Codex claims to trust or independently verify**.
All findings and verified invariants in this report were derived independently
from current source, tests, focused executions, and the live read-only probe.


# Test Coverage Review: DuckDB Server Cache

Date: 2026-08-21  
Slug: `duckdb-server-cache`  
Review scope: all 22 paths in `.agents/logs/review-diff-duckdb-server-cache.patch`, the approved revision-5 plan, and the implementation handoff.

## Verdict

The focused suites give useful confidence in the local happy paths, but coverage is not yet sufficient for the plan's known data-integrity, generation, handshake, and operational failure modes. There are **17 gaps: 7 High, 6 Medium, and 4 Low**.

Coverage: **not measured**. No `coverage.json`, `coverage.xml`, or `.coverage` report is present, and no percentage is estimated.

No newly skipped or xfailed tests were found. Direct inspection of the gathered patch found no suspicious assertion loosening: deleted tests target the removed local-root and split `find_session`/`load_session` APIs, and equivalent final-shape behavior is covered through the replacement APIs. The automated delegated-diff verifier could not corroborate this because it crashed while decoding the Git diff on Windows; details are in Test Execution Results.

## High-Priority Gaps

### [High] `cloud-run/main.py:list_stocks_trades` and `download_file` — deployed server contract is untested and currently fails its gate

- Missing scenario: an acceptance probe against the configured live server that verifies non-empty listing/200, conditional 304, Range 206 plus `Content-Range`, matching and mismatching `If-Range`, unsatisfiable Range 416, and missing-file 404 after deployment.
- Why existing tests do not cover it: `tests/test_cloud_run_main.py` uses Flask's in-process `test_client` against the worker worktree. It proves Werkzeug behavior locally but cannot prove which code is deployed. The handoff's live probe returned 404 for `/api/stocks-trades`, and `server-contract.md` explicitly says no deployment or live probe was performed. This is a mandatory Step 2 gate before client cutover.

### [High] `src/tickreplay/repository.py:_query_with_corruption_retry` — corrupt live-cache recovery and generation consistency are untested

- Missing scenario: start with a corrupt local `<stem>.duckdb` and a valid remote replacement whose metadata/code/range differs; assert exactly one unconditional GET, successful commit, eviction, generation publish, and that `symbol_info()` and `resolve_and_load_session()` return only metadata/ticks from the replacement generation.
- Why existing tests do not cover it: `test_a_file_that_is_not_a_valid_duckdb_file_raises_corrupt` tests a corrupt *downloaded staging file*. No repository or server test drives the line 465 DuckDB query failure on an already-live cache. In particular, no test detects stale local `SymbolInfo` surviving the forced commit inside a compound session read.

### [High] `src/tickreplay/repository.py:_download_and_commit` — disk-full/local-write errors can be mistaken for degraded offline fallback

- Missing scenario: with an existing valid cache, make conditional refresh raise `DiskFullError` (and separately a non-network local `DownloadError`); assert it is not reported as `stale-served` under the network-offline policy and that the distinct error reaches the tracker/API contract chosen for local failure.
- Why existing tests do not cover it: the disk-full test stops at `cache.stage_download()`. The only degraded end-to-end test injects `httpx.ConnectError`. Repository lines 445-451 currently catch every `DownloadError` and return the old file for any conditional refresh, so the plan's required error-class distinction is not protected by a test.

### [High] `src/tickreplay/repository.py:resolve_and_load_session` — the exact former generation race is not exercised concurrently

- Missing scenario: pause a same-stem commit after connection eviction, start `resolve_and_load_session()` on another thread, prove it blocks, release the commit, then assert both session resolution and tick load use the new file/generation. Cover the reverse ordering too (read holds the symbol lock while commit waits).
- Why existing tests do not cover it: `test_a_refresh_concurrent_with_a_read_never_serves_a_torn_state` invokes only `symbol_info()`. Ordinary session tests have no concurrent commit. The plan specifically replaced split `find_session()`/`load_session()` because a session operation could straddle generations, so testing only metadata-cache invalidation leaves the original failure mode unproven.

### [High] `src/tickreplay/cache_commit.py:reconcile_all_at_startup` and fail-closed reads — reconciliation failure is not tested end to end

- Missing scenario: create the post-file-replace/pre-sidecar-replace crash state, make startup's sidecar replace fail, and assert startup either fails fast or marks the stem unavailable; then assert `path_for`, `symbol_info`, `resolve_and_load_session`, `/api/symbols/{stem}`, and `/api/session` refuse it with 503 until restart/recovery.
- Why existing tests do not cover it: startup tests cover successful completion/discard only. `test_reconciliation_that_cannot_complete_marks_the_stem_unavailable` covers the in-process coordinator and checks only the internal flag, not subsequent reads or HTTP mapping. `reconcile_all_at_startup()` currently ignores each `ReconcileOutcome`, so a failed startup repair is a distinct uncovered path.

### [High] `src/tickreplay/server.py:OperationTracker` and the session/status handshake — consecutive real operations are represented only by frontend mocks

- Missing scenario: drive two sequential operations for the same stem through the real tracker/server (for example, a failed first operation followed by retry); assert globally increasing, never-reused `operationId`, revision reset for the second operation, strictly increasing revisions within each operation, reuse only while in flight, and that a previous terminal snapshot cannot stop the second poller. Exercise a lost/timed-out start response against the real endpoint, not a pre-scripted mock response.
- Why existing tests do not cover it: the server test covers two requests sharing one in-flight operation only. The Node test for operations 10 and 11 supplies those IDs/revisions as fixture data, so it proves filtering but not backend production. The timeout test likewise assumes its retry receives operation 12 rather than proving backend idempotence/coalescing.

### [High] `src/tickreplay/cache_commit.py:CommitCoordinator.refresh` and frontend epoch recovery — required crash/restart drills are absent

- Missing scenario: subprocess-level process termination at each commit boundary followed by real startup reconciliation, plus a server process restart while polling that demonstrates a fresh handshake under the new `serverEpoch`. Also run the live network-kill/orphan-cleanup/404-vs-503 drill and the one-machine canary required by Step 9.
- Why existing tests do not cover it: commit tests synthesize post-crash files in-process; they do not terminate a process or validate durability/flush behavior. The frontend epoch test calls `OperationStatusGuard` directly with fabricated values and does not run the coordinator through re-handshake against a restarted backend. The handoff records all failure drills and canary as unrun.

## Medium-Priority Gaps

### [Medium] `cloud-run/main.py:list_stocks_trades` — storage failure is indistinguishable from an authoritative empty listing

- Missing scenario: unreadable/misconfigured `STOCKDATA_CACHE_DIR` returns a non-success availability response, and the client maps that to indeterminate/503 rather than authoritative 404 for every stem; separately retain a healthy, genuinely empty-directory case.
- Why existing tests do not cover it: `test_list_stocks_trades_is_empty_when_directory_is_absent` explicitly treats a missing directory as successful `{"stems": []}`. That test cannot detect a server-storage outage being converted into false absence under the plan's 404-vs-503 policy.

### [Medium] `src/tickreplay/repository.py:_ensure_fresh_locked` — successful 304 revalidation is not verified once per process

- Missing scenario: restart with an existing valid live file and valid sidecar, return 304 to the first conditional access, assert no commit/generation bump, then perform multiple symbol/session accesses and assert there is exactly one conditional GET for the process lifetime.
- Why existing tests do not cover it: cache unit tests check conditional headers and 304 in isolation. The server's once-per-process test covers only a failed offline revalidation. No repository/server test protects the successful 304 path and `_revalidated` transition together.

### [Medium] `src/tickreplay/cache_commit.py:CommitCoordinator.refresh_locked` — early commit boundaries with an existing consistent pair are incomplete

- Missing scenario: inject exceptions after sidecar-temp write, during/after eviction, and when the live-file replace itself raises, while an old live file *and old live sidecar* exist; assert both remain mutually consistent, the temp is handled as specified, no generation is published, and the old file stays queryable.
- Why existing tests do not cover it: the orphan-temp startup test has an old live file but no old sidecar and synthesizes state rather than exercising `refresh_locked`. Existing exception injection starts after the file replace or after the sidecar replace.

### [Medium] `src/tickreplay/static/request-coordinator.mjs:#pollOperation` — retry/backoff and failing terminal states have no assertions

- Missing scenario: repeated status fetch failures assert exponential/capped wait values, reset after a successful poll, abort during the wait, and error after `maxPollFailures`; separately assert `corrupt` and `missing` produce `OperationFailedError` and never reissue the data request.
- Why existing tests do not cover it: all nine Node cases use successful status requests. `corrupt` appears only as a mismatching operation that is ignored, not as the matching terminal failure. No test inspects `wait` calls or the failure limit.

### [Medium] `src/tickreplay/static/app.js` and `index.html` — browser wiring/degraded UI has no integration test

- Missing scenario: a minimal DOM/browser test loads the module script, proves `app.js` imports the coordinator, routes symbols/symbol-info/session/status through it, disables/re-enables the load button correctly, and keeps the degraded warning after applying stale cached playback. Assert `/static/request-coordinator.mjs` is served as well.
- Why existing tests do not cover it: Node tests import only the coordinator module. `test_index_and_static_assets_are_served` checks index text and the chart vendor file, but not the module script tag, the new module asset, or UI behavior.

### [Medium] `src/tickreplay/cache.py:_consume_response` — HTTP/header boundaries are incomplete

- Missing scenario: missing `Content-Length`, malformed/negative `Content-Length`, timeout during streaming, unexpected 3xx/4xx (other than 304/404), and a retry that leaves no stale `.part`; assert the documented error class and cleanup for each.
- Why existing tests do not cover it: current tests cover a numeric length mismatch, connect error, 503, 404, invalid DuckDB bytes, and disk full. The header parse at line 314 and several status/stream failure branches remain unexercised.

### [Medium] `src/tickreplay/server.py:_ensure_ready_or_pending` — cross-endpoint same-stem coalescing is not covered

- Missing scenario: while `/api/symbols/{stem}` has started a slow download, call `/api/session` (and the reverse); assert both handshakes return the exact same operation ID, only one download runs, and both final endpoint responses succeed after completion.
- Why existing tests do not cover it: the current server handshake test overlaps `/api/session` with a second `/api/session` only. The shared tracker behavior across endpoint kinds is relied on by browser bootstrap but not tested.

## Low-Priority Gaps

### [Low] `src/tickreplay/static/request-coordinator.mjs:#request` — actual abort-signal propagation is not asserted

- Missing scenario: have `fetchJson` record its `AbortSignal`, supersede the request, assert `signal.aborted`, and reject with a native-style `AbortError`; also assert `onActivityChange` has balanced transitions.
- Why existing tests do not cover it: the overlap tests use deferred promises that ignore the supplied signal. They prove stale-result suppression, not `AbortController` cancellation itself.

### [Low] concurrency tests in `test_tickreplay_cache.py` and `test_tickreplay_repository.py` — synchronization is partly timing based

- Missing scenario: replace `time.sleep(0.1)` scheduling assumptions with explicit entered/waiting barriers; capture all worker exceptions and assert every thread terminates.
- Why existing tests do not cover it: same-stem coalescing and read-blocking tests use fixed sleeps, and some commit threads do not store exceptions or assert they are no longer alive. This can become flaky or allow a background thread failure to be reported only indirectly.

### [Low] `src/tickreplay/repository.py:_read_persisted_listing` / `_write_persisted_listing` — fallback-file edge cases are not covered

- Missing scenario: corrupt JSON, non-list `stems`, invalid/mixed entries, atomic replace/write failure, and a leftover `_listing.json.tmp`; assert safe fallback to local valid `.duckdb` stems and strict client re-filtering.
- Why existing tests do not cover it: the one persisted-listing test writes a valid listing through a prior successful repository call. No malformed or write-failure path is exercised.

### [Low] `src/tickreplay/config.py:_normalize_server_url` / `resolve_cache_config` — validation boundaries are incomplete

- Missing scenario: `http://`/`https://` with no host, whitespace-only or malformed URL components, and cache directory creation denied by `OSError`; assert `CacheConfigError` with the correct variable/path context.
- Why existing tests do not cover it: the suite tests missing scheme, file-in-place-of-directory, and successful directory creation, but not invalid host structure or the mkdir failure branch.

## Existing Coverage Strengths

- Local file-server tests cover sorted listing, legacy whitelist compatibility, 200/304/404/206/416, and matching/mismatching `If-Range` with response bytes/header assertions.
- Download staging covers successful streamed verification, conditional headers, corrupt sidecar, retryable transport/5xx, length mismatch, invalid DuckDB, disk-full cleanup, same-stem success/failure coalescing, and orphan `.part` removal.
- Commit tests cover first commit, a same-lock barrier, Windows open-handle replace, two in-process post-replace failures, unavailable marking, and several synthesized startup states.
- Repository tests preserve ordinary session behavior, listing fallback, 404-vs-indeterminate distinction, per-stem lock independence, and metadata-cache invalidation for `symbol_info()`.
- Server tests protect no-construction status, fail-fast config, once-only startup hooks, nested/sequential lifespan ownership, pending handshake reuse, and network-offline `stale-served`.
- Node tests cover out-of-order response suppression, operation/revision filtering, cache-hit no-poll, selected-stem cancellation, fixture-level epoch mismatch, timeout retry, and stale cached playback.

## Deleted / Weakened / Skipped Test Audit

- Skips/xfails added: none found.
- Deleted test functions/assertions: present because the final cutover deletes `resolve_data_root`, `find_session`, `load_session`, and the old status schema. Replacement tests exercise the new cache config, compound session API, startup lifecycle, and status contract. No deletion appears intended merely to make a failing suite green.
- Assertions: new happy/error/race assertions are generally specific. The one semantic concern is the absent server directory being asserted as an authoritative empty listing; it is reported as a Medium gap rather than test cheating.
- External dependencies: HTTP is consistently mocked for unit/integration tests. The missing live deployment gate is explicitly not substitutable by those mocks.

## Test Execution Results

Results supplied by the independent integration pass (not rerun during this read-only review):

- Focused Python suite: **89 passed / 89 total**.
- Node suite: **9 passed / 9 total**.
- Ruff, Ruff format, ty, and diff-check: passed.
- Full repository suite: **703 passed, 317 failed**. The failures are concentrated in Windows-only orchestration/CLI/skill tests rather than `tickreplay`/`cloud-run`; therefore they do not negate the focused results, but Step 9's full-green gate remains unmet.
- Coverage: **not measured**.

Reviewer-only command executed:

```text
uv run python .agents/skills/_shared/verify_delegation.py --base main
=> exit 1
=> Windows subprocess reader failed to decode the Git diff as cp932
   (UnicodeDecodeError), then parse_diff received None and raised AttributeError.
```

The failure is in the shared verifier, not the implementation tests. Direct inspection of the already-gathered UTF-8 patch was used for the skip/deletion/assertion audit.

## Recommended Test Order

1. Add High gaps for live contract, corrupt-live recovery, disk-full classification, compound-session concurrency, startup fail-closed, and real consecutive operation handshakes.
2. Run the subprocess crash/restart drills and one-machine canary before declaring Step 9 complete.
3. Add Medium frontend/backoff/cross-endpoint and successful-304 integration tests.
4. Produce a fresh coverage report only after the test matrix is complete; until then keep reporting coverage as **not measured**.

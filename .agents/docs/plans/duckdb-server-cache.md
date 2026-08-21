## Implementation Plan: DuckDB Server-Backed Local Cache

### Purpose

Switch the Backcast tick-replay app's `.duckdb` read path for
`stocks_trades/<stem>.duckdb` files from a local filesystem directory
(`BACKCAST_JQUANTS_DUCKDB_ROOT`) to a download-and-local-cache scheme against
the file server already running at `http://backcast.i234.me:8080`
(`cloud-run/main.py`, verified reachable and serving whitelisted `.duckdb`
files with `ETag`/`Last-Modified`/`Accept-Ranges` headers). This removes the
requirement to keep a locally synced copy of the data root and lets multiple
machines read the same source, without paying WAN latency on point queries
(remote httpfs streaming was evaluated and rejected — see Risks).

This is revision 5 of this document, the version Codex validated as **PASS**
(`.agents/logs/codex/20260821T092202Z-plan-duckdb-server-cache-validate-5.md`)
after four prior rounds of `NEEDS_REVISION`. Revision history, newest first:

- **Revision 5** (this document) fixed the 3 points round 4 left open: a
  session/status handshake gap that could let a poller mistake the
  *previous* download operation's terminal state for the new one's
  completion; an in-process (non-crash) commit-failure path that revision
  4's reconciliation didn't cover (only process-restart was handled); and a
  missing time-ordering for the Step 9 rollback procedure relative to the
  client cutover point. Result: **PASS**, with 5 cosmetic-only notes (folded
  into the steps below) and no remaining correctness/ordering issues
  (`.agents/logs/codex/20260821T092202Z-plan-duckdb-server-cache-validate-5.md`).
- **Revision 4** fixed 4 correctness issues from round 3: a stale
  connection-reopen race in the commit sequence, a revision-counter design
  that would have discarded a second download operation's own early
  progress, an underspecified Node ESM module contract, and a `uv`
  dependency group not actually included in a plain `pytest` run
  (`.agents/logs/codex/20260821T091546Z-plan-duckdb-server-cache-validate-4.md`).
- **Revision 3** addressed 10 gaps from round 2 (crash recovery, a 404-vs-503
  bug, a cache read/invalidate race, a resumable-download contradiction, step
  ordering, startup fail-fast, a deployment gate, status scope, JS modules,
  and a test-dependency gap), but that revision's own restructuring
  introduced the 4 new issues revision 4 then fixed
  (`.agents/logs/codex/20260821T090622Z-plan-duckdb-server-cache-validate-3.md`).
- **Revision 2** restructured the plan around 7 High-risk findings from
  revision 1 (update-ownership split, generation consistency, freshness/
  offline policy, HTTP/integrity contract, multi-process safety, progress
  polling, migration/rollback), but was itself only partially adequate
  (`.agents/logs/codex/20260821T085545Z-plan-duckdb-server-cache-validate-2.md`).
- **Revision 1** was the first draft, reviewed and returned
  `NEEDS_REVISION`
  (`.agents/logs/codex/20260821T083651Z-plan-duckdb-server-cache-validate.md`).

### Scope

**New files**
- `src/tickreplay/cache.py` — download staging: streaming `.part` download
  (always from offset 0 — no resume in v1, see Step 4), retry/backoff,
  integrity check. Does **not** perform the swap-in itself (Codex round 1
  #1).
- `src/tickreplay/cache_commit.py` — the single commit coordinator owning the
  entire "make a staged download live" sequence, including crash-safe
  ordering (Codex round 1 #1, round 2 crash-recovery gap).
- `tests/test_tickreplay_cache.py`, `tests/test_tickreplay_cache_commit.py`
- `tests/test_cloud_run_main.py`
- `.env.example` (does not exist today)
- `src/tickreplay/static/request-coordinator.mjs` — the request-epoch/
  `AbortController`/status-operation logic extracted out of `app.js`. The
  `.mjs` extension (not `.js`) is deliberate (Codex round 3): Node treats
  `.js` as CommonJS by default absent a `package.json` with
  `"type": "module"`, and this repo has neither a `package.json` nor any JS
  bundler under `src/tickreplay/static/` — `.mjs` makes the file
  unambiguously ESM to both Node (for `node --test`) and the browser (via
  `<script type="module">`), with no new config file needed.
- A minimal `node:test` harness for `request-coordinator.mjs`
  (`node --test src/tickreplay/static/*.test.mjs`, or equivalent — exact
  glob decided at implementation time).

**Modified files**
- `cloud-run/main.py` — add `GET /api/stocks-trades` (listing) and verify/fix
  the conditional-GET/Range contract.
- `src/tickreplay/config.py` — new required `BACKCAST_DUCKDB_CACHE_DIR` and
  `BACKCAST_DUCKDB_SERVER_URL` (default `http://backcast.i234.me:8080`),
  added *alongside* the existing resolver in Step 3; the old resolver and env
  var are deleted in Step 6, the same step that switches `repository.py`
  over (Codex round 2: deleting the old path in Step 3 would break `server.py`
  before its own switch-over step runs — see Step 3/Step 6 below).
- `src/tickreplay/repository.py`, `src/tickreplay/server.py`,
  `src/tickreplay/static/app.js`,
  `src/tickreplay/static/index.html` (Codex round 2: needs its `<script>`
  tags updated for the new module), `pyproject.toml`, `uv.lock`,
  `docs/tick-replay.md`, `tests/test_tickreplay_config.py`,
  `tests/test_tickreplay_repository.py`, `tests/test_tickreplay_server.py`.

**Dependencies**
- `httpx` (promoted to runtime; already resolved in `uv.lock` as a dev
  transitive via FastAPI's `TestClient`).
- `flask` and `strawberry-graphql` (Codex round 2: `tests/test_cloud_run_main.py`
  needs `cloud-run/main.py`'s runtime deps, currently listed only in
  `cloud-run/requirements.txt`, not the root `pyproject.toml`/`uv` venv —
  added as a new `[dependency-groups].cloud-run-test` entry, rather than
  standing up a second Python environment just to run one test file). This
  group is also added to `[tool.uv].default-groups` (Codex round 3: a
  non-default `uv` dependency group is **not** installed by a plain
  `uv run pytest` — it needed either this, or every verification command in
  this plan rewritten to `uv run --group cloud-run-test pytest`; adding it to
  `default-groups` keeps the rest of the plan's `pytest`/`ruff`/`ty`
  invocations correct as written).

**Explicitly out of scope (user-confirmed, not gaps)**
- Multi-process safety for a shared cache directory — single-process/
  single-instance assumed per machine, not enforced. (Codex round 2 scored
  this RESOLVED as written; unchanged in this revision.)
- Transport-level authenticity (HTTPS / signed manifest) — plain HTTP is
  consistent with the already-deferred auth decision. SHA-256 (Step 4) is a
  **corruption** check only, not an authenticity guarantee.
- Server-side hosting migration (Cloudflare R2/Workers/Containers,
  `.agents/logs/codex/hosting-choice-*.md`) — explicitly deferred by the
  user.
- **Resumable downloads.** A `.part` file left behind by a crash or restart
  is always discarded and the file is redownloaded in full from offset 0
  (Codex round 2: this resolves a direct contradiction in revision 2, which
  claimed both "delete orphaned `.part` on startup" and "test resume via
  Range/If-Range" — those cannot both be true for the same file. v1 does
  neither: Range/If-Range support is verified on the **server** (Step 2) as
  a general HTTP-correctness property of the file-serving contract, but the
  **client never issues a partial/resumed GET** in v1; only a fresh full GET
  from offset 0, ever.

**Already done (prerequisite infra fix, not part of this feature)**
- `.agents/skills/_shared/codex_consult.py` — fixed two pre-existing Windows
  bugs blocking the Codex consult gate itself: `subprocess.run(["codex", ...])`
  failing with `WinError 2` (fixed via `shutil.which("codex")`'s resolved
  path as `argv[0]`), and `subprocess.run(..., text=True)` decoding UTF-8
  output with the Windows `cp932` codepage and hanging (fixed via
  `encoding="utf-8", errors="replace"`).

### Implementation Steps

#### Step 1: Lock contracts and policy
- [ ] **Stem identifier contract**: the client always re-filters any
      server-provided listing through its own `SYMBOL_STEM_RE`
      (`repository.py:34`, `[0-9A-Z]{4,5}`), regardless of the server's
      looser `ALLOWED_PATHS` (`cloud-run/main.py:29-31`, `\w+`) — defense in
      depth, no server trust assumed.
- [ ] **Cutover semantics**: the new config is required; the old resolver and
      `BACKCAST_JQUANTS_DUCKDB_ROOT` are deleted in the same step that
      switches the code that uses them (Step 6), not before (Codex round 2 —
      see Step 3/Step 6).
- [ ] **Freshness policy**: revalidate (conditional GET) at most once per
      symbol per app process lifetime, on that symbol's first access.
- [ ] **Existence-vs-availability policy** (Codex round 2, fixing a bug in
      revision 2's offline policy): a stem is **404 (does not exist)** only
      when the *authoritative live listing* was fetched successfully and the
      stem is absent from it. If the live listing cannot be fetched (offline
      / network failure) and the stem is absent from both the last-persisted
      listing and the local cache, the correct response is **503
      (existence indeterminate)** — never 404. 404 must never be returned
      purely because of a local/network failure to check.
- [ ] **No resumable downloads in v1** (Codex round 2 — see Scope): orphaned
      `.part` files are always discarded and redownloaded from scratch;
      Range/If-Range on the server is verified for general correctness only,
      not used by the client for resume.
- [ ] **Single-process assumption**: not enforced; recorded in Risks.
- [ ] **Status operation identity** (Codex round 2, redesigned in round 3
      after the per-stem-reset scheme was found to discard a second
      operation's own early progress): every download operation (for any
      stem) is assigned a globally monotonic, **never reset, never reused**
      `operationId` (a single counter shared across all stems, incremented
      each time any new operation starts) plus a within-operation `revision`
      that does reset to 0 for each new operation. The frontend (Step 8)
      compares `operationId` first: a response whose `operationId` is less
      than the last one applied for that stem is discarded outright,
      regardless of its `revision`; among responses sharing the current
      `operationId`, only a strictly increasing `revision` is applied. A
      `serverEpoch` (process start identifier) is also included so the
      client can detect a server restart and reset its own last-seen state
      rather than comparing stale operation IDs against a new process's
      counter that restarted from zero.
- [ ] **Cache state schema**: enumerate `missing` / `downloading` / `fresh` /
      `stale-served` (degraded) / `corrupt`, and what `/api/status` and
      `/api/session` return for each.

**Verification**: This doc has no unresolved bullet under Step 1, and every
state/response pairing above is concrete enough for a later step to
implement against without re-deciding it.

#### Step 2: Server-side listing and transfer contract (deploy before Step 5)
- [ ] Add `GET /api/stocks-trades` to `cloud-run/main.py` returning a sorted,
      de-duplicated JSON list of available `stocks_trades` stems.
- [ ] Verify against the **live** server: `Range`/`If-Range` (partial `206` +
      `Content-Range`, `416` for an unsatisfiable range), conditional GET
      (`If-None-Match`/`If-Modified-Since` → `304`), and the already-confirmed
      `200`/`404` cases. Fix `cloud-run/main.py` if any are missing.
- [ ] `tests/test_cloud_run_main.py` (using the new `cloud-run-test`
      dependency group — see Scope) covering the listing endpoint and the
      conditional/range paths against a fixture `DATA_DIR`.
- [ ] **Deployment gate** (Codex round 2): deploy this endpoint to the live
      `backcast.i234.me:8080` and re-verify live `200`/`206`/`304`/`404`
      *before* any client code from Step 5 onward starts depending on it —
      Step 2 is not "done" until this live check passes, since every later
      step assumes the listing endpoint already exists in production.

**Verification**: `pytest tests/test_cloud_run_main.py` passes; a manual
`curl -I -H "If-None-Match: <etag>"` and `curl -H "Range: bytes=0-99"`
against the **live, redeployed** server return `304` and `206`.

#### Step 3: Add new config alongside the old (no deletion yet)
- [ ] Add `BACKCAST_DUCKDB_SERVER_URL` and `BACKCAST_DUCKDB_CACHE_DIR`
      (required once code actually switches to them in Step 6) to
      `config.py`, **without removing** `resolve_data_root`/
      `resolve_trades_dir`/`BACKCAST_JQUANTS_DUCKDB_ROOT` yet — `server.py`
      still depends on the old resolver until Step 6, and each step must
      leave the app in a working, testable state (Codex round 2: revision
      2's Step 3 deleted the old resolver immediately, which would break
      `server.py` for the three steps in between).
- [ ] Promote `httpx` to a direct runtime dependency; add the
      `cloud-run-test` dev dependency group (Scope); regenerate `uv.lock`.
- [ ] Design the HTTP-transport injection seam (an injectable
      `httpx.Client`/transport) for Steps 4-6's tests.

**Verification**: New config tests cover URL normalization and HTTP-client
injection for the *new* vars; existing `resolve_data_root` tests still pass
unchanged, since nothing was removed yet.

#### Step 4: Download staging (`src/tickreplay/cache.py`)
This module's only job is: given a stem, produce a verified, complete file at
a staging path, or raise. It never touches `TickRepository`'s connection pool
or `_info_cache`.
- [ ] Stream the download to a `.part` file, always starting at offset 0 (no
      resume — Step 1/Scope). Timeout, retry with backoff, validate
      `Content-Length` against bytes actually received.
- [ ] On startup (wired via Step 7's lifespan hook), discard any orphaned
      `.part` file unconditionally — there is nothing to resume.
- [ ] On disk-full during streaming, fail cleanly, remove the partial
      `.part`, and surface a distinct error code — never conflated with "the
      server is unreachable" for the purposes of Step 1's offline-fallback
      policy.
- [ ] Verify the staged file: `Content-Length` match, then open with
      `duckdb.connect(path, read_only=True)` as a structural validity check.
      Compute and store a SHA-256 as a **corruption**-only check (Scope).
- [ ] Conditional GET against the stored `ETag`/`Last-Modified` sidecar
      (Step 1's once-per-session-per-symbol policy); on a local file that
      fails the DuckDB-open check, force exactly one unconditional
      redownload.
- [ ] Per-symbol download coalescing so concurrent callers for the same stem
      share one in-flight download.

**Verification**: `tests/test_tickreplay_cache.py` — failpoints at each
commit boundary, a corrupted sidecar, a simulated disk-full mid-stream, two
concurrent requests for the same stem resulting in one download, and an
explicit test that an orphaned `.part` from a prior run is deleted (not
resumed) on the next startup.

#### Step 5: Atomic commit coordinator (`src/tickreplay/cache_commit.py`)
A single coordinator owns the entire sequence from "Step 4 produced a
verified staged file" to "the repository is safe to use the new generation."
- [ ] **The per-symbol operation lock is the *only* gate to the connection
      pool and cache for that stem — no CAS-only shortcut** (Codex round 3,
      fixing a stale-connection-reopen race introduced in revision 3): every
      generation-dependent read (`symbol_info()`, the compound
      `resolve_and_load_session()` from Step 6, and any `_ConnectionPool`
      access for that stem) must acquire this same per-symbol lock *before*
      touching the pool, and the commit coordinator holds it for the entire
      sequence below, start to finish — not just around the eviction step.
      Revision 3 allowed an "equivalent compare-and-set" alternative to
      lock-holding for the cache write; that alternative is removed, because
      it left a window, between "connection evicted" and "file replaced,"
      where a concurrent reader not holding the lock could call
      `_ConnectionPool.query()` (`repository.py:117-128`, which lazily
      reopens any evicted path on the next call) and reopen a connection to
      the *old* file right before the replace — defeating the eviction and,
      on Windows, potentially making the subsequent `os.replace` fail
      against that freshly reopened handle. With a single lock guarding both
      all reads and the whole commit, this window cannot exist.
- [ ] Crash-safe commit ordering, under that one lock, held for all of:
      1. write the new sidecar (`ETag`/`Last-Modified`/`SHA-256`/new
         generation number) to `sidecar.tmp` next to the live sidecar;
      2. evict this stem's pooled connection in `TickRepository`;
      3. `os.replace(staged_file, live_file)`;
      4. `os.replace(sidecar.tmp, live_sidecar)`;
      5. bump the in-memory generation counter and invalidate
         `_info_cache[stem]`;
      6. release the symbol lock.
      **Invariant per failpoint**: a crash between 1-2 leaves the live file
      and sidecar untouched and consistent (only an orphan `sidecar.tmp` to
      clean up, alongside Step 4's `.part` cleanup, both discarded on
      startup). A crash between 3-4 leaves the live *file* already new but
      the *sidecar* still describing the old one — startup reconciliation
      (next bullet) must detect this specific mismatch and complete step 4
      from the surviving `sidecar.tmp`, never re-download when the live file
      is already correct. A crash after 4 is fully committed. Because no
      other code path may touch the pool for this stem without the lock, no
      reader can observe or create an intermediate state during 1-6.
- [ ] Startup reconciliation (wired via Step 7's lifespan hook): for every
      stem with a leftover `sidecar.tmp`, apply the invariant above — finish
      the swap if the live file already matches `sidecar.tmp`'s recorded
      hash, otherwise discard `sidecar.tmp` and treat the file as needing a
      full redownload.
- [ ] **In-process (non-crash) failure between steps 3 and 5** (Codex round
      4 — a distinct gap from crash recovery: the process keeps running, so
      startup reconciliation never triggers): if step 4 (sidecar replace) or
      step 5 (generation bump / cache invalidate) raises without the process
      dying, the coordinator catches it **before releasing the symbol
      lock** and immediately runs the same reconciliation logic used at
      startup, in-process, for that one stem. If that reconciliation itself
      cannot complete, the stem is marked unavailable (reads refused with a
      clear error) rather than silently left readable with a new file but
      stale sidecar/`_info_cache` — the failure is never allowed to surface
      as "successfully serving mismatched file and metadata." Only after
      reconciliation succeeds or the stem is marked unavailable is the lock
      released. (Implementation note, Codex round 5: write the
      reconciliation logic as an internal helper that assumes its caller
      already holds the symbol lock, never one that re-acquires it, so this
      in-process path and the startup path can share it without a
      reentrant-lock hazard.)
- [ ] Windows-specific: explicit test that opens a connection to a cache
      file, triggers a refresh through this coordinator, and asserts the
      replace succeeds — no existing precedent in this repo for `os.replace`
      onto an open handle, do not assume a read-only DuckDB connection
      doesn't lock the file on Windows.
- [ ] `TickRepository`'s `_ConnectionPool` (`repository.py:104-134`) gains
      targeted eviction (close and drop one specific `Path` only).

**Verification**: `tests/test_tickreplay_cache_commit.py` — the Windows
open-handle-replace test; a test for each of the three crash-failpoint
invariants above (kill the process at each of the numbered steps and assert
the next startup reconciles correctly); an **exception-injection** test
(Codex round 4, distinct from the process-kill tests above) that raises at
the step 3→4 and step 4→5 boundaries *without* killing the process and
asserts in-process reconciliation runs before the lock is released, leaving
the stem either fully consistent or explicitly marked unavailable — never
silently serving a mismatched file/sidecar/cache combination; a test
asserting the old file stays readable until step 3; a **barrier test** (Codex round 3) that pauses the
commit coordinator immediately after step 2 (connection evicted, file not
yet replaced) on one thread, then from a second thread attempts a
`symbol_info()`/session read for the same stem and asserts it blocks on the
same lock rather than reopening a connection to the old file — proving the
race described above cannot occur.

#### Step 6: Generation-safe repository API and final resolver cutover
This step does two things together, because they're coupled: fixing the
generation-consistency bug, and — since this is the step where
`repository.py` stops calling the old resolver — deleting the old resolver
(Codex round 2, moved from Step 3).
- [ ] Replace the two-call `find_session()`/`load_session()` pattern with one
      compound `resolve_and_load_session(stem, day, direction)` under one
      lock acquisition, operating on a single `(path, generation)` pair.
- [ ] **Fix the read-after-invalidate race** (Codex round 2 — revision 2's
      "invalidate `_info_cache[stem]`" in Step 5 is not sufficient by
      itself): `symbol_info()` (`repository.py:165-192`) currently queries
      the DB and *then* writes `_info_cache[stem]`, unconditionally. Fix:
      `symbol_info()` acquires Step 5's per-symbol operation lock for the
      **entire** query-then-cache-write sequence, the same lock Step 5's
      commit coordinator holds for its whole sequence — there is exactly one
      lock per stem and exactly one mechanism (holding it), not a
      lock-or-CAS choice (Codex round 3: the previously-offered "equivalent
      compare-and-set" alternative is removed — it was the source of the
      stale-connection-reopen race fixed in Step 5).
- [ ] Fixed lock order project-wide: symbol operation lock → connection pool
      lock → info cache lock. Step 4's blocking download work happens
      **before** acquiring `_pool._lock` (`repository.py:112-134`).
- [ ] `list_symbol_stems()`/`path_for()` (`repository.py:148-163`) switch
      from local `glob`/`is_file()` to Step 2's listing endpoint plus Step
      1's existence-vs-availability policy (404 vs 503).
- [ ] **Delete** `resolve_data_root`/`resolve_trades_dir` and
      `BACKCAST_JQUANTS_DUCKDB_ROOT` support from `config.py`, and switch
      `server.py`'s `get_repository()` to the new resolver, in this same
      commit — the app must never be left in a state where `server.py`
      references a deleted function.

**Verification**: A test that starts a refresh (Step 5) concurrently with
`symbol_info()`/`resolve_and_load_session()` for the same stem and asserts no
stale write survives past the refresh (directly exercising the fixed race). A
test asserting a slow download for symbol A does not block a query for
symbol B. A grep-based check that no reference to `resolve_data_root`/
`BACKCAST_JQUANTS_DUCKDB_ROOT` remains in `src/`.

#### Step 7: Startup lifecycle, singleton, and `/api/status`
- [ ] **Startup fail-fast via FastAPI `lifespan`** (Codex round 2 — this had
      no implementation point in revision 2): replace the current lazy
      construction (`get_repository()` on first request, `server.py:25-27`)
      with a `lifespan` context manager that, at process startup: validates
      `BACKCAST_DUCKDB_CACHE_DIR`/`BACKCAST_DUCKDB_SERVER_URL` and fails
      immediately with a clear error if invalid; constructs the repository/
      cache manager; runs Step 4/Step 5's orphan `.part` and `sidecar.tmp`
      recovery; and on shutdown calls `repository.close()`
      (`repository.py:274-275`, currently never invoked anywhere — confirm
      and wire it up).
- [ ] A lock (not `lru_cache`, whose behavior on exceptions was
      mischaracterized in revision 1 — `functools.lru_cache` does **not**
      cache exceptions; a failed call is not "poisoned") still deduplicates
      concurrent construction if lifespan startup is somehow re-entered
      (e.g. test harness reuse).
- [ ] `/api/status` returns a snapshot (Step 4's progress state; Step 1's
      `serverEpoch`/`operationId`/`revision`) without performing repository
      construction, network calls, or a directory glob itself.
- [ ] **Operation-start handshake** (Codex round 4 — a correctness blocker:
      `/api/session` and `/api/status` are separate requests, so a status
      poll firing before the session request has registered a new operation
      could observe the *previous* operation's terminal state for that stem
      and stop polling before the new operation even starts): `/api/session`
      (or a dedicated lightweight endpoint, implementation's choice) returns
      the `operationId` it started or reused (possibly none, if it was a
      cache hit) synchronously in its own response — not only discoverable
      via a later `/api/status` poll. The client (Step 8) treats this
      returned `operationId` as the one to wait for; it does not stop
      polling based on any `operationId` lower than this baseline, and if no
      operation was started at all (cache hit), it never begins polling for
      that request.

**Verification**: A test asserting startup fails fast (non-zero exit / clear
error) with an invalid cache dir, before any request is served. A test
asserting orphan recovery runs once at startup, not per-request. A test
asserting `/api/status` never performs network I/O.

#### Step 8: Frontend polling and race-condition guard
- [ ] `app.js` itself becomes `type="module"` (`<script type="module"
      src="app.js">` in `index.html`), so it can `import` the coordinator
      directly; extract the request-epoch/`AbortController`/status-operation
      logic into `request-coordinator.mjs` (see Scope).
- [ ] Wire `/api/symbols`, `/api/session`, `/api/status` fetches through the
      coordinator; discard any response that isn't the latest in-flight
      request for its kind.
- [ ] Implement Step 1's `operationId`/`revision`/`serverEpoch` contract,
      anchored to Step 7's operation-start handshake: after the session
      request returns its baseline `operationId` (or none, for a cache hit),
      start polling `/api/status` only while that specific operation is in
      flight; on each response, first compare `serverEpoch` (a mismatch
      means the server restarted — reset all locally-held last-seen state),
      then `operationId` against the session-provided baseline (never the
      previous request's own idea of "last applied" — a response describing
      an operation older than this request's baseline is not "stale", it is
      simply not this request's operation, and is ignored rather than
      treated as this request's completion), then `revision` within a
      matching `operationId`; ignore responses for a stem that is no longer
      selected; stop polling on a terminal state; back off on repeated poll
      failures. (Confirmed: `/api/status` is currently fetched once at
      bootstrap only, `app.js:862` — this step adds the actual polling loop,
      it does not just guard an existing one.)
- [ ] New `node:test` harness covering: "request for A starts, request for B
      starts before A resolves, only B's result is applied"; "a second
      download operation on the same stem starts and its own early
      `revision`s are applied, not discarded because of the first
      operation's higher revision" (Codex round 3 — this is the scenario the
      per-stem-reset design would have gotten wrong); "a stale response from
      a superseded `operationId` is discarded regardless of its `revision`";
      "a status response for a stem that is no longer selected is ignored";
      "polling stops once state is terminal"; and, from Codex round 4: "a
      status poll for the previous, already-terminal operation on this stem
      arrives after the new session request started but before the new
      operation's first status response — polling does not stop early
      because it is anchored to the session-provided baseline `operationId`,
      not to whatever `/api/status` happens to return first"; "the session
      request itself resolves as a cache hit (no operation started) and
      polling never begins"; "a stale response referencing an old
      `serverEpoch` does not roll back a client that has already advanced to
      a newer epoch"; and, Codex round 5's remaining minor note: "the
      session-start request itself times out or is retried — the retry
      reuses or observes the same `operationId` rather than the client
      treating each retry as a distinct operation."

**Verification**: `node --test src/tickreplay/static/*.test.mjs` (or the
implementation-time equivalent) passes all of the scenarios listed above.

#### Step 9: Documentation, failure drills, staged rollout, rollback
- [ ] `.env.example` (new) and `docs/tick-replay.md`: new env vars,
      cutover note (old `BACKCAST_JQUANTS_DUCKDB_ROOT` no longer works after
      Step 6 lands — update `.env` before upgrading), cache directory
      lifecycle (no auto-cleanup in v1), no-resume policy, single-process
      assumption, transport-authenticity note — all accepted trade-offs.
- [ ] Update `test_tickreplay_config.py`/`test_tickreplay_repository.py`/
      `test_tickreplay_server.py` for the final resolver/singleton shapes.
- [ ] **Rollback procedure, ordered by cutover point** (Codex round 3 raised
      this; round 4 found the ordering itself was still missing — server and
      client rollback are only independent *before* the client cutover):
      - **Before Step 6 (client cutover) has run**: server-side rollback
        (redeploy the previous `cloud-run` image/commit) is fully
        independent — the listing endpoint is additive and nothing yet
        depends on it.
      - **After Step 6 has run**: the client no longer has any code path
        that reads a local root, so server rollback alone does not fix a
        regression — the client must be rolled back first: restore that
        machine's `.env` to include `BACKCAST_JQUANTS_DUCKDB_ROOT` pointing
        at a valid local root *before* starting the previous release's
        process (Codex round 5 — restoring `.env` first means the old
        release never has a moment where it starts against the new,
        already-cutover config), then roll back the server *before or
        together with* the client; server-only rollback post-cutover is not
        a valid recovery by itself.
      - **Preserve the old `.env` value** for `BACKCAST_JQUANTS_DUCKDB_ROOT`
        (back it up, don't delete it) through the canary period below, so a
        rollback has a known-good value to restore rather than requiring it
        be reconstructed from memory.
      - **Canary**: verify against one machine/one symbol end-to-end (fresh
        download, cache hit, forced refresh, and the offline-fallback path)
        before treating the change as done and discarding the backed-up old
        `.env` value.
- [ ] Manual failure drills (not just unit tests) before calling this done:
      kill network mid-download and confirm the orphan-cleanup and the
      404-vs-503 existence policy both behave as specified against the live
      server; kill the process mid-commit (Step 5) and confirm startup
      reconciliation recovers correctly; restart the server process and
      confirm the client's `serverEpoch` check resets stale local state
      instead of misreading old operation IDs.
- [ ] Full gate: focused new tests, `node --test`, a manual lock-ordering
      review against Step 6, `ruff check`, `ruff format --check`,
      `ty check src/`, full `pytest` (including `tests/test_cloud_run_main.py`
      via the `cloud-run-test` group, now part of `default-groups`).

**Verification**: All of the above pass in one clean run, including the
three manual failure drills and the one-machine canary.

### Risks & Considerations

- **Remote httpfs streaming was evaluated and rejected.** WAN link (confirmed
  via `curl`), point queries on `(Code, Timestamp)` → many small range
  requests under `httpfs`, latency multiplies badly. Full-file
  download-and-cache chosen instead.
- **File sizes are large** (`stocks_trades/7203.duckdb` ≈ 2.2GB). Streamed to
  disk, never buffered in memory.
- **No resumable downloads in v1** (Scope) — a network failure partway
  through a large download means starting over, which could be materially
  slow for the largest files on a poor connection. Accepted for v1 simplicity;
  worth revisiting if this proves painful in practice.
- **Transport authenticity is an accepted risk, not solved** — SHA-256 is a
  corruption check only, consistent with the deferred auth decision.
- **Single-process assumption is not enforced** — accepted for v1.
- **No disk-space cap in v1** — accepted trade-off.
- **Cutover breaks any environment that hasn't updated `.env`** by the time
  Step 6 lands — intended fail-fast behavior (Step 7's lifespan validation),
  not a silent failure mode.
- **Windows file-in-use semantics are unverified** until Step 5's explicit
  test runs.
- **`functools.lru_cache` does not cache exceptions** (correction preserved
  from revision 2) — the lock in Step 7 is for concurrent-cold-start
  deduplication only.
- **A separate, earlier exploration recommended migrating the hosting
  provider entirely** (`.agents/logs/codex/hosting-choice-20260821T154733.md`).
  Explicitly not acted on now; Steps 4-8 are provider-agnostic if it happens
  later.
- **Live-server behavior for the new endpoint and Windows `os.replace`
  against an open DuckDB handle are both still unverified as of this
  writing** — Codex's re-review could not check either (no network access,
  no Windows execution in its sandbox). Steps 2 and 5 name the exact live/
  manual checks that close this gap; do not treat this plan as fully
  de-risked until those specific checks have actually been run once.

### Open Questions

- None blocking. All product-level policy points (cutover semantics,
  freshness TTL, multi-process safety) were resolved with the user in
  revision 2. All further points raised in the round-2 Codex review were
  engineering-detail gaps, not product decisions, and are resolved directly
  in this revision's steps.
- Exact `[dependency-groups]` name for the `cloud-run` test dependencies
  (Scope) is left to implementation-time judgment.

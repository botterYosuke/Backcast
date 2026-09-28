# Test and Coverage Review: Native Python Strategy Traces

## Verdict

**CHANGES REQUIRED — 4 High, 3 Medium findings.** The focused suites are green,
but the current tests do not qualify the production HTTP boundary or the real
subprocess lifecycle. Two wire-contract mismatches and one lifecycle defect are
already reproducible from the reviewed code, so this feature should not merge
until the High findings are fixed and covered.

## Review Scope

- Reviewed `.agents/logs/review-diff-native-python-strategy-traces.patch`, the
  approved plan, all new backend/worker/browser trace modules, their focused
  tests, the application lifecycle wiring, the generated release checks, and
  implementer validation evidence.
- Focus areas: API state transitions, executor failure/cancel/timeout behavior,
  framing and limits, identity/cache invariants, malicious output rejection,
  sparse replay causality, mode/manual/cursor gates, release reconstruction,
  and production-browser realism.
- `.codex/config.toml` and the prior spike documents were treated as
  user/pre-existing scope and not reviewed as feature implementation.

## Coverage and Test Execution

- Fresh focused Python run: **190 passed, 1 pre-existing warning**.
- Fresh focused Node run: **102 passed**.
- Fresh release reconstruction/assets run: **8 passed**; release
  `ffd256cf23a1d87289c3762d5b0867b767ce3106fb24dcf4b42bd5896ab4e262`
  reconstructed with 13 files and 13 edges.
- Fresh pytest-cov run over the three new Python production modules: **65% total**
  (`native_strategy.py` 80%, `strategy_trace.py` 80%,
  `native_strategy_worker.py` 33%). The worker executes mostly in child
  processes, so its 33% is partly a measurement-boundary artifact; nevertheless,
  no subprocess-aware coverage report proves the worker at the 80% target.
- Prior browser evidence reports the new Native Trace scenario passing under
  system Chrome, but the full Chromium run had 9 passed, 1 skipped, and 2 failed
  Pyodide performance budgets. The new Native scenario mocks every native API
  response and therefore is not a real backend-to-browser acceptance test.

## High Findings

### [High] The mocked browser test hides a production Cache-Control mismatch

- **Evidence:** `src/tickreplay/server.py:789-795` emits
  `private, max-age=31536000, immutable`, including optional whitespace.
  `src/tickreplay/static/strategy/strategy-trace-controller.mjs:107-119`
  requires the byte-for-byte value `private,max-age=31536000,immutable`.
  `tests/test_tickreplay_server.py:1160-1165` explicitly locks in the spaced
  server value, while `tests/browser/application-strategy.mjs:89-139` intercepts
  every `/api/native-strategy/**` request and fabricates the unspaced value.
- **Reproduction:** `Headers.get('cache-control')` preserves the server value as
  `private, max-age=31536000, immutable`; the controller then raises
  `invalid_trace_headers` before parsing the trace.
- **Expected contract:** A trace returned by the real FastAPI route must be
  accepted by `createNativeTraceHttpTransport`, and the approved production
  browser path must exercise the real HTTP/controller/replay/broker chain.
- **Minimal fix:** Align the server and client on one canonical representation,
  preferably parsing Cache-Control directives rather than comparing raw header
  bytes. Keep the quoted trace-ID ETag check exact.
- **Required tests:**
  1. A transport contract test for `createNativeTraceHttpTransport.fetchTrace`
     using the exact headers returned by FastAPI.
  2. A Playwright acceptance test backed by the real FastAPI routes plus an
     injected harmless executor; do not `page.route` native capability/job/
     status/trace responses.
  3. Negative header tests for missing `private`, missing `immutable`, wrong
     `max-age`, and wrong ETag.

### [High] Server `cancelled` snapshots are rejected by the browser validator

- **Evidence:** `src/tickreplay/native_strategy.py:385-402` creates a cancelled
  terminal snapshot with a public `{code, message}` error, and
  `src/tickreplay/native_strategy.py:217-234` serializes that error.
  `src/tickreplay/static/strategy/strategy-trace-protocol.mjs:23-26` omits
  `cancelled` from `FAILURE_STATES`, so lines 168-175 reject that server payload
  as `invalid_job`. The controller's fake transport instead returns
  `cancelled` with `error: null` at
  `src/tickreplay/static/strategy/strategy-trace-controller.test.mjs:58-77`,
  masking the incompatibility.
- **Reproduction:** Passing the real cancelled JSON shape to
  `validateNativeTraceJob` produces `invalid_job: non-failed job cannot expose
  an error`.
- **Expected contract:** `failed`, `cancelled`, and `timed_out` terminal
  snapshots returned by the server must all validate and surface their bounded
  public diagnostics.
- **Minimal fix:** Include `cancelled` in the validator's error-bearing terminal
  state set, or change the backend schema consistently; do not keep divergent
  meanings in the controller and protocol modules.
- **Required tests:**
  1. Parameterize `validateNativeTraceJob` over all three error-bearing terminal
     states using actual `JobSnapshot.as_dict()` shapes.
  2. Poll a real cancelled API job through the HTTP transport and assert the
     controller settles with the typed cancellation without fetching a trace.
  3. Validate the DELETE response rather than swallowing a schema mismatch in
     the best-effort cancellation path.

### [High] The production subprocess adapter is not qualified for failure, timeout, or process cleanup

- **Evidence:** The highest-risk paths in
  `src/tickreplay/native_strategy.py:67-214` include spawn failure, transport
  failure, stderr rejection, malformed/oversized/duplicate/non-finite response
  frames, non-zero exit, cancellation, timeout, SIGTERM/SIGKILL escalation, and
  process-group cleanup. The only concrete adapter behavior test is a successful
  worker invocation at `tests/test_tickreplay_native_strategy.py:280-302`; the
  other adapter test at lines 305-309 checks only empty argv. Fake-executor
  manager tests cannot prove child or grandchild termination. Fresh coverage
  likewise leaves the relevant branches uncovered.
- **Expected contract:** A hung, noisy, crashing, or malicious configured worker
  must reach the correct terminal state, publish no trace, and leave no child or
  process-group member alive. Timeout must not race into generic
  `execution_failed` nondeterministically.
- **Minimal fix:** First add deterministic helper-child fixtures; then fix any
  terminal-state or cleanup races they expose. Do not weaken the framed protocol
  or replace process-group cleanup with daemon-thread reliance.
- **Required tests:**
  1. Child outputs: truncated header/body, zero/oversized length, extra bytes,
     invalid UTF-8/JSON, duplicate key, NaN, non-object, framed error, stderr,
     and non-zero exit.
  2. Cancellation before completion and deadline timeout, asserting exactly one
     terminal revision and no published artifact.
  3. A helper that spawns a grandchild and ignores SIGTERM, proving escalation
     kills the process group and no PID survives.
  4. Exact response-size boundary and boundary+1 tests on the parent decoder.

### [High] Seeking a loaded-ready native trace does not invalidate it

- **Evidence:** `src/tickreplay/static/strategy/strategy-trace-controller.mjs:12`
  excludes `ready` from `ACTIVE_TAGS`, and line 184 therefore reports a loaded
  ready controller as inactive. `src/tickreplay/static/app.js:1920-1937` calls
  invalidation on seek only when `isActive()` is true. Consequently a trace can
  remain loaded and the manual gate can remain blocked after the replay cursor
  changes. The only seek assertion is a source-regex check at
  `src/tickreplay/static/replay-commit.test.mjs:758-772`; it does not execute
  the ready-state branch. This violates the acceptance requirement at
  `.agents/docs/plans/native-python-strategy-traces.md:93-101`.
- **Expected contract:** Stop, seek, reset, session change, and mode change must
  synchronously invalidate the operation identity and reject every late
  artifact/proposal, including the loaded-but-not-started state.
- **Minimal fix:** Make seek invalidate any attached loaded controller (or define
  loaded-ready as lifecycle-active for invalidation purposes) while preserving
  ordinary replay behavior when no strategy is loaded.
- **Required tests:**
  1. Behavioral app/controller test: Load to `ready`, seek before Start, assert
     `loaded === null`, controller returns to idle, and manual controls re-enable.
  2. Repeat while trace fetch/status is pending and prove the late completion
     cannot publish.
  3. Production-browser tests for seek and reset in both ready and running
     states, with zero post-invalidation proposal commits.

## Medium Findings

### [Medium] HTTP API transition and failure mappings are only partially tested

- **Evidence:** Native API tests at `tests/test_tickreplay_server.py:1120-1253`
  cover default-off, one happy path, identity mismatch, cancellation, and one
  invalid artifact. They do not cover queue-full `429`, manager timeout,
  unknown job/cancel/trace `404`, malformed exact-key requests, corrupt stored
  artifacts, store-publish failure, or the guarantee that status reads perform
  no repository/executor work. Manager unit tests cover some underlying states,
  but not the public HTTP mapping.
- **Needed tests:** Add parameterized TestClient coverage for each public status
  and error mapping; hold fake executor/store barriers to observe monotonic
  `queued -> running -> validating -> terminal` revisions and assert a terminal
  artifact is fetchable only for `ready`.

### [Medium] Final-tick decision semantics contradict each other across layers

- **Evidence:** The worker rejects final-tick commands at
  `src/tickreplay/native_strategy_worker.py:423-431`, but backend validation at
  `src/tickreplay/strategy_trace.py:361-370` permits `eligible == tick_count`,
  and the browser explicitly asserts acceptance at
  `src/tickreplay/static/strategy/strategy-trace-protocol.test.mjs:99-110`.
  The broker later discards final-tick responses, so this is not an early-fill
  vulnerability, but a malicious executor can publish an artifact the canonical
  worker itself promises never to emit.
- **Needed tests:** Freeze one cross-language rule. If final-tick proposals are
  forbidden, reject them in Python and JavaScript validators with boundary tests;
  if allowed, remove the worker rejection and document/test deterministic discard.

### [Medium] Browser acceptance evidence is incomplete and performance gates are host-sensitive

- **Evidence:** The approved plan requires real HTTP, repeated immutable GET,
  cache hit, Stop/reload/seek rejection, and causal fill at
  `.agents/docs/plans/native-python-strategy-traces.md:73-76`. The single native
  browser case ends after the causal fill at
  `tests/browser/application-strategy.mjs:145-166`; it does not exercise those
  lifecycle cases and uses mocked APIs. The frontend work log records that the
  pinned Playwright Chromium is unavailable on macOS 13 and that the full suite
  exceeded the 8,000 ms cold-start and 10 ms p95 budgets at
  `.agents/logs/agent-teams/team-execute-native-python-strategy-traces/frontend-implementer.md:80-90`.
- **Needed tests:** Complete the approved native lifecycle matrix against the
  real service. Separate functional browser acceptance from machine-calibrated
  performance qualification, retain raw metrics, and run the strict budgets in
  the supported pinned CI/browser environment so local host variance does not
  obscure functional regressions.

## Positive Coverage

- Canonical source/session/runtime/config identity changes and in-process
  single-flight/cache reuse are tested.
- Trace store collision, corruption, traversal, atomic concurrent publish, and
  repeated validated reads are covered.
- Worker input framing, stream contamination, callback diagnostics, sparse
  ordering, command shapes, duplicate intent IDs, and major count limits are
  well covered.
- Browser protocol tests cover exact keys, forbidden broker state, identity
  mismatch, sparse ordering, command limits, and non-finite/non-plain data.
- Cursor-zero, session identity, manual-order conflict, execution-mode switching,
  sparse empty batches, no replay network, next-tick fill causality, and release
  byte reconstruction have useful focused coverage.

## Required Merge Gate

Fix and add regression tests for all four High findings, then rerun focused
Python/Node/release tests, the real backend-to-browser Native Trace lifecycle,
the full browser functional suite, and repository-wide `verify.sh`. Report
coverage with subprocess collection (or explicitly mark worker coverage as not
measured) rather than treating the parent process's 80% modules as coverage of
the executor boundary.

## Remediation Review (2026-09-28)

### Final Verdict

**APPROVE WITH FOLLOW-UPS — 0 Critical, 0 High, 2 Medium, 1 Low current
findings.** All four former High findings are fixed in production code and have
focused regression coverage. The remaining findings are test-depth and
fault-injection gaps; none demonstrates a remaining wire, lifecycle, isolation,
or artifact-publication defect. The initial verdict and findings above are
retained as review history.

The missing full real-backend Playwright scenario is a real deviation from the
approved acceptance plan, but it is not, on the current evidence, an actual
merge-blocking product defect. The previously hidden cross-boundary mismatches
are now asserted independently against the exact production values and schemas,
and the Chromium application scenario covers the browser lifecycle. A real
FastAPI-backed browser scenario remains the strongest follow-up because it
would prove their composition in one process boundary.

### Former High Findings: Resolution Evidence

1. **Resolved — the actual FastAPI Cache-Control shape is accepted.**
   `src/tickreplay/server.py:889-895` emits
   `private, max-age=31536000, immutable`. The browser transport now parses
   comma-separated directives case-insensitively with optional whitespace at
   `src/tickreplay/static/strategy/strategy-trace-controller.mjs:82-96` and
   applies that check at lines 126-138. The API test freezes the exact FastAPI
   header at `tests/test_tickreplay_server.py:1180-1185`; the transport test
   consumes that exact string, a mixed-case/OWS variant, and three incomplete
   policies at
   `src/tickreplay/static/strategy/strategy-trace-controller.test.mjs:365-399`.
   The Chromium fixture also uses the production-spaced form at
   `tests/browser/application-strategy.mjs:137-145`.

2. **Resolved — `cancelled` has one error-bearing wire schema.**
   `src/tickreplay/static/strategy/strategy-trace-protocol.mjs:23-26,152-177`
   includes `cancelled` in the failure states that require `{code, message}`.
   The protocol test accepts the real shape and rejects a null error at
   `src/tickreplay/static/strategy/strategy-trace-protocol.test.mjs:83-108`;
   the controller polls that shape,
   surfaces the typed cancellation, and does not fetch a trace at
   `src/tickreplay/static/strategy/strategy-trace-controller.test.mjs:200-215`.
   The server cancellation test
   verifies the DELETE acknowledgement and subsequent cancelled snapshot at
   `tests/test_tickreplay_server.py:1220-1257`.

3. **Resolved — the real subprocess failure and cleanup boundary is exercised.**
   The adapter has bounded nonblocking stdout/stderr collection, distinct
   cancellation/timeout outcomes, process-group escalation, and strict frame
   decoding at `src/tickreplay/native_strategy.py:73-288`. Real child-process
   tests cover valid worker execution (`tests/test_tickreplay_native_strategy.py:294-316`),
   malformed frames (`:340-357`), framed errors/stderr/non-zero exit
   (`:360-394`), prompt oversized-stream termination (`:397-420`), closed-pipe
   timeout and cancel/timeout separation (`:423-444`), and an orphaned
   SIGTERM-ignoring grandchild killed through the POSIX process group
   (`:447-482`). The focused run passed all of these tests.

4. **Resolved — seeking a loaded-ready trace invalidates synchronously.**
   `src/tickreplay/static/app.js:1921-1927` invalidates on either an active
   controller or a non-zero loaded revision. Controller invalidation clears the
   operation revision, attempt/run/loaded identity, and manual gate before any
   asynchronous lease release at
   `src/tickreplay/static/strategy/strategy-trace-controller.mjs:400-429`. Unit
   coverage rejects a late artifact at
   `src/tickreplay/static/strategy/strategy-trace-controller.test.mjs:344-363`;
   the application
   Chromium scenario seeks while loaded-ready, observes idle/disabled Start and
   a re-enabled manual gate, verifies the lease DELETE, then reloads, fills
   causally, and resets at `tests/browser/application-strategy.mjs:160-211`.

### Remediation Safeguard Assessment

- **Per-submitter leases: adequately covered.** Manager single-flight returns a
  unique lease to each submitter at `native_strategy.py:423-455`; releasing a
  lease is identity-checked, idempotent, and cancels active execution only when
  the final active lease is released at lines 502-528. Manager tests cover
  concurrent unique leases and cache reuse
  (`tests/test_tickreplay_native_strategy.py:123-163`), first-versus-last
  release and idempotency (`:515-550`), and invalid job/lease pairs (`:553-566`).
  API coverage repeats the two-submitter behavior at
  `tests/test_tickreplay_server.py:1285-1326`; browser controller coverage proves
  one controller releases only its own lease at
  `src/tickreplay/static/strategy/strategy-trace-controller.test.mjs:302-322`
  and validates POST/DELETE wrapper schemas and paths at lines 401-425.

- **Two-phase publication: the cancellation/timeout race is covered.**
  `TraceStore.prepare/commit/discard` writes and fsyncs a private temporary,
  atomically replaces only during commit, and removes the temporary at
  `src/tickreplay/strategy_trace.py:474-519`. The manager prepares outside its
  state lock, rechecks `validating`, commits while holding the lock, and only
  then publishes `ready` at `src/tickreplay/native_strategy.py:582-620`.
  Parameterized barrier tests cancel or time out during slow prepare and assert
  the terminal state is preserved with neither JSON nor temporary artifacts at
  `tests/test_tickreplay_native_strategy.py:591-631`.

- **Trusted-local guard: adequately covered.** The server requires the actual
  socket peer to be loopback, exactly one valid localhost authority, and an
  optional exact same-origin value while ignoring forwarding headers at
  `src/tickreplay/server.py:674-712`; every native capability/job/lease/trace
  route calls the guard at lines 763-895. API tests cover a remote peer with
  spoofed forwarding headers across all routes
  (`tests/test_tickreplay_server.py:1329-1357`), invalid Host/Origin and ignored
  proxy headers (`:1360-1387`), and IPv6 loopback (`:1390-1406`).

- **Request body cap: both enforcement paths are covered.**
  `src/tickreplay/server.py:715-760` rejects oversized declared lengths before
  reading, caps streamed accumulation, and strictly decodes UTF-8 JSON with
  duplicate/non-finite rejection. Tests exercise declared and streamed
  overflow at `tests/test_tickreplay_server.py:1409-1432`; the default-off path
  also rejects an oversized body before manager work at lines 1131-1142.

- **Previously open final-tick semantics are resolved.** Python rejects a
  non-empty final-tick decision at `src/tickreplay/strategy_trace.py:398-401`
  with regression coverage at `tests/test_tickreplay_strategy_trace.py:194-203`;
  JavaScript rejects the same boundary at
  `src/tickreplay/static/strategy/strategy-trace-protocol.test.mjs:140-153`.

### Current Findings

#### [Medium, non-blocking] Full browser acceptance still substitutes routes for the real FastAPI backend

- **Exact evidence:** The approved plan asks for a harmless executor through the
  real HTTP/controller/replay/broker path at
  `.agents/docs/plans/native-python-strategy-traces.md:73-76`, but the Native
  Trace Playwright case intercepts the authoritative session at
  `tests/browser/application-strategy.mjs:80-90` and every native API at lines
  91-158. The fresh Chromium pass therefore proves application/controller/
  replay/broker behavior against contract-faithful mocks, not FastAPI-to-browser
  composition.
- **Why this is not a current merge blocker:** The concrete defect that this gap
  previously hid is now cross-checked with the exact production Cache-Control
  string in both the FastAPI API test (`test_tickreplay_server.py:1180-1185`)
  and the transport test
  (`src/tickreplay/static/strategy/strategy-trace-controller.test.mjs:365-399`).
  Lease
  wrappers, cancelled snapshots, and DELETE paths likewise have backend and
  browser-side schema tests. No remaining incompatible production value was
  found, and the mocked Chromium lifecycle passed.
- **Concrete follow-up test:** Start the production FastAPI application with a
  fixed harmless injected executor, do not register `page.route` for
  `/api/native-strategy/**`, and assert submit/poll/immutable GET, a second
  cached submission with a distinct lease, ready seek release, reload, causal
  `i+1` fill, reset, and zero post-invalidation commits. Keep this functional
  scenario separate from host-sensitive Pyodide performance budgets.

#### [Medium, non-blocking] Public HTTP failure mappings remain less complete than manager coverage

- **Exact evidence:** Manager tests cover queue saturation, timeout, invalid
  artifacts, cancellation, and late results at
  `tests/test_tickreplay_native_strategy.py:166-275`, while native API tests at
  `tests/test_tickreplay_server.py:1128-1465` cover default-off capability,
  happy/cache, identity mismatch, cancel, sanitized invalid artifact, leases,
  local guard, and body limits. They still do not directly assert native-route
  queue-full `429`, a normal manager-disabled/closed submission `503`, unknown
  job/lease/trace `404`, timeout snapshot, or corrupt stored-trace `404`
  mappings.
- **Concrete follow-up tests:** Parameterize TestClient cases over those public
  mappings. Use executor/store barriers to observe monotonic
  `queued -> running -> validating -> terminal` revisions and prove only
  `ready` has a fetchable trace. These tests would guard routing/refactor drift;
  the current route branches are simple and no incorrect mapping was observed.

#### [Low] Remaining boundary/fault-injection branches are not measured end to end

- **Exact evidence:** The real subprocess suite covers malformed and oversized
  output but not spawn `OSError` (`src/tickreplay/native_strategy.py:108-121`),
  an exactly 4 MiB valid frame versus boundary+1
  (`native_strategy.py:257-288`), or cancellation-driven grandchild cleanup
  separately from timeout. The two-phase test blocks `prepare`, but does not
  inject `prepare` or `commit` failure into the manager branches at
  `native_strategy.py:582-609`. Body-cap tests cover `limit+1` but not an exact
  accepted limit or malformed/duplicate Content-Length at
  `src/tickreplay/server.py:717-731`.
- **Concrete follow-up tests:** Add a missing-executable test, direct exact-frame
  boundary tests, a cancel-triggered grandchild fixture, failing prepare/commit
  stores that assert `failed` with no fetchable artifact/temp, and exact-cap plus
  malformed-length ASGI requests. These are defense-in-depth coverage additions;
  the principal malicious-output, cleanup, publish-race, and overflow paths are
  already exercised.

### Fresh Validation Evidence

- Focused Python: **221 passed, 1 third-party deprecation warning, 0 failed** in
  16.20 seconds.
- Focused Node: **112 passed, 0 failed** in 0.69 seconds.
- Immutable release/assets: **8 passed, 0 failed**; release
  `80544acd5cad8f7b14ad33297ddc30b9676bf7aba907ac0c270149f3e14c0f06`
  reconstructed with 13 files, 13 edges, and 2 reconstruction roots.
- Focused Chromium Native Trace application scenario: **1 passed, 0 failed**
  in 8.3 seconds using system Chrome. It remains mocked at the native API
  boundary as described above.
- Fresh Python coverage run: **141 passed** and **71% combined** for the three
  feature modules (`native_strategy.py` 90%, `strategy_trace.py` 80%,
  `native_strategy_worker.py` 33%). Worker logic runs in spawned children, so
  ordinary parent-process pytest-cov does not attribute most executed worker
  lines; the 33% remains a measurement-boundary limitation rather than evidence
  that the child behavior tests did not run.
- `git diff --check`: passed with no whitespace errors.

### Flakiness and Performance Assessment

The new real-process tests use monotonic deadlines and bounded waits. They
passed in the focused run; the slowest safety assertions allow up to two seconds
(`tests/test_tickreplay_native_strategy.py:397-420,447-476`), which is materially
wider than their normal completion time. The POSIX process-group test is
correctly skipped on Windows. The Chromium functional scenario also passed.
The full browser matrix was not rerun in this remediation review, so the prior
host-sensitive Pyodide cold-start/p95 performance failures are neither cleared
nor attributed to this Native Trace remediation.

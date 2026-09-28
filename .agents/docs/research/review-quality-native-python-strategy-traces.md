# Code Quality Review: Native Python Strategy Traces

## Verdict

**FAIL** — four High-severity integration/lifecycle regressions block merge. The immutable release reconstructs correctly and the replay authority boundary is sound, but the real backend trace response is rejected by the real browser client, cancellation can lose to publication or another coalesced caller, and a repository-supported session shape now breaks the ordinary session API even while Native Trace is disabled.

## Findings

### High — Real trace responses are always rejected because cache-control is compared byte-for-byte

- Evidence: `src/tickreplay/server.py:789-795` emits `Cache-Control: private, max-age=31536000, immutable`, including optional whitespace after commas. `src/tickreplay/static/strategy/strategy-trace-controller.mjs:110-117` accepts only the byte-exact string `private,max-age=31536000,immutable`. `tests/browser/application-strategy.mjs:131-136` mocks the whitespace-free client expectation instead of the actual server value.
- Confirmed behavior: passing the server's header through the production `createNativeTraceHttpTransport()` raises `invalid_trace_headers`; therefore every real Native Trace Load reaches `ready` and then fails before publishing the artifact.
- Impact: the primary feature cannot complete an end-to-end Load against FastAPI, despite the mocked browser test passing.
- Actionable fix: parse cache-control directives case-insensitively while tolerating HTTP optional whitespace, or share one canonical serializer between server and client. Change the browser acceptance fixture to run against the actual FastAPI route (or at minimum feed the exact server header), and add the semantically equivalent whitespace variants as regression cases.

### High — A coalesced execution has no per-caller cancellation ownership

- Evidence: `src/tickreplay/native_strategy.py:321-323` returns the same `job_id` to every identical active submission, while `src/tickreplay/native_strategy.py:385-402` lets any holder of that ID cancel the shared execution immediately. `src/tickreplay/static/strategy/strategy-trace-controller.mjs:415-422` asynchronously cancels the superseded attempt before the replacement Load submits.
- Defect: the job ID is simultaneously an execution identity and a caller-owned cancellation handle. Two tabs loading the same source/session share one job; Stop in either tab cancels both. An identical superseding Load can also reattach to the still-active old job before the best-effort DELETE is processed, after which that DELETE cancels the newly accepted Load.
- Impact: single-flight coalescing violates independent lifecycle ownership and the stated last-accepted-Load-wins invariant under ordinary concurrent/superseding use.
- Actionable fix: separate the input-keyed execution record from per-submitter lease/job handles. DELETE should release only the caller's lease and cancel the child process only when no active lease remains. Add deterministic two-consumer and identical-superseding-Load race tests.

### High — Cancellation and timeout cannot win once artifact publication takes the manager lock

- Evidence: `src/tickreplay/native_strategy.py:453-478` acquires the manager lock, performs the filesystem `store.publish()`, and publishes `ready` without another cancellation/deadline check. Both `cancel()` at `src/tickreplay/native_strategy.py:385-402` and `_time_out()` at `src/tickreplay/native_strategy.py:509-521` require the same lock.
- Defect: a Stop or timer that fires first while validation/publication is slow waits behind publication; the publisher sets `ready`, and the waiting cancel/timeout then treats the ready job as terminal success.
- Impact: output from a cancelled or timed-out run can become ready/fetchable, violating an explicit acceptance criterion and the intended stale-completion suppression.
- Actionable fix: use a two-phase store API (bounded validated staging outside the manager lock, then a short commit). Make cancel/deadline observable without waiting for the publication lock, recheck state/cancel/deadline immediately before commit, and add deterministic cancel-during-publish and timeout-during-publish tests.

### High — The new session hash breaks repository-supported empty trade types even when Native Trace is off

- Evidence: `src/tickreplay/repository.py:735-750` deliberately normalizes database `NULL Type` to `""`. The feature changed every `SessionTicks.as_dict()` call to compute `data_hash` at `src/tickreplay/repository.py:176-182`, but `_canonical_session()` routes trade types through the non-empty `_string()` validator at `src/tickreplay/strategy_trace.py:69-75,151-154`. The worker independently permits empty trade types at `src/tickreplay/native_strategy_worker.py:318-319`.
- Defect: repository, identity, and worker disagree on an existing supported session value.
- Impact: the ordinary `/api/session` response now raises during hash calculation for any session containing a NULL trade type. This is a default-off Pyodide/manual replay regression, directly violating additive compatibility.
- Actionable fix: introduce a trade-type validator that accepts the repository's canonical empty string (or change the repository contract consistently), and add repository-to-`/api/session` regression coverage with NULL `Type` while native execution is disabled.

### Medium — The cache identity omits the executor protocol version

- Evidence: `src/tickreplay/strategy_trace.py:17-19` defines the protocol, schema, and execution-contract versions separately, but `compute_input_id()` at `src/tickreplay/strategy_trace.py:182-214` hashes the contract version, source/session bytes, runtime fingerprint, and run config without hashing `EXECUTOR_PROTOCOL_VERSION`. The test named `test_canonical_identity_changes_at_every_required_boundary` at `tests/test_tickreplay_strategy_trace.py:74-110` likewise has no protocol-version boundary case.
- Impact: two protocol revisions can expose the same `input_id`, contrary to the fixed identity contract. This makes protocol upgrades ambiguous for API consumers and any future durable job/cache index, even though the current process-local manager still validates the artifact's protocol field.
- Actionable fix: make executor protocol version an explicit framed input to `compute_input_id()`, pass the configured constant at every call site, and add a change-one-boundary test.

### Medium — The browser wire schema rejects the backend's valid `cancelled` snapshot

- Evidence: the backend attaches a public error to cancellation at `src/tickreplay/native_strategy.py:396-402`. The browser declares `cancelled` as a job state but excludes it from `FAILURE_STATES` at `src/tickreplay/static/strategy/strategy-trace-protocol.mjs:22-26`, then rejects errors for every non-failure state at `src/tickreplay/static/strategy/strategy-trace-protocol.mjs:165-175`.
- Confirmed behavior: validating the exact backend cancellation shape raises `invalid_job: non-failed job cannot expose an error`. The controller hides this today because `#cancelJob()` swallows validation failures at `strategy-trace-controller.mjs:432-435`, but a polled cancellation becomes a protocol error rather than the intended typed cancellation.
- Impact: producer and consumer disagree on a terminal public contract; diagnostics and cancellation state are unreliable.
- Actionable fix: include `cancelled` in the terminal failure/error-shape set (or remove backend cancellation errors consistently), assert the DELETE response instead of swallowing a known-valid contract, and add an exact backend-to-client fixture test.

### Medium — Production timeout terminal state is scheduler-dependent

- Evidence: `SubprocessNativeExecutor.execute()` raises `TimeoutError` on its own deadline at `src/tickreplay/native_strategy.py:131-146`. The manager also schedules a timer for the same deadline at `src/tickreplay/native_strategy.py:411-417`, but catches every executor exception as generic `execution_failed` at `src/tickreplay/native_strategy.py:419-431`.
- Defect: whichever thread acquires the manager lock first determines whether an identical timeout becomes `timed_out` or `failed/execution_failed`.
- Impact: the public state machine and retry/diagnostic behavior are nondeterministic in production; the fake-executor timeout test does not exercise the adapter race.
- Actionable fix: catch `TimeoutError` separately and transition through one idempotent timeout helper, or designate exactly one layer as the timeout owner. Add a real adapter test at the deadline boundary.

### Medium — The parent's output limit is enforced only after unbounded buffering

- Evidence: `src/tickreplay/native_strategy.py:114-150` uses `Popen.communicate()` and holds all stdout/stderr in memory before `_decode_response()` applies the 4 MiB frame bound at `src/tickreplay/native_strategy.py:183-189`.
- Impact: a buggy configured executor can exhaust the FastAPI process before the advertised output bound is checked. Trusted/local-only removes the untrusted-code claim, but it does not make resource bounds or failure containment optional.
- Actionable fix: read the four-byte header first, reject an oversized declared frame, read exactly the bounded payload plus one EOF byte, and drain/cap stderr independently while preserving timeout and process-group termination.

### Medium — Seek leaves a native trace displayed as ready although it can no longer start

- Evidence: `ready` is absent from `ACTIVE_TAGS` and therefore `isActive()` returns false at `src/tickreplay/static/strategy/strategy-trace-controller.mjs:12,184`. `seekTo()` invalidates only an active controller at `src/tickreplay/static/app.js:1921-1923`. The workspace still enables Start for `ready` at `src/tickreplay/static/strategy/strategy-workspace.mjs:318`, while native `start()` rejects every non-zero cursor at `src/tickreplay/static/strategy/strategy-trace-controller.mjs:301-305`.
- Impact: Load followed by seek leaves stale loaded identity and a Start button that deterministically fails with `cursor_policy`, instead of applying the documented seek invalidation.
- Actionable fix: invalidate whenever a controller has a loaded revision/trace, not only while its run is active; cover Load-ready → seek/reset → disabled Start/cleared identity.

### Medium — Authoritative validators accept final-tick proposals that the reference worker rejects

- Evidence: backend validation permits `observed_session_tick_index == tick_count - 1` and `eligible == tick_count` at `src/tickreplay/strategy_trace.py:361-370`; browser validation mirrors it at `src/tickreplay/static/strategy/strategy-trace-protocol.mjs:195-200`. The worker rejects commands at the last tick at `src/tickreplay/native_strategy_worker.py:426-431`, while `strategy-trace-protocol.test.mjs:99-110` explicitly fixes the opposite behavior into the client contract.
- Impact: another configured executor can publish a verified proposal for a nonexistent next replay tick; the browser later discards it, so producer validation and accepted artifact semantics disagree.
- Actionable fix: require `eligible_from_session_tick < tick_count` in both authoritative validators and reverse the browser boundary test, or explicitly change the worker and documented contract if final-tick discard is intended.

### Medium — The approved real-boundary acceptance gate is still mocked

- Evidence: the plan requires a harmless fixed executor through the real HTTP/controller/replay/broker path at `.agents/docs/plans/native-python-strategy-traces.md:72-76`, but `tests/browser/application-strategy.mjs:89-140` intercepts the entire native API and fabricates its headers/status/artifact. `tests/test_tickreplay_native_strategy.py:280-302` covers only the real subprocess happy path.
- Impact: cross-language wire drift, lifecycle races, malformed output, and process cleanup are not exercised; this is why the merge-blocking cache-control mismatch passed all reported suites.
- Actionable fix: add an acceptance fixture using FastAPI plus the real fixed-argv worker and generated browser release without route interception, including cancel, timeout, malformed/non-zero output, repeated GET, and `i+1` broker fill.

### Low — Invalid Unicode source escapes become an internal server error

- Evidence: `_validate_source()` encodes input without handling `UnicodeEncodeError` at `src/tickreplay/native_strategy.py:343-353`; the route catches only `TraceValidationError` at `src/tickreplay/server.py:736-743`. JSON can represent an unpaired surrogate escape, so this path is reachable by an external request.
- Confirmed behavior: submitting `"\ud800"` directly raises `UnicodeEncodeError` rather than the public validation type.
- Impact: malformed source produces HTTP 500 and bypasses the otherwise sanitized input-error contract.
- Actionable fix: catch `UnicodeEncodeError` and raise `TraceValidationError("source must be valid Unicode text")`; add the escaped-surrogate API case.

### Low — Native-mode orchestration extends an already oversized workspace module

- Evidence: `src/tickreplay/static/strategy/strategy-workspace.mjs` is now 969 lines, above the repository's 800-line maximum. Native capability/mode/controller concerns are spread across state declarations at `:199-203`, rendering at `:279-334`, capability/controller acquisition at `:531-579`, and mode transition logic at `:647-678`.
- Impact: execution-mode lifecycle is coupled to editor persistence, diagnostics, keyboard handling, and DOM rendering, making future protocol or mode changes harder to review safely.
- Actionable fix: after the blocking correctness fixes, extract a small execution-mode coordinator that owns capability, selected mode, controller creation/invalidation, and loaded trace identity while leaving the existing workspace as the UI/persistence adapter.

## Verified Invariants

- `src/tickreplay/native_strategy.py` never imports or executes submitted source; execution occurs only in the configured subprocess worker.
- `src/tickreplay/strategy_trace.py:278-406` enforces exact artifact fields, sparse strict ordering, consecutive decision sequences, next-tick eligibility, command bounds, and global intent-ID uniqueness before publication.
- `src/tickreplay/strategy_trace.py:418-498` stores canonical content-addressed bytes atomically and rehashes/revalidates every GET; repeated reads are non-destructive.
- `src/tickreplay/static/replay-commit.mjs:167-224` keeps the broker as fill/portfolio authority: proposals observed at tick `i` are staged only after tick `i` commits and are eligible for broker processing at `i+1`.
- Native mode remains additive/default-off, and the Pyodide controller path remains selected explicitly by `scripts/build_strategy_release.mjs:123-142`.
- `npm run check:strategy-release` passed for release `ffd256cf23a1d87289c3762d5b0867b767ce3106fb24dcf4b42bd5896ab4e262` with 13 files, 13 edges, and two reconstruction roots; authored trace modules are byte-identical to their generated release copies.
- No accidental feature implementation was found in `.codex/config.toml` or the pre-existing spike documents, which were excluded from scope as instructed.

## Codex Consultation

- Read-only Codex review: `.agents/logs/codex/20260928T012327Z-quality-review.md` (verdict `FAIL`). Its header mismatch, publish/cancel race, protocol identity, seek-ready state, cancelled wire shape, timeout race, empty trade type, final-tick proposal, output buffering, and missing real-boundary gate findings were independently checked against the live files and incorporated above. The separate per-caller single-flight cancellation defect and invalid-Unicode handling were found directly in this review.

## Remaining Risks

- Distributed job coordination, artifact/job retention, multi-process state, and untrusted sandboxing are explicitly deferred by the approved plan; they are not treated as defects in this review.
- The native event snapshot is intentionally an open-loop tick proposal ABI and does not reproduce browser fill/portfolio feedback. This limitation should be evaluated against each RL model before claiming strategy compatibility, but it matches the approved proposal-only architecture.

---

## Remediation Review (2026-09-28)

### Final Verdict

**PASS WITH FINDINGS** — all four previous High-severity defects are resolved in the live implementation. Six of the seven previous Medium findings and the invalid-Unicode Low finding are also resolved. No merge-blocking code-quality defect remains. The approved real FastAPI/executor/browser/broker acceptance boundary is still absent (Medium), and the oversized workspace module remains a maintainability concern (Low).

Final counts: **0 Critical, 0 High, 1 Medium, 1 Low**.

### Resolution of Previous High Findings

1. **Resolved — Cache-Control compatibility.** The server still emits the valid OWS form at `src/tickreplay/server.py:889-896`, while `hasImmutableTraceCachePolicy()` now parses comma-separated directives case-insensitively, trims optional whitespace, rejects duplicates, and requires the three semantic guarantees at `src/tickreplay/static/strategy/strategy-trace-controller.mjs:82-95`. The HTTP transport applies that parser at `:126-138`, and the exact FastAPI header plus case/OWS variants are covered by `strategy-trace-controller.test.mjs:365-398` and `tests/test_tickreplay_server.py:1180-1186`.
2. **Resolved — Per-caller cancellation ownership and superseding Loads.** Each submission now carries a distinct `leaseId` (`src/tickreplay/native_strategy.py:311-338,428-455`); releasing one lease cancels the shared execution only after the last active lease is gone (`:502-528`). The browser stores/releases the accepted lease and releases a late superseded submission (`src/tickreplay/static/strategy/strategy-trace-controller.mjs:223-230,251-259,452-483`). Deterministic manager, HTTP, and two-controller coverage exists at `tests/test_tickreplay_native_strategy.py:515-550`, `tests/test_tickreplay_server.py:1285-1326`, and `strategy-trace-controller.test.mjs:302-321`.
3. **Resolved — Cancel/timeout versus publication.** Validation and durable temporary-file preparation occur outside the manager lock, then the manager rechecks the still-current `validating` state before the short commit/ready linearization under the same lock (`src/tickreplay/native_strategy.py:563-620`; `src/tickreplay/strategy_trace.py:474-519`). A cancellation or timeout that becomes terminal before commit therefore prevents publication and the prepared file is discarded. `tests/test_tickreplay_native_strategy.py:591-631` deterministically exercises both terminal outcomes during slow preparation and asserts that neither a final artifact nor a temporary file survives. Once commit owns the lock, success is the intentional linearization winner; a later lease release cannot retroactively cancel an immutable ready artifact.
4. **Resolved — NULL trade type/default-off session compatibility.** Repository NULL values remain canonically represented as `""` (`src/tickreplay/repository.py:735-750`), and the dedicated trace trade-type validator now accepts empty strings while preserving type, byte, and NUL bounds (`src/tickreplay/strategy_trace.py:89-96,171-174`). `tests/test_tickreplay_server.py:1468-1490` exercises `/api/session` with native mode off and verifies an empty trade type plus a data hash.

### Resolution of Previous Medium/Low Findings

- **Resolved — protocol identity:** executor protocol version is a framed input to `compute_input_id()` at `src/tickreplay/strategy_trace.py:202-243`, is explicitly supplied by the manager at `src/tickreplay/native_strategy.py:408-414`, and has a change-one-boundary assertion at `tests/test_tickreplay_strategy_trace.py:75-120`.
- **Resolved — cancelled schema:** `cancelled` is an error-bearing terminal state in the browser schema at `src/tickreplay/static/strategy/strategy-trace-protocol.mjs:23-26,152-177`; exact producer-shaped coverage is at `strategy-trace-protocol.test.mjs:84-106` and `strategy-trace-controller.test.mjs:200-213`.
- **Resolved — deterministic timeout:** the subprocess adapter raises `TimeoutError` at `src/tickreplay/native_strategy.py:209-218`, and the manager routes it through the single idempotent timeout transition at `:552-554,651-663`. Coverage is at `tests/test_tickreplay_native_strategy.py:423-443,569-588`.
- **Resolved — bounded parent output:** stdout/stderr are incrementally read into capped buffers, with immediate termination on overflow or unexpected stderr, at `src/tickreplay/native_strategy.py:142-224`. Real-child overflow tests are at `tests/test_tickreplay_native_strategy.py:397-420`.
- **Resolved — ready trace seek/reset:** `seekTo()` now invalidates both active runs and any loaded revision at `src/tickreplay/static/app.js:1921-1927`; the controller clears loaded identity synchronously at `strategy-trace-controller.mjs:400-429`. Unit and real-browser coverage is at `strategy-workspace.test.mjs:288-314`, `strategy-trace-controller.test.mjs:324-364`, and `tests/browser/application-strategy.mjs:170-211`.
- **Resolved — final-tick proposals:** both validators require a real eligible replay tick at `src/tickreplay/strategy_trace.py:394-401` and `strategy-trace-protocol.mjs:209-225`; the browser boundary test now rejects final-tick proposals.
- **Resolved — invalid Unicode:** `_validate_source()` converts `UnicodeEncodeError` to the public validation type at `src/tickreplay/native_strategy.py:457-470`, with manager/API coverage at `tests/test_tickreplay_native_strategy.py:634-645` and `tests/test_tickreplay_server.py:1435-1465`.

### Remaining Findings

#### Medium — The approved real-boundary acceptance gate remains mocked

- Evidence: the implementation plan still requires a fixed executor through the real HTTP/controller/replay/broker path at `.agents/docs/plans/native-python-strategy-traces.md:73-77`. The only Native Trace browser scenario intercepts every `/api/native-strategy/**` request and fabricates capability, submission, polling, artifact headers/body, and lease release at `tests/browser/application-strategy.mjs:68-158`. The server happy path injects `FakeNativeExecutor` at `tests/test_tickreplay_server.py:1151-1193`, while the real subprocess test stops at the manager/store boundary at `tests/test_tickreplay_native_strategy.py:294-316`.
- Impact: the formerly broken Cache-Control seam is now covered by paired unit/API fixtures, but no executable gate proves that the configured subprocess worker, FastAPI serialization/headers, generated browser release, controller, replay commit, and broker work together without interception. Future cross-boundary drift can again pass all component suites.
- Actionable fix: run the browser application against a FastAPI fixture configured with the harmless fixed-argv `native_strategy_worker.py`, real local session data, and a temporary trace store. Do not intercept native routes. Assert repeated immutable GET/cache reuse, lease cancellation, seek/reset rejection, zero replay-time network, and causal tick `i+1` broker fill.

#### Low — Native-mode orchestration remains in an oversized workspace module

- Evidence: `src/tickreplay/static/strategy/strategy-workspace.mjs` is now 976 lines. It owns editor/persistence state together with execution-mode/capability/controller state at `:177-204`, native-aware rendering at `:279-334`, controller acquisition and mode switching at `:538-684`, and workspace lifecycle/DOM concerns through the end of the file.
- Impact: the remediation is behaviorally sound, but subsequent mode or protocol changes still require edits across a broad UI/persistence coordinator and make race review unnecessarily difficult.
- Actionable fix: extract an execution-mode coordinator that owns capability discovery, selected mode, controller acquisition/invalidation, and loaded trace identity; leave the workspace responsible for UI, source persistence, and diagnostics presentation.

### Remediation Validation

- `uv run pytest -q tests/test_tickreplay_strategy_trace.py tests/test_tickreplay_native_strategy.py tests/test_tickreplay_native_strategy_worker.py tests/test_tickreplay_repository.py tests/test_tickreplay_server.py` — **180 passed**, one pre-existing Starlette/httpx deprecation warning.
- Focused Node protocol/controller/workspace/replay tests — **79 passed**.
- `STRATEGY_SPIKE_USE_SYSTEM_CHROME=1 npm run test:strategy-browser -- --project=chromium --grep "Native Trace uses"` — **1 passed**; this confirms browser behavior but remains route-mocked as described above.
- `npm run check:strategy-release` — **passed** for current release `80544acd5cad8f7b14ad33297ddc30b9676bf7aba907ac0c270149f3e14c0f06` with 13 files, 13 edges, and two reconstruction roots.

### Scope and Compatibility Recheck

- Native execution remains default-off, trusted/local-only, forbidden on Cloud Run and Windows, and explicitly configured through `.env.example:14-21`; the Pyodide selection path remains intact.
- `docs/tick-replay.md:783-819` and `docs/pyodide-strategy-release-runbook.md:35-44` now document limits, lifecycle, trust boundary, leases, final-tick behavior, immutable response requirements, and the current generated release.
- The previous immutable release is retained rather than mutated, and the release checker reconstructs the newly referenced release successfully.
- No accidental implementation scope was attributed to user-owned `.codex/config.toml` or the pre-existing spike documents.

## Feature Brief: Native Python strategy decision traces

### Current State
- Architecture: backcast loads a content-addressed Pyodide Worker runtime in the browser, executes callback-style Python strategies, stages commands for the next eligible tick, and commits paper-broker state atomically. FastAPI owns session data access and an existing pending/status polling pattern for asynchronous cache work.
- Relevant files: `src/tickreplay/server.py`, `config.py`, `repository.py`, `src/tickreplay/static/app.js`, `strategy/strategy-workspace.mjs`, `strategy/strategy-protocol.mjs`, `replay-commit.mjs`, `order-broker.mjs`, `scripts/build_strategy_release.mjs`, and focused Python/Node/browser tests.
- Patterns: exact-key typed validation; source/session/generation identity; last-accepted-load-wins; fail-closed stale response rejection; next-tick eligibility; candidate-state validation before atomic publish; immutable content-addressed release assets.

### Feature Goal
Allow a user to submit Python source for native batch execution against the selected replay session, receive a content-addressed immutable decision trace, and replay its typed order proposals locally through the existing validated paper broker without per-tick network inference.

### Scope
- Include: a versioned `native_trace_v1` schema; canonical source/session/runtime/execution identities; stable session-data hashing; a bounded single-flight job manager; create/status/cancel/artifact HTTP APIs; atomic verified trace storage; a separately configured external executor protocol; default-off trusted/local-only execution; a browser trace protocol/controller; editor/workspace progress and lifecycle integration; cursor-zero-only v1; Stop/seek/reset invalidation; existing broker/next-tick reuse; immutable release generation; focused Python/Node/browser tests; documentation.
- Include: preserve the existing Pyodide strategy mode as the default/backward-compatible path and expose Native Trace as an explicit mode only when the server reports it available.
- Exclude: executing source inside FastAPI; claiming same-container subprocess isolation; multi-tenant untrusted-code hosting; per-tick HTTP/WebSocket inference; arbitrary mid-session trace resume; live brokerage; profitability claims; changing `_moomoo` model status; training in backcast; modifying retained immutable releases.

### Complexity Classification (from Codex)
- Classification: COMPLEX
- Estimated files: 20-35
- Estimated LOC: 2,000-4,000 including tests
- Implementation route: team-execute

### Integration Points
- FastAPI lifespan/API: own the trace job manager, expose capability plus create/status/cancel/artifact routes, and never execute user code in-process.
- TickRepository: reload the authoritative selected session server-side and bind the trace to a stable data hash rather than accepting tick arrays from the browser.
- External executor: receive one bounded request, run in a separately configured environment, return one bounded typed trace, and support timeout/cancel/process-group termination.
- Strategy workspace/controller: submit and poll a Load, retain source/trace identities, cancel on Stop or superseding Load, and synthesize existing command batches from the accepted trace without further network calls.
- Replay/paper broker: keep `replay-commit.mjs` and `order-broker.mjs` authoritative; trace decisions are proposals only and become eligible no earlier than the tick after observation.
- Immutable release: add authored trace runtime files to a new content-addressed release while retaining all existing releases byte-for-byte.

### Risks
- Remote code execution: ship v1 disabled by default and explicitly trusted/local-only; require a separately configured executor and never describe a child process as a sandbox.
- Look-ahead/semantic drift: version `native_trace_v1`, drive execution causally, bind the execution-contract identity, and never imply equivalence with `_moomoo` evaluation unless its fill/cost/capital rules match.
- Job/cancellation races: use attempt-local ownership, monotonic revision, single-flight identity, process-group cleanup, and stale completion rejection.
- Browser/trace divergence: bind source, session bytes, runtime, model/config, and execution contract; prohibit manual orders and mid-session Start while active; fail closed on precondition mismatch.
- Resource exhaustion/privacy: hard-limit source/input/output/logs/decisions/time/concurrency; redact diagnostics; define private artifact caching and retention.
- Deployment mismatch: keep Cloud Run native execution disabled until a separately isolated executor and durable job/artifact coordination are supplied.
- Release regression: build a new release and run real browser integration; do not modify retained releases.

### Success Criteria
- A disabled or unavailable native executor leaves existing Pyodide strategies and ordinary replay unchanged.
- FastAPI never compiles or executes submitted source; execution crosses a configured external process/service boundary with bounded request/response, timeout, cancellation, and independent parent validation.
- Identical canonical source/session/runtime/execution inputs coalesce and reuse one verified artifact; changed source, session bytes, runtime, or contract produces a different identity.
- Completed artifacts are canonical, content-addressed, atomically written, rehashed and schema-validated on read, and contain no executable source.
- Browser Load shows queued/running/validating/ready states, rejects stale/wrong job identities, and Stop/superseding Load/seek/reset cannot apply old decisions.
- Native Trace v1 starts only at cursor zero, blocks incompatible manual trading, and applies decision `i` through existing broker validation no earlier than `i+1`.
- Malformed, oversized, non-finite, unordered, duplicate, mismatched, timed-out, or cancelled executor output commits no trace or paper state and produces a typed degraded error.
- Focused Python, Node, release, and production Chromium tests pass; retained release byte integrity passes; the repository quality gate is run and every unrelated failure is reported with evidence.

# Codebase Scan: Native Python Strategy Decision Traces

Date: 2026-09-28  
Mode: existing codebase / read-only affected-area scan  
Scope: native-Python batch execution, immutable decision artifacts, and replay through the existing paper broker. Live brokerage is excluded.

## Summary

- This is a **COMPLEX** feature. It crosses the FastAPI lifecycle/API, a new native executor and artifact store, the browser strategy controller/workspace, replay integration, the content-addressed strategy release, deployment configuration, and Python/Node/browser tests.
- Preserve the two strongest existing boundaries: `TickRepository` remains the server-side source of exact session ticks, and the browser `createReplayCommitter` plus `createOrderBroker` remain the only paper-portfolio mutation path. The executor must produce proposals/decisions only.
- The least disruptive frontend seam is a new HTTP-backed trace controller that implements the controller methods already required by `app.js`. Keep the editor, local source store, workspace revision model, replay committer, and broker. Do not teach `app.js` to execute trace orders directly.
- The browser should submit only canonical source identity, selected session identity, and execution contract. The server reloads the session through `TickRepository`, runs native Python once, stores a content-addressed trace, and returns the trace/job identity. No tick-by-tick browser-to-server traffic is needed.
- Reuse the **semantics**, not the exact shape, of the current async download protocol: single-flight keyed work, `serverEpoch`/operation identity/revision, monotonic status, latest-request-wins, cancel/supersede handling, and a cheap status snapshot. Strategy jobs need a dedicated bounded worker pool, cancellation, terminal diagnostics, and immutable on-disk artifacts; the current daemon-thread-per-stem `OperationTracker` is not sufficient as-is.
- Native server execution materially expands the trust boundary. Arbitrary source must never be `exec`'d in the FastAPI process. The first implementation must either be explicitly trusted/local-only and disabled by default, or use a separately isolated executor service/container with no application credentials, no writable data mount, restricted network/filesystem, bounded CPU/memory/time/output, and process-group termination.
- A new native trace ABI must be explicit. The existing callback ABI receives portfolio, fills, and open orders on every replay tick; a precomputed trace cannot silently claim equivalent behavior unless the native runner reproduces those broker semantics and the browser verifies state preconditions. For the first version, prefer a separate `native_trace_v1` batch contract and label it accordingly.

## Affected Areas

### Backend and data ownership

| Area | Existing contract | Recommended change |
|---|---|---|
| `src/tickreplay/repository.py` | `SessionTicks` already exposes columnar `us`, `price`, `qty`, and `trade_type` (`repository.py:151-173`). `resolve_and_load_session` resolves and reads one `(path, generation)` atomically under the per-stem lock (`repository.py:650-675`). | Reuse it directly from the job producer with `direction=0`; do not accept tick arrays from the browser. Add a stable session-data fingerprint either to `SessionTicks` or a new trace-input adapter so `/api/session` and the trace job bind to identical bytes. |
| `src/tickreplay/server.py` | `/api/session` already performs readiness/download handling and returns the resolved columnar session (`server.py:522-556`). Repository lifetime is singleton/ref-counted (`server.py:274-339`). | Add create/status/cancel/artifact routes for strategy traces. Construct and close the job manager/executor with the FastAPI lifespan rather than at import time. Keep strategy status snapshots free of repository/network work, as `/api/status` is today (`server.py:473-484`). |
| `src/tickreplay/config.py` | `CacheConfig` owns server/cache settings and creates the cache root (`config.py:77-164`). | Add a separate typed native-executor configuration: enabled flag (default off), interpreter/service command or URL, artifact root, concurrency, timeout, input/output limits, and trusted/local-only mode. Do not add `_moomoo`/NumPy to the web-process dependency closure. |
| New `src/tickreplay/strategy_trace.py` | No current trace schema/store. | Own canonical JSON, exact-key/type/size checks, session fingerprint, input/job key, artifact digest, atomic write/verified read, sparse decision lookup, and bounded diagnostics/log metadata. |
| New `src/tickreplay/native_strategy.py` | `OperationTracker` starts one daemon thread per stem and records exceptions as status (`server.py:177-240`). | Own bounded single-flight jobs keyed by deterministic input identity, subprocess/service invocation, cancellation, progress snapshots, terminal status, and cache hits. Use a bounded executor; never one unbounded thread/process per POST. |
| New `src/tickreplay/native_strategy_worker.py` (or separate executor service) | The browser runner is standard-library-only and explicitly independent of Pyodide (`strategy-runner.py:1-5`), but it executes source with unrestricted builtins (`strategy-runner.py:376-401`). | Execute exactly one bounded batch request outside the web process. Read source/session once from stdin or a private read-only input, emit one bounded response, sanitize environment/cwd, terminate the process group on timeout/cancel, and validate the result again in the parent. A separate `_moomoo` Python 3.13/NumPy environment should be configured here, not imported by FastAPI. |

### API and immutable artifact contract

Recommended HTTP surface:

1. `POST /api/strategy-traces` with `{source, sourceHash, session:{stem,code,actualDate,dataHash}, contractVersion}`.
2. Cache hit: return the verified immutable artifact identity immediately. Cache miss: return `{pending:true, serverEpoch, operationId, jobId, inputId}` and start/coalesce one job.
3. `GET /api/strategy-trace-jobs/{jobId}` returns a pure snapshot `{serverEpoch, operationId, revision, state, progress, traceId?, diagnostic?}`.
4. `DELETE /api/strategy-trace-jobs/{jobId}` requests cancellation and kills the owned subprocess when possible. An aborted browser request alone must not be represented as server cancellation.
5. `GET /api/strategy-traces/{traceId}` returns a rehashed, schema-validated artifact. Use `private, immutable` caching or `no-store`; do not use the current public static-asset cache policy for user-derived diagnostics/logs.

The parent must independently canonicalize and hash the source; client `sourceHash` is only an assertion. It must reload the exact resolved session, validate `stem`/`code`/date and `dataHash`, and compute the job identity from at least:

```text
source SHA-256
session-data SHA-256
native_trace_v1 contract/schema
executor/runtime identity
model/config identity supplied by the strategy contract
broker/execution-contract identity
```

The trace artifact should contain no executable source. It should include schema/contract versions, source/session/runtime hashes, creation result, bounded logs/diagnostics, and sparse decisions. A decision needs at least `observedSessionTickIndex`, `eligibleFromSessionTick = observed + 1`, validated commands, and (if the batch ABI simulates state) an expected event/portfolio precondition digest. The file name/`traceId` should be the SHA-256 of canonical artifact bytes, and every read should verify it.

### Browser controller, workspace, and release

| Area | Existing contract | Recommended change |
|---|---|---|
| New `src/tickreplay/static/strategy/strategy-trace-controller.mjs` | `app.js` accepts any controller implementing `load/start/resume/dispatchOneEvent/pause/invalidate/stop/end/getState/isActive/manualEntryBlocked/validateEventSnapshot` (`app.js:1349-1365`). | Implement that interface over the trace-job API. `load` submits/polls/loads a trace; `start` binds a browser run ID/revision; `dispatchOneEvent` validates the current tick/preconditions and synthesizes the existing broker batch envelope from the cached trace without HTTP. |
| `src/tickreplay/static/strategy/strategy-protocol.mjs` or new `strategy-trace-protocol.mjs` | Existing limits, exact-key validation, finite-number rules, and command types are centralized (`strategy-protocol.mjs:3-30`, `385-436`). | Reuse command validation and limits; add exact trace/job schemas, hash patterns, maximum decisions/artifact bytes, sorted/unique tick rules, and immutable deep copies. Keep protocol versions separate from the browser callback ABI. |
| `src/tickreplay/static/strategy/strategy-workspace.mjs` | Source hashing, immutable Load snapshots, last-accepted-Load-wins, Stop settlement, Restart from the loaded source, diagnostics, editor degradation, and raw-copy recovery already exist (`strategy-workspace.mjs:532-599`, `686-757`). | Preserve these invariants. Add visible stages such as queued/running/validating/ready, job cancellation on Stop, trace identity/data hash in the loaded target, and native-executor degraded errors. Restart should reuse the loaded trace when identity still matches; it should not necessarily re-execute Python. |
| `scripts/build_strategy_release.mjs` | The current release builder hashes authored files and vendor identities, generates the controller loader, reconstructs twice, and writes a new immutable directory (`build_strategy_release.mjs:27-35`, `141-190`, `291-346`). | Add the trace controller/protocol to the authored closure and generate a release entry that constructs it. Never alter retained release directories. Build a new release and update `STRATEGY_RELEASE_URL` in `app.js`. Pyodide assets may stay in older releases; the new native release should not fetch them. |
| `src/tickreplay/static/index.html`, `styles.css` | Current UI has editor, Load/Start/Stop/Restart, status, diagnostics, logs, and a warning tied to Load/editor (`index.html:140-224`). | Replace the Pyodide/same-origin warning with the native trust boundary, show execution progress and immutable trace identity, retain plaintext-storage disclosure and textarea/raw recovery, and distinguish “executor unavailable” from editor failure. |

### Replay and paper broker

- Keep `src/tickreplay/static/replay-commit.mjs` as the only trace-to-broker bridge. It already commits the prior staged batch, matches fills, validates the full outgoing event before publication, calls the strategy port, and stages the new result for the next tick (`replay-commit.mjs:152-224`).
- Keep `src/tickreplay/static/order-broker.mjs` authoritative. It checks batch identity, strict sequence, `eligibleFrom = observed + 1`, per-event/run limits, duplicate intents, and atomically swaps candidate state only after validation (`order-broker.mjs:468-606`). Market/limit orders then fill only when eligible (`order-broker.mjs:655-763`).
- `app.js` already invalidates strategy execution before seek/session change, pauses replay on errors, and has a separate strategy replay clock (`app.js:1915-2024`, `2027-2069`, `2235-2266`). Do not bypass these paths.
- Reset and scrub behavior is already centralized: reset invalidates, clears broker state, and seeks to the pre-first-tick time; scrubber input calls `seekTo` (`app.js:2490-2520`). A trace controller must make repeated invalidation idempotent because reset currently invalidates explicitly and `seekTo` invalidates again.
- For v1, either (a) require trace Start at cursor 0 and force Reset before Restart after a seek, or (b) include validated state checkpoints/precondition digests for every allowed mid-session cursor. Starting a precomputed stateful trace at an arbitrary cursor without one of these is unsafe.
- The browser session currently receives all ticks once and converts them to typed arrays (`app.js:2260-2325`). Add the server-issued `dataHash` to `state.meta`; trace activation must fail closed if its session/data/contract identity differs.

### Configuration and deployment

- The root runtime is Python `>=3.11` and has no NumPy or `_moomoo` dependency (`pyproject.toml:1-13`). `_moomoo` native inference therefore needs a separate configured environment/service.
- The Cloud Run image is Python 3.11 and copies only this repository (`cloud-run/Dockerfile:1-12`). A local path such as `/Users/sasac/_moomoo` does not exist there. Deployment must either build a separately pinned executor image or explicitly keep native strategy execution disabled.
- Cloud Run is intentionally one worker because `OperationTracker` and the repository are in-process state (`cloud-run/Dockerfile:14-16`). New job status will also be process-local unless moved to durable/shared coordination. Artifacts under the cache root may not survive a restart unless the deployment supplies persistent storage.
- If user source is not strictly trusted, a subprocess in the current container is not an adequate sandbox: it can potentially read `/cache`, environment variables, and use network access. The security gate is architectural, not an import blacklist.

## Existing Patterns

### Asynchronous job and polling pattern to reuse

The strongest reusable pattern is:

```text
initial request
  -> pending handshake (serverEpoch + operationId)
  -> monotonic status revisions
  -> matching terminal state only
  -> reissue/fetch immutable result
```

- Backend: `OperationTracker` coalesces same-key inflight work, increments operation IDs, rejects stale progress callbacks, and exposes immutable snapshots (`server.py:177-267`). `_ensure_ready_or_pending` checks inflight work before taking repository locks (`server.py:449-470`).
- Frontend: `RequestCoordinator` implements latest-kind cancellation, selected-session rejection, handshake identity checks, bounded poll failure retry/backoff, and re-fetch after ready (`request-coordinator.mjs:92-250`). Tests cover out-of-order responses, wrong operations, epoch changes, cancellation, retries, cache hits, and degraded terminal states (`request-coordinator.test.mjs:38-250`).
- Required adaptation: `RequestCoordinator.#pollOperation` currently hardcodes `/api/status?stem=` (`request-coordinator.mjs:220-246`). Generalize it to receive a status URL/key or create a dedicated trace-job coordinator. Do not overload stem-download status with source-derived jobs.
- Required backend divergence: unlike downloads, native strategy jobs need bounded concurrency, explicit cancellation, subprocess ownership, artifact IDs, and non-lossy typed diagnostics. Do not reuse the current “exception => corrupt string” behavior unchanged.

### Session ticks without browser round-tripping

The server already has the exact path needed:

```text
POST trace request carries session identity only
  -> get_repository()
  -> resolve_and_load_session(stem, date, direction=0)
  -> validate resolved code/date and compute canonical dataHash
  -> serialize once to executor stdin/private input
  -> executor returns trace once
```

`SessionTicks` is already in the required columnar shape (`repository.py:151-173`), and its query orders by timestamp while dropping unusable rows and making duplicate timestamps strictly increasing (`repository.py:708-750`). This is preferable to accepting browser arrays because it avoids payload duplication, tampering, and a second tick-by-tick protocol. It also keeps large DuckDB access in the existing locked repository path.

### Source/editor lifecycle to preserve

- Workspace Load snapshots canonical source and SHA-256, keeps the last accepted attempt, and rejects stale hashing/controller acquisition (`strategy-workspace.mjs:532-573`). Existing tests explicitly cover reverse-order hash completion and Stop/dispose settlement (`strategy-workspace.test.mjs:286-365`).
- The current controller state exposes loaded/running revision, session, run ID, sequence, in-flight state, diagnostics, and terminal outcome (`strategy-controller.mjs:128-163`). Match this observable state shape where practical so `app.js` and workspace rendering need minimal changes.
- Editor failure degrades to a textarea while controller failure leaves source operations available; only bootstrap-level failure enters read-only raw recovery (`strategy-workspace.mjs:461-529`, `686-715`). Native executor failures should follow the controller-failure branch, not disable source recovery or ordinary replay.

### Broker and causality guarantees to preserve

- Existing command batches must observe tick `i` and become eligible only at `i+1` (`order-broker.mjs:468-507`).
- Candidate broker state, fills, portfolio, replay cursor, and outgoing strategy snapshot are validated before atomic publication (`replay-commit.mjs:167-205`; `order-broker.mjs:755-763`).
- Broker market orders fill on the first eligible trade, while limits fill on crossing at the limit price (`order-broker.mjs:671-705`). A native trace generator must not claim `_moomoo` evaluator parity unless its trace-generation contract models the same rules.
- Browser paper positions are signed net positions with no fee/capital semantics (`paper-trading.mjs:13-16`, `75-103`). Keep the artifact’s `executionContract` explicit; technical reuse does not make the current rejected/observation RL model profitable.

### Test conventions and fixtures

- Python tests use `pytest`, FastAPI `TestClient`, `monkeypatch`, `tmp_path`, and a DuckDB `tick_db_factory` plus in-memory `httpx.MockTransport` file server (`tests/conftest.py:81-140`; `tests/test_tickreplay_server.py:48-69`).
- Async server behavior is tested with real overlapping requests and `threading.Event`, then status polling to a bounded deadline (`tests/test_tickreplay_server.py:882-949`). Mirror this for job coalescing/cancellation without running arbitrary test code in the pytest process.
- DOM-free browser logic uses `node:test` with injected fake ports, deferred promises, and exact state assertions (`request-coordinator.test.mjs:1-60`; `replay-commit.test.mjs:147-205`).
- The production browser suite verifies the real immutable release, Load/Start lifecycle, causal fills, Stop/restart, Python errors, degraded editor/runtime paths, and raw recovery (`tests/browser/application-strategy.mjs:63-125`, `203-255`). Replace/add a native-trace fixture; mocked fetch-only controller tests are insufficient.
- Release tests validate every retained content-addressed directory and require canonical authored bytes to match the current release closure (`tests/browser/strategy-release.test.mjs:32-89`). A canonical controller change requires a newly generated release and an updated application release ID.

## Risks

1. **Remote code execution (critical).** `exec` in the API process or an equally privileged child exposes server data, credentials, filesystem, and network. Python-level AST/import filtering is not isolation. Default-off configuration and an explicit trusted-only or separate-sandbox decision are release blockers.
2. **Semantic drift (high).** Existing user code receives event-time portfolio/fills/open orders. A batch trace that sees the full session or simulates a different broker is a new ABI/execution contract. Version and label it; do not silently reuse callback-v1 claims.
3. **Look-ahead (high).** Supplying full session arrays lets arbitrary code use future ticks. If causal backtesting is a requirement, the runner—not user convention—must expose ticks incrementally or audit the trace against a controlled iterator. Otherwise mark causality as unverified.
4. **Trace/browser divergence (high).** Any difference in session bytes, broker state, fill rules, fees, capital, cursor, or prior manual orders can make recorded decisions invalid. Bind identities, block manual trading while active, validate preconditions, and fail before submitting a mismatched batch.
5. **Cancellation/resource leaks (high).** Browser AbortController cancellation currently stops polling but does not terminate server work. Jobs need owned subprocess/process-group cleanup on Stop, superseding Load, timeout, lifespan shutdown, and client cancellation endpoint.
6. **Unbounded resource use (high).** Source, session, output, logs, decisions, simultaneous jobs, filesystem artifacts, and execution time all need hard limits. A daemon thread per request is not acceptable.
7. **In-memory coordination loss (medium/high).** Cloud Run/process restart loses job snapshots; more than one worker would split them. Immutable completed artifacts can be recovered from disk, but pending jobs must become explicit failed/unknown states.
8. **Mutable data race (medium).** The browser session and later server job may be loaded across a cache generation change. The session-data hash must be part of both identities; mismatches require reloading the browser session or regenerating the trace.
9. **Seek/reset ambiguity (medium).** A stateful precomputed trace cannot resume arbitrarily after `truncateTradingAt` without checkpoints. Cursor-zero-only v1 is safer and simpler.
10. **Model portability/evidence (high).** `_moomoo` is external to the container and uses a separate native dependency closure. Its model remains `Reject / OBSERVATION`; live brokerage stays excluded and paper results must name the execution contract.
11. **Artifact privacy (medium).** Source, stdout/stderr, trace decisions, and model paths can disclose user information. Do not public-cache logs; bound/redact diagnostics and define artifact retention/deletion.
12. **Release drift (medium).** Editing canonical strategy files without generating a new immutable release leaves the application on old bytes; editing a retained release violates the repository’s release contract.

## Suggested Workstreams

### Workstream A — Backend contract, store, and job lifecycle

Ownership: new `strategy_trace.py`, new `native_strategy.py`, `config.py`, backend portions of `server.py`, `.env.example`, and focused Python tests.

- Define `native_trace_v1`, canonical identities, limits, job state machine, cache hit/coalescing, atomic artifact persistence, verified reads, and cancellation.
- Add API routes and lifespan ownership.
- Add stable session `dataHash` while preserving the existing `/api/session` fields.
- Implement a fake injected executor for endpoint tests; no user/model source is executed in API tests.

### Workstream B — Native executor boundary

Ownership: new worker/service entrypoint and its direct unit/contract tests only.

- Implement one-request/one-result framed stdio or service protocol, source/result bounds, deterministic canonical output, timeout/cancel behavior, sanitized environment, process-group termination, logs/diagnostics, and runtime/model identity reporting.
- Add a configured seam for the `_moomoo` interpreter/project. Keep imports and model files out of the web process.
- Security acceptance must explicitly state whether v1 is trusted/local-only or separately sandboxed.

### Workstream C — Trace controller and protocol

Ownership: new trace controller/protocol modules and their Node tests; no `app.js`/HTML edits.

- Generalize or wrap request polling for job URLs.
- Implement the existing controller port over immutable traces, with last-load-wins, status/progress, Stop cancellation, trace cache, session/data hash checks, sparse decision lookup, exact broker batches, precondition validation, and cursor-zero policy.
- Unit-test stale job status, reversed completion, cancel races, malformed/oversized traces, hash mismatch, decision ordering, empty ticks, final-tick discard, and idempotent invalidation.

### Workstream D — UI/replay integration and immutable release

Ownership: `app.js`, `index.html`, `styles.css`, `strategy-workspace.mjs` and its test, `scripts/build_strategy_release.mjs`, generated new release directory, release/browser tests, and docs.

- Wire session `dataHash`, progress/status, Stop/seek/reset behavior, Restart from a retained trace, and degraded messages.
- Update the sample/warning for `native_trace_v1`; preserve editor/local-storage/raw-recovery behavior.
- Generate a new content-addressed release, update only the application’s release URL, and retain all older releases byte-for-byte.
- Add a real application browser test using a harmless deterministic executor fixture through HTTP and the actual broker/replay path.

### Integration order

1. Freeze trace/job/source/session/execution contracts and trust boundary.
2. Land A and B behind dependency injection/config default-off.
3. Land C against fake API/artifact fixtures.
4. Integrate D, generate the immutable release, and run browser acceptance.
5. Add the `_moomoo` adapter as a separate sample/configured strategy after the generic trace path passes; do not couple model-specific code to the broker.

Files with shared ownership requiring sequencing: `server.py` (A after route contract; B does not edit it), `strategy-workspace.mjs` (D only), `scripts/build_strategy_release.mjs` and generated releases (D only), and `app.js` (D only). This avoids parallel conflicts.

## Validation Commands

Focused backend and executor:

```bash
uv run pytest -q \
  tests/test_tickreplay_strategy_trace.py \
  tests/test_tickreplay_native_strategy.py \
  tests/test_tickreplay_repository.py \
  tests/test_tickreplay_server.py \
  tests/test_tickreplay_config.py
uv run ruff check src/tickreplay tests/test_tickreplay_strategy_trace.py \
  tests/test_tickreplay_native_strategy.py tests/test_tickreplay_server.py
uv run ruff format --check src/tickreplay tests
uv run ty check src/
```

Focused browser logic and broker invariants:

```bash
node --test \
  src/tickreplay/static/request-coordinator.test.mjs \
  src/tickreplay/static/strategy/strategy-trace-protocol.test.mjs \
  src/tickreplay/static/strategy/strategy-trace-controller.test.mjs \
  src/tickreplay/static/strategy/strategy-workspace.test.mjs \
  src/tickreplay/static/replay-commit.test.mjs \
  src/tickreplay/static/order-broker.test.mjs
node --check src/tickreplay/static/app.js
```

Immutable release and production browser path:

```bash
npm run build:strategy-release
npm run check:strategy-release
npm run test:strategy-assets
npm run test:strategy-browser -- --project=chromium
```

Repository-wide completion gates:

```bash
uv run pytest -q
node --test src/tickreplay/static/*.test.mjs src/tickreplay/static/strategy/*.test.mjs
npm run test:strategy-browser
bash .agents/skills/_shared/verify.sh
```

Manual/acceptance evidence must additionally prove: cache hit does not execute Python again; identical input coalesces; source/session/runtime changes miss cache; Stop kills pending execution; seek/reset cannot apply stale decisions; decision `i` becomes eligible only at tick `i+1`; a trace/session hash mismatch commits no order/cursor; malformed/oversized/timeout executor results remain typed degraded errors; browser reload can reactivate a verified immutable trace; and ordinary replay/manual trading still works when native execution is disabled/unavailable.

## File Written

`.agents/docs/research/feature-native-python-strategy-traces-codebase.md`

This scan made no production-code changes. It intentionally did not execute user/model code, deserialize model artifacts, install dependencies, or call external services.

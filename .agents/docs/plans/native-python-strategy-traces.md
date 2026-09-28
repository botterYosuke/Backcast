# Native Python Strategy Traces — Implementation Plan

## Status

- Mode: existing codebase
- Complexity: COMPLEX
- Approval: user approved the cache-first native batch architecture and requested implementation on a new branch.
- Branch: `feature/native-python-strategy-traces`
- Validation: initial Codex plan review returned `NEEDS_REVISION`; this plan incorporates every required revision.

## Scope and Trust Boundary

Ship an additive `native_trace_v1` execution mode alongside the retained Pyodide mode. Native execution is disabled by default and may be enabled only for an explicitly configured trusted/local executor. The FastAPI process validates, schedules, streams input to, and validates output from the executor; it never imports, compiles, evaluates, or executes submitted source.

The executor process is not a sandbox. V1 does not support untrusted multi-tenant execution, Cloud Run enablement, live brokerage, per-tick network inference, manual-order mixing, or non-zero-cursor starts.

## Fixed Contracts

### Identity

The trace input identity includes:

- canonical source bytes and source SHA-256;
- exact ordered session tick bytes and session-data SHA-256;
- executor protocol version and administrator-supplied runtime fingerprint;
- canonical run configuration;
- `native_trace_v1` schema and execution-contract version.

Completed trace bytes are canonical JSON and `trace_id` is their SHA-256. Reads rehash and fully validate the artifact.

### Sparse trace

Artifacts contain only ticks with non-empty proposal batches. Each row contains a consecutive decision sequence, `observed_session_tick_index`, `eligible_from_session_tick = observed + 1`, and the existing validated command objects. They never contain fills, portfolio, cash, execution prices, source text, absolute paths, or raw stderr.

### Replay authority

The trace controller fetches the immutable artifact once per accepted Load, keeps it in memory, and performs no network access during replay. At tick `i`, it returns only the row observed at `i`; `replay-commit.mjs` and `order-broker.mjs` remain the only order-validation, fill, and portfolio-mutation path. Immutable trace `GET` is idempotent so Restart/reload can refetch the same bytes.

### Python ABI

The external worker uses the existing `on_start` / `on_event` / `on_end` source ABI and command schema. It receives events sequentially from cursor zero. The parent streams one exact event snapshot at a time; the user callback does not receive a full-session array or repository handle. V1 batch events use an explicitly versioned native-trace event snapshot; future ticks are never included.

## Implementation Steps

1. **Contract and identity (TDD)**
   - Add `src/tickreplay/strategy_trace.py` with canonical encoding, limits, source/session/input/trace identities, exact-key trace validation, sparse ordering, causal tick rules, and atomic filesystem store.
   - Add `tests/test_tickreplay_strategy_trace.py` covering hash vectors, forbidden fields, duplicate/reversed/out-of-range decisions, non-finite/oversized values, collision, corruption, path traversal, and concurrent atomic publish.

2. **Job lifecycle and external executor port (TDD)**
   - Add `src/tickreplay/native_strategy.py` with typed job states, bounded queue/concurrency, single-flight by input identity, timeout/cancel/shutdown, monotonic revisions, stale-completion suppression, injected executor/store/session provider, and public error sanitization.
   - Add `tests/test_tickreplay_native_strategy.py` using fake executors only for concurrency and lifecycle coverage.

3. **Trusted native worker boundary (TDD)**
   - Add `src/tickreplay/native_strategy_worker.py` as the fixed-argv framed-JSON worker entrypoint and a subprocess executor adapter owned by `native_strategy.py` or a narrowly scoped companion module.
   - Reuse the existing Python strategy ABI semantics without letting the FastAPI process import submitted source.
   - Add direct worker contract tests for frame fragmentation, malformed/oversized output, print redirection, compile/runtime diagnostics, deterministic sparse trace, timeout, cancellation, non-zero exit, and process-group cleanup.

4. **Configuration, repository snapshot, and HTTP API (TDD)**
   - Extend `src/tickreplay/config.py` with default-off trusted/local settings, fixed executor argv, runtime fingerprint, artifact root, queue/concurrency/time/size/log limits, and an explicit deployment guard.
   - Extend `src/tickreplay/repository.py` only as needed to expose a stable canonical session snapshot/hash.
   - Extend `src/tickreplay/server.py` lifespan and routes: capability; job submit/status/cancel; immutable idempotent trace GET. Status reads perform no repository or executor work.
   - Add focused config/server/API tests with dependency-injected fake executor. Verify feature-off/Pyodide behavior, hash mismatch, coalescing, cancellation, immutable repeated GET, and information non-disclosure.

5. **Browser trace protocol/controller (TDD)**
   - Add `src/tickreplay/static/strategy/strategy-trace-protocol.mjs` and `.test.mjs` for exact wire validation, ID/hash formats, limits, sparse ordering, and causal rules.
   - Add `strategy-trace-controller.mjs` and `.test.mjs` implementing the existing controller port over submit/poll/fetch, last-accepted-Load-wins, monotonic revisions, Stop cancellation, idempotent invalidation, cursor-zero enforcement, session/source/hash checks, manual-mode exclusion, sparse lookup, and existing command-batch envelopes.

6. **Workspace/app/UI integration (TDD)**
   - Extend `strategy-workspace.mjs` and its tests with explicit `pyodide` / `native_trace_v1` mode selection, capability gating, queued/running/validating/ready/failed states, loaded trace identity, Restart/refetch rules, and preserved editor/raw-recovery behavior.
   - Extend `app.js` to include server-issued session data hash, construct the chosen controller, block incompatible manual entry, and preserve all existing Stop/seek/reset/session-change invalidation paths.
   - Update `index.html` and `styles.css` with mode/status controls and an accessible persistent trusted/local non-sandbox warning.

7. **Immutable release and production-browser acceptance**
   - Extend `scripts/build_strategy_release.mjs` to include the new authored modules and generate a new immutable release. Do not alter retained release files.
   - Update only the application release URL/manifest reference required by the established builder.
   - Extend release tests and `tests/browser/application-strategy.mjs` with a harmless fixed executor fixture through the real HTTP/controller/replay/broker path, repeated immutable GET, cache hit, Stop/reload/seek rejection, causal `i+1` fill, and Pyodide regression.
   - Update `docs/tick-replay.md` and the release runbook with enablement, limits, lifecycle, trust boundary, and Cloud Run-disabled status.

8. **Integration and review**
   - Run focused Python/Node/release/browser tests, then repository-wide gates.
   - Reconcile ownership and inspect the complete diff for source execution in the web process, weakened tests, retained-release mutations, placeholders, and out-of-scope changes.
   - Run parallel security, quality, and test reviews; fix every Critical/High finding and re-run gates.

## Parallel Workstreams and Ownership

After the contract/fixture shape is frozen, implementation may run in parallel with these disjoint ownership sets:

- Backend owner: `src/tickreplay/strategy_trace.py`, `src/tickreplay/native_strategy.py`, `src/tickreplay/config.py`, `src/tickreplay/repository.py`, `src/tickreplay/server.py`, `.env.example`, and matching Python tests.
- Executor owner: `src/tickreplay/native_strategy_worker.py` and direct worker contract tests. It must not edit backend job/API files.
- Frontend owner: new trace `.mjs` modules/tests plus `strategy-workspace.mjs`, `app.js`, `index.html`, and `styles.css`.
- Release/browser integration follows after frontend/backend contracts are green and owns the builder, generated new release, browser tests, and docs.

## Acceptance Matrix

- Identical canonical input coalesces and produces/reuses byte-identical trace content.
- Changing source, any observable session tick field/order, runtime fingerprint, run config, or contract version changes the input identity.
- Partial, cancelled, timed-out, malformed, oversized, non-causal, corrupt, or hash-mismatched output never becomes fetchable.
- Immutable trace GET can be repeated and returns byte-identical validated content.
- FastAPI unit/API tests never execute submitted source; only direct worker contract tests execute harmless fixtures.
- Stop, superseding Load, session change, mode change, seek, and reset reject all late job/poll/fetch/proposal results.
- Native Trace starts at cursor zero only and cannot mix with manual or Pyodide execution in the same active run.
- Decision observed at tick `i` cannot fill before tick `i+1`; proposal batches remain atomic.
- Disabled/unavailable native mode changes no existing Pyodide, replay, or manual-trading behavior.
- Retained strategy release bytes remain unchanged and the newly generated release passes reconstruction/integrity tests.
- Cloud Run cannot enable the trusted/local executor.

## Validation Commands

```bash
uv run python .agents/skills/_shared/run_tests.py --target tests/test_tickreplay_strategy_trace.py --expect pass --label native-trace-contract-green
uv run python .agents/skills/_shared/run_tests.py --target tests/test_tickreplay_native_strategy.py --expect pass --label native-trace-job-green
uv run pytest -q tests/test_tickreplay_server.py tests/test_tickreplay_config.py tests/test_tickreplay_repository.py
node --test src/tickreplay/static/strategy/strategy-trace-protocol.test.mjs src/tickreplay/static/strategy/strategy-trace-controller.test.mjs src/tickreplay/static/strategy/strategy-workspace.test.mjs src/tickreplay/static/replay-commit.test.mjs src/tickreplay/static/order-broker.test.mjs
node --check src/tickreplay/static/app.js
npm run build:strategy-release
npm run check:strategy-release
npm run test:strategy-assets
npm run test:strategy-browser -- --project=chromium
uv run ruff check src/tickreplay tests
uv run ruff format --check src/tickreplay tests
uv run ty check src/
uv run pytest -q
bash .agents/skills/_shared/verify.sh
```

## Deferred

- Untrusted or multi-tenant sandboxing
- Cloud Run/native hosted execution
- Live brokerage
- Non-zero-cursor start or state checkpoints
- Manual-order/strategy composition
- Distributed job coordination and advanced retention/quota/GC
- User-selected dependencies or arbitrary executor argv

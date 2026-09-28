## Feature Brief: Bundled RL Default Strategy

### Current State
- Architecture: Native Trace v1 sends canonical strategy source plus authoritative session identity to a local-only fixed-argv child, validates a sparse immutable proposal trace, and replays proposals through the existing next-tick paper broker. Native execution is currently environment-configured and default-off.
- Relevant files: `src/tickreplay/{config,native_strategy,native_strategy_worker,strategy_trace,server}.py`, strategy workspace/source-store/trace modules, release builder, browser tests, and `_moomoo/models/rl_scalp_evolution_v1.json` plus its inference references.
- Patterns: FastAPI never executes submitted source; child execution is framed and bounded; trace contains proposals rather than fills/portfolio; browser broker is authoritative; saved source uses validated CAS-like records; authored frontend changes generate a new immutable release and never mutate retained releases.

### Feature Goal
Copy the selected `_moomoo` RL checkpoint and the minimum inference-only implementation into backcast so `uv run python run.py` works without `_moomoo`, external model paths, or Native environment variables. Make the bundled RL preset and Native execution the initial selection while preserving every existing custom draft and requiring explicit Load/Start before execution.

### Scope
- Include: unchanged 11,932-byte `rl_scalp_evolution_v1.json`; compact provenance manifest; strict NumPy 2.4.5 inference adapter; causal 10-second feature scheduling; position-conditional flat/long proposals resolved from the actual broker portfolio; trace/protocol/identity revision; supported-local bundled-only executor defaults; explicit disable and custom-executor overrides; bundled RL preset; saved-custom recovery and persisted selection; legacy default handling; immutable release; Python/Node/browser/real-subprocess tests and documentation.
- Exclude: `_moomoo` training code, market data, validation report, paper/live broker clients, credentials, virtualenv, unrelated checkpoints, model retraining, live brokerage, claims that upstream P&L is reproduced, public/multi-tenant arbitrary-code execution, automatic execution of saved custom code, and implicit Pyodide fallback for the RL preset.

### Complexity Classification (from Codex)
- Classification: COMPLEX
- Estimated files: approximately 42-52 including generated release files; approximately 29-38 authored files.
- Estimated LOC: approximately 1,900-4,550 authored LOC plus generated immutable release copies.
- Implementation route: team-execute.

### Integration Points
- Bundled model: module-relative hash-verified JSON and inference adapter reproduce upstream float32 features, Q values, and target decisions without importing `_moomoo`.
- Native worker: bundled-only local default recognizes the exact shipped preset and runs in the existing bounded child-process boundary; explicit custom Native configuration retains the arbitrary trusted-source workflow.
- Trace and broker: each decision contains bounded flat/long position branches; the browser selects against the actual committed portfolio and sends only the necessary target-delta market command through existing next-tick broker authority.
- Identity/cache: checkpoint bytes, provenance/runtime contract, adapter/worker semantics, Python/NumPy fingerprint, source, session bytes, and trace schema invalidate cache deterministically.
- Local bootstrap: absent environment configuration enables only the bundled executor on supported loopback macOS/Linux; explicit false disables; Cloud Run and unsupported platforms remain unavailable/fail-closed; custom argv still requires explicit trusted configuration.
- Browser defaults: selection records are separate from the existing exact source record. A missing selection chooses bundled RL/Native, while prior source bytes remain recoverable as Saved strategy and are never implicitly executed.
- Release: canonical authored strategy modules generate a new content-addressed closure; retained release directories stay byte-identical.

### Risks
- Position-dependent model can diverge if proposals are treated as fills: precompute both position branches and select using actual broker state; reject unsupported positions/open-order states fail-closed.
- Research simulator and backcast fill semantics differ: preserve model numerics but document execution differences and never reuse upstream performance claims.
- Clock/slot causality can leak future prices: use the repository's exchange-wall-clock epoch convention, fixed half-session resets, causal marks, gap/final-tick tests, and source-oracle fixtures.
- Default local Native changes the prior threat posture: auto-enable only the hash-pinned bundled preset, retain loopback peer/Host/Origin guards, and keep arbitrary custom Native opt-in.
- Saved-source migration can destroy user code: never rewrite the existing source record; introduce versioned preset selection and separate bundled draft state with explicit recovery.
- NumPy wheels/imports can affect portability and cold start: pin 2.4.5, lazy-load only in the worker, test supported Python/macOS/Linux paths, and leave Cloud Run dependency/runtime unchanged where possible.
- Upstream has no applicable root license: record user-authorized local provenance and hashes without asserting public redistribution rights.

### Success Criteria
- With `_moomoo` unavailable/renamed and Native env variables absent, `uv run python run.py` exposes a bundled RL capability and the real child produces a valid trace from the copied checkpoint.
- Copied model bytes hash to `528b9dc44928984c471731bb64ef82fa4a299a2ffd5707d1ce2a482223d28ef1`; golden source-oracle Q values/actions match NumPy 2.4.5 within float32 tolerance.
- Fresh and existing browser profiles initially select bundled RL/Native; the user's existing `print(event)` or any custom source remains byte-for-byte recoverable and is never auto-executed.
- Explicit Load then Start runs the bundled policy. Repeated long targets do not accumulate beyond 100 shares, flat targets exit exactly, proposals use actual committed position, and tick-i decisions cannot fill before tick i+1.
- Invalid model, incompatible genome, unsupported portfolio/open-order state, missing capability, Cloud Run, Windows, unsafe Host/Origin/peer, timeout, cancellation, or stale session fails closed without a fetchable artifact or implicit Pyodide run.
- Model/adapter/runtime/schema changes change identity; identical input reuses the immutable artifact; replay performs no Native HTTP after trace retrieval.
- New release reconstructs exactly, retained releases are unchanged, focused tests and real model/server/browser/broker integration pass, and repository-wide verification remains green.

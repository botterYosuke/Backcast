# Bundled RL Default Strategy: Affected-Area Scan

Date: 2026-09-28. Scope: read-only inspection of backcast and `_moomoo`; this report is the sole written artifact.

## Key Conclusions

1. Bundle `models/rl_scalp_evolution_v1.json`, the current 11,932-byte evolutionary fitted-Q champion. It is a NumPy MLP, not PyTorch. Do not select `rl_scalp_full_v3_q.json`: upstream documents that its flat-state policy makes no entries. Do not select smoke or pre-lossfix artifacts.
2. The champion uses only tick sign, three normalized returns, clock sine/cosine, and position. Its genome explicitly disables minute and day features. Therefore this exact checkpoint needs no daily/minute databases, historical universe CSV, training code, or `_moomoo` runtime path.
3. A small inference-only module plus the unchanged JSON checkpoint can run inside the existing child worker. NumPy is the only new runtime dependency needed for faithful float32 matrix/activation semantics; copying the upstream ML dependency stack is unnecessary.
4. Position is an input. Existing batch callbacks have no fill/portfolio state. Correct integration must either precompute both position branches and select using actual host portfolio, or use an explicitly constrained flat-start/100-share/market-order next-tick model with an independently proven broker parity contract. Merely treating a proposal as an upstream fill is incorrect.
5. The user-visible default requires coordinated changes to HTML source, workspace source fallback, saved-source migration, default execution mode, and local launcher configuration. Changing only the sample string will leave existing localStorage drafts and Pyodide mode active.
6. The bundled execution identity must include checkpoint, adapter, worker/runner semantics, Python/NumPy runtime, and actual tick identity. A fixed friendly model label cannot invalidate traces when bundled model or inference code changes.
7. The model is a research artifact (`VALIDATION_RESEARCH_ONLY`, `OBSERVATION`); the browser paper-fill contract differs from upstream evaluation. Preserve provenance and describe the port as this checkpoint evaluated under backcast execution, without presenting upstream reported performance as backcast results.

## Required Assets and Upstream Entry Points

| Path under `_moomoo` | Role | Needed in shipped runtime? |
|---|---|---|
| `models/rl_scalp_evolution_v1.json` | Exact selected genome, weights, biases, training/provenance hashes | Yes; copy unchanged |
| `src/moomoo/rl_scalp_evolution.py:102` | `encode`: feature selection, clipping, float32 position | Port inference-only logic |
| `src/moomoo/rl_scalp_evolution.py:135` | `MLP.forward`: hidden tanh, linear two-value output | Port inference-only logic |
| `src/moomoo/rl_scalp_evolution.py:259` | `policy_for`: separate flat/long evaluations, margin comparison | Port exact comparison |
| `src/moomoo/rl_scalp_evolution.py:83` | `causal_features`: decision-mark return history, clock convention | Port/adapt causal scheduling |
| `src/moomoo/rl_scalp_full_benchmark.py:145` | `build_membership_episodes`: decision clock, session segmentation, tick-sign reference | Reference only |
| `src/moomoo/rl_scalp_full_benchmark.py:48` | `BenchmarkConfig`: 100 shares, 10-second steps, 1-second historical latency, 2 bps side cost | Provenance/reference |
| `src/moomoo/kabu_rl_paper.py:57` | `_load_evolution_model`: checkpoint/source/validation checks and shape/finite checks | Reference; do not import live paper stack |
| `docs/analysis/rl_scalp_evolution_v1.md` | Research verdict, provenance, architecture, evaluation limitations | Preserve relevant facts in bundled README/manifest |
| `docs/analysis/rl_scalp_evolution_v1_validation.json` | 6,154,621-byte complete research output | Not needed for inference; compact provenance manifest is sufficient |

Checkpoint facts:

- Full file SHA-256: `528b9dc44928984c471731bb64ef82fa4a299a2ffd5707d1ce2a482223d28ef1`.
- Upstream validation SHA-256: `304a0293e04300835b30c7bef864149ce4d944f8696562b968e21e39805536b0`; upstream calculates this after stripping trailing CR/LF. Preserve and label both hash conventions.
- `spec_id`: `rl_scalp_evolutionary_fqi/v1`.
- Genome: raw input, window 3, minute false, day false, clock true, width 16, depth 2, gamma .98, learning rate .001, action margin 1 bps.
- Matrix shapes: `(7,16)`, `(16,16)`, `(16,2)`; biases lengths 16,16,2.
- Encode: tick sign; `clip(return_bps / 30, -2, 2)` for 10/20/30-second history; sine/cosine of `2*pi*seconds_of_day/86400`; position 0 or 1. NumPy float32 at feature/weight boundaries.
- Choose target long iff `Q[1] > Q[0] + 0.1`; equality chooses flat. Long target means 100 shares, not one incremental buy per decision.
- Upstream feature rows contain 11 base fields but selected encoder produces seven. Reject unsupported bundled genome changes rather than supplying fabricated daily/minute inputs.

## Causal Scheduling and Execution Compatibility

Historical decision slots run every 10 seconds, beginning 09:01 and 12:31. Last modeled decisions are 11:24:30 and 14:49:30. Half-session return history resets. Tick sign compares the as-of mark at slot `t` against the as-of mark at `t-10s`; return lags use previous decision marks. Early unavailable lags are zero.

`repository.py` stores timezone-naive exchange timestamps as UTC-interpreted epoch microseconds for display compatibility. Decode exchange wall-clock using this existing representation; adding JST conversion again shifts model clock features nine hours.

At a sparse gap, processing all missed slots using the newly arrived tick would leak future prices into old decisions. Select causal marks using only ticks at/before each slot and define how a slot maps to an observed tick. Multiple logical slots mapping to one observed tick must remain deterministic and bounded. Never emit proposals on the final tick because the current worker rejects them.

Upstream evaluation uses first print of at least 100 shares after one second and within the next decision interval, with pending exit handling and forced session exits. Backcast `order-broker.mjs:matchTick` fills every eligible market order at the next committed tick price without print-quantity filtering or that latency window. Therefore upstream fill/reward simulation must not be copied into the strategy as if authoritative.

The strongest integration keeps trace rows proposal-only and performs position-branch resolution in the existing host transaction. `replay-commit.mjs` already provides actual `portfolio`, `fills`, and open orders to `strategyPort.dispatchOneEvent` after broker commit. The native controller currently ignores these fields and reads the precomputed row. This is the natural location to resolve bounded precomputed policy branches if the trace contract is extended.

A narrower alternative could retain plain commands if flat start, fixed 100 shares, market-only orders, exact next-tick position update, no manual mixing, no overlapping orders, and no skipped fill/commit are all enforced and tested. The current native controller only rejects nonzero cursor/lastCommitted and manual open orders; it does not itself enforce flat portfolio.

## Backcast Integration Points

| Area | Existing files and behavior | Required change surface |
|---|---|---|
| Native settings | `src/tickreplay/config.py`, `.env.example`: all native settings default off; enabled mode requires trusted flag, argv and fingerprint; Cloud Run/Windows rejected | Configure bundled runtime for ordinary local `run.py` launch while preserving explicit overrides and remote guards |
| Launcher | `run.py`: adds `src` to parent sys.path; uvicorn import-string startup; default bind 127.0.0.1 | Supply safe bundled local defaults without requiring `_moomoo/.venv` or user env setup; reload subprocess must receive configuration |
| Worker import | `src/tickreplay/native_strategy_worker.py`: file-mode CLI; imports runner by absolute sibling path; sanitized subprocess environment | Ensure bundled package imports work without inherited PYTHONPATH/current directory |
| Worker callback | `on_start`: contract/session tick count/run config; `on_event`: tick index, replay microseconds, raw price, quantity, trade type | Add needed bounded inference/proposal support; no FastAPI source execution |
| Identity/store | `strategy_trace.py`, `native_strategy.py`: exact schema, canonical source/session bytes/runtime/run config; immutable store | Extend exact validation and identity consistently if conditional proposals introduced |
| Server | `server.py`: capability/jobs/leases/traces; local peer/Host/Origin guard; authoritative ticks supplied server-side | Expose bundled default metadata/capability if needed; retain local-only protections |
| HTML | `static/index.html:172`: hardcoded empty sample; Pyodide option first | RL source preview and correct default mode |
| Workspace | `static/strategy/strategy-workspace.mjs:43`: `SAMPLE_STRATEGY_SOURCE`; line 201 initial mode `pyodide`; `restoreStoredState` unconditionally takes saved source | Fresh default, capability-aware Native mode, narrow migration, visible sample restore |
| Source storage | `strategy-source-store.mjs`: source.v1/settings.v1; validated SHA/source revisions and parent record CAS | Preserve user-written drafts; migrate only recognized legacy empty templates, including user-visible `print(event)` variant if exact match |
| Native controller | `strategy-trace-controller.mjs`: cursor zero admission, no manual order mixing, asynchronous batch load then local dispatch | Policy branch selection if selected; session/default orchestration |
| Release | `scripts/build_strategy_release.mjs`; `static/app.js`; `tests/browser/strategy-assets.mjs` | Build a new immutable release and update import pointer/test fixtures; never edit retained release bytes |
| Dependencies/docs | `pyproject.toml`, `uv.lock`, `docs/tick-replay.md`, release runbook | Add only inference dependency and document one-command startup/provenance/contracts |

There is no Python package build configured (`tool.uv.package=false`), so a worker launched from a standalone path does not automatically inherit the launcher's in-memory sys.path. Resolve module path explicitly from worker location or choose a robust fixed-argv package launcher.

The workspace only persists panel expansion in settings; execution mode currently resets to Pyodide. Existing saved source is independently validated and protected against concurrent tabs/future schema. Do not blindly clear localStorage to change defaults.

## Dependency Graph

`run.py` -> bundled runtime configuration -> FastAPI native job manager -> fixed argv native worker -> bundled inference module -> bundled JSON model + NumPy.

Browser HTML/workspace -> default RL Python source -> source identity/store -> native submit -> immutable trace -> native controller -> existing replay committer -> existing broker.

Training code, kabu network client, credentials, market data copied from `_moomoo`, large validation output, and the full upstream dependency lock are not runtime edges for the selected champion.

## Risks and Boundaries

- No LICENSE/NOTICE file was found under `_moomoo` by filename search. The user explicitly authorized copying their repo assets; preserve exact source provenance and avoid inventing a license grant for public redistribution.
- Copy only the identified JSON/model-support code. Never copy `.env`, credentials, kabu clients, database archives, virtualenv, or unrelated trained models.
- The 12 KiB JSON requires strict finite-number, dimension, genome, and content-hash validation. No pickle/joblib loader is necessary.
- Hashes in upstream JSON refer to upstream source files; ported inference files will have different hashes. Keep those as provenance and use separate backcast runtime hashes, not a fake upstream source-validation claim.
- Default local enablement changes a prior design decision. Bind/source-origin protections and Cloud Run restrictions must stay explicit in tests; do not turn on arbitrary-code execution for deployed public app instances merely to enable a bundled default.
- User's dirty `.codex/config.toml` and prior Native Trace implementation are present; preserve both. Work branch is already a feature branch with uncommitted prior work.
- Model output can validly remain flat on some sessions. Do not add fallback momentum/random trades to manufacture visible entries. Tests should use known deterministic model fixtures and real-model predictions that demonstrably change target.
- Automatic startup of a strategy should respect stored custom source: never execute an arbitrary restored draft just because bundled default starts automatically. Restrict automatic preparation to recognized bundled source, or keep explicit Start for custom drafts.

## Proposed Ownership Split

1. **Inference/assets owner:** new `src/tickreplay/rl_strategy.py` (or package), bundled model/provenance files, isolated inference tests. Owns NumPy dependency and lock change only if explicitly assigned. Produces exact default source string/API contract for other owners.
2. **Runtime/protocol owner:** native worker/config/server/trace schemas and Python tests; local launcher integration. If needed, owns Python conditional-policy schema and communicates exact schema to frontend before implementation.
3. **Frontend owner:** workspace/source migration/default mode, native controller/protocol validation, HTML/app wiring, Node/browser tests, immutable release builder/output and related fixture pins. Owns browser paper-trading compatibility tests, not backend worker.

One owner should handle shared docs and final integration after contracts are frozen. Root should independently verify real-process execution and browser-to-real-server flow, not only mocked transport.

## Exact Acceptance Tests to Add/Run

- Model loader: copied bytes/hash, expected genome/layer sizes, NaN/inf/malformed shape rejected, no `_moomoo` file dependency.
- Inference parity: golden feature encodings/Q values/actions captured from upstream exact checkpoint, both positions, margin tie, clipped extremes and float32 tolerances.
- Causality: fixed 10-second boundaries, half-session reset, first-lag zeros, irregular ticks, same-time normalization, large gaps, no final-tick command, no tomorrow/future tick dependency.
- Portfolio parity: repeated long target creates only one 100-share entry; flat exits exact inventory; pending/failed host transaction does not cause assumed position; actual next-tick eligibility retained; no accidental shorting/manual mixing.
- Runtime: plain `uv run python run.py --no-browser` with native env absent enables bundled local capability and loads bundled module through real subprocess from unrelated cwd; explicit disable/custom argv preserved; Cloud Run and unsafe host remain protected.
- Identity: checkpoint bytes, adapter bytes/runtime version changes invalidate trace cache; unmodified repeat is reusable.
- Persistence/default: fresh browser has RL source/Native mode; exact old empty sample migration; custom source preserved; corrupt/future schema preserved; multi-tab parent CAS intact; unsupported backend produces actionable unavailable state.
- Real server E2E: load actual cached session, prepare RL trace through FastAPI/native process, replay and observe a deterministic entry and exit; browser requests stop after trace retrieval; pause/reset/session switch invalidate safely.
- Gates: `uv run pytest -q tests/test_tickreplay_native_strategy.py tests/test_tickreplay_native_strategy_worker.py tests/test_tickreplay_strategy_trace.py tests/test_tickreplay_config.py tests/test_tickreplay_server.py` plus new RL tests; focused `node --test` workspace/trace/broker/replay suites; `npm run check:strategy-release`; `npm run test:strategy-assets`; focused Playwright; repository verification script.

## Conventions

Use `uv`; Python annotations and Ruff conventions; plain native ES modules and node:test; source edits through apply_patch; frozen immutable release generation through existing builder. This report records findings, not implementation or final architectural approval.

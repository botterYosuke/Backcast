# Feasibility Analysis: Pyodide RL replay/paper auto-trading

Date: 2026-09-28 (Asia/Tokyo)  
Mode: RESEARCH-ONLY

## Executive verdict

**PARTIALLY FEASIBLE / CONDITIONAL GO for a shadow-first replay/paper experiment; NO-GO as-is and NO-GO for live trading.** The discovered artifact is a small, plain-JSON custom MLP rather than a PyTorch, Stable-Baselines3, ONNX, or pickle checkpoint. Its inference can be reproduced in the existing Worker with a generated standard-library-only adapter. The original `_moomoo` loader cannot run directly because it imports NumPy while the current backcast release is intentionally standard-library-only. More importantly, backcast's current event ABI exposes logical tick levels, not the actual prices required by the trained return features, and its paper broker has materially different fill, fee, capital, and position semantics. The current model is itself classified `Reject` and `OBSERVATION`, so it must not be promoted to live orders.

| Question | Verdict | Reason |
|---|---|---|
| Reuse model for deterministic shadow replay | Conditional GO | Complete small inference closure is identifiable, but the price/feature ABI, scheduling, warm-up, identity, and parity gates must be added. |
| Reuse model for current paper broker | Conditional GO only as a new experiment contract | Host validation/atomicity is suitable, but the broker is not semantically equivalent to the model's historical evaluator. Results must not be compared without labeling the contract change. |
| Run original `_moomoo` artifact/loader directly in current Worker | NO-GO as-is | The loader depends on NumPy and custom `_moomoo` modules; the Worker loads neither. |
| Standard-library converted inference | Preferred browser route | A 7→16→16→2 MLP needs only bounded scalar arithmetic and `math.tanh`; no new runtime package is required. |
| Training in Pyodide | NO-GO for this workflow | Training requires a wider data/dependency/lifecycle closure and provides no benefit to deterministic replay. |
| Live brokerage automation | NO-GO | The model has rejected economics and the browser boundary has no live-order safety or evidence basis. |

## Investigation safety and method

The artifact was inspected only with file metadata, text tools, hashes, and source references (`file`, `wc`, `shasum`, `rg`, `sed`, and `nl`). It was **not deserialized**, no model loader or strategy was executed, training was not run, brokerage APIs were not called, and no order was placed. The JSON was observed as 11,932 bytes and one line at `/Users/sasac/_moomoo/models/rl_scalp_evolution_v1.json:1`. The byte-for-byte file SHA-256 obtained with `shasum -a 256` is `528b9dc44928984c471731bb64ef82fa4a299a2ffd5707d1ce2a482223d28ef1`; `_moomoo` deliberately hashes the bytes after stripping trailing CR/LF and reports `304a0293e04300835b30c7bef864149ce4d944f8696562b968e21e39805536b0` (`/Users/sasac/_moomoo/src/moomoo/kabu_rl_paper.py:61-64`).

## Model artifact and inference closure

### Artifact and status

- Artifact: `/Users/sasac/_moomoo/models/rl_scalp_evolution_v1.json:1`, spec `rl_scalp_evolutionary_fqi/v1`, plain JSON, approximately 12 KiB.
- Canonical implementation: `/Users/sasac/_moomoo/src/moomoo/rl_scalp_evolution.py:33-140`; JSON export is at lines 407-458.
- Native loader: `/Users/sasac/_moomoo/src/moomoo/kabu_rl_paper.py:58-101`. It validates the validation report, model hash, genome, training digest, universe/config/source hashes, shapes, and finite values before reconstructing NumPy arrays.
- Evidence status: the current report records +¥109,942.81 at 2 bps, but −¥615,257.96 at 5 bps and −¥1,823,925.93 at 10 bps, and labels adoption `Reject`, phase `Hold`, evidence `OBSERVATION`, with an explicit prohibition on real orders (`/Users/sasac/_moomoo/docs/analysis/rl_scalp_evolution_v1.md:3-7`). Economics/execution remain blocked and the board-proxy paper environment is not forward evidence (lines 42-49).

### Observation, state, network, and action

The selected genome uses seven ordered inputs:

1. tick-direction sign;
2. 10-second return divided by 30 and clipped to `[-2, 2]`;
3. 20-second return, identically scaled/clipped;
4. 30-second return, identically scaled/clipped;
5. clock sine;
6. clock cosine;
7. current binary position.

The causal feature definitions and encoding are in `/Users/sasac/_moomoo/src/moomoo/rl_scalp_evolution.py:83-119`. The selected genome has `minute=false`, `day=false`, `window=3`, `clock=true`, and `input_mode=raw` (`rl_scalp_evolution.py:33-50` and the artifact at line 1). There is no learned normalization object and no recurrent neural state. The stateful preprocessor does require rolling decision marks and the current position.

Inference is a custom feed-forward NumPy MLP with two 16-unit `tanh` hidden layers and a two-value linear output (`rl_scalp_evolution.py:122-140`). Its shape is 7→16→16→2: 400 weights plus 34 biases, 434 scalar parameters. Action target `1` is selected only when `Q1 > Q0 + action_margin_bps / 10`; for the selected 1 bps genome the threshold is `0.1` (`rl_scalp_evolution.py:259-266`). The model is not recurrent and requires no GPU, process, or thread.

### Dependency split

- Inference as written: `_moomoo` custom modules, NumPy, and the Python standard library. The project pins Python `>=3.13,<3.14` and NumPy `2.4.5` (`/Users/sasac/_moomoo/pyproject.toml:4-13`).
- Converted inference: standard-library `json` plus scalar loops and `math.tanh`, with the frozen parameters embedded or installed as an immutable same-origin asset.
- Training/evaluation only: historical daily/minute/trade data, DuckDB-backed data access, fitted-Q training loops, batching, target-network updates, candidate selection, and report generation (`rl_scalp_evolution.py:223-243,407-458`). These do not belong in the replay Worker.

## Current backcast runtime

### Pyodide and package bootstrap

The frozen runtime is Pyodide 0.27.6 with CPython 3.12.7 (`scripts/fetch_pyodide_assets.py:15-16`). Its release manifest sets `approved_wheels=[]`, `dynamic_package_install=false`, `load_packages_from_imports=false`, `micropip=false`, and `stdlib_only=true` (`scripts/fetch_pyodide_assets.py:136-147`). The Worker loads Pyodide, writes only the Python runner into the virtual filesystem, and invokes it; it does not install a model, custom `_moomoo` package, or NumPy (`src/tickreplay/static/strategy/strategy-worker.mjs:120-180`).

Pyodide 0.27.6 officially offers NumPy 2.0.2, but only standard-library modules are initially available; non-stdlib packages require explicit loading ([Pyodide package list](https://pyodide.org/en/0.27.6/usage/packages-in-pyodide.html), [package loading](https://pyodide.org/en/0.27.6/usage/loading-packages.html)). Backcast's lock metadata names that wheel, but it is not an approved, vendored runtime asset. Therefore `import numpy` in the original loader fails under the current policy. Pyodide also cannot start Python threads/processes and has browser socket constraints, none of which blocks this feed-forward inference but does rule out silently importing wider native training stacks ([WebAssembly constraints](https://pyodide.org/en/0.27.6/usage/wasm-constraints.html)).

### Strategy ABI and limits

The Python ABI accepts a source program, compiles/executes it once, and retains one strategy object between lifecycle callbacks (`src/tickreplay/static/strategy/strategy-runner.py:345-463`). Limits include 256 KiB source, 8,192 source lines, 4,096 scalar elements per line, 256 KiB request, 64 KiB response, 32 commands per event, and 10,000 commands per run (`strategy-runner.py:18-31`). The 11,932-byte artifact fits the total source budget but not as one 4,096-scalar literal; generated multiline/chunked parameters or a release asset is required.

The typed event contains session tick index, replay timestamp, logical `price_level`, quantity, and trade type (`src/tickreplay/static/strategy/strategy-protocol.mjs:282-318`). The application converts the actual replay price to a logical level before dispatch (`src/tickreplay/static/app.js:1937-1954`). The trained model's 10/20/30-second features are actual-price returns, so they cannot in general be reconstructed faithfully from a logical level alone. This is the primary semantic blocker to reusing the policy under ABI v1.

Desktop callback deadlines are 20 s load, 3 s compile, 1 s start, 250 ms event, and 500 ms end; mobile uses 40 s, 6 s, 2 s, 500 ms, and 1 s (`src/tickreplay/static/strategy/strategy-controller.mjs:30-35`). The tiny forward pass is plausibly within those bounds, but no model-specific browser benchmark has been run. Existing warm Worker measurements (desktop p95 6.3 ms, mobile p95 13.4 ms) exercise the strategy spike, not this adapter (`tests/browser/strategy-spike-result.md:43-57`).

## Replay and paper-trading compatibility

Backcast already has the correct *authority boundary*: the Worker can return only validated typed commands, while the host owns broker state (`strategy-worker.mjs:183-191`; `strategy-protocol.mjs:385-437`). Replay processes each tick with backpressure, commits the previous command batch atomically, applies the current tick/fills, invokes the strategy, and stages the new response for the next eligible tick (`docs/tick-replay.md:530-554`; `src/tickreplay/static/strategy/replay-commit.mjs:152-232`). Broker batches are validated and swapped atomically only after candidate completion (`src/tickreplay/static/strategy/order-broker.mjs:468-606,755-763`). Generation/run identity and stale-response checks already exist (`strategy-controller.mjs:165-303,553-586`). A model must remain an unprivileged proposal producer behind this boundary.

However, this is not the evaluator on which the policy evidence was produced:

- `_moomoo` makes decisions on fixed 10-second slots and fills on the first qualifying tick from decision time +1 s through +10 s, with quantity, ¥1,000,000 capital, and 2 bps per-side cost (`/Users/sasac/_moomoo/src/moomoo/rl_scalp_full_benchmark.py:47-57,175-230`).
- Backcast market orders fill at the first eligible trade without the same elapsed-time window or quantity constraint; limit fills use crossing and the limit price (`order-broker.mjs:655-738`).
- Backcast paper positions are signed long/short and the ledger has no equivalent fee or capital-limit semantics (`src/tickreplay/static/strategy/paper-trading.mjs:13-16,75-103`).

Thus transactional safety is compatible, but economic semantics are not. An adapter can be deterministic and safe while still evaluating a different experiment. It must be labeled as such unless the host adds an explicit `_moomoo`-compatible research mode.

## Runtime route comparison

| Route | Feasibility | Compatibility/risk | Decision |
|---|---|---|---|
| A. Original JSON + original NumPy/custom loader | Not in current release | NumPy/custom package absent; Python/NumPy versions differ | NO-GO as-is |
| B. Generated standard-library fixed MLP | High after parity work | Smallest closure; preserves stdlib-only release; second arithmetic implementation may round differently | **Preferred** |
| C. Vendor Pyodide NumPy 2.0.2 | High with a new release | Closer source shape, but not native 2.4.5; adds startup, asset, integrity, license, memory, and browser gates | Fallback if B cannot meet parity |
| D. ONNX Runtime Web | Technically viable | Custom conversion, new JS/WASM runtime and provenance surface are disproportionate for 434 parameters | Defer |
| E. Version-pinned native inference server | Technically viable | Can preserve native closure but adds network nondeterminism, availability, authentication, privacy, and audit concerns | Future native-only/large-model fallback |
| F. Browser training | Narrowly possible, unjustified | Data, packages, CPU/memory, persistence, restart, and Worker responsiveness conflict with replay | Do not pursue |

For route B, action parity—not bit-for-bit Q-value equality—is the decisive requirement. Scalar `math.tanh` and NumPy/WASM can differ near `Q1-Q0=0.1`; use fixed native NumPy 2.4.5 golden vectors, a predeclared Q tolerance, and 100% identical discrete actions, with dense cases around the threshold. If route C is needed, it requires a new immutable release and the full package/asset/browser gate rather than runtime installation. Official feasibility evidence is collected in `.agents/docs/research/spike-pyodide-rl-auto-trading-research.md:27-98,128-145`.

## Recommended architecture boundary

Use a three-part boundary:

1. **Offline authority:** training, model selection, validation, and manifest generation remain in `_moomoo`. Publish only immutable weights and a manifest containing model/spec hash, feature schema/version, layer dimensions, decision threshold, native dependency versions, and provenance digests.
2. **Worker inference:** a generated, bounded, standard-library-only adapter maintains deterministic warm-up/rolling history, computes the seven frozen features, emits Q-values and a typed target/proposal, and has no network, portfolio mutation, credential, or brokerage capability.
3. **Host authority:** extend the versioned event contract with either exact actual price as safe integer minor units plus scale, or host-computed frozen feature values. The controller verifies model/preprocessor identity, generation, cursor, timestamp, finiteness, action enum/range, replay/paper mode, and next-tick eligibility. Only the existing candidate transaction may mutate paper state. Log the observation/feature digest, Q-values, threshold, proposal, host decision, and resulting transaction identity.

Start in **shadow-only** mode with no commands. Promotion to paper requires all acceptance gates below. No path from this spike authorizes live brokerage access.

## Acceptance gates for a follow-up prototype

1. Freeze schema, dimensions, feature order, scaling/clipping, warm-up/reset rules, 10-second slot rule, action threshold, model/preprocessor hashes, and model provenance.
2. Add an exact-price or host-feature ABI; reject using logical `price_level` as a silent substitute.
3. Generate a trusted native NumPy 2.4.5 golden corpus covering ordinary observations, clip extrema, zero prices/positions, reset/restart, NaN/Inf rejection, and dense boundary cases around `Q1-Q0=0.1`.
4. In pinned Pyodide 0.27.6 require a predeclared numeric tolerance and 100% action agreement; fail closed rather than adjusting tolerance after failures.
5. Verify deterministic outputs over repeat runs, seek/restart/reset, browser reload, and supported browsers; benchmark cold load, warmed event latency, and memory under existing deadlines.
6. Exercise malformed artifacts, wrong hashes/shapes, non-finite values, stale cursor/generation/model ID, callback timeout, over-limit output, and transactional rollback.
7. Compare end-to-end replay against a native reference only under an explicitly matched fill/cost/capital/position contract. If contracts differ, report both separately.
8. Keep model status `research/observation`; require new forward paper evidence and a separate risk/operations decision before any live-trading discussion.

## Risks and remaining unknowns

- **Evidence risk (critical):** the selected model loses under modestly worse costs and is explicitly rejected. Technological feasibility does not establish tradability.
- **Feature-contract risk (high):** exact price is absent from ABI v1; substituting logical levels changes the policy.
- **Execution-contract risk (high):** current paper fills, fees, capital, and signed positions differ from training/evaluation.
- **Numerical risk (medium):** tiny Q differences can flip actions at the strict margin boundary.
- **Lifecycle risk (medium):** rolling state must be reset/rebuilt identically across start, seek, restart, cancellation, and Worker replacement.
- **Supply-chain/security risk (medium):** JSON is safer than pickle but still needs byte, depth, shape, finiteness, magnitude, provenance, and hash limits; Pyodide is a same-origin execution environment, not an application security sandbox (`docs/tick-replay.md:516-528`).
- **Release drift (low/medium):** `docs/tick-replay.md:783-784` and `docs/pyodide-strategy-release-runbook.md:10-15` name different current release identities. The generated runtime identity should remain authoritative and documentation should be reconciled before a release change.
- **Unmeasured:** model-adapter browser latency/memory, cross-browser `tanh` parity, full historical end-to-end replay parity, exact redistribution rights for model/training data, and restart/warm-up UX.

## Codex analysis and independent verification

Three mandatory read-only consultations completed through `uv run python .agents/skills/_shared/codex_consult.py`:

- Technical feasibility: `.agents/logs/codex/20260927T223334Z-spike-feasibility.md`
- Architecture compatibility: `.agents/logs/codex/20260927T223334Z-spike-architecture.md`
- Risk/trade-offs: `.agents/logs/codex/20260927T223334Z-spike-risk.md`

Their common conclusion—original loader NO-GO, standard-library adapter conditional GO, architecture changes required, shadow-first, live NO-GO—was independently checked against the cited source files. Two details were corrected during verification: the network contains 400 weights plus 34 biases (not 418 weights), and a generic concern about a six-mark deque does not affect the selected `window=3` genome. Codex output was advisory rather than treated as primary evidence.

## Final recommendation

Do **not** auto-trade with the current model. Approve only a separate, bounded prototype of route B: a versioned exact-price/feature ABI plus standard-library adapter, golden parity suite, and shadow replay through the existing host-owned transactional broker. If those gates pass, a paper-only experiment is technically supportable but remains a new execution contract and must retain `OBSERVATION` status. Keep training native/offline. Use Pyodide NumPy only if scalar parity fails, ONNX only if future model complexity warrants it, and an inference-only server only for a future model whose native closure cannot reasonably be converted. Live trading remains out of scope and unsupported by the evidence.

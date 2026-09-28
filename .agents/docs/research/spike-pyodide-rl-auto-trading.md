# Spike Report: Pyodide RL auto trading

## Question
Can the RL model under `/Users/sasac/_moomoo` be reused for automated replay/paper trading in backcast, and can its inference run directly in the existing Pyodide Worker?

## Verdict: GO
**Confidence**: MEDIUM  
**Decisive factor**: A tiny plain-JSON MLP can be ported to a standard-library-only inference adapter and kept behind backcast's host-owned transactional broker, but the original loader, immediate paper-order activation, and live trading are not approved.

## Investigation Parameters
- Time budget: 30 minutes
- Mode: RESEARCH-ONLY
- Date: 2026-09-28

## Success Criteria Evaluation
| Criterion | Evidence | Met? |
|-----------|----------|------|
| GO if the model/runtime and contracts are identifiable and a safe inference path exists | The artifact is a ~12 KiB JSON 7→16→16→2 `tanh` MLP with 434 scalar parameters. A `json` + `math.tanh` adapter can emit proposals through existing host validation and atomic paper transactions. | YES, for a bounded shadow prototype |
| NO-GO if the artifact/preprocessing cannot be reconstructed or every route violates the broker boundary | The artifact, seven-feature preprocessor, rolling state, action threshold, and runtime closure are identifiable; converted inference need not bypass the broker boundary. | NO |
| INCONCLUSIVE if a missing artifact or fixed-input test is required to choose a route | No artifact is missing. Golden parity and browser benchmarks remain implementation acceptance gates rather than blockers to choosing the standard-library route. | NO for route selection; PARTIAL for paper activation |

## Sub-question Findings
### Artifact and runtime closure
- Finding: The selected model is plain JSON for a custom, non-recurrent MLP, not a PyTorch/SB3/ONNX/pickle artifact.
- Evidence: `/Users/sasac/_moomoo/models/rl_scalp_evolution_v1.json`, `_moomoo/src/moomoo/rl_scalp_evolution.py`, and the dependency manifest identify the weights, seven features, threshold, custom NumPy forward pass, and native version pins.
- Assessment: FEASIBLE

### Direct Pyodide execution
- Finding: The original `_moomoo` loader does not run unchanged because the current Pyodide 0.27.6 release is intentionally standard-library-only and does not load NumPy or custom `_moomoo` modules. A generated scalar adapter is feasible without new packages.
- Evidence: backcast's frozen release manifest and Worker bootstrap; official Pyodide 0.27.6 package-loading and package-list documentation.
- Assessment: NOT_FEASIBLE as-is; FEASIBLE after conversion

### Replay/paper integration
- Finding: Backcast already provides stale-result rejection, next-tick staging, typed commands, host validation, and atomic broker transactions. ABI v1 lacks the actual price required for the trained return features, and current broker economics differ from the training evaluator.
- Evidence: strategy protocol/controller/worker, replay commit, order broker, and `_moomoo` benchmark code.
- Assessment: FEASIBLE only with an explicit exact-price or host-feature ABI and a separately labeled research execution contract

### Browser training
- Finding: Training adds data, dependency, persistence, CPU/memory, and lifecycle requirements without improving deterministic replay.
- Evidence: `_moomoo` training/evaluation closure and Pyodide WebAssembly constraints.
- Assessment: NOT_FEASIBLE for the intended production workflow; keep it offline

## Risks
| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Model is documented `Reject / OBSERVATION` and loses under modestly higher costs | High | High | No live trading; shadow first; do not infer profitability from technical feasibility |
| Logical price level cannot reproduce trained actual-price returns | High | High | Add exact safe-integer price/scale or host-computed frozen features to a versioned ABI |
| Paper broker semantics differ from training evaluator | High | High | Define and label a separate research contract; compare only under matched fills, costs, capital, and positions |
| `math.tanh`/NumPy differences flip actions near `Q1-Q0=0.1` | Medium | High | Native NumPy 2.4.5 golden corpus, predeclared tolerance, and 100% discrete-action parity |
| Rolling history diverges after seek/reset/restart | Medium | High | Specify deterministic warm-up/reset/rebuild rules and test lifecycle transitions |
| Model JSON or release asset is malformed/untrusted | Medium | High | Enforce size/depth/shape/finiteness/magnitude limits, provenance, and SHA-256 identity |

## Prototype Results (if applicable)
- Tested: No runtime prototype was executed in this RESEARCH-ONLY spike.
- Result: INCONCLUSIVE for paper-order activation; route selection is supported by code and dependency evidence.
- Evidence: A follow-up fixed-input Pyodide golden-parity and replay-shadow prototype is required before enabling paper commands.

## Architecture Compatibility
- Assessment: REQUIRES_CHANGES
- Required changes: immutable model/preprocessor manifest; standard-library-only inference adapter; versioned exact-price or host-feature event ABI; deterministic 10/20/30-second rolling state; model/cursor/generation audit identity; golden parity; shadow logging. The Worker remains proposal-only and the host remains the sole portfolio mutation authority.

## Alternatives Considered
| Alternative | Pros | Cons | Verdict |
|-------------|------|------|---------|
| Standard-library JSON/`math.tanh` adapter | Smallest closure, offline, no new runtime asset | Second arithmetic implementation; needs parity tests | Preferred |
| Pyodide NumPy 2.0.2 | Closer to the native source form | New immutable asset/release; differs from native NumPy 2.4.5; startup and supply-chain costs | Fallback if scalar parity fails |
| ONNX Runtime Web | Purpose-built browser inference | Conversion/runtime overhead is disproportionate for 434 parameters | Defer |
| Version-pinned server inference | Preserves native runtime closure | Network nondeterminism, availability, privacy, authentication, and audit burden | Future native-only model fallback |
| Training in Pyodide | Browser-local experimentation | Conflicts with replay lifecycle, persistence, responsiveness, and dependency policy | Reject |

## Recommendation
GO only for an existing-codebase feature that builds a bounded **shadow replay prototype**. Do not run the original loader unchanged, do not enable automatic paper orders until parity and lifecycle gates pass, and do not connect this rejected/observation-only model to live brokerage.

### If GO
- Next step: `/feature — existing mode`
- Key constraints to carry forward: offline training; immutable model manifest; stdlib-only proposal adapter; exact-price/feature ABI; host-owned validation and atomic transactions; shadow-first rollout; 100% action parity.
- Risks to monitor: feature/execution-contract drift, threshold rounding, rolling-state lifecycle, browser performance, release identity, model/data redistribution rights, and false inference of profitability.

### If NO-GO
- Decisive blocker: Not applicable to the bounded shadow prototype. The original unchanged loader and live brokerage are individually NO-GO.
- Suggested alternatives: standard-library conversion first; pinned Pyodide NumPy only if parity fails; inference-only server only for future native-only or larger models.

### If INCONCLUSIVE
- Missing evidence: Cross-browser Q/action parity, cold/warm latency and memory, full replay parity, restart/seek state recovery, and matched broker-economics evidence.
- Suggested follow-up: Implement the bounded shadow prototype under `/feature — existing mode`, with paper commands disabled until all acceptance gates pass.

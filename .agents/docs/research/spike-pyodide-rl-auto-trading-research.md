# External Research: Pyodide feasibility for `_moomoo` RL inference

Date: 2026-09-28 (Asia/Tokyo)  
Scope: official/primary-source research only; no production-code inspection, order placement, or brokerage design.

## Executive finding

The `_moomoo` inference implementation cannot run **as-is** in backcast's current Pyodide Worker because it imports NumPy while the Worker deliberately loads only the standard library. The model itself is reusable in that Worker with a very small adapter: the discovered artifact is **not** a PyTorch/SB3 checkpoint but a ~12 KiB plain-JSON genome for a custom, feed-forward 7→16→16→2 `tanh` MLP, with no recurrent state and no mandatory GPU/thread/process dependency. This local fact was supplied by the feasibility analyst and was not independently inspected by the researcher.

The preferred browser route is therefore to port only this small forward pass to Python standard-library loops (`json`, `math.tanh`) and validate it against native NumPy golden vectors. This preserves backcast's current standard-library-only asset policy. A second viable route is to vendor and explicitly load Pyodide 0.27.6's NumPy 2.0.2 wheel. The latter adds asset/supply-chain work and differs from `_moomoo`'s NumPy 2.4.5 pin. ONNX Runtime Web and server inference remain fallbacks, but conversion to ONNX is unnecessary overhead for this model.

This is a feasibility finding, not authorization for live trading. Inference should emit a typed proposal only; backcast's validated replay/paper broker must remain the sole mutation boundary.

## Local facts provided by the feasibility analyst

- Artifact: `/Users/sasac/_moomoo/models/rl_scalp_evolution_v1.json`, plain JSON, approximately 12 KiB.
- Runtime: custom NumPy feed-forward MLP; no PyTorch, Stable-Baselines3, ONNX, pickle, recurrent state, GPU, worker thread, or child process.
- Inputs: seven features (tick sign, clipped/scaled 10/20/30-second returns, clock sine/cosine, position).
- Network/action: 7→16→16→2 with `tanh`; action 1 only when `Q1 > Q0 + 0.1`.
- `_moomoo` pin: Python >=3.13,<3.14 and NumPy 2.4.5.
- Backcast runtime: Pyodide 0.27.6 / CPython 3.12.7, intentionally standard-library-only. Its lock metadata names NumPy 2.0.2, but the wheel is not vendored or loaded.

These are inputs to this research, not external-source claims.

## Evidence by spike sub-question

### 1. Artifact and inference runtime closure

**Evidence for direct execution**

- Plain JSON has no native-extension or pickle loader requirement. A small dense MLP needs only scalar multiply/add and `tanh`, all available in the Python standard library.
- Pyodide supports custom pure-Python code directly; for larger modules its official guidance is to package the code and load it into the Emscripten virtual filesystem ([Pyodide 0.27.6 custom code](https://pyodide.org/en/0.27.6/usage/loading-custom-python-code.html)).
- If fidelity testing shows NumPy is required, Pyodide 0.27.6 officially ships NumPy **2.0.2** ([0.27.6 package list](https://pyodide.org/en/0.27.6/usage/packages-in-pyodide.html)).

**Evidence against assuming drop-in compatibility**

- Only Python's standard library exists immediately after Pyodide initialization. Non-stdlib packages must be explicitly loaded with `loadPackage()`/`micropip`, or implicitly through `loadPackagesFromImports()` ([0.27.6 loading packages](https://pyodide.org/en/0.27.6/usage/loading-packages.html)). Therefore, under backcast's reported current policy, `import numpy` will fail even though NumPy appears in Pyodide's lock/package catalog.
- Pyodide 0.27.6 offers NumPy 2.0.2, while `_moomoo` pins 2.4.5. The operations used are simple, but exact bitwise parity across NumPy versions, WebAssembly, and native builds should not be assumed near the `Q1 - Q0 = 0.1` decision boundary.

**Required closure before implementation**

Validate the JSON schema (layer dimensions, finite numeric values, size/depth limits, model ID/hash), freeze the exact feature order/scaling/clipping, and produce native reference inputs with expected Q-values and final actions, including boundary cases around the 0.1 margin.

### 2. Forward pass in the current Pyodide Worker

**Route A — standard-library forward pass (recommended)**

- Feasible without additional package loading: parse JSON, calculate dense layers in loops, and apply `math.tanh`.
- The network is tiny (418 weights plus biases if fully connected), so framework acceleration is not needed for single-observation inference. This size statement is an inference from the supplied 7→16→16→2 architecture.
- The Worker is the appropriate isolation unit for synchronous Python computation; Pyodide documents Workers as keeping long-running Python work off the UI thread ([Pyodide 0.27.6 Web Worker](https://pyodide.org/en/0.27.6/usage/webworker.html)).
- Caveat: scalar `math.tanh` may differ by tiny rounding amounts from NumPy's ufunc. Acceptance must compare Q-values within an agreed tolerance and require identical discrete actions over a fixed and randomized corpus, especially near the threshold.

**Route B — Pyodide NumPy 2.0.2**

- Feasible only after vendoring/serving the matching Emscripten wheel and explicitly calling `await pyodide.loadPackage("numpy")`; the official docs show this exact API ([0.27.6 loading packages](https://pyodide.org/en/0.27.6/usage/loading-packages.html)).
- Prefer a pinned, locally hosted asset with a recorded digest. Pyodide documents PyPI hash validation for `micropip`, but also warns that arbitrary-URL/CORS-proxy installs do not receive the same integrity protection.
- Against: adds download/startup/memory cost, widens the approved asset set, and still does not reproduce NumPy 2.4.5 exactly. A normal 2.4.5 Linux/macOS wheel is unusable; `micropip` accepts pure-Python or wasm32/Emscripten wheels, and native extensions require a Pyodide build ([micropip usage](https://micropip.pyodide.org/en/latest/project/usage.html)).

**Runtime constraints**

- Pyodide 0.27.6 cannot start Python threads or processes; `threading`, `multiprocessing`, and sockets are non-functional ([Pyodide WebAssembly constraints](https://pyodide.org/en/0.27.6/usage/wasm-constraints.html)). This does not block the reported inference loop, but rules out dependencies that silently require thread/process pools.
- The model does not need GPU support. Introducing a GPU runtime would add portability and reproducibility concerns without a credible benefit at this size.

### 3. Safe deterministic connection to replay/paper trading

External runtime documentation cannot establish backcast's transactional correctness. The following are required application controls inferred from the spike brief:

- The model output must be an unprivileged, typed action proposal, never direct portfolio mutation or a brokerage API call.
- The host must validate model hash/version, observation timestamp/cursor/generation, finite feature and Q values, action enum/range, next-tick eligibility, and replay/paper mode before accepting a proposal.
- Log the model ID/hash, feature vector (or deterministic digest), Q-values, threshold, proposal, host acceptance/rejection, and resulting transaction ID for replayability.
- Fail closed on malformed JSON, missing/stale features, inference exception/timeout, non-finite values, or cursor mismatch. Preserve rollback in the existing broker transaction.
- Treat the JSON as untrusted input unless its provenance and digest are verified; apply byte-count, nesting, dimensions, and numeric-magnitude limits before allocating arrays or running inference.

These controls are independent of whether the arithmetic uses pure Python, NumPy, ONNX Runtime Web, or a server.

### 4. Training in Pyodide

**Technically possible in a narrow sense, not operationally justified.** Pyodide 0.27.6 includes NumPy 2.0.2 and DuckDB 1.1.2 in its package catalog, so a single-threaded port may be possible after adding and loading assets. Against doing so:

- The current runtime does not load these packages, so training is not available today.
- Pyodide cannot start Python threads or processes, constraining data loading and optimization ([constraints](https://pyodide.org/en/0.27.6/usage/wasm-constraints.html)).
- Training is longer-lived and stateful; browser Worker lifecycle, tab closure, memory pressure, and persistence complicate reliable checkpoints. Pyodide's in-browser filesystem requires explicit persistence/synchronization rather than behaving like a native durable filesystem ([file access](https://pyodide.org/en/0.27.6/usage/accessing-files.html)).
- Running training in the replay Worker risks responsiveness and makes deterministic replay harder to reason about. Train and select the genome offline; ship a small, immutable, hashed inference artifact.

### 5. Converted browser or server alternatives

**ONNX Runtime Web — viable but unnecessary for this artifact**

- Official ONNX Runtime documentation supports browser inference through `onnxruntime-web`, with WASM CPU and optional WebGPU/WebGL/WebNN providers. WASM has the broadest operator coverage; GPU providers support subsets ([ONNX Runtime Web overview](https://onnxruntime.ai/docs/tutorials/web/)).
- WASM multi-threading requires browser WebAssembly threading plus cross-origin isolation; a proxy Worker has CSP and WebGPU limitations ([ORT Web environment flags](https://onnxruntime.ai/docs/tutorials/web/env-flags-and-session-options.html)). None of this is needed for the tiny MLP.
- Conversion would require producing an ONNX graph from custom JSON and proving numerical/action parity. It adds an NPM runtime, WASM assets, operator/version compatibility, and model lifecycle without removing existing Python preprocessing. Use it only if a future model becomes large/standardized enough to justify a dedicated inference runtime.
- Large-model browser constraints are real (about 2 GiB ArrayBuffer/protobuf limits and 4 GiB WebAssembly memory), though irrelevant to the current ~12 KiB artifact ([ORT Web large models](https://onnxruntime.ai/docs/tutorials/web/large-models.html)).

**Version-pinned native server — viable fallback, not preferred now**

- Server inference can preserve `_moomoo`'s Python/NumPy versions and is appropriate when a model is too large, hardware-sensitive, or should not be downloaded to clients; ONNX Runtime's official guide explicitly distinguishes browser and server targets ([ORT Web overview](https://onnxruntime.ai/docs/tutorials/web/)).
- Against: network latency/failure, service operations, authentication, privacy exposure of observations, and a new nondeterministic dependency during replay.
- If used later, the endpoint must accept a versioned observation and return only a versioned proposal with model hash and Q-values. It must have no brokerage credentials or order-placement capability. Pin dependencies, authenticate, use TLS, set strict request limits/timeouts, and retain an auditable request/response record.

## Performance, security, serialization, maintenance, and licensing gotchas

### Performance

- Prefer the standard-library loop to avoid loading a much larger scientific runtime for a few hundred multiply-adds.
- Benchmark Worker initialization separately from warmed inference. Record median and tail latency over replay-sized batches and verify cancellation/timeout behavior.
- Any optimization is secondary to action parity at the 0.1 boundary.

### Security and serialization

- Plain JSON is materially safer than pickle, but it is not inherently trusted: validate provenance, digest, schema, size, finite values, and dimensions.
- Do not dynamically install dependencies or fetch a model from an arbitrary URL at runtime. Pin and self-host approved artifacts with hashes. Pyodide notes that arbitrary URL/CORS proxy installs can lack integrity checking ([package loading](https://pyodide.org/en/0.27.6/usage/loading-packages.html)).
- Pyodide's browser sandbox does not make arbitrary Python safe; Python has access to browser Web APIs. The model should be data only, and the inference module should expose no order or secret-bearing capability ([Pyodide overview](https://pyodide.org/en/0.27.6/)).
- If future artifacts use PyTorch, `torch.load()` uses an unpickler and explicitly warns never to load untrusted data ([PyTorch `torch.load`](https://docs.pytorch.org/docs/stable/generated/torch.load.html)).
- ONNX also requires provenance checks, isolation, and resource limits for untrusted models ([ONNX security policy](https://github.com/onnx/onnx/blob/main/SECURITY.md)).

### Maintenance

- Pure Python has the smallest dependency surface but creates a second implementation of the arithmetic; prevent drift with shared golden fixtures generated from the canonical native implementation.
- NumPy in Pyodide couples inference to a Pyodide-specific Emscripten wheel and package version. Upgrading Pyodide requires rerunning parity and browser tests.
- ONNX adds exporter/opset/runtime compatibility. Server inference adds API and deployment versioning. Neither is justified for the current model unless requirements change.

### Licensing

- Pyodide is MPL-2.0 (weak copyleft); NumPy is BSD-licensed, though distributed wheels may include third-party components with additional notices ([Pyodide licensing](https://pyodide.org/en/0.27.4/development/contributing.html), [NumPy packaging license metadata](https://github.com/numpy/numpy/blob/main/pyproject.toml)).
- ONNX Runtime is MIT-licensed but carries third-party notices ([ONNX Runtime setup metadata](https://github.com/microsoft/onnxruntime/blob/main/setup.py), [third-party notices](https://github.com/microsoft/onnxruntime/blob/main/ThirdPartyNotices.txt)).
- No runtime license found here grants rights to the model weights, training data, or `_moomoo` source. Confirm their provenance and redistribution rights separately before shipping the artifact to browsers.

## Decision matrix

| Route | Feasibility | Benefits | Main costs/risks | Recommendation |
|---|---|---|---|---|
| Pure-stdlib port of MLP in existing Pyodide Worker | High | No new runtime asset; offline; tiny; fits current policy | Not as-is reuse; second arithmetic implementation; rounding parity | **Preferred**, after golden parity and broker-boundary validation |
| Pyodide 0.27.6 + NumPy 2.0.2 | High | Closest source shape to native NumPy | Must vendor/load wheel; 2.0.2 vs 2.4.5; startup/supply-chain cost | Fallback if pure-Python parity fails |
| ONNX Runtime Web | High in principle | Purpose-built browser inference; CPU/GPU options | Conversion and new runtime are disproportionate | Defer until model complexity grows |
| Version-pinned native server | High | Exact native dependency closure; centralized model | Network/ops/privacy/availability | Fallback for future larger/native-only models |
| Training in browser | Possible but weak | Local/offline experimentation | assets, lifecycle, persistence, blocking, no Python threads/processes | Do not pursue for production workflow |

## Recommended acceptance experiment

1. Freeze a model manifest: SHA-256, JSON schema/version, feature contract, 7→16→16→2 dimensions, threshold, and canonical native dependency versions.
2. Generate a native NumPy 2.4.5 golden corpus: normal replay observations, extrema after clipping, zero/position transitions, NaN/Inf rejection cases, and many cases around `Q1-Q0=0.1`.
3. Run the standard-library implementation in the pinned Pyodide 0.27.6 Worker. Require all Q-values within a predeclared tolerance and 100% action agreement. Do not tune tolerance after seeing failures.
4. Measure cold initialization and warmed per-observation/batch latency and memory on supported browsers.
5. Pass proposals through replay/paper host validation and intentionally test stale cursor/generation/model ID, malformed JSON, non-finite output, timeout, and rollback.
6. If parity fails only because of arithmetic differences, repeat with the pinned Pyodide NumPy 2.0.2 wheel. If action parity still fails, stop and retain native/server inference rather than weakening the gate.

## Source-quality assessment

| Source | Quality | Why |
|---|---|---|
| Versioned Pyodide 0.27.6 docs | High | Official, exact runtime version used by backcast |
| Current Pyodide/micropip docs | High for package mechanics; medium for 0.27.6 specifics | Official, but current behavior can differ from pinned runtime |
| ONNX Runtime official docs | High | Primary runtime documentation |
| PyTorch official API docs | High | Primary serialization warning; relevant only to future artifacts |
| ONNX project security policy | High | Primary project threat-model guidance |
| Official GitHub license/packaging files | High | Canonical license and third-party metadata |
| Local facts from feasibility analyst | Medium until caller verifies | Decision-relevant repository inspection, not independently checked by this researcher |

## Bottom line

The original question's assumed “RL framework in Pyodide” problem is much smaller than expected: the artifact is data for a tiny custom MLP. **As-is `_moomoo` code is NO-GO under the current stdlib-only Worker, while reuse through a pure-Python inference adapter is a conditional GO.** The conditions are a strict native-vs-Pyodide parity suite and preservation of the replay/paper transaction boundary. NumPy loading, ONNX conversion, and server inference are viable escalation paths, not the first implementation.

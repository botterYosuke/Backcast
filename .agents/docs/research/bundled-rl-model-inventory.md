# Bundled RL Model Inventory

Date: 2026-09-28. Inspection scope: read-only `/Users/sasac/_moomoo`, with backcast dependency comparison. No source assets were copied during this inventory.

## Summary

Use the current selected NumPy neural-network checkpoint, `models/rl_scalp_evolution_v1.json`. The source repository's latest commit and README identify this checkpoint as the selected model for the September 28 paper runner. The older `rl_scalp_full_v3_q.json` is a separate tabular model, not the latest NN. There is no PyTorch, Stable Baselines, pickle, ONNX, or separate normalization checkpoint on the selected inference path.

The smallest runtime copy set is one 11,932-byte JSON checkpoint, plus a new backcast-owned inference adapter implementing the documented numerical operations. Add a small provenance manifest and retain relevant source excerpts or source snapshots for audit. Do not import the original paper client: that unnecessarily imports training, external market data, broker probing, and validation-report loaders.

Source repository HEAD: `2166235559e31357d1cf8e84b7c6305ece06667e`. Relevant model/source paths were clean at inspection. No root license or source-level redistribution grant was found. The user's explicit instruction authorizes this local repository copy; the inspection does not establish a public redistribution license.

## Required Copy Set

All source paths below are relative to `/Users/sasac/_moomoo`.

| Path | Bytes | SHA-256 | Role |
| --- | ---: | --- | --- |
| `models/rl_scalp_evolution_v1.json` | 11,932 | `528b9dc44928984c471731bb64ef82fa4a299a2ffd5707d1ce2a482223d28ef1` | Required unchanged checkpoint, including final newline |
| `src/moomoo/rl_scalp_evolution.py` | 24,274 | `3219e76904eced142eec05a66116598ae730b81f5781e70b2610248d30f3ffb3` | Audit reference; extract Genome validation, causal_features, encode, MLP inference, policy_for semantics |
| `src/moomoo/rl_scalp_full_benchmark.py` | 37,760 | `0349e150b38a71ca206e3011dc6ee0c367aebc7c03cba1b5941beea2e8d41ad8` | Audit reference for session timing/mark selection; no runtime import needed |
| `src/moomoo/rl_scalp_sample.py` | 28,610 | `04d8376d926201ea1c8654da64fc3aa2b63d5754eaba744e9707af8e9999af8a` | Audit reference for sign and original position/target semantics; no runtime import needed |

The checkpoint's embedded `source_sha256` exactly matches all three source files above. Its research-report model digest strips trailing CR/LF and therefore differs from the unchanged-file digest:

- File digest: `528b9dc44928984c471731bb64ef82fa4a299a2ffd5707d1ce2a482223d28ef1`.
- Research model digest: `304a0293e04300835b30c7bef864149ce4d944f8696562b968e21e39805536b0`.
- Validation spec hash: `b19a989e9c5d94f6094494495c44d0c4a8ea5616eff22d975895968e385daa15`.
- Validation status: `VALIDATION_RESEARCH_ONLY`; evidence tier: `OBSERVATION`.
- Training episode digest: `84290caff138a658f763923d85a2f0bd513779f3d056b3e897d94af1f4a2d76b`.
- Universe digest: `c24deb0fdefc6f221af15dacf079455a9c69f9440015a6c5c8f5a8fc4ef90a2a`.

Portable target layout proposal:

```text
src/tickreplay/rl_strategy.py                 # Pure inference and causal streaming adapter
src/tickreplay/models/rl_scalp_evolution_v1.json
src/tickreplay/models/rl_scalp_evolution_v1.provenance.json
docs/rl-strategy.md                          # Input/target semantics and differences from source execution
tests/test_tickreplay_rl_strategy.py         # Source parity, causality, target/position coverage
```

Resolve the asset relative to the installed module (`Path(__file__)` or package resources), never the current directory or sibling `_moomoo`. Include checkpoint bytes, adapter bytes, and NumPy version in runtime identity. If original files are copied for provenance, keep them outside the import path and record that they are audit snapshots rather than supported training entry points.

### Selected inference contract

- Genome: `input_mode=raw`, `window=3`, `minute=false`, `day=false`, `clock=true`, `width=16`, `depth=2`, `action_margin_bps=1.0`.
- Actual input order: tick sign, normalized lag-1 return, lag-2 return, lag-3 return, clock sine, clock cosine, binary position. Input shape is `(N, 7)`.
- Return before normalization: `10000 * (current_decision_mark / prior_decision_mark - 1)`. A missing lag at session start produces zero. Normalize by dividing by 30 and clipping to `[-2, 2]`, using source float32 staging.
- Clock is JST wall-clock seconds of day, angle `2*pi*seconds/86400`. Convert epoch timestamps to JST; host timezone must not change output.
- Tick sign uses the latest print at or before the decision timestamp versus the latest print at or before decision minus 10 seconds. This differs from simply comparing adjacent ticks or first-slot decision marks.
- Network layers: `(7,16)`, `(16,16)`, `(16,2)` with corresponding biases; hidden activation `tanh`, output linear; source loader casts all checkpoint weights/biases to float32.
- Action is `1` only when `Q[1] > Q[0] + 0.1`; ties select `0`. The divisor 10 comes from reward scaling, not a generic basis-point conversion.
- Targets mean flat or long 100 shares. Position input means actual flat/long state, not merely last proposed target. A precomputed trace needs explicit treatment of both possible positions or a host-authoritative target-resolution mechanism; updating a pretend position on proposal would diverge when an order is rejected or remains unfilled.
- Source decision slots are every 10 seconds, 09:01:00–11:24:30 and 12:31:00–14:49:30 JST. Morning and afternoon each reset the decision-return history. Source marks are the latest causal continuous-session prints at or before the slot.
- Selected checkpoint excludes minute and day features. Historical training still validated those datasets, but they are not numerical inputs for this checkpoint and no prior-close database is needed for inference. Reject incompatible future genomes unless their inputs are implemented.
- The source execution model requires a print of at least 100 shares at least one second after the decision and no later than the next decision; source session exits have their own later-print handling. Backcast's next-tick broker has different execution semantics. Preserve policy numerical semantics and describe the execution difference explicitly; do not claim reproduced research P&L.
- Original `capital_yen=1000000`, `qty=100`, `side_cost_bps=2`, `latency_seconds=1`, `step_seconds=10` are metadata about the research simulator. Backcast remains responsible for actual replay cash/fills.

## Dependencies

| Dependency | Source version | Backcast current | Required action |
| --- | --- | --- | --- |
| Python | Original project `>=3.13,<3.14`; inference uses standard Python + NumPy | `>=3.11` | Installed NumPy 2.4.5 metadata requires `>=3.11`, compatible with backcast. Smoke ran on Python 3.13.11. Do not copy the entire source environment. |
| NumPy | `2.4.5` exact in project and standalone research commands | Not currently declared | Add direct dependency/pin compatible with supported Python; parity-check actual chosen version. Exact 2.4.5 inference was smoke-tested successfully with backcast's current interpreter. |
| DuckDB | Research reproduction pins `1.5.5` | `>=1.5.5` | Already present for replay; inference adapter itself needs none. |
| FastAPI / Uvicorn / HTTPX | Not part of NN numerics | Already present | Existing native trace transport only. |
| Torch / SB3 / pandas / scipy / sklearn / lightgbm / nautilus / wandb / websocket broker clients | Some unrelated packages appear in source project | Not relevant | Do not copy or add. |

No fitted scaler, imputer, vocabulary, optimizer, or random generator is required for inference. The original `MLP.from_document` creates optimizer buffers through its constructor, then replaces weights; a minimal loader can omit those training-only arrays while preserving loaded dtype and forward arithmetic.

## Smoke Test

This read-only source smoke was executed successfully from backcast (Python `-B` prevents bytecode writes to `_moomoo`):

```sh
uv run --with numpy==2.4.5 python -B -c 'import sys,json,pathlib,numpy as np; sys.path.insert(0,"/Users/sasac/_moomoo/src"); from moomoo.rl_scalp_evolution import Genome,MLP,encode; doc=json.loads(pathlib.Path("/Users/sasac/_moomoo/models/rl_scalp_evolution_v1.json").read_text()); genome=Genome(**doc["genome"]); model=MLP.from_document(doc["network"]); base=np.zeros((1,11),dtype=np.float32); base[0,10]=1.0; values=[model.forward(encode(base,p,genome))[0].tolist() for p in (0,1)]; print(json.dumps({"numpy":np.__version__,"encoded_shape":list(encode(base,0,genome).shape),"q_by_position":values,"target_by_position":[int(q[1]>q[0]+genome.action_margin_bps/10.0) for q in values]}))'
```

Observed output:

```json
{"numpy":"2.4.5","encoded_shape":[1,7],"q_by_position":[[-0.11576415598392487,-0.13215237855911255],[-0.24025781452655792,0.10134830325841904]],"target_by_position":[0,1]}
```

The implemented adapter should reproduce these Q values within float32 numerical tolerance and exact target decisions; add a broader deterministic grid of both position states, signs, return clipping boundaries, and session clocks. Final portability proof must run the resulting app/tests from backcast without adding `_moomoo` to `sys.path` and without its venv/data roots. The command above establishes a source oracle, not final portability.

## Licensing/Security

- No top-level LICENSE/NOTICE/COPYING or explicit source-file license was found. License files found were confined to unrelated `.agents/skills/skill-creator` and kabusapi samples. Do not copy those licenses and pretend they apply to this model.
- Preserve checkpoint metadata and source SHA values; add a provenance notice describing the user-authorized local transfer. Public distribution rights remain unestablished by this inspection.
- The checkpoint is plain JSON with network arrays, model configuration, training identifiers, public symbol codes, and aggregate metrics. An inspection of its string values found no `/Users/` absolute path, Windows `C:` path, password, or token strings. No executable serialization/pickle is needed.
- Do not copy `.env`, credentials, broker clients, raw logs, full market data, user home paths, source `.venv`, or machine config. Source paper/training entry points read external roots; the extracted adapter must not.
- Model training dates and selection history must remain provenance rather than be promoted to proof of performance on the selected replay session. Replaying training/validation dates is permitted engineering behavior but is not fresh out-of-sample evidence.
- Load only the bundled, hash-verified JSON; validate spec/genome, shapes, finite values, and known target semantics before inference. Fail visibly on mismatch, rather than falling back silently to a different model.

## Exclusions

| Exclusion | Reason |
| --- | --- |
| `docs/analysis/rl_scalp_evolution_v1_validation.json` (6,154,621 bytes, SHA-256 `635ccffa9c51b58d5f43c53bc0d2f8a62cec1651d3e811705174efdb550899b1`) | Original paper loader cross-checks this report, but frozen inference only needs checkpoint integrity and a small extracted provenance manifest. Keep its hash/status/spec digest, not 6 MB of research aggregates. |
| `models/rl_scalp_full_v3_q.json` (4,858 bytes, SHA-256 `6ad5815e7d6a62c38d3da54ad495315ba9ae8122bc5ff8453cd41c763e6322dc`) | Older tabular model; bundling it as default would select a different policy. |
| `models/rl_scalp_evolution_v1_pre_lossfix.json` | Historical precursor; current model corrects reported loss metadata. |
| `models/rl_scalp_evolution_smoke.json` | Smoke/training artifact, not chosen model. |
| `src/moomoo/kabu_rl_paper.py`, `kabu_paper_probe.py` | Broker paper collection/execution dependencies and machine-specific roots are unnecessary for replay inference. |
| Training entry points, full benchmark implementation as imports, optimizers, `scripts/export_*`, freeze scripts | Training, selection, and historical report verification are outside inference. Preserve hashes or audit snapshots instead. |
| `Silver/`, `raw/`, DuckDB stores, universe CSV/manifest | Needed for original research reproduction, not selected network inference on backcast-supplied ticks. Do not copy potentially licensed market-data collections. |
| Entire source pyproject/uv.lock/.venv | Would import large unrelated dependencies and Python restrictions. Declare only inference requirements in backcast. |

## Artifact Path

`.agents/docs/research/bundled-rl-model-inventory.md`

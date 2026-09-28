## Spike Brief: Pyodide RL auto trading

### Question
Can the RL model under `/Users/sasac/_moomoo` be reused for automated replay/paper trading in backcast, and can its inference run directly in the existing Pyodide Worker?

### Parameters
- Time budget: 30 minutes
- Investigation mode: RESEARCH-ONLY
- Started: 2026-09-28 Asia/Tokyo
- Deadline: 30 minutes after investigation start

### Success Criteria
- GO if: the model artifact, preprocessing state, observation/action contract, and runtime dependencies are identifiable; inference is compatible with the current browser runtime or has a concrete conversion/server alternative; and outputs can enter the existing validated transactional replay/paper broker without direct portfolio mutation.
- NO-GO if: the artifact or required preprocessing cannot be reconstructed, or every feasible inference route violates the replay correctness/security boundary.
- INCONCLUSIVE if: compatibility looks plausible but requires a fixed-input load/forward-pass benchmark or missing external artifact to decide.

### Sub-questions (from Codex decomposition)
1. What is the `_moomoo` model artifact and complete inference runtime closure?: Evidence needed: matching artifact/load code, exact framework versions, custom classes, feature transforms, normalization state, observation/action schema, and recurrent state.
2. Can that runtime load and execute a forward pass in the current backcast Pyodide Worker?: Evidence needed: Pyodide/Emscripten package availability, serialization compatibility, absence of mandatory unsupported native/GPU/process/thread dependencies, and plausible memory/latency bounds.
3. Can the observation/action contract connect safely and deterministically to replay/paper trading?: Evidence needed: point-in-time features, typed action proposals, next-tick eligibility, generation/cursor/model identity checks, and host-side validation plus rollback.
4. Is training in Pyodide technically and operationally justified?: Evidence needed: WebAssembly support for the training stack, bounded CPU/memory, checkpoint/restart behavior, and no conflict with Worker responsiveness and lifecycle guarantees.
5. If direct Pyodide inference is unsuitable, is converted browser inference or version-pinned server inference viable?: Evidence needed: conversion compatibility/output parity, or an inference-only API that returns proposals rather than placing orders and preserves replay auditability.

### Critical Path
Identify the model artifact and runtime closure first. An unsupported native ML runtime immediately rules out running the original artifact directly in Pyodide, while leaving model conversion or server-side inference open.

### Investigation Plan
- Researcher (Opus): verify current official Pyodide/package/runtime constraints and credible browser inference alternatives, with direct source links.
- Feasibility Analyst (Codex): inspect `/Users/sasac/_moomoo` and backcast architecture, map model dependencies and schemas to the Worker/broker boundary, and assess direct, converted, and server inference options.
- Prototype (if applicable): None in this RESEARCH-ONLY spike; if only runtime performance remains unknown, recommend a separate fixed-input prototype.

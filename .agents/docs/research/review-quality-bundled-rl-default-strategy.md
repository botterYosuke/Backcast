# Quality Review: Bundled RL Default Strategy

## TL;DR

The implemented Bundled RL path is correct on its primary data path: model bytes and float32 inference match the upstream oracle, return features are cumulative current-versus-lag values, decision rows are causal, and browser replay selects the branch from the broker-committed position before staging an order for exactly the next tick. The four previously reported Medium lifecycle findings are resolved with targeted regressions, and I found no remaining High- or Medium-severity code-quality blocker.

Ship verdict: **PASS**. Severity counts after re-review: High 0, Medium 0, Low 1.

## Ship Verdict

- **No quality blocker.** The feature may ship, subject to separately owned review findings.
- All four prior Medium failure-path/lifecycle findings are resolved and covered by targeted regressions.
- No hard-coded fake result, submitted-source Native path, weakened Custom Python coverage, or retained-release mutation was found.

## Findings

### [RESOLVED — Medium] Native self-check timeout aborted the entire application lifespan

- File: `src/tickreplay/server.py:325`
- Resolution: `_build_native_manager()` now catches `TimeoutError` alongside `BundledRlRuntimeError` and degrades only the Bundled RL capability to unavailable. The regression proves ordinary `/api/status` remains available after a forced self-check timeout.

### [RESOLVED — Medium] A child that closed its pipes but did not exit was not terminated

- File: `src/tickreplay/bundled_rl_runtime.py:275`
- Resolution: an EOF-followed-by-wait timeout now terminates/reaps the child and raises the classified `BundledRlRuntimeError`. The fake-process regression confirms termination.

### [RESOLVED — Medium] Non-invalidation Load failures left a known active server job running

- File: `src/tickreplay/static/strategy/strategy-trace-controller.mjs:146`
- Resolution: a failed Load now best-effort cancels only its attempt-local known job, while invalidated attempts leave cancellation to the supersession path. Cancellation is deliberately not awaited, so a stalled/failing DELETE cannot delay or mask the original Load error. Regressions cover transport/protocol/hash/identity failures, cancellation failure/stall, and stale/new attempt races.

### [RESOLVED — Medium] Artifact publication held the manager lock across filesystem I/O

- File: `src/tickreplay/bundled_rl_service.py:274`
- Resolution: prepare/commit I/O now runs outside the manager lock. Exact snapshot and deadline checks occur before commit and again before ready publication, preventing cancellation or timeout races from publishing ready state. Prepare/commit failures are classified as `artifact_store_failed`, and regressions cover responsive cancellation, post-deadline completion, failure classification, and temporary-file cleanup.

### [Low] Terminal job metadata grows without a retention bound

- File: `src/tickreplay/bundled_rl_service.py:109`
- Current behavior: every terminal `JobSnapshot` remains in `_jobs` for the process lifetime. Failed/cancelled inputs are removed from `_by_input`, but their job records are never evicted; successful records are also retained indefinitely for deduplication.
- Impact: a long-running local server receiving many distinct sessions or repeated failed jobs accumulates in-memory metadata without a cap.
- Suggested improvement: add a bounded terminal-job retention policy while preserving active jobs and current input-to-ready deduplication.

## Invariant Audit

1. **PASS — model semantics.** `bundled_rl_inference.py` implements the upstream float32 MLP, `m0/m1`, `m0/m2`, `m0/m3` cumulative returns, 10-second slots, lunch reset, same-time barriers, latest-slot gap collapse, and final-tick omission. The copied model is exactly 11,932 bytes with SHA-256 `528b9dc...28ef1`.
2. **PASS — next-tick and broker authority.** `replay-commit.mjs` commits the current tick/fill first, forms the event from the candidate committed portfolio, then `dispatchOneEvent()` selects the 0/100 branch and stages eligibility at observed+1. Production broker/committer files were not modified.
3. **PASS — source-free exact preset.** Request parsing rejects oversize, invalid UTF-8/JSON, duplicate keys, wrong/excess top-level fields, and wrong preset before repository/manager access. Active Native routes contain no source submission.
4. **PASS — deterministic identity/publication.** Input identity frames contract, exact preset, proposal contract, runtime fingerprint, canonical session, and session digest. Trace publication is content-addressed and collision checked.
5. **PASS — lifecycle.** State revisions are monotonic; identical same-revision polls are accepted and changed same-revision polls rejected. Explicit cancellation/timeout has late-result guards. Abandoned-job, pipe-exit, self-check-timeout, and commit-lock failure paths now have targeted regression coverage.
6. **PASS — runtime boundary.** Child argv is fixed, the worker recomputes the runtime identity, exact NumPy 2.4.5 and model/provenance identities are checked, and only `bundled_rl_inference.py` imports NumPy.
7. **PASS — browser lifecycle.** Mount probes capability only; Load does not start replay or dispatch; Start checks cursor, actual position, and complete empty open orders. Generation checks reject stale results, and broker/protocol failure terminates the run.
8. **PASS — product selection.** Missing, obsolete, and malformed selection records resolve to Bundled RL. Custom Python remains explicit and its editor/source/Pyodide tests remain present.
9. **PASS — release closure.** `npm run check:strategy-release` reconstructed release `fd77a665...17b5`; current authored inputs match release copies and all retained release identities verified.
10. **PASS — implementation/test integrity.** No placeholder implementation, swallowed execution error used to fake success, or weakened assertion was found in the feature scope. The four deliberately deleted files are the obsolete custom-Native implementation/tests authorized by the breaking plan.

## Codex Consultation

- Prompt: `.agents/logs/codex/prompt-quality-review.md`
- Wrapper artifact: `.agents/logs/codex/20260928T062009Z-quality-review.md`
- Status: **failed / no usable response**. The read-only wrapper remained silent and the response file stayed empty; it was interrupted so the review could finish. No Codex conclusion is used as evidence.
- Re-review status: not retried, per the lead's explicit instruction; the original failed status remains unchanged.

## Tests and Evidence Reviewed

- Independently run: 34 focused Python tests passed (one pre-existing Starlette/httpx deprecation warning).
- Independently run: 38 selection/protocol/controller/workspace Node tests passed.
- Independently run: release reconstruction and strategy asset tests passed (8 tests; release ID `fd77a665...17b5`).
- Upstream comparison: `_moomoo/src/moomoo/rl_scalp_evolution.py` feature encoding and the exact source checkpoint bytes.
- Lead-provided broader evidence: full gate 1,347 passed; focused Python 169 passed; Node 107 passed; app Chromium 6 passed/1 skipped; real backend browser 1 passed.
- Re-review independently run: 6 targeted Python regression cases passed (one pre-existing Starlette/httpx deprecation warning), and the controller test file passed 20/20.
- Re-review lead-provided broader evidence: backend 192 passed; Node 119 passed.

## Residual Risks

- Public redistribution rights for the copied model remain explicitly unconfirmed and outside this code-quality verdict.
- Browser proof was run on Chromium only; Firefox/WebKit are not part of the reported gate.
- This report does not supersede independent security or test-coverage review findings.

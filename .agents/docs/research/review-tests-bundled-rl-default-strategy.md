# Test Review: Bundled RL Default Strategy

## Re-review TL;DR — 2026-09-28

**Final verdict: PASS / ship from the test-review perspective.** The final
narrow re-review confirms that the uniquely titled real-browser test now wraps
the existing production host/controller port, captures typed Start inputs
before and after Load, compares cursor, last committed tick, portfolio, and the
complete open-order snapshot, and asserts zero Load-time dispatch. The test
passes against the real fixed Python backend.

Remaining gap count: **High 0, Medium 0, Low 0**. All initial findings are
resolved. Coverage remains **not measured**; no percentage is estimated.

Final release reviewed:
`51a39c13a038b423cffdab52cc4185bb1614a0ff6b5b44152c442cf7853b61e5`.

## Initial Review TL;DR — superseded

**Verdict: BLOCK shipment pending three High-priority regression fixes.** The
model oracle, causal scheduler, immutable trace, default/reset selection,
actual-position branch selection, next-tick broker semantics, cancellation,
cache reuse, Custom Python preservation, and release identity all have useful
tests. The required browser gate does not select a test as written, the Native
request rejection matrix is not proven before side effects, and no integrated
regression proves broker rejection terminally stops Bundled RL without retry.

Gap count: **High 3, Medium 1, Low 1**. Coverage is **not measured**; no
percentage is estimated from test counts.

## Scope and Evidence

Reviewed the approved plan, ownership manifest, feature-owned implementation
and tests, frozen contract fixture, and final immutable release
`fd77a665088401277ccbc947976acc3ac650b9debe9b1e3493bf08bd37f417b5`.
Pre-existing dirty work and retained release implementations were excluded.

The following acceptance surfaces are directly covered:

- Exact model bytes/version/shapes/finite validation and upstream float32 Q/target oracle.
- Cumulative lag returns, lunch reset, slot boundaries, gaps, same-time prints,
  timezone independence, and final-tick omission.
- Target-only immutable traces, ordered 0/100 branches, trace tamper detection,
  and content-addressed concurrent publication.
- Fixed child handshake and a real child capability/job/poll/trace round trip
  from an unrelated working directory without `_moomoo`.
- Default/reset selection, source-free Bundled RL Load, explicit Custom Python
  editor/Pyodide selection, same-revision polling rejection, actual-position
  branch selection, and no replay-time Native HTTP.
- Broker next-tick eligibility and atomic rollback primitives, service
  deduplication, job-scoped cancellation with late-result suppression, and
  immutable release reconstruction/identity.
- Existing Custom Python unit and browser scenarios remain present; the
  feature Node gate exercised the workspace/source-store behavior, while the
  full Custom Python browser matrix was inspected but not rerun in this review.

## Acceptance-Criteria Mapping

| Plan gate | Evidence | Review status |
|---|---|---|
| 1. Remove obsolete custom Native paths | Residue/snapshot gates are owned by integration; current feature tests only exercise the new contract | Integration evidence required, no new test gap |
| 2. Exact preset and all malformed requests reject before side effects | Happy path and top-level `source` rejection exist | **High gap T2** |
| 3. Exact model/runtime/oracle and no `_moomoo` dependency | `test_tickreplay_bundled_rl_inference.py`, runtime/process tests | Covered |
| 4. Bundled RL default/reset; Custom Python explicit; mount inert | selection/workspace tests, typed real-browser Load probe, and application browser tests | Covered; final T1 remediation proves complete Load-inert state |
| 5. Proposal-only trace, actual-position branch, numeric/open-order safety | Python/Node protocol and controller tests | Covered |
| 6. Real browser entry/exit exactly at `i+1`, not `i`/`i+2` | Unique-title browser gate passes and now includes the typed Load-inert probe | Covered; T1 resolved |
| 7. Broker rejection rolls back, terminally stops, and never retries | Broker/committer rollback primitive is covered | **High gap T3** for integrated terminal/no-retry behavior |
| 8. Real fixed child and zero Native HTTP during replay | process test and real browser request count | Covered; malformed-source matrix remains T2 |
| 9. Deterministic exclusive release and retained integrity | release builder check and release tests; snapshot gate is integration-owned | Covered by available test evidence |
| 10. Preserve protected/unrelated bytes | snapshot verifier is integration-owned | No feature-test gap |
| 11. Provenance/local-copy basis | frozen asset/provenance test | Covered |

## Findings

### T1 — High, RESOLVED: the real-browser gate proves the full Load-inert snapshot

- **Files/functions:** `.agents/docs/plans/bundled-rl-default-strategy.md:256,271`;
  `tests/browser/bundled-rl-real.mjs:3-53`.
- **Resolution evidence:** A test-only initialization probe wraps the existing
  `tickReplayStrategyHost` and attached production controller without changing
  product or release code. It invokes the real host Start boundary in a typed,
  intentionally aborted capture mode immediately before and after Load. The
  test compares `cursor`, `lastCommitted`, `portfolio`, `openOrders`,
  `openOrdersTotal`, and `openOrdersTruncated`; additionally it asserts an empty,
  complete open-order snapshot and zero dispatches during Load. Existing DOM
  tape/clock/position/fill assertions remain, as do entry/exit next-tick and
  post-trace zero-Native-HTTP assertions.
- **Execution evidence:** The corrected unique-title Chromium gate independently
  selected one test and passed in 37.3 seconds. Root separately reports one pass
  in 40.2 seconds after an artifact-race retry.
- **Status:** RESOLVED; the approved line-271 browser evidence is now present.

### T2 — High, RESOLVED: malformed Native API contracts reject before every side effect

- **Files/functions:** `tests/test_tickreplay_server.py:1068-1134`, covering
  `server._read_native_json()` and `server._native_job_request()`.
- **Resolution evidence:** `tests/test_tickreplay_server.py` now sends 14 raw
  malformed request classes covering missing/wrong preset, `source`,
  `sourceHash`, unknown top-level/session/preset fields, duplicate JSON key,
  invalid UTF-8, non-finite/truncated JSON, and invalid date/stem/hash. It spies
  repository, cache tracker, job manager/allocation, child process, and artifact
  preparation, snapshots the cache tree, and asserts zero side effects. A
  separate underdeclared streamed-overflow case returns 413 with zero backend
  calls/writes. The happy path reorders preset keys and succeeds.
- **Status:** RESOLVED; acceptance gate 2 is executable at the HTTP boundary.

### T3 — High, RESOLVED: integrated broker rejection proves rollback, terminal stop, and no retry

- **Files/functions:** `src/tickreplay/static/replay-commit.test.mjs:207-230`,
  `tests/browser/bundled-rl-real.mjs`, and the application strategy harness.
- **Resolution evidence:** The new replay-commit test uses the real
  `BundledRlTraceController`, trace artifact, broker, committer, and extracted
  application error path. It injects rejection at the eligible broker stage,
  proves serialized broker rollback precedes cleanup, cursor is not consumed,
  staged state is cleared, cleanup/cancel/pause occur once, later animation
  frames and explicit resume/commit attempts cannot progress, and broker/
  strategy dispatch plus rejection counters remain unchanged.
- **Status:** RESOLVED; acceptance gate 7 now has direct regression evidence.

### T4 — Medium, RESOLVED: bounded service failure states have direct lifecycle tests

- **Files/functions:** `tests/test_tickreplay_bundled_rl_service.py:68-106` and
  `tests/test_tickreplay_bundled_rl_runtime.py`.
- **Resolution evidence:** Added tests cover bounded queue admission without an
  extra child, terminal running timeout with timer removal and late-result
  suppression, cancellation while artifact commit is blocked, commit crossing
  its deadline, prepare/commit store failures with public errors and no temp
  files, self-check timeout degradation, and termination of a child that closes
  pipes without exiting. Active/queued shutdown is exercised by the bounded
  queue case.
- **Status:** RESOLVED for the prior Medium finding.

### T5 — Low, RESOLVED: preset JSON key-order independence is frozen

- **Files/functions:** `src/tickreplay/static/strategy/strategy-trace-protocol.test.mjs:49-71`
  and `tests/test_tickreplay_server.py:1080-1113`.
- **Resolution evidence:** The real server happy path now submits the exact
  preset as `digest`, `revision`, `id` and completes job/trace successfully.
  Browser protocol fixtures also exercise reordered preset members in trace
  validation while preserving exact values and key set.
- **Status:** RESOLVED.

## Test Execution Results

### Re-review

- Focused nine-file Python gate: **192 passed, 0 failed, 1 deprecation warning,
  exit 0** in 14.04 seconds.
- Focused six-file Node gate: **119 passed, 0 failed, exit 0**.
- Corrected uniquely titled real-backend Playwright gate: **1 passed, 0 failed,
  exit 0** in 38.1 seconds.
- `npm run check:strategy-release`: **pass**, final release
  `51a39c13a038b423cffdab52cc4185bb1614a0ff6b5b44152c442cf7853b61e5`,
  14 files, 14 edges, two reconstruction roots.
- `npm run test:strategy-assets`: **8 passed, 0 failed, exit 0**.
- **Coverage: not measured.** No fresh repository-wide coverage artifact was
  produced; no percentage is inferred from pass counts.

### Final narrow re-review

- Real fixed-backend Chromium gate after the typed Load-inert probe was added:
  **1 passed, 0 failed, exit 0** in 37.3 seconds.
- The first two attempts did not execute a test because another concurrent
  reviewer temporarily owned port 8766; no process was killed. The clean retry
  selected and passed exactly one test.
- Root independently reports the same real-browser gate passing in 40.2 seconds
  and the final shared verifier at **1,370 passed**.
- No product or immutable release bytes changed for this final remediation.
- **Coverage: not measured.** This verdict makes no percentage claim.

### Initial review — superseded

- `uv run pytest -q --basetemp=/tmp/backcast-review-pytest-20260928 ...` over
  the nine planned Python files: **169 passed, 0 failed, 1 deprecation warning,
  exit 0** in 10.02 seconds.
- Planned six-file Node gate: **107 passed, 0 failed, exit 0**.
- Exact planned anchored Playwright command: **0 tests selected, exit 1**
  (`Error: No tests found`).
- Same Playwright config with `--grep 'Bundled RL real backend'`: **1 passed,
  0 failed, exit 0** in 39.2 seconds.
- `npm run check:strategy-release`: **pass**, release ID
  `fd77a665088401277ccbc947976acc3ac650b9debe9b1e3493bf08bd37f417b5`,
  14 files, 14 edges, two reconstruction roots.
- `npm run test:strategy-assets`: **8 passed, 0 failed, exit 0**.
- Main-agent evidence reports the broader suite at 1,347 passing tests; this
  reviewer did not rerun that nine-minute suite.
- **Coverage: not measured.** The review artifact had null/stale coverage, and
  no fresh percentage was produced. No percentage is inferred from pass counts.

## Ship Verdict

**PASS / SHIP.** T1-T5 are resolved, the final real-browser proof passes, and no
High, Medium, or Low test gap remains from this review. Coverage remains not
measured and should not be represented as a percentage, but it is not a new
blocker introduced by this feature review.

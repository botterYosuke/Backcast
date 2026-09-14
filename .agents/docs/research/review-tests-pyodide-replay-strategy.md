# Test Review: Browser Python Remediation (R6)

## Verdict

PASS after final corrective re-review for test coverage sufficiency of the authorized local remediation. No blocking coverage gap remains identified. The original defects and the later independently reproduced host-ready cleanup and notification-reentry Medium findings now have direct regressions. Final-release browser evidence is attributed to integration; the retained reviewer browser execution below predates the final corrective release. This is not a pinned-CI browser, performance qualification, deployment, or real-device acceptance claim.

Scope: `.agents/logs/review-diff-pyodide-replay-strategy.patch`, current canonical source and changed tests, the active plan's thirteen acceptance rows, original T1–T6 test review, and gate artifacts. Current release is `fde82a53f12e81c6fbac68d116c2afc3fd8e0254907dd7b6e93cb9750a63f14d`. The saved patch predates the last review fixes; current `git diff` and files were used for those additions. This reviewer previously implemented the lifecycle subset; this separate assessment uses assertions and fresh execution rather than treating that implementation report as proof. Separate security/quality review remains the cross-author check.

## Final Corrective Re-review

The initial coverage PASS missed two concrete lifecycle entry boundaries later found by the independent security/quality reviewers. It is superseded by this re-review, not evidence that those findings were absent. Both are now directly covered:

- `src/tickreplay/static/replay-commit.test.mjs:64` executes the actual extracted app Stop/invalidation and workspace dispose functions with the real canonical controller. Ready Stop and repeated disposal assert Worker termination exactly once, worker=null and idle without an active run. Tests at `:100`, `:115`, `:136` cover pending Load rejection, waiting for idle retirement, active-committer precedence and no-controller behavior. The retirement resource is intentionally injected; this is a focused host-routing unit test, not a real-browser claim.
- `src/tickreplay/static/strategy/strategy-controller.test.mjs:144` expands to four cases: loading notification before/after pending creation (`inFlight=false/true`) synchronously Stop or Load B. Each asserts zero stale posts, A settles invalidated without firing the inert deadline, B succeeds with its own hash when applicable, and final shutdown leaves every Worker terminated exactly once with no timer/listener retention. Tests at `:193`, `:213`, `:240`, `:267` additionally exercise Start/manual gate, ready/paused success, end/manual gate and malformed-response settlement reentry. Both original Stop and replacement-Load reproductions therefore have behavioral assertions, not merely test names.

Inspected Red evidence records host ready/dispose/idle-retirement failures before the one-condition host fix (3 failures) and the four stale-post notification failures before the attempt/Worker-owned request fix. Current narrow fresh execution passed **78 tests, zero failures/skips, observed exit 0** in 1181.39 ms:

```sh
node --experimental-test-coverage --test-coverage-include='src/tickreplay/static/strategy/strategy-controller.mjs' --test-coverage-include='src/tickreplay/static/replay-commit.mjs' --test-reporter=spec --test-reporter-destination=.agents/logs/test-review-r6-final-narrow.log --test-reporter=lcov --test-reporter-destination=.agents/logs/test-review-r6-final-narrow.lcov --test src/tickreplay/static/strategy/strategy-controller.test.mjs src/tickreplay/static/replay-commit.test.mjs tests/browser/strategy-assets.test.mjs tests/browser/strategy-release.test.mjs
```

Final-source focused coverage: controller **785/873 lines (89.92%), 198/256 branches (77.34%)**; replay committer **230/243 lines (94.65%), 49/55 branches (89.09%)**. App coverage remains unmeasured because the host functions are extracted into the unit harness. The earlier nine-module total below is historical and was not recalculated or presented as a final-tree measurement.

Fresh final `build_strategy_release.mjs --check` and `--check-config tests/browser/strategy-assets.mjs` both returned ok=true, exit 0; reconstruction identifies the final release above, 11 files, 9 edges, 2 roots. `git diff --check` returned exit 0. No full verify or browser rerun was performed during this bounded re-review.

All thirteen acceptance rows remain mapped below. New tests strengthen Load admission and stale lifecycle; the corrective production changes are the controller ownership guards and unconditional delegation to an existing controller after active-committer precedence. Numeric/rollback/import/parity/warning/Worker/script assertions remain present and were not weakened. The fresh narrow run re-executed affected controller/replay and all eight asset/release tests. Other prior executions are retained with their revision/provenance limits, not relabeled as final-tree reruns.

Integration's updated work log records final-release desktop/mobile **11 passed / 1 expected skip**, including the actual production Worker causal path, on local Chrome 152. Root again confirmed there is no retained raw full-matrix report. The skip at `tests/browser/application-strategy.mjs:250` remains only the desktop exclusion of the mobile-specific layout test. The reviewer causal log below is retained proof on the earlier `2914b610...666ed` release, not an independently rerun final-release browser artifact. Final full-tree verification remains lead-owned.

## Blocking Coverage Gaps

None. In particular, fake Worker tests are no longer the sole evidence for the event bridge, and input-quantity tests are no longer the sole evidence for numeric closure.

## Nonblocking Gaps

### R6-T1 — Low: browser cancellation checks remaining count rather than exact retained order identity

`tests/browser/application-strategy.mjs:107` creates a manual buy limit and a strategy sell limit, then observes `open_orders_total` falling from two to one after strategy cancellation. It does not inspect the remaining order's owner, side, price, or ID. That browser assertion alone could pass if the wrong one of the two orders were canceled. The actual broker's exact-owner tests at `order-broker.test.mjs:360` and `:376` independently cover the ownership invariant, so this is supplementary browser precision rather than a blocker. A future assertion of the retained manual buy price/owner would strengthen the end-to-end proof.

### R6-T2 — Low: script and lifecycle branch coverage remains below the general 80% target

Fresh measurements are reported below, not estimated. Both build/fetch scripts now have real negative tests, but successful manifest construction/CLI publication branches and some version/shape branches remain uncovered by the focused offline unit run. `build_strategy_images.py:50` zero/multiple vendor directory branches are not covered by the current-release ambiguity test, which exercises a different function. Worker/controller/workspace/editor/protocol branch percentages also remain below 80%; for example, a fully valid progress event's deadline-rearming branch and some log/callback-failure branches lack Node coverage. The browser does exercise actual runtime progress. Positive release/reconstruction checks complement these units but are not included in this coverage denominator. No 80% executable threshold was configured in the active remediation plan; the report does not describe these values as meeting that broad target.

### R6-T3 — Low: the complete desktop/mobile browser run lacks a retained raw report

The integration work log records the final serial **11 passed / 1 expected skip**, including prior overloaded-host failures and the eventual unchanged-deadline pass. Lead confirmed that the corresponding raw full-matrix JSON/log is unavailable; existing `.last-run.json` files are metadata only. Do not treat an unrelated overwritten two-test reporter JSON as the full run. This reviewer saved a new successful production causal test log and artifacts, but did not recreate the full matrix. Retain unique per-run reporter paths in future runs.

## Acceptance Matrix Mapping

Paths without another prefix below are relative to `src/tickreplay/static/`. Each row was inspected for behavioral assertions, not merely for a matching test title. Line references below are from the initial review snapshot; the final corrective section gives current references for newly affected lifecycle cases. Existing controller references after its initial Load test moved by 154 lines, and existing replay references moved by 128 lines after host-regression insertion.

| Plan acceptance row | Exact evidence | Assessment / limit |
| --- | --- | --- |
| Load admission | `strategy/strategy-controller.test.mjs:379,400,442,488,511`; workspace `:327,349,367` | Ready/loading overlap, Load–Stop–Load, reversed hash completion, canceled acquisition and synchronous reentry; winning hash/revision, all awaited results, no premature candidate allocation. PASS. |
| Stale lifecycle | Controller `:196,214,235,253,285,400,415,465,511,559`; workspace `:377,408`; replay `:401,427` | Stale response/error/listener/timer delivery, cleanup reservation, interrupt retirement, repeated Stop/dispose, terminal overflow, barrier rejection. Every created/discarded Worker termination count and removed listeners are asserted in the overlap tests. PASS; not every possible host-callback exception is instrumented. |
| Numeric inputs and aggregates | `order-broker.test.mjs:54,90,103,129,147,172,189`; `replay-commit.test.mjs:132,153,295` | Buy/sell exposure boundary and boundary+1, signed money, quantity aggregation, allocation/fill/delivery/eligibility counters and unsafe tick fields. Assertions use typed errors and unchanged serialized state. PASS. |
| Portfolio closure | Broker `:68,82,114,161`; replay `:173,278,295,308,314`; controller `:543,559` | No-fill valuation, legacy Start refusal, intermediate opposite fills, close/reversal, equity-only and nonfinite/average conversion failures; pure exact outgoing preflight and terminal sequence rejection. PASS. |
| Atomic rollback | Broker `:54,68,103,114,129,147,172,189,525`; replay `:79,104,132,153,199` | Byte-equal `exportState()` includes orders/fills/events/IDs/run ledgers; multi-command failure preserves staged batch and replay status/cursor, dispatch count stays unchanged, injected failure is followed by a successful retry. PASS. |
| Trust/accessibility | `tests/test_tickreplay_server.py:655`; editor test description-preservation assertions; browser `application-strategy.mjs:44,57,216` | Persistent trust, immediate Load execution, non-sandbox/browser capability and plaintext statements; actual named input and Load described-by attributes in enhanced and degraded modes. PASS from source plus recorded desktop/mobile execution. |
| Load execution | `tests/test_tickreplay_strategy_runner.py:76`; browser `application-strategy.mjs:57,107` | Harmless stdout marker appears after Load before Start; browser controller is ready with no run callback needed. Fresh causal run also asserts the Load-only marker and two loads on restart. PASS. |
| Real Worker causal path | Browser `application-strategy.mjs:107`; fixed five-tick HTTP fixture in `tests/browser/spike_server.py:106` | Actual immutable release and bundled Pyodide/Python execute orders, with no fake strategy response: event 1 has no fill; event 2 fills two shares at 102, cash -20400, one fill/cursor 1; event 3 gives unrealized/equity 200 and a reduced open-order count; exact tape/scrubber positions, Stop/reset, distinct restarted run, fresh event 0. PASS. The UI uses Play/Pause to advance one observed tick, not a mocked Step function. R6-T1 limits the cancellation identity assertion. |
| Worker failures | Worker `strategy-worker.test.mjs:52,62,81,88`; controller stale-response/progress tests; browser `application-strategy.mjs:194` | Real canonical JS runtime seam covers runner failure, loader/fetch/toPy/invoke/toJs/JSON/post failures, structured identity, globals/result destruction and messageerror completion discard. Browser executes an actual Python exception with diagnostic and no position. PASS. |
| Import bounds | Workspace `:392`; source store `:231,260` | Fake `.py` oversized/wrong-extension files throw if read; read count remains zero and source/hash/revision/loaded snapshot stay equal. Decoder rejects raw overflow before invalid UTF-8 and accepts exact maximum canonical bytes with BOM/CRLF overhead. PASS. |
| Line parity | Source store `:241` calls store, protocol request construction and decoder; native runner `tests/test_tickreplay_strategy_runner.py:350` | 8192/8193 physical lines, final LF on/off, ordinary LF and BOM/CRLF. Longest-line rejection remains in existing tests. All eight native variants executed freshly. PASS. |
| Build/fetch failures | `tests/test_fetch_pyodide_assets.py:50,63,75,92,109`; `tests/test_build_strategy_images.py:27,43,56,66,74,100` | Byte/hash/malformed-response errors, JSON-object rejection, allowlist/content mismatch, invalid OID/path before subprocess, subprocess failure, missing/ambiguous matching release, staging/build failure preserving destination bytes. Fifteen offline cases execute freshly. PASS; R6-T2 records remaining branches. |
| Release closure | `tests/browser/strategy-release.test.mjs:24,49,80`; `strategy-assets.test.mjs:25,54,77,93,117`; fresh builder `--check` and `--check-config` | All retained release identities, current relative closure and authored-byte parity; fixed vendor URLs/manifests/budgets and configuration. Fresh Node run includes all eight asset/release tests; builder confirms 11 files, 9 edges, 2 reconstruction roots. PASS. |

## Test Quality and Skip Assessment

The new numeric tests compare complete exported state rather than checking only balances; replay tests additionally protect the actual cursor, staged commands, and delivery count. Boundary+1 and both signs/sides are explicit. Lifecycle tests use controlled futures/timers and captured callbacks, including the reentrant invalidation bug that a simple sequential Load suite cannot detect. A terminal envelope error is tested for cleanup and exactly-once termination, not only for rejection.

The browser path serves the actual app and immutable Worker and loads real Python. Python logs expose exact event snapshots, while tape, position, average price, scrubber and distinct run identity assertions independently check host behavior. The earlier stale-status Restart assertion was strengthened; the product's load deadline was not increased. The Play/Pause scheduling can fail under host contention, but exact tick counts make unintended advancement a failure rather than a false pass.

The sole expected full-matrix skip is `test.skip(!testInfo.project.name.startsWith('mobile'), 'mobile-only layout evidence')` in `application-strategy.mjs`'s narrow-workspace test. It excludes desktop only; the mobile project executes that same layout/touch-target assertion. It does not skip the production bridge, warning, exception, or lifecycle requirements. Pinned Chromium 153, Firefox/WebKit and real-device assistive testing remain unexecuted compatibility/promotion work, not implicit passes.

No deleted or skipped regression was found in the final test diff. Existing release-ID expectations and the application fixture's tick count were updated for the new release and causal scenario; these changes do not weaken the original integrity assertions. Some app wiring tests still use source regex/function extraction; the new production browser scenario materially reduces reliance on those checks without making their unmeasured branches covered.

## Initial Review Execution and Coverage (Pre-corrective Release)

These measurements and the browser execution were freshly performed for the initial R6 review on release `2914b610cc3391acc1552c7df6aa8ff805d44ef0ea120135838050499e6666ed`. They are retained historical evidence; final-source narrow measurements and final-release attribution are reported above.

### Python

```sh
uv run --offline --with pytest-cov pytest -q tests/test_fetch_pyodide_assets.py tests/test_build_strategy_images.py tests/test_tickreplay_strategy_runner.py --cov --cov-config=.agents/logs/coverage-r6-python.rc --cov-branch --cov-report=term-missing --cov-report=json:.agents/logs/coverage-r6-python.json
```

**45 passed in 2.19s, observed process exit 0.** Python 3.13.11, Coverage.py 7.16.1. The temporary cached dependency environment did not modify repository dependencies or locks. Raw log: `.agents/logs/coverage-r6-python.log`; JSON and data use the `coverage-r6-python` basename.

| File | Covered statements / total | Line coverage | Covered branches / total | Branch coverage |
| --- | ---: | ---: | ---: | ---: |
| `scripts/build_strategy_images.py` | 45 / 87 | 51.72% | 11 / 30 | 36.67% |
| `scripts/fetch_pyodide_assets.py` | 63 / 98 | 64.29% | 14 / 32 | 43.75% |
| `strategy-runner.py` | 258 / 298 | 86.58% | 96 / 132 | 72.73% |

Coverage.py's displayed `Cover` values combine statements and branches and are not the line percentages above. Server coverage was not freshly measured in this bounded run. No repository-wide or new-lines-only coverage is claimed.

### JavaScript

```sh
node --experimental-test-coverage --test-coverage-include='src/tickreplay/static/**' --test-coverage-exclude='**/*.test.mjs' --test-coverage-exclude='**/releases/**' --test-coverage-exclude='**/vendor/**' --test-reporter=spec --test-reporter-destination=.agents/logs/coverage-r6-js.log --test-reporter=lcov --test-reporter-destination=.agents/logs/coverage-r6-js.lcov --test src/tickreplay/static/*.test.mjs src/tickreplay/static/strategy/*.test.mjs tests/browser/*.test.mjs
```

**281 passed, zero failed/skipped, observed process exit 0**, Node v24.15.0. Raw LCOV and native summary are retained at the command's destinations.

| Feature module | Line coverage | Branch coverage |
| --- | ---: | ---: |
| order-broker | 92.29% | 83.87% |
| paper-trading | 100.00% | 96.15% |
| replay-commit | 94.65% | 89.09% |
| strategy-controller | 88.61% | 75.10% |
| strategy-editor | 89.36% | 65.38% |
| strategy-protocol | 89.55% | 74.37% |
| strategy-source-store | 93.59% | 82.31% |
| strategy-worker | 89.83% | 63.64% |
| strategy-workspace | 83.94% | 69.29% |
| Nine-module subtotal | **89.71% (3924 / 4374)** | **76.22% (1035 / 1358)** |

The subtotal sums LCOV counts and includes the changed paper-trading helper; it is not an average of percentages or a direct denominator match to the original eight-module review. Unchanged chart/coordinator modules are excluded from this subtotal. `app.js` and browser source-mapped coverage were not measured; generated releases/vendor are intentionally excluded. V8 line counts and Python executable-statement counts are not interchangeable.

### Browser and reconstruction

```sh
STRATEGY_SPIKE_USE_SYSTEM_CHROME=1 STRATEGY_SPIKE_PYTHON='.venv/bin/python' node_modules/.bin/playwright test --config tests/browser/playwright.config.mjs application-strategy.mjs --project=chromium --workers=1 --grep 'production Worker preserves causal fills' --output=.agents/logs/playwright-r6-causal-artifacts
node scripts/build_strategy_release.mjs --check
node scripts/build_strategy_release.mjs --check-config tests/browser/strategy-assets.mjs
```

Initial-review browser execution: **1 passed (1.1m), observed exit 0, no retry**, Google Chrome **152.0.7977.83**. Persistent raw log: `.agents/logs/browser-r6-causal.log`; artifact directory is in the command. A separate reviewer Python process was active during the run; no failure occurred in this run and no performance conclusion is inferred from its duration. Both initial builder checks returned `ok=true`, observed exit 0, for the earlier `2914b610...666ed` release.

## Other Gate Evidence and Limits

- `.agents/logs/verify.log` contains Ruff success, 89 formatted files, Ty success, and **1300 passed / 1 warning in 954.24s**. The lead supplied the successful overall gate exit; this reviewer inspected its log rather than rerunning the 16-minute full suite. The warning is the existing Starlette/httpx deprecation.
- Lead/integration supplied focused Python **107 passed**, native contract **8/8**, and final serial browser **11 passed / 1 expected skip**. These are attributed evidence, not new reviewer executions; R6-T3 identifies the missing raw full-browser report. Release/assets **8/8** were independently included in this reviewer's fresh 281-test Node run.
- Prior reported restart/load and outer browser timeouts under concurrent load remain a host-contention risk. Later serial passes and this fresh causal pass do not erase those failures or prove stable pinned-environment performance.
- The original selected-script 0% gap is materially reduced, but low branch percentages remain explicit. Coverage collection never changes an unexecuted compatibility or deployment gate into a pass.

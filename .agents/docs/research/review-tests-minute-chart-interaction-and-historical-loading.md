# Test Review: Minute-chart interaction and historical loading

## Summary

The pure `MinuteHistorySession` and merge/range helpers have strong unit coverage for the stated policy constants and most boundary/error transitions. The focused JavaScript and Python suites pass. The remaining risk is concentrated in the browser-facing integration: the tests mostly inspect `app.js` source text instead of executing the actual Lightweight Charts callbacks, replay loop, prepend path, and session/request wiring.

- Findings: 4 total (High: 2, Medium: 1, Low: 1)
- JavaScript focused suite: 48 passed, 0 failed
- Python minute-context/server suite: 27 passed, 0 failed
- Coverage: not measured. `gather_diff.py` reported no usable coverage artifact, and the repository has no configured JavaScript coverage tool that measures both `app.js` and `minute-history.mjs`.
- Manual browser acceptance: not performed at review time.

## Review Scope

Reviewed implementation and tests in:

- `src/tickreplay/static/app.js`
- `src/tickreplay/static/minute-history.mjs`
- `src/tickreplay/static/minute-history.test.mjs`
- `src/tickreplay/static/index.html`
- `src/tickreplay/static/styles.css`
- `docs/tick-replay.md`

Intent was checked against `DESIGN.md`, the feature brief, the approved Codex plan, and the gathered review patch. Test structure was evaluated against `.agents/rules/testing.md` and `.agents/rules/dev-environment.md`.

## Coverage Traceability

| Required behavior | Current evidence | Assessment |
| --- | --- | --- |
| Ordered prepend, reverse input, incoming/existing duplicates | Executable helper tests at `minute-history.test.mjs:84-153` | Covered |
| Two consecutive pages in both canonical arrays | Executable helper-level simulation at `minute-history.test.mjs:133-153` | Covered at helper level; production path gap below |
| Threshold boundary (`barsBefore <= 10`) | Executable controller test at `minute-history.test.mjs:193-201` | Covered, including 10/11, negative, and `NaN` |
| Empty-page exhaustion | Executable controller test at `minute-history.test.mjs:232-245` | Covered |
| Failure cooldown 5,000 ms and maximum 3 failures | Executable controller test at `minute-history.test.mjs:247-269` | Covered, including exact cooldown expiry and per-cutoff isolation |
| Single-flight admission | Executable controller test at `minute-history.test.mjs:203-213` | Covered in controller; rapid callback wiring gap below |
| Abort and generation rejection | Executable controller tests at `minute-history.test.mjs:280-320` | Covered in controller; request/session wiring gap below |
| Nested programmatic suppression | Executable controller test at `minute-history.test.mjs:215-230` | Covered in controller; real chart callback timing gap below |
| Logical range shift by unique prepend count | Pure helper test at `minute-history.test.mjs:174-179` plus source inspection at `415-426` | Calculation covered; actual viewport behavior not executed |
| Replay-time pan/zoom remains stable | Source inspection at `minute-history.test.mjs:404-413` | Not behaviorally verified |
| Replay/trading state remains unchanged during prepend | Helper simulation only | Not verified against production `prependMinuteHistory()` |
| Application exception is contained | Extracted loader test at `minute-history.test.mjs:360-402` | Containment covered before real state/chart application; partial-application cases remain |
| Null oldest bar / null visible range | Production early returns exist at `app.js:938-941`, `986-991`, and `1015-1017` | Not directly executed |

## Findings

### [High] `src/tickreplay/static/app.js:927-933,1015-1020,1103-1145,1370-1374` / replay viewport integration: core pan/zoom behavior is not executed

The core acceptance result is currently inferred from source-string assertions that `step()` no longer names `followMinuteView()` and that the subscription text exists. These checks do not prove that real wheel/drag gestures arm paging, that x1/x500 replay leaves the selected logical range unchanged, that the right edge still follows new candles, or that the asynchronous `setTimeout(0)` programmatic guard suppresses the callbacks Lightweight Charts actually emits. A syntactically valid but incorrectly wired chart API could pass all 48 JavaScript tests.

Needed case: run the manual browser checklist or add a browser/integration harness that records the minute logical range before and after multiple replay frames at x1 and x500, exercises wheel/drag, confirms tick follow continues, and verifies programmatic `setData`/`setVisibleLogicalRange` callbacks do not start history requests.

### [High] `src/tickreplay/static/app.js:961-982` / `prependMinuteHistory`: production state and viewport invariants are not executed

The two-page test reimplements the intended merge sequence locally, while the production-function test only searches its source. No test calls the actual `prependMinuteHistory()` with chart spies and a realistic `state`. Consequently it does not prove that both canonical arrays receive the identical unique prefix, the saved range is restored as exact `+N`, loaded history survives `seekTo()`/reset, replay and paper-trading fields remain deep-equal, or `refreshMarkers()` leaves trading events unchanged.

The application-error regression injects a stub that throws before the real prepend path. It therefore also misses exceptions after `state.contextBars` / `state.bars` have been assigned, after only one series has updated, during range restoration, or in marker refresh. Those cases should verify the intended retry/cooldown semantics and rule out partially applied state/chart data.

Needed case: execute an injectable production prepend seam with candle/volume/time-scale/marker spies, snapshot all replay and trading fields named in the plan, test two pages plus seek/reset, and inject failures at each chart-application stage.

### [Medium] `src/tickreplay/static/app.js:986-1012,1221-1241,1244-1283` / request lifecycle: source ordering and controller tests are not joined by an async integration test

The controller correctly rejects stale tokens and enforces single-flight in isolation, and source checks confirm that expected statements appear in the right textual order. There is no deferred-promise test driving the real loader/session glue through rapid visible-range callbacks, a same-symbol/different-date switch, abort, empty response, malformed payload, and failure/cooldown/retry. In particular, no test proves that many range callbacks produce exactly one `fetchLatest` call and that an old response cannot mutate state while a fresh request is active.

Needed case: extract or export dependency-injected loader/session functions, use the real `MinuteHistorySession` with deferred request promises, and assert request counts, state snapshots, current-generation application, and completion transitions.

### [Low] `src/tickreplay/static/app.js:938-941,986-991,1015-1020` / empty chart guards: the `oldest === null` and null-range paths are not directly tested

The early returns are simple and appear correct, but the requested `oldest null` boundary is absent from the executable suite. Add a small behavior test asserting no admission/fetch for missing state/meta/bars, invalid cutoff, null visible range, and a `barsInLogicalRange()` result without a finite `barsBefore`.

## Test Quality Assessment

- Tests are fast, independent, and generally follow Arrange/Act/Assert despite not labeling the phases explicitly.
- Network and chart dependencies are mocked where the extracted loader is executed; no real external service is required.
- Boundary coverage in `MinuteHistorySession` is notably good: threshold 10/11, exact cooldown expiry, three-failure cutoff, empty exhaustion, per-cutoff isolation, nested suppression, abort, and generation reset are covered.
- The main weakness is the source-contract style in `minute-history.test.mjs:324-358,404-433`. Textual ordering is a useful guardrail, but it cannot establish control-flow dominance, runtime callback timing, or correct arguments/state effects. It should supplement, not replace, executable integration tests.

## Test Execution Results

Commands run by the reviewer:

```text
node --test --test-isolation=none src/tickreplay/static/request-coordinator.test.mjs src/tickreplay/static/minute-history.test.mjs src/tickreplay/static/paper-trading.test.mjs
Result: 48 passed, 0 failed

uv run pytest tests/test_tickreplay_minute_context.py tests/test_tickreplay_server.py -q
Result: 27 passed, 0 failed, 1 existing Starlette/httpx deprecation warning
```

Total independently executed: 75 tests, 75 passed, 0 failed.

The repository-wide `verify.sh` result supplied to reviewers was 744 passed / 315 failed, predominantly existing orchestration and Windows/CLI-environment failures. This review does not reinterpret those failures as minute-chart regressions.

## Remaining Risk

Until the browser checklist and a production prepend/request integration seam are exercised, the suite cannot conclusively demonstrate the two user-visible outcomes: replay-time pan/zoom stability and jump-free older-history prepend. The 10,000-retained-bar responsiveness/memory check is also still unmeasured.

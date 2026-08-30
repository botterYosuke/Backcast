# Daily Chart History Paging Implementation Plan

Purpose: make Daily open at the newest 90 bars with five logical bars of right
padding, then prepend older 200-bar pages after real user pan/zoom without
moving the viewport or interrupting replay.

Scope: modify the existing Daily client aggregate, focused JavaScript tests,
one Python API-contract test, and project/design documentation. Preserve the
initial 500-bar fetch for SMA200 warm-up. Do not change production Python,
`/api/daily-context`, Minute paging, dependencies, migrations, adjustment
handling, Daily markers, or O(1) per-frame terminal-SMA updates.

## Implementation Steps

### Step 1: Establish the baseline and add RED pure-policy tests

Files:

- `src/tickreplay/static/daily-chart.test.mjs`

Tasks:

1. Run the focused Node baseline before editing and record any pre-existing
   failure separately from this feature.
2. Add failing tests for exported `DAILY_HISTORY_DEFAULTS` and
   `dailyInitialLogicalRange(barCount, options)`:
   - `barCount <= 0` returns `null`;
   - 1 and fewer than 90 bars use `from: 0`;
   - 90, 500, and 501 observations use `from = lastIndex - 89`;
   - every non-empty result uses `to = lastIndex + 5`;
   - a partial day is counted in the supplied total.
3. Add failing tests for extending `dailyContextUrl(identity, options)` while
   preserving its no-options output. The page case must validate
   `beforeDate=<oldestHistoryDate>` and `limit=200`; reject invalid dates,
   limits outside 1..500, and malformed identities.
4. Add failing tests for `mergeOlderDailyBars` and
   `shiftDailyLogicalRange`: chronological output, exact duplicate collapse,
   existing-bar precedence, strict-before boundary, duplicate-only result,
   two consecutive pages, invalid range, and exact `+N` on both range edges.

RED verification:

```powershell
node --test src/tickreplay/static/daily-chart.test.mjs
```

Expected result: only the newly added tests fail because the symbols or behavior
do not yet exist. Existing tests must not be edited, skipped, or weakened.

### Step 2: Implement pure policy helpers and the DOM-free page controller

Files:

- `src/tickreplay/static/daily-chart.mjs`
- `src/tickreplay/static/daily-chart.test.mjs`

Tasks:

1. Add immutable `DAILY_HISTORY_DEFAULTS` with:
   `visibleBars=90`, `rightPaddingBars=5`, `pageSize=200`,
   `edgeThresholdBars=10`, `failureCooldownMs=5000`, and
   `maxFailuresPerCutoff=3`.
2. Implement `dailyInitialLogicalRange`, the backwards-compatible options form
   of `dailyContextUrl`, `mergeOlderDailyBars`, and
   `shiftDailyLogicalRange`. Reuse `normalizeDailyPayload(payload, maxBars)` for
   the existing OHLCV validation and 200-row page bound.
3. Extend `DailyChartSession` without coupling it to `MinuteHistorySession`.
   Keep existing `request` for initial load and add:
   `historyPageRequest`, `historyArmed`, `historyExhausted`,
   `historyFailures`, `historyPageSize`, `historyEdgeThresholdBars`, and
   `hasInitialViewport` (replacing `hasFitContent`).
4. Implement these DOM-free methods and exact return behavior:
   - `armHistory(identity) -> boolean`;
   - `canLoadOlder({identity, barsBefore, programmatic}) -> boolean`;
   - `admitOlderPage(cutoff, {identity}) -> token|null`, where token contains
     `{id,generation,identity,cutoff}`;
   - `isCurrentOlderPage(token) -> boolean`;
   - `completeOlderExhausted(token) -> boolean`;
   - `failOlderPage(token) -> boolean`;
   - `abortOlderPage(token) -> boolean`;
   - `markInitialViewport() -> boolean`.
5. Reset the page request, arming, exhaustion, failure map, and initial-view
   flag in `resetSession` and `failSession`. A page failure must not change a
   successfully loaded Daily `phase` from `ready`.
6. Add GREEN tests for no-gesture admission, threshold 10/11, programmatic
   suppression, single-flight, full identity and generation staleness,
   cancellation without retry consumption, five-second cooldown, three-failure
   maximum, success clearing a cutoff failure, and session-local exhaustion.

GREEN verification:

```powershell
node --test src/tickreplay/static/daily-chart.test.mjs
node --check src/tickreplay/static/daily-chart.mjs
```

### Step 3: Add RED prepared-page and transaction tests

Files:

- `src/tickreplay/static/daily-chart.test.mjs`

Tasks:

1. Add failing tests for
   `DailyChartSession.prepareOlderPage(token, normalizedPayload)` returning the
   tagged union `{kind:'stale'|'failure'|'exhausted'|'page', ...}` without
   mutating canonical state. A page plan contains `token`, `added`,
   `previousSnapshot`, `nextSnapshot`, and `nextCached`.
2. Cover valid empty and duplicate-only responses as exhaustion; treat
   `available=false`, malformed payloads, and non-empty all-invalid payloads as
   retryable failure.
3. Add failing tests for `commitOlderPage(plan)` replacing the cache record for
   the same `stem|code|actualDate`, prepending the exact unique bars, retaining
   the partial day last, leaving `phase='ready'`, and accepting a plan only
   while its token remains current.
4. Prove page-time SMA behavior: exactly one history precomputation per
   committed page; new early SMA points may appear; previously complete SMA
   values and the partial-day terminal SMA remain numerically unchanged; replay
   still performs no historical recomputation per tick.
5. Add a fake four-series/time-scale harness for
   `applyDailyHistoryPage({session,plan,viewport,ports,timeScale})`. Require the
   live and saved range to shift by the exact unique `N`, using the live range
   read at apply time rather than request admission time.
6. Add failing rollback tests for painter, candle, volume, SMA25, SMA200, live
   range, saved-range replacement, and session-commit failures. Record all four
   previous datasets, the prior live and saved ranges, canonical bars, cache,
   oldest cutoff, rolling windows, and request token; every failure before a
   successful canonical commit must restore all of them. Inject a rejected
   `commitOlderPage(plan)` after chart/live/saved writes and require the same
   complete rollback. Assert the success event order is four-series writes,
   live `+N`, saved `+N`, canonical/cache commit, then return with no further
   state mutation. A rollback failure must surface an `AggregateError`.

RED verification:

```powershell
node --test src/tickreplay/static/daily-chart.test.mjs
```

Expected result: the new prepare/commit/transaction tests fail; the completed
Step 2 policy/controller tests remain green.

### Step 4: Implement page preparation, SMA recomputation, and atomic apply

Files:

- `src/tickreplay/static/daily-chart.mjs`
- `src/tickreplay/static/daily-chart.test.mjs`

Tasks:

1. Refactor private historical precomputation so a candidate history can be
   derived without mutating session fields or metrics. Build immutable SMA25,
   SMA200, trailing 24/199-close windows, and a candidate terminal point.
2. Implement side-effect-free `prepareOlderPage`. Filter to unique ISO dates
   strictly older than `historyBars[0].time`; never use the selected-day
   partial candle as cutoff or merge boundary.
3. Implement `commitOlderPage` as the only canonical commit. It replaces the
   full-identity cache record, applies candidate history/SMA/windows, re-derives
   the terminal SMA from the current partial candle, clears the page
   single-flight and cutoff failure, and increments precomputation metrics once.
   Validate the token and complete plan before assignments, then use only
   non-throwing assignments so a rejected plan cannot partially mutate session
   state.
4. Implement `applyDailyHistoryPage` with this boundary:
   - capture prior snapshot, latest live logical range, and prior saved logical
     range;
   - calculate `shiftedRange = {from: old.from+N, to: old.to+N}`;
   - inside `ChartViewportState.runProgrammatic`, render all four candidate
     series using `paintedLinePoint` clones and set the shifted live range;
   - replace the saved range with `shiftedRange` only after all chart writes and
     the live-range update succeed;
   - call `session.commitOlderPage(plan)` as the final state mutation, after all
     four series plus live and saved range updates succeed;
   - after successful commit, construct and return the result without any
     further chart, viewport, session, or cache mutation;
   - on any earlier failure or a false/throwing commit, restore the prior four
     datasets, prior live range, and prior saved range, and leave canonical
     history/cache/SMA/request state unchanged; aggregate rollback errors.
5. Keep `snapshot()` and the per-frame `deriveTerminal()` API compatible with
   the existing replay and mutation-crash regression tests.

GREEN verification:

```powershell
node --test src/tickreplay/static/daily-chart.test.mjs
node --check src/tickreplay/static/daily-chart.mjs
```

### Step 5: Add RED `app.js` lifecycle and inactive-mode wiring tests

Files:

- `src/tickreplay/static/daily-chart.test.mjs`

Tasks:

1. Add source/injected-function tests for new
   `onDailyLogicalRangeChange(range)` and `loadOlderDailyBars()` seams.
2. Assert that Daily `wheel`, `pointerdown`, and `touchstart` handlers only arm
   the current ready Daily identity; mode switching alone does not arm.
3. Assert callback order: capture the user range, reject inactive/programmatic
   events, call `dailyCandleSeries.barsInLogicalRange(range)`, use threshold 10,
   and request only after controller admission.
4. Assert request order: `admitOlderPage` occurs before
   `requests.fetchLatest`; the URL uses the oldest completed history date and
   `state.daily.historyPageSize`; token, full session identity, and
   `dailySessionIdentity(state.meta)` are rechecked immediately after `await`
   and before normalization, chart writes, status writes, or mutation.
5. Add active-mode tests for transaction failures becoming page failures
   without escaping into replay.
6. Add inactive-mode tests for a request admitted in Daily and completed after
   switching to Minute: no Daily chart-series call; a current plan still
   commits canonical/cache state; `dailyViewport.savedRange` shifts once by
   `N`; switching back renders canonical data at the shifted range.
7. Assert `loadSession` cancels `daily-context`, advances the Daily generation,
   and clears `dailyViewport.savedRange` before its first await. Assert a page
   prepend never calls `redrawAll`.
8. Add failing tests for the shared initial-range path with both an initial
   history snapshot and a first partial day. Make `setVisibleLogicalRange` fail
   once and assert the call remains under the programmatic guard,
   `hasInitialViewport` stays false, and a later render retries successfully
   before setting the flag.

RED verification:

```powershell
node --test src/tickreplay/static/daily-chart.test.mjs
```

### Step 6: Wire initial range, gestures, requests, and hidden-mode commit

Files:

- `src/tickreplay/static/app.js`
- `src/tickreplay/static/daily-chart.mjs`
- `src/tickreplay/static/daily-chart.test.mjs`

Tasks:

1. Add one shared initial-range helper used by both `renderDailyChart` and
   `updateDailyPartialChart`. Remove Daily `fitContent()`. Restore a saved
   same-session range first; otherwise calculate
   `dailyInitialLogicalRange(snapshot.bars.length)`, enter
   `dailyViewport.runProgrammatic`, and call `setVisibleLogicalRange` exactly
   once. Call `state.daily.markInitialViewport()` only after that range call
   succeeds. If the range call throws, leave `hasInitialViewport=false` so a
   later render retries.
2. Use that shared helper when valid empty initial history gains its first
   partial day, producing `{from:0,to:5}` under the same programmatic guard.
   Do not reset the range on subsequent ticks, seek/reset, page load, or mode
   switch.
3. In `ensureDailyChart`, subscribe to `onDailyLogicalRangeChange`. Attach
   passive Daily gesture handlers alongside the existing Minute handlers, but
   keep the controllers independent.
4. Implement `loadOlderDailyBars` using
   `dailyContextUrl(identity,{beforeDate:historyBars[0].time,limit:200})` and the
   existing cancellable `daily-context` coordinator kind. Admission must happen
   before fetch and only one page may be in flight.
5. Map outcomes explicitly:
   - stale: no operation;
   - cancellation: `abortOlderPage`;
   - transport/unavailable/malformed/all-invalid/apply failure:
     `failOlderPage`;
   - valid empty or duplicate-only: `completeOlderExhausted`;
   - active page: `applyDailyHistoryPage`;
   - inactive but current page: capture the old saved range, replace it with
     saved `+N`, then call `commitOlderPage(plan)` as the final mutation without
     touching hidden chart ports; if commit rejects or throws, restore the old
     saved range, and after successful commit perform no further viewport or
     hidden-chart write.
6. Preserve existing `step`, `runReplayFrame`, `commitReplayFrame`,
   `updateDailyPartialChart`, Tick, Tape, board, orders, positions, Minute
   history, clock, scrubber, and liveness call order.

GREEN verification:

```powershell
node --test src/tickreplay/static/daily-chart.test.mjs src/tickreplay/static/minute-history.test.mjs
node --check src/tickreplay/static/app.js
node --check src/tickreplay/static/daily-chart.mjs
```

### Step 7: Lock the unchanged backend contract and update documentation

Files:

- `tests/test_tickreplay_daily_context.py`
- `docs/tick-replay.md`
- `.agents/docs/DESIGN.md` through the `design-tracker` workflow

Tasks:

1. Add a Python test using more than one small page. Query first with a replay
   date, then with the first page's oldest date. Assert both responses are
   ascending, the second page is strictly older, no date overlaps, limits are
   honored, and the next cutoff eventually yields `available=True,bars=[]`.
2. Do not modify `src/tickreplay/daily_context.py`,
   `src/tickreplay/server.py`, `cloud-run/main.py`, or the API response schema;
   existing strict-before and 1..500 behavior already satisfies paging.
3. Document the initial 500-bar warm-up, visible 90+5 policy, 200-bar page size,
   10-bar edge threshold, user-only arming, strict-before cutoff, `+N` view
   preservation, retry/cooldown/exhaustion semantics, inactive-mode safety, and
   page-time-only SMA recomputation in `docs/tick-replay.md`.
4. Through `design-tracker`, replace the obsolete total-client-500/no-paging
   constraint in `DESIGN.md` with: backend queries remain capped at 500 rows;
   initial client load remains 500; client history grows in 200-bar pages until
   finite source exhaustion; replay retains O(1) terminal-SMA work.

Verification:

```powershell
uv run pytest tests/test_tickreplay_daily_context.py tests/test_tickreplay_server.py -q
```

Review `git diff -- src/tickreplay/daily_context.py src/tickreplay/server.py cloud-run/main.py`
and require no feature-authored production backend change.

### Step 8: Run full focused gates and fresh-browser acceptance

Files inspected, not edited solely for this step:

- all files changed in Steps 1-7
- existing browser/runtime logs under `.agents/logs/`

Tasks:

1. Run the combined automated gates below.
2. Self-review the complete diff for unrelated edits, test weakening, swallowed
   exceptions, hard-coded success paths, mutable canonical SMA objects, and any
   accidental change to Minute/replay ordering.
3. Start or reuse the app at `http://localhost:8080` and open a fresh browser
   page, not a cached tab. Verify:
   - first Daily view shows newest 90 bars and five logical bars of right space;
   - fewer-than-90 and first-partial cases retain five-bar padding;
   - wheel zoom and drag near the left edge load repeated 200-bar pages;
   - the visible bars do not jump after each prepend;
   - a Daily-to-Minute switch during an in-flight page and return is safe;
   - SMA25/SMA200 remain continuous and terminal values remain stable;
   - replay, Tick, Tape, board, orders, and position continue at x1 and x500;
   - browser console/runtime exceptions remain zero.
4. Save concise browser evidence to `.agents/logs/` and run the required
   security, quality, and test-coverage review phase before reporting complete.

Final automated commands:

```powershell
node --test src/tickreplay/static/daily-chart.test.mjs src/tickreplay/static/minute-history.test.mjs
node --check src/tickreplay/static/daily-chart.mjs
node --check src/tickreplay/static/app.js
uv run pytest tests/test_tickreplay_daily_context.py tests/test_tickreplay_server.py -q
```

## File Changes

| File | Planned change |
| --- | --- |
| `src/tickreplay/static/daily-chart.mjs` | Add 90+5 policy helpers, page URL/merge/range helpers, independent page controller state, prepared-plan commit, page-time SMA derivation, and atomic four-series apply/rollback. |
| `src/tickreplay/static/app.js` | Replace Daily fit with one-time logical range; add Daily gesture arming, visible-range admission, older-page request handling, active transaction, hidden-mode commit, and session reset wiring. |
| `src/tickreplay/static/daily-chart.test.mjs` | Add RED/GREEN unit, state-machine, transaction, source-wiring, inactive-mode, SMA, and replay-invariant tests. |
| `tests/test_tickreplay_daily_context.py` | Add a two-page strict-before API/query contract regression only. |
| `docs/tick-replay.md` | Document Daily viewport and paging behavior. |
| `.agents/docs/DESIGN.md` | Update the obsolete total-500/no-paging constraint through `design-tracker`. |

Explicitly unchanged: `src/tickreplay/daily_context.py`,
`src/tickreplay/server.py`, `cloud-run/main.py`,
`src/tickreplay/static/minute-history.mjs`, dependencies, and API schema.

## Test Plan

The minimum acceptance matrix is:

| Area | Cases | Failure detected |
| --- | --- | --- |
| Initial range | 0, 1, <90, 90, 500, 500+partial, initial/partial range-write failure and retry | Fit-content regression, off-by-one, missing right padding, premature initialization flag |
| Arming/admission | no gesture, wheel/pointer/touch, 10/11, programmatic, inactive | Recursive or unsolicited paging |
| Request state | single-flight, cancel, generation/stem/code/date/cutoff stale | Cross-session or concurrent commit |
| Retry policy | transport, unavailable, malformed, all-invalid, 5s cooldown, 3 failures | Tight request loop or false exhaustion |
| Exhaustion | valid empty, duplicate-only, reset | Repeated end-of-history requests |
| Merge | unordered, duplicate, collision, boundary, two pages | Corrupt chronology or incorrect `N` |
| Transaction | four series, live range, saved range, commit-last order, rejected commit, rollback failure | Partial chart/viewport/canonical commit |
| Viewport | latest live range and saved range both `+N` | Horizontal jump or wrong restore |
| SMA | new early points, unchanged complete values/terminal, one recompute/page | Indicator discontinuity or per-tick O(n) work |
| Inactive mode | in-flight switch, no hidden writes, return restore | Mode-switch corruption |
| Replay | x1/x500 Tick/Tape/board/orders/position | Recurrence of Daily replay freeze |
| Backend | adjacent strict-before pages and valid empty | Unproven paging assumption |

No test may be deleted, skipped, loosened, or replaced by a source-only
assertion where executable behavior can be exercised.

## Dependencies Between Steps

```text
Step 1 RED policy tests
  -> Step 2 GREEN helpers/controller
     -> Step 3 RED plan/transaction tests
        -> Step 4 GREEN plan/transaction/SMA
           -> Step 5 RED app integration tests
              -> Step 6 GREEN app wiring
                 -> Step 7 API contract + docs/design
                    -> Step 8 combined gates + browser + reviews
```

- Step 2 must define stable page tokens and helper return shapes before the
  transaction tests can be written against a real contract.
- Step 4 must make page application atomic before `app.js` can safely perform
  asynchronous fetch-and-commit wiring.
- Step 6 must pass both Daily and Minute focused suites before documentation can
  claim replay and independent viewport behavior.
- Step 7's Python test proves an existing backend assumption; a failure pauses
  implementation for diagnosis rather than authorizing an unplanned backend
  change.
- Fresh-browser acceptance is last because it depends on every client layer,
  but a browser failure returns to the earliest responsible RED/GREEN step.

## Estimated Effort per Step

| Step | Work | Estimate |
| --- | --- | --- |
| 1 | Baseline and RED policy/helper tests | 0.5-1.0 hour |
| 2 | Pure helpers and page-controller GREEN implementation | 1.5-2.5 hours |
| 3 | RED plan, SMA, transaction, and rollback tests | 1.0-1.5 hours |
| 4 | Prepared plan, cache/SMA commit, and atomic apply | 2.0-3.0 hours |
| 5 | RED lifecycle, request-order, and inactive-mode tests | 1.0-1.5 hours |
| 6 | `app.js` integration and GREEN stabilization | 2.0-3.0 hours |
| 7 | Python contract test and documentation/design update | 0.75-1.25 hours |
| 8 | Combined gates, diff review, browser QA, and review phase | 1.0-2.0 hours |

Total estimate: approximately 10-16 hours, excluding investigation of unrelated
baseline failures or any newly proven backend defect.

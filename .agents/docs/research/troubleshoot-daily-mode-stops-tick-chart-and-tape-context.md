# Phase 1 Context: Daily Mode Stops Tick Chart and Tape

## Reproduction Verdict

**Reproduced in a fresh headless Chrome profile against `http://127.0.0.1:8080/`.**
This is a real client-side rendering bug, not an intentional pause and not only a perceptual-liveness issue.

The visual probe sampled the rendered tick-chart canvases and the actually visible first tape row every 250 ms. In minute mode, both changed on all 7 sample transitions. After Daily was selected and its history became ready, both changed on **0 of 15** transitions:

| Observation | Minute | Daily |
|---|---:|---:|
| Tick canvas hash transitions | 7/7 | 0/15 |
| Tape DOM-head transitions | 7/7 | 0/15 |
| Visible tape-row transitions | 7/7 | 0/15 |

In Daily, the visible tape stayed at `09:12:28.993 52,190 200 1`, and the tick-canvas hash stayed `2566617727`, while the replay clock advanced from `09:12:29` to `09:12:54`.

## Key Findings

1. **The immediate cause is a repeated JavaScript exception in the Daily SMA update.** Chrome captured 252 `Runtime.exceptionThrown` events during the 4-second Daily sample. The stack is:
   `TypeError: Cannot add property zb, object is not extensible` -> Lightweight Charts `Pe.update` -> `updateDailyPartialChart` at `app.js:461` -> `commitReplayFrame` at `daily-chart.mjs:295`.

2. **A frozen terminal SMA point is passed directly to a library that mutates its input.** `terminalPoint()` returns `Object.freeze({ time, value })` (`daily-chart.mjs:211`), and `deriveTerminal()` retains that frozen object (`:556-560`). `updateDailyPartialChart()` then calls `dailySma25Series.update(state.daily.terminalSma25)` without cloning (`app.js:461`). In contrast, candle and volume updates pass newly allocated objects from `paintedBar()` and `paintedVolume()`.

3. **The exception occurs before both downstream displays are written.** The Daily branch calls `ports.updateDaily()` at `daily-chart.mjs:295`; tick and tape writes are next at `:296-297`. When the SMA update throws, neither `pushTickPoints()` nor `pushTapeRows()` executes for that replay tick. This ordering exactly explains why the two user-visible components stop together.

4. **Replay state can continue while those views freeze.** `step()` schedules the next RAF first (`app.js:1653`) and advances virtual time/cursor before rendering (`:1662-1677`). No-tick frames skip `commitReplayFrame()` and can still update the clock/liveness (`daily-chart.mjs:303-311`), while tick-bearing frames repeatedly throw. Therefore `playing=true`, a moving cursor, and a moving liveness clock do not prove that tick/tape rendering works.

5. **The freeze begins after Daily history supplies a terminal SMA, not necessarily at the tab click itself.** The previous log switched at `dailyPhase="loading"`, during which the tick array gained 53 points and the tape head moved from `09:12:13.144` to `09:12:15.484`; its later snapshot was `dailyPhase="ready"`. This delayed transition made the earlier result appear healthy even though the post-ready path was broken.

6. **Layout, hidden-chart ownership, and tape scroll anchoring are not the cause in the reproduced case.** In Daily, the tick chart remained a visible 700 x 324 px element with 7 canvases, and hit-testing its center returned a `CANVAS`. The tape remained at `scrollTop=0` with a 291 px viewport. `setChartMode()` toggles only the minute/daily price panes (`app.js:519-522`), and the `ResizeObserver` resizes only the price-chart stage (`:2090-2094`).

7. **The existing unit and browser checks test control flow, not the real chart boundary.** `daily-chart.test.mjs:636-708` uses harmless fake ports to assert that tick/tape callbacks are present and ordered; `:891-920` only inspects source wiring. The prior browser script records `tapeHead` and `state.tickPoints.length`, but its pass/fail expression checks only replay progression and liveness. It neither checks canvas/tape transitions nor enables runtime exception collection.

## Immediate Cause Hypotheses, Ranked

1. **Confirmed, very high confidence:** Lightweight Charts mutates a frozen SMA25 terminal point, throws, and aborts the remaining replay-frame side effects.
2. **Confirmed latent equivalent:** if SMA25 is absent but SMA200 exists, the identical direct call at `app.js:462` can fail for the same reason.
3. **Rejected for this reproduction:** Daily-pane layout/ResizeObserver ownership or tape scroll anchoring. Both controls remained visible and unscrolled while their rendered content was static.

## Test Gap and Fix Boundary

The repair should preserve immutable application state but pass mutable copies at the Lightweight Charts boundary, at minimum for both terminal SMA `.update()` calls. Regression coverage must wait until `dailyPhase` is ready, collect browser exceptions, and assert that the real tick canvas plus the visible tape head both change after the switch. A unit test should also use a chart-port double that attempts to attach a property to each supplied point; a frozen application-state point must remain frozen while the object given to the port is extensible.

## Evidence

- Initial false-pass log: `.agents/logs/troubleshoot-repro-daily-mode-stops-tick-chart-and-tape-initial.log`
- Visual probe: `.agents/logs/daily-mode-stops-tick-chart-and-tape-visual-repro.mjs`
- Visual/runtime log: `.agents/logs/troubleshoot-repro-daily-mode-stops-tick-chart-and-tape-visual.log`

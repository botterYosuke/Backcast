# Root Cause: Daily Mode Stops Tick Chart and Tape

## Conclusion

The defect is an uncaught third-party chart exception, not replay pausing or a layout issue. A frozen terminal SMA25 point crosses directly into Lightweight Charts `series.update()`. The library attempts to attach its internal `zb` property and throws `TypeError: Cannot add property zb, object is not extensible`. That exception aborts the remaining tick-bearing frame before the Tick chart and Tape are updated.

## Execution Flow and Evidence

1. `step()` schedules the next RAF first (`app.js:1653`), advances virtual time (`:1662`), calls `applyTick()` for due trades (`:1674-1676`), and advances the cursor. `applyTick()` also replaces the immutable daily partial bar (`:602-604`).
2. `runReplayFrame()` enters `commitReplayFrame()` when `to > from` (`daily-chart.mjs:303-306`).
3. In Daily mode, the exact commit order is defer Minute, derive terminal SMA, update Daily, push Tick, push Tape, match orders, update board, update position (`daily-chart.mjs:291-300`).
4. `deriveTerminal()` stores points returned by `terminalPoint()`, which freezes both `{ time, value }` objects (`daily-chart.mjs:209-211,556-560`).
5. `updateDailyPartialChart()` safely creates new candle/volume objects, but passes `state.daily.terminalSma25` directly to `dailySma25Series.update()` (`app.js:456-462`). Chrome captured 252 exceptions at this call. The calls to `pushTicks()` and `pushTape()` at `daily-chart.mjs:296-297` are therefore never reached.
6. Replay still appears active because virtual time/cursor were advanced before rendering and the next RAF was already scheduled. Frames with no due tick skip `commitReplayFrame()` and can run clock/liveness housekeeping (`daily-chart.mjs:303-311`).

The visual probe is decisive: Minute produced 7/7 Tick-canvas and Tape transitions; after Daily history became ready, Daily produced 0/15 for both, while the clock advanced. Both panels remained visible and hit-testable, eliminating pane layout, ResizeObserver ownership, and tape scroll anchoring as causes.

## Mutability and Latent SMA200 Defect

SMA25 fails first because it is updated first. SMA200 is created by the identical frozen-point path and passed directly to the identical API on the next line. Cloning only SMA25 would expose the same failure at SMA200 when enough history exists.

A direct Node probe against `DailyChartSession` confirmed that `{ ...state.daily.terminalSma25 }` is a distinct, extensible object while the original remains frozen, non-extensible, unmodified, and referenced by session state. Clone-at-port therefore preserves application-state immutability.

## Alternatives

- **Clone at the chart boundary:** pass fresh `{ time, value }` objects for both SMA25 and SMA200 updates. This is O(1), preserves load-time precomputation, and directly removes the confirmed and latent failures.
- **Stop freezing session SMA points:** makes the library call succeed but permits a third party to mutate authoritative replay state. This weakens the project's immutability rule and can contaminate snapshots/caches.
- **Catch/reorder chart exceptions so side effects continue:** can mask chart corruption, leave Daily broken, and complicate frame atomicity. It treats the blast radius rather than the cause.
- **Layout/ResizeObserver changes:** unsupported by the visible, hit-testable controls and exact exception stack.

## Recommendation

Clone both terminal SMA points at the Lightweight Charts port before `.update()`. Prefer one small line-point painter/helper and apply it consistently to both `.update()` calls. As low-risk boundary hardening, also map frozen SMA points to mutable copies before `setData()`; current Chrome shows `setData()` succeeds, so this is preventive rather than required to stop the reproduced freeze.

Add a mutating-port unit regression proving the port receives extensible copies while session points remain frozen, plus a fresh-browser regression that waits for `dailyPhase === "ready"`, requires zero runtime exceptions, and observes both Tick-canvas and visible Tape transitions.

## Codex Status

One bounded read-only consultation was attempted with `.agents/logs/codex/prompt-root-cause-daily-mode-stops-tick-tape.md`. It produced no response for more than 60 seconds and was interrupted on the Lead's instruction; the captured response file is empty. No retry was made and no Codex claim is used as evidence.

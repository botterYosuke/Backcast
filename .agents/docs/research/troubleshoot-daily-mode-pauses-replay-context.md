# Daily Mode Appears to Pause Replay: Initial Context

## Reproduction Command

```powershell
uv run python .agents/skills/troubleshoot/repro.py "node .agents/logs/daily-mode-pauses-replay-repro.mjs" --label daily-mode-pauses-replay-initial
```

The Node probe launches the installed Chrome in headless mode with a newly created OS-temp profile, connects over CDP, and removes the verified profile after `Browser.close`. It returns exit 1 only if minute-mode progression is healthy and replay then stops before the session end in daily mode. Setup failures are reported as `INCONCLUSIVE` without falsely identifying the product bug.

## Observed Behavior

The reported replay pause did not reproduce with the live app at `http://127.0.0.1:8080`, symbol 285A, session 2026-08-19, and x10 speed. After clicking Daily while playback remained active:

- `state.playing` stayed `true` and the pause glyph remained visible.
- `state.cursor` advanced from 961 to 1,522 (+561), while virtual time advanced 15.49 seconds.
- The clock moved from 09:12:14 to 09:12:23; the scrubber remained at rounded value 1 because its 0-1000 resolution is coarse over a full session.
- Tick points increased by 197 (frame-stride sampling), the tape head changed, and the board quote changed.
- The selected-day partial daily candle advanced: close 52,150 to 52,160 and volume 1,387,400 to 1,525,100 (+137,700).
- Paper state stayed unchanged because no orders were placed; the same replay path continued to call matching and position updates.

This distinguishes an actual pause from a visually quiet daily candle: replay state, dependent UI, and the selected-day candle model all advanced.

## Immediate Cause Candidates

1. **Visual-scale effect (most likely):** a full-day candle changes far less visibly per tick than a minute candle, especially at x1 and when close/high/low stay in the same chart pixel.
2. **No-trade intervals:** `step()` advances clock/scrubber every animation frame, but `updateDailyPartialChart()` runs only when one or more ticks were consumed. A sparse interval can therefore look static without being paused.
3. **Independent saved viewport:** daily mode intentionally restores its own logical range. If the user panned away from the right edge, the live selected-day candle can update outside the visible range.
4. **Environment/session-specific regression:** the deterministic 285A case is negative; the user's exact symbol/date/speed and interaction sequence may expose a condition not covered by this run.

Code tracing supports intentional continuity: `setChartMode()` (`app.js:484`) never changes `state.playing`; `step()` (`app.js:1627`) always schedules the next frame; `runReplayFrame()` (`daily-chart.mjs:255`) always follows the tick chart and updates clock/scrubber; and `commitReplayFrame()` (`daily-chart.mjs:243`) preserves tick, tape, order, board, and position side effects in either mode.

## Relevant Tests

- `daily-chart.test.mjs:634` asserts identical tick/tape/order/board/position effects in minute and daily frame commits.
- `daily-chart.test.mjs:664` asserts clock, scrubber, and tick-follow calls in both modes, including no-tick frames.
- `daily-chart.test.mjs:822` checks that `app.js` wires all replay effects outside chart-owner guards.
- Existing tests do not drive a real chart-tab click in a browser or assert `state.playing/cursor` across it. The CDP probe is the first browser-level executable coverage of that contract.

## Artifacts

- Probe: `.agents/logs/daily-mode-pauses-replay-repro.mjs`
- Captured run: `.agents/logs/troubleshoot-repro-daily-mode-pauses-replay-initial.log`
- This analysis: `.agents/docs/research/troubleshoot-daily-mode-pauses-replay-context.md`

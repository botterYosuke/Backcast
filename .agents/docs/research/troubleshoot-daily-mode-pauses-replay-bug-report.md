## Bug Report: Daily mode appears to pause replay

### Error
- Message: Selecting the daily chart appears to stop replay, although daily mode is required to preserve replay progression.
- Location: `src/tickreplay/static/app.js:setChartMode`, `step`, and `updateDailyPartialChart`.
- Stack trace: None observed; the browser console probe reported no exception.

### Reproduction
- Steps:
  1. Open the live app with 285A / 2026-08-19.
  2. Start replay at x10.
  3. Switch the upper price chart from minute to daily.
  4. Compare playing state, cursor, clock, tape, board, tick points, and the selected-day daily partial before and after a short wait.
- Reproducibility: The reported visual symptom is user-observed, but an actual replay-state pause was not reproduced in the deterministic browser probe.

### Immediate Context
- Failing code: No state mutation that pauses replay was found. `setChartMode()` changes chart ownership only; `step()` continues through `runReplayFrame()` in both modes.
- Call chain: daily-tab click -> `setChartMode('daily')` -> retained requestAnimationFrame loop -> `step()` -> `runReplayFrame()` -> chart-specific leaf writes plus shared replay effects.
- Recent changes: The uncommitted daily-chart feature added the mode switch and separate daily chart; repository HEAD is `f7a939e`.

### Affected Area
- Files involved: `src/tickreplay/static/app.js`, `src/tickreplay/static/daily-chart.mjs`, `src/tickreplay/static/index.html`, `src/tickreplay/static/styles.css`.
- Related tests: `src/tickreplay/static/daily-chart.test.mjs` replay parity/no-tick/app-wiring tests pass; the browser probe also passed and observed cursor 961 -> 1451, clock +9 seconds, tick/tape/board progress, and daily partial volume +118,300.

### Initial Hypotheses (informed by Codex analysis)
1. Visual-scale feedback: replay continues, but one tick changes a whole-day candle by less than a visible pixel -- evidence confidence: high; Codex consultation timed out without a usable response.
2. Sparse/no-trade interval or coarse scrubber: clock advances while the candle and integer 0-1000 scrubber appear unchanged -- evidence confidence: medium; Codex response unavailable.
3. Restored independent viewport: the live selected-day candle updates off-screen after the user pans daily history -- evidence confidence: medium; Codex response unavailable.

### Codex Pattern Recognition
- Error pattern: Mandatory read-only Codex consultation produced no response and was interrupted after exceeding its 120-second bound; no Codex classification is used as evidence.
- Known similar patterns: Perceptual liveness failures occur when state advances but the active view has low visual sensitivity or does not keep the live observation visible.
- Recommended investigation priority: Prove real browser state progression first, then evaluate active-view liveness feedback and viewport visibility, and finally add an executable tab-switch regression.

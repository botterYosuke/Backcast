## Bug Report: Daily mode stops the Tick chart and Tape

### Error
- Message: `TypeError: Cannot add property zb, object is not extensible`
- Location: `src/tickreplay/static/app.js:461`, called from `commitReplayFrame()` before Tick/Tape writes
- Stack trace: `Lightweight Charts Pe.update -> updateDailyPartialChart -> commitReplayFrame -> runReplayFrame -> step`

### Reproduction
- Steps: start replay at 10x, switch the upper chart to Daily, wait until daily history is `ready`, then sample the visible Tick canvases and first visible Tape row.
- Reproducibility: always in a fresh Chrome profile with the current 7203 session.

### Immediate Context
- Failing code: `dailySma25Series.update(state.daily.terminalSma25)` passes a frozen terminal SMA point directly to Lightweight Charts.
- Call chain: `step -> runReplayFrame -> commitReplayFrame -> updateDailyPartialChart`; the exception prevents later `pushTickPoints` and `pushTapeRows` calls.
- Recent changes: the uncommitted Daily chart/SMA feature added frozen terminal SMA points and direct chart updates.

### Affected Area
- Files involved: `src/tickreplay/static/app.js`, `src/tickreplay/static/daily-chart.mjs`, `src/tickreplay/static/daily-chart.test.mjs`, and the Chrome visual repro artifact.
- Related tests: existing static tests pass but use non-mutating fake ports; the new Chrome probe reproduces 0/15 Tick-canvas and 0/15 Tape transitions in Daily versus 7/7 in Minute.

### Initial Hypotheses (informed by Codex analysis)
1. Frozen SMA25 terminal point is mutated by Lightweight Charts and throws before Tick/Tape writes: confirmed by 252 Chrome runtime exceptions; Codex confidence unavailable because the consultation timed out.
2. SMA200 has the same latent boundary defect: high evidence confidence from the identical direct `.update()` call; Codex confidence unavailable.
3. Daily layout or hidden-chart ownership freezes lower panels: eliminated because both panels remain visible/hit-testable and the runtime exception fully explains the shared stop; Codex confidence unavailable.

### Codex Pattern Recognition
- Error pattern: Codex consultation timed out; direct evidence classifies this as an immutable application object crossing into a mutating third-party API.
- Known similar patterns: candle and volume paths already avoid the defect by passing freshly allocated painted objects.
- Recommended investigation priority: clone both SMA terminal points at the chart boundary, add a mutating-port regression test, then require zero browser exceptions plus visible Tick/Tape transitions.

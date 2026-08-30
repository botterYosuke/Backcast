# Security and Accessibility Review: Daily Replay Liveness

## Verdict

PASS. The daily replay liveness change has no Critical, High, Medium, or Low findings.

## Review Scope

- `src/tickreplay/static/daily-chart.mjs`
- `src/tickreplay/static/app.js`
- `src/tickreplay/static/index.html`
- `src/tickreplay/static/styles.css`
- `src/tickreplay/static/daily-chart.test.mjs`

The review focused on XSS, remote text handling, safe DOM APIs, ARIA behavior, replay and order-state isolation, DOM update frequency, and mobile presentation.

## Findings

- **Critical:** None.
- **High:** None.
- **Medium:** None.
- **Low:** None.

## Evidence

- Liveness labels are fixed application strings; time and progress are derived from numeric replay state. No remote or user-provided text reaches this indicator.
- `app.js:348-353` writes labels through `textContent` and assigns a bounded number to `progress.value`. No `innerHTML`, `insertAdjacentHTML`, `eval`, or equivalent executable sink was added.
- The liveness container intentionally has no `aria-live`. This avoids per-second screen-reader announcement flooding. The native `<progress>` has an accessible Japanese label, while the daily loading status remains a separate low-frequency `role="status"` region.
- `ReplayLivenessPresenter` suppresses duplicate DOM writes when hidden state, play state, displayed whole second, and rounded percentage are unchanged.
- `setChartMode()` changes chart/tab visibility and presentation only. It neither assigns `state.playing` nor calls `setPlaying()`. Replay frame order continues to call tick, tape, order matching, board, and position ports independently of chart mode.
- The mobile rule places the indicator on its own full-width row; its fixed-width progress element and compact, non-wrapping text fit the supported narrow layout without obscuring chart controls.

## Validation

- `node --test src/tickreplay/static/daily-chart.test.mjs`: 36 passed, 0 failed.
- `node --check src/tickreplay/static/daily-chart.mjs`: passed.
- `node --check src/tickreplay/static/app.js`: passed.

## Residual Risk

Automated screen-reader and extreme-width device matrices were not executed. The semantic structure and responsive CSS were reviewed statically, and no actionable issue was identified.

# Quality Review: Minute-Chart Interaction and Historical Loading

## Summary

The steady-state replay loop no longer writes the minute-chart viewport, and the pure history controller correctly centralizes generation checks, single-flight admission, cooldown, exhaustion, deduplication, and logical-range shifting. However, one cross-session race can mix old-session candles into a newly loaded session, and the prepend apply path is not atomic when chart application throws.

Findings: 3 total — High: 1, Medium: 1, Low: 1.

## Review Scope

- `src/tickreplay/static/app.js`
- `src/tickreplay/static/minute-history.mjs`
- `src/tickreplay/static/minute-history.test.mjs`
- `src/tickreplay/static/request-coordinator.mjs` (lifecycle dependency)
- `src/tickreplay/static/index.html`
- `src/tickreplay/static/styles.css`
- `docs/tick-replay.md`
- `.agents/docs/DESIGN.md`
- `.agents/docs/research/feature-minute-chart-interaction-and-historical-loading-brief.md`
- `.agents/logs/codex/minute-chart-plan-synthesized.md`
- `.agents/logs/review-diff-minute-chart-interaction-and-historical-loading.patch`

No project library documentation exists under `.agents/docs/libraries/`.

## Findings

### [High] A lazy request for the old session can be admitted into the new generation

- **File:** `src/tickreplay/static/app.js:1244`
- **Related lines:** `src/tickreplay/static/app.js:986`, `src/tickreplay/static/app.js:1248`, `src/tickreplay/static/app.js:1263`, `src/tickreplay/static/app.js:1370`; `src/tickreplay/static/minute-history.mjs:146`
- **Current code:** `loadSession()` cancels the existing history request and resets the controller before its first await, but it leaves the old `state.meta`, `state.bars`, and interactive chart active until the new session response arrives. A pointer/wheel event can therefore arm the freshly reset controller and `loadOlderMinuteBars()` can capture the old `state.meta` while receiving a token from the new generation.
- **Concrete failure sequence:**
  1. Session A is displayed.
  2. Loading session B calls `requests.cancel('minute-history')` and `minuteHistory.resetSession()`.
  3. While the separate `session` request is pending, the user touches the still-visible minute chart. The controller is armed and admits a session-A history request in B's generation.
  4. Session B returns and replaces `state.meta`. Its initial preload is denied because the A history request occupies the controller's single-flight slot.
  5. The A history response is still current in the controller and `prependMinuteHistory()` applies A candles to B's arrays.
- **Evidence:** A direct controller probe returned `newInitialPreloadAdmitted: false` and `oldTokenStillCurrent: true` after exactly this reset/arm/admit ordering. `RequestCoordinator` does not prevent it because `session` and `minute-history` are separate request kinds.
- **Suggested improvement:** Introduce an explicit session-loading/ready identity and reject chart arming/history admission while a session load is in progress. At minimum, invalidate the old active session before the first await (for example, clear `state.meta` or check a dedicated immutable session key in `loadOlderMinuteBars()`), and require the token to match both generation and the captured `{stem, date}` immediately before every mutation. Add a deterministic regression test for this ordering.

### [Medium] A chart-application exception leaves state and rendered series non-atomic

- **File:** `src/tickreplay/static/app.js:969`
- **Related lines:** `src/tickreplay/static/app.js:970`, `src/tickreplay/static/app.js:973`, `src/tickreplay/static/app.js:981`, `src/tickreplay/static/app.js:1007`
- **Current code:** `prependMinuteHistory()` assigns the expanded `state.contextBars` and `state.bars` before the candle series, volume series, logical range, and markers have all been applied. `loadOlderMinuteBars()` now contains the exception, but it records the page as a failure after state may already have changed.
- **Concrete failure sequence:** If candle `setData()` succeeds and volume `setData()` or `refreshMarkers()` throws, the canonical arrays already contain the page while the two series can disagree. A retry can then see zero unique bars, mark the session exhausted, and leave the visible chart inconsistent with canonical state.
- **Suggested improvement:** Stage and validate the next arrays and painted payloads first. Commit canonical state only after both core series updates succeed; if the second series fails, restore the first series and prior logical range. Treat marker refresh as a separate best-effort side effect after the history page is committed, so a marker-only exception does not convert an applied page into a retryable history failure. Extend the application-error test to assert state, both series, range, and controller consistency, not only rejection containment.

### [Low] Page-size policy is duplicated and the controller field is unused

- **File:** `src/tickreplay/static/minute-history.mjs:85`
- **Related lines:** `src/tickreplay/static/app.js:921`, `src/tickreplay/static/app.js:1001`
- **Current code:** `MinuteHistorySession.pageSize` is configured from `MINUTE_HISTORY_PAGE_BARS`, but request construction uses the app-level constant directly. The controller property has no production consumer, so the two policy values can drift.
- **Suggested improvement:** Use `minuteHistory.pageSize` when constructing the request, or remove `pageSize` from the controller/defaults and keep the policy in one documented location.

## What Is Sound

- `step()` follows only the tick chart; minute pan/zoom is no longer overwritten per replay frame.
- The initial/seek/reset minute viewport uses logical ranges, avoiding the earlier first-bar time-range clamping problem.
- History admission happens before fetch, and ordinary pre-existing in-flight responses are rejected after session reset.
- Older bars are merged chronologically with existing-bar precedence and duplicate removal.
- Both canonical arrays retain history across seek/reset, and range shifting uses the unique prepend count.
- Empty-page exhaustion, abort-to-idle, same-cutoff cooldown, and bounded failure tracking are isolated in a DOM-free controller.
- Index/CSS gesture defaults do not disable Lightweight Charts pan, wheel zoom, or touch handling.

## Performance Assessment

Each 200-bar prepend constructs full-array Sets twice, maps the complete retained series twice, calls full `setData()` for candle and volume series, and rebuilds markers. One page is bounded, but repeated paging to approximately 10,000 retained bars has cumulative quadratic traversal with respect to the number of pages. No measured defect is asserted: the required 10,000-bar browser observation was not performed. This remains a release validation gap; record frame responsiveness and replay progress during a prepend before accepting the performance behavior.

## Codex Consultation

- **First call:** Wrapper exit `0`, model `gpt-5.6-sol`, response file `.agents/logs/codex/20260822T074502Z-quality-review.md`, stderr `.agents/logs/codex/20260822T074502Z-quality-review.err.log`. The response only repeated context-loader output and asked for the Objective despite the complete prompt, so it provided no substantive review evidence.
- **Single retry:** Prompt included the original objective plus explicit failure context. It produced no wrapper JSON or response content for more than three minutes and was interrupted to avoid blocking the review. The response artifact `.agents/logs/codex/20260822T074639Z-quality-review.md` is empty. Codex consultation is therefore inconclusive; all findings above are independently derived from code and a direct controller probe.

## Validation Gaps

- Manual browser checks at x1 and x500 replay speeds are not complete.
- Two-page viewport stability and seek/reset retention are unit-covered but not browser-confirmed.
- Approximately 10,000 retained bars have not been profiled or observed interactively.
- The focused Node and Python suites passed as reported by the lead, while the repository-wide gate remains red from known unrelated baseline failures.


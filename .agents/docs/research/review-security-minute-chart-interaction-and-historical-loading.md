# Security Review: Minute-chart interaction and historical loading

## Summary

- Findings: 2 total (Critical: 0, High: 0, Medium: 2, Low: 0).
- No hardcoded secrets, SQL/command injection, XSS sink, authentication/authorization regression, sensitive logging, or dependency change was found in the reviewed scope.
- The same-origin history URL encodes every variable query value, and the single-flight, abort, generation, and current-token checks correctly reject stale history responses before mutation.
- The remaining findings concern client-side availability and canonical minute-history integrity when a same-origin API response or chart application is malformed.

## Review Scope

- `docs/tick-replay.md`
- `src/tickreplay/static/app.js`
- `src/tickreplay/static/index.html`
- `src/tickreplay/static/minute-history.mjs`
- `src/tickreplay/static/minute-history.test.mjs`
- `src/tickreplay/static/styles.css`
- Intent references: `.agents/docs/DESIGN.md`, the feature brief, the synthesized plan, and the gathered review patch.

## Findings

### [Medium] Minute-history responses are not fully validated or bounded before entering canonical state

- **File**: `src/tickreplay/static/minute-history.mjs:42`
- **Related call site**: `src/tickreplay/static/app.js:1006`
- **Description**: `loadOlderMinuteBars()` accepts any array returned as `payload.bars`, and `mergeOlderBars()` validates only that `bar.time` is finite and older than the boundary. It does not validate that `open`, `high`, `low`, `close`, and `volume` are finite numbers, that OHLC relationships are coherent, that the time is representable by `Date`, or that the received page is no larger than the requested 200 bars. A corrupted or compromised same-origin response can therefore persist malformed bars in both `state.contextBars` and `state.bars`, provoke chart exceptions on every redraw/seek, or impose avoidable CPU and memory work with an oversized page. This is a client availability and data-integrity risk; it is not an XSS path because the values are not inserted as HTML.
- **Recommended fix**: Normalize the response through a pure validator before merge. Accept only plain bar objects whose `time/open/high/low/close/volume` values are finite and whose timestamp is within the supported epoch range; enforce non-negative volume and the expected OHLC invariants; cap the normalized page to `MINUTE_HISTORY_PAGE_BARS`. Treat a non-empty response that normalizes to no valid bars as a failure (cooldown/retry), not as proven history exhaustion. Add malformed-field, invalid-date, and oversized-page tests.

### [Medium] A chart-application exception can leave canonical history committed despite failure completion

- **File**: `src/tickreplay/static/app.js:969`
- **Related handler**: `src/tickreplay/static/app.js:1007`
- **Description**: `prependMinuteHistory()` assigns both canonical arrays at lines 970-971 before `candleSeries.setData`, `volumeSeries.setData`, range restoration, and marker refresh finish. `loadOlderMinuteBars()` now catches an exception from that path and calls `completeFailure()`, which prevents an unhandled promise rejection and keeps replay running, but it does not undo the earlier assignments. The next request can consequently use the newly advanced cutoff even though the page was reported as failed, while the chart and canonical arrays may disagree; the inconsistent history is then retained by seek/reset. Replay and paper-trading fields are not overwritten, but minute-history state is not failure-atomic.
- **Recommended fix**: Make the history application transactional. Prepare and validate the merged arrays and painted series payloads without mutating `state`; apply the chart updates; then commit `state.contextBars` and `state.bars` only after all operations that can reject the page have succeeded. If chart APIs cannot be updated atomically, retain the previous arrays/range and restore both series and state in the catch path. Add a regression that injects a failure after merge and asserts the two canonical arrays, earliest cutoff, visible range, replay fields, and trading fields remain unchanged.

## Controls Verified

- **URL/input construction**: `stem`, `code`, `date`, and `time` are encoded with `encodeURIComponent`; `limit` is a fixed numeric policy constant, so the new URL builder does not permit query injection or an arbitrary-origin fetch.
- **XSS**: The reviewed production code adds no HTML-producing sink. Existing status and data presentation paths use `textContent`; the `Function` constructor occurs only in a Node test over trusted local source text.
- **Cancellation and stale responses**: Session load cancels the `minute-history` request and advances generation before its first `await`. After fetch, `isCurrent(token)` runs before mutation. Reset clears loading/failure/exhaustion/arming state, and stale completion methods cannot alter the new generation.
- **Request pressure**: Admission precedes fetch; one token can be active; programmatic range changes are suppressed; empty success exhausts the session; failure retry is bounded by a 5-second cooldown and three attempts per cutoff.
- **State isolation**: Successful prepend changes only `contextBars` and `bars`, then redraws the two minute series and markers. It does not restore a request-start snapshot or directly mutate replay/trading fields.
- **Secrets/auth/dependencies**: No credentials, new logging, authentication boundary, package/version, or third-party dependency change appears in this feature.

## Validation

- `node --test --test-isolation=none src/tickreplay/static/minute-history.test.mjs`: 26 passed, 0 failed.
- Manual browser testing was not performed by this reviewer; interaction timing and real Lightweight Charts failure behavior remain runtime acceptance risks.


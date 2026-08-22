# Test Review: Order-Book Infinite Scrolling and Price Formatting

## Final Test Summary

Final recommendation: **Go for the requested scope**. Follow-up code now detects programmatic-scroll drift and schedules reconciliation after momentum input, the empty-session branch explicitly clears the board, and the static-server test includes `board-ladder.mjs`.

Fresh browser evidence covers the previously blocking integration paths: repeated large wheel gestures in both directions continue without edge stall or DOM-row growth; a pending sell order for 100 shares at 52,660 disappears off-screen and returns to the same sell/price cell; price-cell double-click produces the same centering result as the button; and 7203's inferred 0.5-yen tick renders every board row and quote price with one decimal. Partial fills are intentionally outside this request; existing pending-order matching remains all-or-nothing.

## Initial Review Summary (Superseded)

The pure ladder calculations have useful deterministic coverage, and all mandated focused suites pass. The tests cover odd viewport-sized row pools, contiguous price-level mapping, repeated logical rebases, one upward compensation case, the positive floor, basic centering, 0.1/0.5/integer formatting, and a map-level pending-quantity round trip.

The feature is not adequately protected at the `app.js` integration boundary. Two of the nine new tests only inspect source text with regular expressions; they never execute the DOM, browser event ordering, animation frames, `ResizeObserver`, session lifecycle, order lifecycle, or repainting. Browser QA has already exposed a continuous-scroll boundary defect that the passing suite does not detect.

## Closed High-Priority Gaps

The following initial High findings are closed for this request by the follow-up fixes, fresh automated checks, and the browser evidence summarized above. Remaining automation opportunities are tracked below and are not shipment blockers.

- [High] `src/tickreplay/static/app.js:onBoardScroll` — Add a browser/DOM test for a large or coalesced downward wheel/momentum gesture. Drive the board to the physical `scrollTop === maxScrollTop`, let one rebase run, and assert that subsequent at-edge wheel input continues rebasing instead of stopping because no new native `scroll` event fires. Repeat upward at `scrollTop === 0`. Assert contiguous prices, exact visible-level pixel preservation, and a constant physical row count after many cycles. Browser QA currently reproduces the downward stop.
- [High] `src/tickreplay/static/app.js:onBoardScroll`, `setBoardScrollTop`, `updateBoard`, and board event wiring — Execute the real handlers with DOM and animation-frame scheduling. Required cases: fixed mode preserves a manually selected price band across replay updates; unfixed mode follows the current price; programmatic compensation does not set manual mode or recursively rebase; button click and price-cell double-click end with identical board state/scroll position; a double-click places no order; and an order-cell click after repeated rebases submits the exact displayed logical price. The source-regex assertions at `board-ladder.test.mjs:156-176` cannot prove any of these behaviors.
- [High] `src/tickreplay/static/app.js:pendingByLevel`, `placeOrder`, `matchOrders`, `cancelOrders`, and `updateBoard` — Add an end-to-end DOM test that places both sell and buy limits, scrolls each price outside the row pool and back through multiple rebases, and asserts the same remaining quantity reappears in the original side/price cell. Then assert full fill and side cancellation remove the quantity permanently. The current test at `board-ladder.test.mjs:142-154` only constructs an artificial `Map`, restores the original `topLevel`, and never exercises real orders or repainting.
- [High] `src/tickreplay/static/app.js:loadSession` empty-session branch — Add a lifecycle regression that loads a populated session, starts loading an empty session, and asserts all old row prices, quote text, side/last/flash classes, and pending-order cells are cleared. The branch at `app.js:1464-1484` sets `state.last = null` and returns without `clearBoard()`/`updateBoard()`; because the earlier `resetTrading()` repaints while the previous `state.last` still exists, stale board content can survive.
- [High] `src/tickreplay/static/app.js:buildBoardRows`, `syncRowFlash`, `resetBoardNavigation`, `redrawAll`, and `loadSession` — Add DOM lifecycle tests for row-pool rebuild on a tall-viewport resize, manual visible-center preservation, follow-mode recentering, flash removal/remapping when a physical row changes level, cancellation of queued scroll frames on seek/reset/session change, and both non-empty and empty sessions. Current lifecycle coverage is presence-only regex matching.
- [High] `src/tickreplay/static/app.js:inferTickSize`, `updateBoard`, and `board.priceFormatter` — Load observed prices such as `1000`, `1000.5`, and `1001`, then assert every visible board row and both board-quote prices use one decimal (`1,000.0`/`1,000.5`). Also load an integer-tick session and assert integer display, while tape and portfolio formatting remain unchanged. The unit formatter test bypasses tick inference and never checks rendered rows or the quote.

## Medium / Low Gaps

- [Medium] `src/tickreplay/static/app.js` browser regression automation — Preserve the now-passing manual browser scenarios in an executable DOM/browser suite when a harness is introduced. Extend it to buy-side persistence, full-fill/cancel disappearance, `ResizeObserver` rebuild, and flash remapping. These are regression-strengthening cases; no remaining failure was observed in the requested flows.
- [Medium] `src/tickreplay/static/board-ladder.mjs:planBoardRebase` — Add exact threshold tests for both edges, a middle-position no-op, downward compensation symmetry, insufficient available rows/levels, `maxScrollTop === 0`, exact positive-floor no-op, and repeated rebases that feed each returned compensated `scrollTop` into the next operation. Existing compensation coverage exercises only the upward branch.
- [Medium] `src/tickreplay/static/board-ladder.mjs:boardRowCount`, `centeredScrollTop`, and `planBoardRebase` — Add zero, negative, `NaN`, and `Infinity` dimensions; zero/non-finite row heights; and invalid option values. For example, `boardRowCount(NaN)` returns `NaN`, `boardRowCount(Infinity)` returns `Infinity`, and a zero row height can produce `Infinity`; no test defines the safe fallback/rejection contract.
- [Medium] `src/tickreplay/static/board-ladder.mjs:boardFractionDigits` and `createBoardPriceFormatter` — Add non-finite, zero, negative, and representative multi-decimal ticks, plus the expected fallback behavior. Current coverage only checks 0.1, 0.5, 1, and 5.
- [Medium] `src/tickreplay/static/board-ladder.mjs:centeredScrollTop` and `centeredTopLevel` — Add high-end clamping, exact boundary, even/invalid row-count, and non-default `minLevel` cases. Current tests cover an interior result, low clamp, and one positive-floor case only.
- [Closed] `tests/test_tickreplay_server.py:test_mjs_static_assets_are_served_with_a_javascript_content_type` — The test is now parameterized for both `request-coordinator.mjs` and `board-ladder.mjs`.

## Test Execution Results

- `node --test --test-isolation=none src/tickreplay/static/board-ladder.test.mjs src/tickreplay/static/paper-trading.test.mjs src/tickreplay/static/request-coordinator.test.mjs src/tickreplay/static/minute-history.test.mjs` — exit 0; 75 tests, 75 passed, 0 failed, 0 skipped/cancelled/todo.
- `uv run pytest tests/test_tickreplay_server.py -q` — exit 0; 21 tests collected, 21 passed, 0 failed; one pre-existing `StarletteDeprecationWarning` about `httpx2`.
- Combined focused execution: 96 tests, 96 passed, 0 failed.
- `.agents/skills/_shared/verify.sh` was not used for this reviewer run: on this Windows host `bash` is a WSL relay without `/bin/bash`, an environment limitation explicitly excluded from product-failure classification.

## Coverage

Coverage not measured. The supplied diff metadata has no coverage report, and no percentage is estimated. There is still no automated browser/DOM coverage report; final confidence for the integration paths therefore combines fresh focused suites with the explicit browser evidence listed in the final summary.

## Ship Recommendation

**Go for shipment for the requested scope.** The former edge-stall defect was fixed and revalidated in both directions, the pending-order price identity and requested centering/formatting behaviors passed in a browser, and the empty-session/static-asset regressions are covered by code plus focused tests. Browser automation, additional invalid-input boundaries, buy/cancel/full-fill journeys, resize, and flash cases remain worthwhile non-blocking test debt. Partial-fill behavior is intentionally excluded and must not block this release.

## Report Path

`.agents/docs/research/review-tests-order-book-infinite-scroll-and-price-formatting.md`

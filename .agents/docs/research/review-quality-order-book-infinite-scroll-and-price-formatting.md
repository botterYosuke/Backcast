# Quality Review: Order-book infinite scrolling and price formatting

## Quality Summary

- The logical-level design is sound: rows, order clicks, pending quantities, ask/bid/current classes, and price text are all derived from `topLevel - rowIndex`. Pending orders remain in `trading.orders` keyed by level, so recycling a physical row cannot move an order to another price.
- Upward rebasing uses `topLevel += k` and `scrollTop += k * rowHeight`; downward rebasing uses the same equation with negative `k`. This preserves the pixel position of every logical level. The row pool is viewport-sized plus overscan and the lower bound keeps the last row at level 1 or above.
- The Central button and price-cell double-click call the same `centerBoardOnCurrent()` function. Ordinary clicks on `.board-price` do not match `.board-order`, so the double-click gesture cannot submit an order.
- Board prices and the board quote use a board-only formatter with identical minimum/maximum fraction digits. `0.5` therefore renders both `1,000.0` and `1,000.5`, while the global tape/portfolio formatter is untouched. Invalid/non-positive ticks fall back to zero fraction digits in the formatter; the application only assigns inferred positive candidates to `board.tick`.
- Final verification passed: 75/75 Node tests, `node --check` for `app.js` and `board-ladder.mjs`, and `git diff --check`.
- Final browser evidence confirms three large downward gestures moved the logical top from 52,880 to 51,600 and settled at `scrollTop 339/499`; three large upward gestures settled at `101/499`. The physical row count remained 53 throughout. A pending sell 100 at 52,660 disappeared off-screen and returned in the same sell cell, Central/double-click states were identical, and all 7203 half-tick board rows used one decimal.
- **Final open counts: Critical 0, High 0, Medium 0, Low 1.**

## High Findings

### Open High findings: None

### Resolved High — Momentum could strand the ladder at a physical edge

- **File:** `src/tickreplay/static/app.js:499`, `src/tickreplay/static/app.js:759`, `src/tickreplay/static/app.js:799`
- **Failure path:** A large downward wheel gesture reached physical `scrollTop = 523`. The first edge callback rebased eight levels and assigned a compensated target near `363`, but remaining momentum returned the element to `523` while `programmaticScroll` suppressed scroll handling. Once the element was already at its maximum, another downward wheel produced no changed `scrollTop` and therefore no new scroll event. The supposedly continuous ladder stopped after one rebase. The same mechanism applies at the upper edge.
- **Resolution:** The working tree records the programmatic target, compares the actual position on release, and schedules drift reconciliation as manual input (`app.js:499-518`, `759-810`). It also retains the target long enough to discard a delayed expected scroll event without swallowing a later real gesture. Automated tests exercise repeated upward/downward coalesced momentum and the positive floor.
- **Verification:** The final browser run crossed repeated logical ranges in both directions while retaining a constant 53-row pool and non-terminal `scrollTop` runway (`339/499` downward and `101/499` upward). The original browser-only failure is resolved.

## Medium / Low Findings

### Resolved Medium — An empty session left the previous session's board visible

- **File:** `src/tickreplay/static/app.js:1497`, `src/tickreplay/static/app.js:1514`
- **Failure path:** The no-trades branch committed `state.last = null` and returned without calling `updateBoard()` or `clearBoard()`. `resetBoardNavigation()` at load start reset navigation only, so old prices, side classes, flashes, and pending-order text could remain visible indefinitely under the “no trades” status.
- **Resolution:** `clearBoard()` is now called at `app.js:1519` before the branch returns, and a source regression test covers the committed no-trades path.

### Low — Synthetic quantity cache grows with distinct visited price levels

- **File:** `src/tickreplay/static/app.js:478`, `src/tickreplay/static/app.js:482`, `src/tickreplay/static/app.js:634`, `src/tickreplay/static/app.js:832`
- **Failure path:** `quantityAt()` inserts every newly encountered near-current level into `board.qty`. Entries are removed only when that exact level trades or the whole board is cleared, so a long/high-range session retains one emoji string per distinct visited level. DOM rows remain bounded, but this auxiliary cache is proportional to the session's price range rather than the row pool.
- **Fix:** Either document and measure this session-local bound as intentional, or add an explicit retention policy with a named cap/horizon. If pruning is chosen, reconcile it with the documented promise that a synthetic quantity remains stable until that price trades.

### Resolved Low — A user gesture that begins during centering could be reconciled without entering manual mode

- **File:** `src/tickreplay/static/app.js:515`, `src/tickreplay/static/app.js:516`, `src/tickreplay/static/app.js:799`
- **Resolution:** Target drift now calls `scheduleBoardScrollReconcile(actualScrollTop, true)` at `app.js:516`. `onBoardScroll()` separately ignores a delayed event only when its actual position still matches the retained programmatic target (`app.js:805-809`). The wiring regression test requires both behaviors.

## Codex Consultation

- Command: `uv run python .agents/skills/_shared/codex_consult.py --prompt-file .agents/logs/codex/prompt-quality-review.md --label quality-review-order-book --sandbox read-only --timeout 300`.
- Result: no stdout or response content was produced after approximately 240 seconds. At the lead's request to finalize promptly, the process was interrupted; wrapper exit code was 1 and `.agents/logs/codex/20260822T101847Z-quality-review-order-book.md` remained empty. No Codex conclusion was used in this report.

## Ship Recommendation

**Ship-ready for the requested feature.** The browser-only High defect, the manual-mode race, and the empty-session stale-board path are resolved and covered by final browser/automated evidence. No Critical, High, or Medium findings remain. The session-local synthetic quantity cache is the sole open Low finding and is non-blocking for this release.

## Report Path

`.agents/docs/research/review-quality-order-book-infinite-scroll-and-price-formatting.md`

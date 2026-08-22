# Security Review: Order-Book Infinite Scrolling and Price Formatting

## Scope

Reviewed the complete implementation and relevant diff hunks for:

- `src/tickreplay/static/app.js`
- `src/tickreplay/static/board-ladder.mjs`
- `src/tickreplay/static/board-ladder.test.mjs`
- `src/tickreplay/static/styles.css`
- `docs/tick-replay.md`

The review used `FR-TICKREPLAY-3` through `FR-TICKREPLAY-6`, the approved feature brief, and `.agents/logs/review-diff-order-book-infinite-scroll-and-price-formatting.patch`. Unrelated `feature-agent-agnostic-*` hunks were excluded.

Focus areas were XSS and event-target trust, malformed remote values, numeric boundaries, bounded DOM/RAF/memory behavior, stale callbacks across session changes, order side/price integrity after row rebases, and accidental order submission from a price-cell double-click.

## Critical / High Findings

None. No Critical or High security issue blocks shipment.

## Medium / Low Findings

### Medium — Malformed session numerics are committed without shape, finiteness, or safe-range validation

- Location: `src/tickreplay/static/app.js:1459`, `src/tickreplay/static/app.js:1460`, `src/tickreplay/static/app.js:1461`, `src/tickreplay/static/app.js:452`, `src/tickreplay/static/board-ladder.mjs:26`
- Evidence: `session.us`, `session.price`, and `session.qty` are coerced directly with `Float64Array.from`. The code does not first require equal array lengths, finite values, positive prices/quantities, monotonic timestamps, or a level that remains a safe integer. Board mapping later computes `Math.round(price / board.tick)` and passes the result through ladder arithmetic.
- Failure path: a malformed or tampered same-origin session response can introduce `NaN`, `Infinity`, missing entries, or unsafe-magnitude levels. Those values can poison centering and row mapping, make displayed/order levels ambiguous, or make chart/tape processing throw and stop the replay UI. Numeric text still reaches the DOM through `textContent`, so this is an availability/integrity issue rather than XSS.
- Recommendation: validate the complete session payload before committing any state. Require matching lengths/count, finite monotonic timestamps, finite positive prices and quantities, and `Number.isSafeInteger(levelOf(price))`; enforce a documented maximum session length and reject the whole response atomically on failure.
- Shipment decision: not a feature-specific Critical/High blocker, but it should be hardened because the data ultimately originates outside the browser trust boundary.

### Low — Synthetic board-quantity cache has no explicit retention bound

- Location: `src/tickreplay/static/app.js:414`, `src/tickreplay/static/app.js:474`, `src/tickreplay/static/app.js:478`, `src/tickreplay/static/app.js:799`
- Evidence: `quantityAt` inserts every newly visited logical level into `board.qty`. Entries are cleared on session reset and individual traded levels are deleted, but there is no size cap or pruning policy across a replay with a very wide price range.
- Failure path: a long or adversarial session that repeatedly visits distinct, widely separated prices can retain an increasing number of small strings for the session lifetime. Infinite manual scrolling alone does not populate off-current levels because quantities are generated only inside `BOARD_QTY_SPAN`, and the DOM/RAF work remains bounded; therefore the practical impact is limited browser-memory pressure.
- Recommendation: cap the cache with a small LRU or prune entries outside a bounded band around the current/visible levels. Pending orders do not depend on this map and must remain stored separately by logical level.

## Verified Security Properties

- Board rows, quantities, prices, quotes, and remote trade-type text are rendered with `textContent`; no HTML injection sink is present in the reviewed paths.
- A price-cell double-click targets `.board-price`, while order submission accepts only `.board-order`; the two click events preceding `dblclick` therefore do not submit an order.
- Order placement derives the logical level from the current `board.topLevel` and immutable row index. Pending sell/buy quantities are rebuilt from `trading.orders` keyed by logical price level, so recycled physical rows do not change price or side.
- Session/seek/reset paths increment the scroll generation and cancel both pending scroll RAF handles. A stale callback cannot rebase the next session.
- The physical row pool is viewport-sized with fixed overscan, the scroll handler admits at most one rebase RAF, and the release RAF is replaced/cancelled. No unbounded DOM or RAF queue was found.

## Verification Notes

- `node --test --test-isolation=none src/tickreplay/static/board-ladder.test.mjs`: 9/9 passed.
- `node --check src/tickreplay/static/app.js`: passed.
- `node --check src/tickreplay/static/board-ladder.mjs`: passed.
- Scoped `git diff --check`: passed (line-ending warnings only).
- Previously reported integrated checks were accepted as context: static Node 69/69 and tickreplay server 20/20.
- `.agents/skills/_shared/verify.sh` was not rerun because the known Windows WSL launcher lacks `/bin/bash`; this environment issue is not a product security finding.

## Conclusion

Security gate: **PASS WITH NON-BLOCKING HARDENING**. Critical: 0, High: 0, Medium: 1, Low: 1.

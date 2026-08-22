# Order-book infinite scrolling and price formatting — codebase scan

## Summary

- This is a frontend-only order-book change. No API, repository, DuckDB, or server-schema work is required.
- The current ladder is a fixed pool of 41 DOM rows. Native `overflow-y: auto` can only reveal another part of those same 41 rows; scrolling never changes their price levels, so it cannot continue past either end.
- The existing **中央へ** action is exactly `board.topLevel = null; updateBoard();`. A shared `centerBoardOnLast()`-style function should own that behavior and be called by both the button and delegated `dblclick` handling for `.board-price`.
- Mixed `1,000` / `1,000.5` rendering is caused by the global formatter having only `maximumFractionDigits: 2`. The board already infers `board.tick`; board-only formatting should set both minimum and maximum fraction digits from that tick (one digit for `0.1`/`0.5`, zero for integer ticks).
- Recommended complexity is **MODERATE**: 4–5 files and roughly 110–170 production/doc LOC plus 120–180 test LOC. The scroll state is coupled to row-to-price mapping, order placement, pending-order display, trade flashes, fixed/auto-follow behavior, and programmatic centering.

## Existing Flow

### DOM and layout

- `src/tickreplay/static/index.html:114-143` contains the board card. The price ladder is the empty `<ol id="board" class="board">` at line 127; **板固定**, **中央へ**, and the quote are lines 135-141. No new markup is inherently required.
- `src/tickreplay/static/styles.css:318-355` makes the board card a fixed-width flex column and the `<ol>` the native scroll container (`flex: 1`, `min-height: 0`, `overflow-y: auto`). Rows are 20 px high (`styles.css:357-360`). The price cell is styled at `styles.css:380-385`; it currently has no double-click affordance or `user-select: none`.

### State, row generation, and price-level mapping

- `src/tickreplay/static/app.js:55` fixes `BOARD_ROWS` at 41. `buildBoardRows()` (`app.js:462-494`) creates those rows once during startup (`app.js:1458`), assigns each row a permanent `data-index`, and stores element/text caches in `board.rows`.
- `board` (`app.js:396-403`) owns the inferred tick size, `topLevel` (the highest price level represented by row 0), the row pool, per-level synthetic quantities, spread, and last trade side.
- `levelOf()` and `priceOfLevel()` (`app.js:431-437`) convert between prices and integer tick levels. All other board behavior uses level identity, including pending orders (`app.js:656-663`, `697-713`), trade flashes (`app.js:600-608`), and quantity invalidation (`app.js:611-625`).
- `anchorBoard(centerLevel)` (`app.js:497-507`) sets `topLevel = centerLevel + 20`, rewrites all 41 price cells, and sets `scrollTop` so DOM row 20 is centered in the viewport.
- `updateBoard()` (`app.js:557-597`) derives the current, best ask, and best bid levels; maps each row as `topLevel - index`; paints quantities, side/last classes, pending orders, and the quote. It calls `anchorBoard(lastLevel)` when there is no anchor, **板固定** is off, or the current price leaves the represented 41-level range (`app.js:563-573`).
- Replay calls `applyTradesToBoard()` and therefore `updateBoard()` whenever new ticks arrive (`app.js:1118-1143`). A seek/reset rebuild nulls `topLevel` and redraws (`app.js:1063-1102`). Session loading infers the tick, clears board state, and seeks to the pre-open position (`app.js:1307-1324`).

### Current scrolling limit

- The only board scroll write is the programmatic center in `anchorBoard()` (`app.js:503-506`). There is no `scroll`, `wheel`, pointer, or touch listener for `els.board`.
- Consequently, native scrolling changes only which subset of the fixed 41 rows is visible. It does not change `board.topLevel`, row price text, quantities, orders, or flash mapping. A tall viewport may show all rows and have no scroll range at all.
- A robust infinite ladder should keep a bounded DOM pool and recycle/rebase price levels near either scroll edge, restoring `scrollTop` by the same row offset so the visible prices do not jump. Unbounded row append/prepend would leak DOM nodes and, potentially, per-level state during long sessions.
- `topLevel` currently serves two roles: render origin and auto-reanchor boundary. Infinite manual navigation needs those concerns separated (or an explicit manual-navigation state), otherwise the next replay tick will call `updateBoard()`, see the current price outside the rebased 41-level window, and snap back to the current price.

### Centering and event flow

- **板固定** changes currently null `topLevel` and call `updateBoard()` (`app.js:1416-1419`). **中央へ** does exactly the same (`app.js:1421-1424`).
- Order entry uses one delegated `click` listener on `els.board`, but it accepts only `.board-order` cells (`app.js:1426-1432`). A delegated `dblclick` listener restricted to `.board-price` will not place orders; calling a shared centering function prevents button/double-click behavior from drifting apart.
- Double-click generates ordinary click events first, but those price-cell clicks are ignored by the existing order listener. The double-click handler should still prevent default text selection, or the price cell CSS should be non-selectable.

### Tick inference and formatting

- `TICK_CANDIDATES` is `[0.1, 0.5, 1, 5, 10, 50, 100, 500, 1000]` (`app.js:61`). `inferTickSize()` (`app.js:406-429`) examines at most the first 4,000 trades, finds the smallest positive difference between unique prices, and selects the nearest candidate by log-distance. `loadSession()` stores that result in `board.tick` at `app.js:1317`.
- The global `priceFormat` is `Intl.NumberFormat('ja-JP', { maximumFractionDigits: 2 })` (`app.js:101-102`). Its default minimum is zero, which intentionally removes trailing zeros.
- Board row prices use that formatter in `anchorBoard()` (`app.js:500`), and the best ask/bid quote uses it in `updateBoard()` (`app.js:593-596`). Thus a 0.5-yen ladder renders integer levels without `.0` and half levels with `.5`.
- The formatter is also used by tape prices (`app.js:342-358`) and portfolio average prices (`app.js:812-838`, `854-875`). Changing the global formatter would broaden the user-visible change beyond the board. Prefer a board-specific formatter derived after `board.tick` is inferred.
- Inference cannot discover an exchange tick that never appears in the sampled trades. A day whose first 4,000 trades contain only whole-yen moves may infer `1` even if the instrument permits `0.5`. There is no tick-size metadata contract in the current session payload; this limitation should remain explicit unless scope expands to backend/reference-data work.

## Affected Files and Complexity

Recommended implementation surface:

| File | Change | Reason |
| --- | --- | --- |
| `src/tickreplay/static/app.js` | Modify | Wire bounded ladder rebasing, shared centering, delegated price-cell double-click, board-only formatter, and preserve fixed/unfixed replay semantics. |
| `src/tickreplay/static/board-ladder.mjs` | New (recommended) | Keep row/level rebasing, center calculations, and tick-to-decimal policy DOM-free and directly testable. `app.js` is already 1,482 lines, while existing complex frontend state is isolated in pure modules. |
| `src/tickreplay/static/board-ladder.test.mjs` | New | Cover repeated bidirectional shifts, no-gaps mapping, centering, decimal policy, and boundary behavior with `node:test`. |
| `src/tickreplay/static/styles.css` | Modify, small/optional | Prevent price text selection and optionally expose the price column's double-click affordance; virtual-spacer styling is needed only if the chosen design uses spacers rather than scroll rebasing. |
| `docs/tick-replay.md` | Modify | Update the control table and board behavior at lines 164-175 and 218-238, plus the static module/test inventory at lines 288-336. |

No required changes:

- `src/tickreplay/static/index.html`: existing IDs/classes are sufficient.
- `src/tickreplay/server.py`: `/static` already mounts the whole directory (`server.py:494-508`) and explicitly registers `.mjs` as JavaScript.
- `tests/test_tickreplay_server.py`: existing tests cover index/static serving and `.mjs` MIME (`tests/test_tickreplay_server.py:305-331`). Extending the MIME test to the new module is optional, not a new server behavior.

Existing test conventions:

- Pure frontend logic lives in sibling `.mjs` modules and is imported directly from sibling `*.test.mjs` files using `node:test` and `node:assert/strict` (`paper-trading.test.mjs:1-18`; `minute-history.test.mjs:1-15`).
- When wiring inside `app.js` must be guarded without a browser DOM, tests read `app.js` and extract/assert function source (`request-coordinator.test.mjs:24-36,252-290`; `minute-history.test.mjs:15-30,601-803`). A small source-seam assertion can verify that both **中央へ** and `.board-price` double-click call the same centering function.

**Complexity: MODERATE.** The feature skill defines MODERATE as 3–5 files. A robust implementation is expected to touch 4–5 files, approximately 110–170 production/doc LOC and 120–180 test LOC. Keeping all behavior inside `app.js` might reduce the file count, but would make scroll invariants and decimal policy hard to test and would extend an already oversized file.

## Risks

- **Manual scroll vs replay auto-reanchor:** rebasing `topLevel` away from the current price will immediately trigger the existing out-of-range recenter condition on the next trade. Preserve current auto-follow when **板固定** is off, but keep a user-selected ladder position stable when it is on until **中央へ**, price double-click, seek/reset, session load, or an explicitly chosen boundary policy resets it.
- **Row identity and order correctness:** after every rebase, visible text, `data-index`, pending-order cells, `placeOrder()` level calculation, `flashBoardLevel()`, and ask/bid classes must all agree on the same `topLevel - index` mapping. A visually correct ladder with stale order mapping can submit at the wrong price.
- **Programmatic scroll recursion/jumps:** center and edge-rebase writes to `scrollTop` can fire the same listener used for user scrolling. Use a suppression guard and restore by an exact row-height/row-count offset so the price under the pointer remains stable.
- **DOM/performance growth:** do not implement “infinite” by appending forever. Use a constant-size pool/buffer and bound any scroll-only state. Replay may run at x500 and calls board updates per animation frame.
- **Price floor:** literal downward infinity produces zero/negative share prices. Define the lowest valid level (normally positive one-tick price) and test it; upward scrolling can remain unbounded for practical purposes.
- **Tick inference gap:** trailing-zero formatting is only as correct as `inferTickSize()`. Do not silently claim exchange-authoritative tick sizes. Tests should use observed `1000`/`1000.5` data, and documentation should retain the inference caveat.
- **Formatting blast radius:** keep the new minimum fraction digits board-specific. Mutating the shared `priceFormat` would also change tape and portfolio displays, which the request does not require.
- **Existing dirty work must be preserved:** before this scan, `app.js` had only the user change from default `7203` to `285A` (`app.js:1468-1470`), and `index.html` had the position panel moved after the picker plus its `285A` placeholder. Do not overwrite or normalize those hunks. `README.md` and `cloud-run/main.py` are also already dirty and out of scope.

## Acceptance Tests

### Automated

1. **Tick-derived board decimals**
   - With prices including `1000` and `1000.5`, inference returns `0.5` and every board price/quote is formatted with exactly one digit: `1,000.0`, `1,000.5`, `1,001.0`.
   - Tick `0.1` also uses one digit; integer ticks use zero digits and retain grouping.
   - Verify the tape/global formatter remains unchanged unless separately approved.
2. **Infinite/bounded ladder mechanics**
   - Repeated upward and downward edge shifts produce contiguous integer levels with no duplicates/gaps and never increase the DOM row-pool size.
   - The scroll compensation keeps the same logical level at the same viewport offset after each rebase.
   - Hundreds/thousands of shifts do not hit a synthetic top/bottom; downward movement respects the chosen positive-price floor.
3. **Center equivalence**
   - A shared centering operation maps the current `lastLevel` to the middle row and restores the canonical center scroll position.
   - Source/wiring assertion: both `boardCenter` click and delegated `.board-price` double-click invoke that same operation.
4. **Fixed/auto-follow behavior**
   - With **板固定** checked, manual scrolling/rebasing survives subsequent `updateBoard()` calls while current price changes.
   - With it unchecked, new trades keep the current price centered.
   - Seek/reset/session load clears manual navigation and recenters, matching existing behavior.
5. **Order and flash mapping after rebase**
   - Clicking a sell/buy order cell after several rebases passes the level shown in that row to `placeOrder()`.
   - Pending-order quantities and a trade flash appear on the correct rebased row.
6. **Regression commands (PowerShell-safe explicit paths)**

   ```powershell
   node --test --test-isolation=none `
     src/tickreplay/static/board-ladder.test.mjs `
     src/tickreplay/static/paper-trading.test.mjs `
     src/tickreplay/static/request-coordinator.test.mjs `
     src/tickreplay/static/minute-history.test.mjs
   node --check src/tickreplay/static/app.js
   node --check src/tickreplay/static/board-ladder.mjs
   uv run pytest tests/test_tickreplay_server.py -q
   ```

Current baseline observed during this scan: frontend tests **60/60 pass** and `node --check src/tickreplay/static/app.js` exits 0. There is currently no automated board-scroll, centering, tick-inference, or board-decimal coverage.

### Manual browser cases

1. Load a known 0.5-yen instrument/session (the current dirty default is `285A`). Confirm the complete visible price column and 売/買 quote use one decimal, including whole levels ending in `.0`.
2. Load an integer-tick instrument and confirm board prices do not gain unnecessary decimals.
3. Pause, keep **板固定** checked, and wheel/trackpad upward and downward through many more than 41 levels. There must be no terminal blank edge, duplicate/gapped level, visible jump, or growing DOM row count.
4. Repeat during x1 and x500 replay. Manual ladder position must remain stable in fixed mode; unchecked mode must continue to follow/center the current price.
5. After scrolling away, double-click a price cell and compare against **中央へ**: current price row, `scrollTop`, colors, quantities, and quote must be identical.
6. After multiple rebases, place and cancel both buy and sell orders; verify the pending quantity is displayed at the clicked price and fills only at the intended price.
7. Verify wheel, mouse double-click, and trackpad behavior at responsive widths above and below the `1180px` layout breakpoint (`styles.css:656-663`).

## Artifact Path

`.agents/docs/research/feature-order-book-infinite-scroll-and-price-formatting-codebase.md`

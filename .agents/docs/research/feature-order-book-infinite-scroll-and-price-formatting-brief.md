## Feature Brief: Order-book infinite scrolling and price formatting

### Current State
- Architecture: `src/tickreplay/static/app.js` owns a fixed 41-row order-book DOM pool, tick-level mapping, replay-driven updates, centering, pending-order display, and trade flashes. The board `<ol>` already scrolls natively, but scrolling never rebases the represented price levels.
- Relevant files: `src/tickreplay/static/app.js`, `src/tickreplay/static/styles.css`, sibling DOM-free `.mjs` modules/tests, and `docs/tick-replay.md`. `index.html` and backend/API changes are not required.
- Patterns: Complex frontend state/calculations are isolated in DOM-free `.mjs` modules tested with `node:test`; `app.js` wires DOM and lifecycle events. A bounded DOM pool is preferred over unbounded node growth.

### Feature Goal
Make the price ladder continuously scrollable in both directions, make a double-click on any price cell perform exactly the same current-price recentering as the existing **中央へ** button, and render every board price with a consistent decimal place when the inferred tick is fractional (for example `1,000.0`, `1,000.5`).

### Scope
- Include: bounded/recycled row-pool rebasing near both scroll edges; exact scroll-position compensation; explicit manual-vs-follow state; shared current-price centering used by both button and price-cell double-click; board-only tick-derived decimal formatting for prices and the board quote; positive one-tick lower price floor; automated unit/wiring regression coverage; relevant board documentation.
- Exclude: exchange-authoritative tick-size metadata; backend/API/DuckDB changes; global tape or portfolio price-format changes; unbounded DOM row creation; changes to `index.html`, paper-trading business rules, or commit `85f381a`'s 285A/header behavior.

### Complexity Classification (from Codex)
- Classification: MODERATE
- Estimated files: 5 (`app.js`, new `board-ladder.mjs`, new `board-ladder.test.mjs`, `styles.css`, `docs/tick-replay.md`)
- Estimated LOC: 110-170 production/documentation LOC plus 120-180 test LOC
- Implementation route: Codex + review

### Integration Points
- Board rendering: `buildBoardRows()` / `anchorBoard()` / `updateBoard()` must keep visible price text, quantities, side classes, pending orders, and flashes aligned with `board.topLevel - rowIndex` after every rebase.
- Replay following: manual navigation remains stable while **板固定** is enabled; when it is disabled, replay retains the existing current-price follow behavior. Center, seek/reset, and session load clear manual navigation.
- Centering controls: a single shared current-price centering operation is called by both `boardCenter` click and delegated `.board-price` double-click; price-cell double-click never submits an order.
- Price formatting: board rows and the board ask/bid quote use a formatter derived from `board.tick`; the global `priceFormat` remains unchanged.
- Order entry: after arbitrary rebases, clicking a buy/sell cell must submit the exact price shown in that row.

### Risks
- Replay updates can snap a manually rebased ladder back to the current price unless render origin is separated from follow/manual state; test fixed ON/OFF explicitly.
- Programmatic `scrollTop` changes can recursively trigger edge rebasing or cause visual jumps; suppress programmatic scroll handling and compensate by exact row offsets.
- A visually correct ladder can still submit or display orders at stale levels; centralize level-window calculations and test orders/flashes after repeated rebases.
- Tick size is inferred from observed trades and is not exchange-authoritative; keep that limitation documented and avoid expanding scope to reference-data work.
- Current work may change concurrently; preserve commit `85f381a` and verify the actual diff base immediately before implementation.

### Success Criteria
- Repeated upward and downward navigation continues beyond the original 41 levels without blank terminal edges, gaps, duplicates, visible jumps, or DOM-row growth; downward navigation stops at a positive one-tick floor.
- With **板固定** on, manual board position survives replay updates; with it off, the existing current-price following behavior remains.
- A price-cell double-click produces the same board state and scroll position as **中央へ**, and does not place an order.
- For observed 0.5-yen ticks, all board prices and the board quote show one decimal including `.0`; integer-tick boards retain integer formatting; global tape/portfolio formatting is unchanged.
- Orders, pending quantities, and trade flashes remain mapped to the displayed price after multiple rebases.
- New board-ladder tests, all existing static Node tests, JavaScript syntax checks, and relevant server tests pass; manual x1/x500 wheel/trackpad checks are documented.

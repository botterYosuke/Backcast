# Agent State

## Main Agent

Claude Code

## Repository Identity

<!-- Managed by /init. Re-run /init to refresh. -->

_Not initialized yet. Run `/init` to populate._

Macro requirements and design live in [docs/DESIGN.md](docs/DESIGN.md).

## Progress Tracker

Rolling progress summary (latest 5 checkpoints): [PROGRESS.md](../PROGRESS.md)

<!-- Working state below is maintained by workflow skills and manual notes. -->

---

## Current Feature: Minute chart interaction and historical loading
<!-- orchestra:block-id: minute-chart-interaction-and-historical-loading -->

### Context

- Goal: Keep minute-chart pan and zoom under user control during replay, and load older candles on demand near the left edge.
- Key files: `src/tickreplay/static/app.js`, `src/tickreplay/static/minute-history.mjs`, `src/tickreplay/static/minute-history.test.mjs`, `docs/tick-replay.md`.
- Dependencies: existing Lightweight Charts runtime, `RequestCoordinator`, and `/api/minute-context`; no backend or package changes.
- Complexity: MODERATE.

### Architecture

- The replay loop follows only the tick chart; minute-chart resets use logical ranges only at session load, seek, and reset boundaries.
- `MinuteHistorySession` owns loading/ready identity, generation rejection, single-flight admission, user arming, exhaustion, cooldown, and bounded retry state.
- Older pages are normalized and bounded before merge, then staged and applied transactionally to candle/volume/range before canonical arrays are committed.

### Codex Validation

- The approved plan was implemented with TDD and reviewed through `team-execute --review-only` security, quality, and test reviewers.
- Post-fix quality and security re-review marked the cross-session race, response validation, apply atomicity, and page-size duplication findings resolved.

### Integration Points

- `/api/minute-context` supplies the initial 30-bar preload and older 200-bar pages.
- Minute logical-range callbacks trigger paging only after a real wheel, pointer, or touch interaction.
- Session changes cancel `minute-history`, advance generation, and reject old chart events and stale responses until the new session is ready.

### Decisions

- Preserve the user's viewport by shifting the logical range by the unique prepended count; never call `redrawAll()` for history prepend.
- Treat a non-empty all-invalid response as retryable failure, an empty valid response as exhaustion, and marker-only refresh failure as best-effort after page commit.
- Keep replay and paper-trading state outside history-page mutation and rollback boundaries.

### Validation and Remaining Risks

- Focused gates: JavaScript 60/60 passed; Python minute-context/server 27/27 passed; JavaScript syntax and diff checks passed.
- Repository-wide gate remains red from unrelated Windows/orchestration baseline issues (744 passed, 315 failed; ruff/format findings are outside this feature).
- Manual x1/x500 browser pan/zoom, multi-page prepend, and approximately 10,000 retained-bar responsiveness remain unverified because no Browser backend was connected.

---

## Current Feature: Order-book continuous scrolling and price formatting
<!-- orchestra:block-id: order-book-infinite-scroll-and-price-formatting -->

### Context

- Goal: Provide a bounded continuously scrollable order-book ladder, current-price centering from price-cell double-click, uniform tick-derived decimals, and price-stable pending-order display.
- Key files: `src/tickreplay/static/app.js`, `src/tickreplay/static/board-ladder.mjs`, `src/tickreplay/static/board-ladder.test.mjs`, `src/tickreplay/static/styles.css`, and `tests/test_tickreplay_server.py`.
- Complexity: MODERATE.

### Architecture

- A viewport-sized odd physical row pool is recycled across logical price levels; rebase compensation preserves visible logical prices without unbounded DOM growth.
- Scroll generation and programmatic-target tracking reject stale callbacks and reconcile wheel momentum that returns the physical viewport to an edge.
- Pending orders and flash state are keyed by logical price level and repainted into whichever physical row currently represents that level.

### Codex Validation

- Initial Codex planning consultations produced no response before timeout, so no Codex output was used as design evidence.
- Implementation was independently reviewed through `team-execute --review-only`; the reproduced inertial edge-stall and stale empty-session board findings were fixed before final validation.

### Integration Points

- `app.js` imports DOM-free ladder helpers for row sizing, level mapping, centering, rebase planning, scroll drift detection, pending quantity lookup, and board-only price formatting.
- The price-column `dblclick` handler and `[中央へ]` button share `centerBoardOnCurrent()`.
- The existing static-file server now explicitly tests the new `.mjs` module's JavaScript Content-Type.

### Decisions

- Keep the DOM bounded and move a logical price window; never append rows indefinitely.
- Treat scroll drift beyond the programmatic target as user/momentum input, mark manual navigation, and reconcile until physical runway is restored.
- Derive board decimal precision from the inferred tick while leaving tape and portfolio formatting unchanged.
- Preserve the existing all-or-nothing paper-fill model; this feature changes only how still-pending orders are mapped and displayed.

### Validation and Remaining Risks

- Focused gates: JavaScript 75/75 passed; tick-replay server 21/21 passed; JavaScript syntax and diff checks passed.
- Browser QA passed repeated large wheel gestures in both directions with a constant 53-row pool, pending sell-order off-screen/back restoration at the same price and side, identical button/double-click centering, and uniform `.0`/`.5` display for cached 7203 half-yen data.
- Security review found no Critical or High issue. Non-blocking hardening remains for malformed session numeric validation and an explicit retention cap on synthetic `board.qty`.
- Repository-wide gates remain affected by pre-existing Windows/orchestration baseline failures; the shared bash verifier cannot start because this host lacks WSL `/bin/bash`.

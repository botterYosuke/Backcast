# Design Document — 要件定義書 (Requirements & Macro Design)

> **Role:** Macro-level requirements and design — *what* this project builds and *why*.
> Written at `/init`, kept current by `/design-tracker` (also invoked from `/checkpointing`).
>
> **Document map:** Shared rules → [rules/](../rules/) ·
> Shared bootstrap → [AGENTS.md](../../AGENTS.md) · State → [STATE.md](../STATE.md) · Claude symlink → [CLAUDE.md](../../CLAUDE.md) ·
> Micro work progress (latest 5 checkpoints) → [PROGRESS.md](../../PROGRESS.md)

## 背景・目的 (Background & Purpose)

<!-- Why does this project exist? What problem does it solve, for whom?
     State the business/technical context and the goal in a few sentences. -->

## スコープ (Scope)

### In Scope

<!-- What this project explicitly delivers. -->

- 

### Out of Scope

<!-- What is explicitly NOT covered, to prevent scope creep. -->

- 

## 機能要件 (Functional Requirements)

<!-- What the system must do. Each requirement gets a stable ID (FR-1, FR-2, ...). -->

| ID | Requirement | Priority | Notes |
|----|-------------|----------|-------|
| FR-1 | | | |
| FR-TICKREPLAY-1 | Keep minute-chart pan and zoom usable while replay continues without overwriting the user-selected viewport. | High | Tick-chart following remains active; minute positioning is reset only for session load and explicit seek/reset. |
| FR-TICKREPLAY-2 | Load older minute candles on demand when the user navigates near the oldest loaded candle. | High | Use the existing strict-before /api/minute-context endpoint with single-flight, stale-response rejection, deduplication, and viewport preservation. |
| FR-TICKREPLAY-3 | Preserve every unfilled order at its original price level while the order-book ladder scrolls outside and back into that price range. | High | Pending-order state is independent of recycled DOM rows. When a price level becomes visible again, the sell/buy column must repaint the correct remaining quantity at that same price; filled or cancelled orders must not reappear. |
| FR-TICKREPLAY-4 | Allow continuous upward and downward navigation through the order-book price ladder without unbounded DOM growth. | High | Recycle a viewport-sized bounded row pool with overscan, preserve the visible price during rebase, and stop downward navigation at the first positive tick level. |
| FR-TICKREPLAY-5 | Make a double-click on a board price cell perform exactly the same current-price centering operation as the Central button. | High | The clicked price is not the center target and the gesture must never place an order. |
| FR-TICKREPLAY-6 | Render board prices and the board quote with a consistent tick-derived decimal precision. | High | Ticks 0.1 and 0.5 use one decimal including .0; integer ticks use no decimals. Tape and portfolio formatting remain unchanged. |
| FR-TICKREPLAY-7 | Allow the upper price chart to switch between minute and daily views without changing replay, trading, tick-chart, board, tape, or minute-history state. | High | Each mode owns an independent chart instance, series, time scale, and viewport. |
| FR-TICKREPLAY-8 | Render daily history strictly before the actual replay session date and derive the selected-day candle only from ticks already replayed. | High | Use raw point-in-time OHLCV across legacy/current Code partitions; never expose the stored selected-day final OHLC. |
| FR-TICKREPLAY-9 | Display SMA25 and SMA200 in daily mode using valid raw daily closes, including a non-empty partial day as one observation. | High | Emit the first point only at 25 or 200 observations; insufficient history produces no premature line. |
| FR-TICKREPLAY-10 | Show Daily mode initially with the newest 90 bars and 5 logical bars of right padding, then load older daily bars when the user pans or zooms near the oldest loaded bar. | High | Use user-armed single-flight paging, stale-response rejection, deduplication, exhaustion/cooldown, and exact logical-range compensation after prepend. |

## 非機能要件 (Non-Functional Requirements)

<!-- Quality attributes: performance, availability, security, maintainability, etc.
     Prefer measurable targets in the Metric column. -->

| Category | Requirement | Metric / Target |
|----------|-------------|-----------------|
| Performance | Bound daily history and avoid whole-history application materialization. | At most 500 completed sessions; LIMIT in DuckDB; replay updates only the partial candle and terminal SMA points. |
| Availability | | |
| Security | | |
| Maintainability | | |
| Correctness | Reject stale daily data and preserve all minute/replay side effects during chart-mode changes. | Generation, full session identity, and request token checked before commits; mode gates apply only to leaf chart writes. |
| Replay performance | Precompute historical SMA25/SMA200 series and rolling-window state when daily history loads; never rebuild full SMA arrays per replay tick. | Historical SMA calculation once per accepted daily payload; at most one O(1) terminal-point update per animation frame and once after seek/reset completion. |
| Daily history performance | Keep initial Daily history and every older page bounded while preserving O(1) replay-tick SMA updates. | Initial request at most 500 completed sessions; older pages 200 sessions; full historical SMA recalculation only once per accepted load or page. |

## アーキテクチャ (Architecture)

<!-- High-level architecture: components, data flow, boundaries.
     Add a diagram or description here. -->

### Agent Roles

| Agent | Role | Responsibilities |
|-------|------|------------------|
| | | |

- Tickreplay minute history: `app.js` owns chart/lifecycle wiring, while `minute-history.mjs` owns DOM-free paging state and merge/range calculations. Both initial preload and older-page requests share one session generation/token and cancellable request kind. History is prepended to both `contextBars` and `bars`; the visible logical range is shifted by the unique prepend count without mutating replay or paper-trading state.

- Tickreplay order-book scrolling: `app.js` owns DOM and replay lifecycle wiring, while `board-ladder.mjs` owns DOM-free row-count, rebase, level mapping, scroll compensation, navigation-state, and tick-precision calculations. Physical rows are repainted atomically from logical price levels so quantities, pending orders, flashes, and order-entry prices remain aligned.

- TickReplay daily chart: `daily_context.py` exposes bounded strict-before raw daily bars; `daily-chart.mjs` owns validation, duplicate rules, partial-day aggregation, SMA calculation, and request/session state; `app.js` wires a separate daily chart without suppressing existing replay side effects.

- Daily SMA performance: `daily-chart.mjs` computes immutable historical SMA25/SMA200 arrays and rolling window sums once when an accepted daily payload is loaded; replay folds partial OHLCV per tick but derives only the terminal SMA point at the frame boundary or after seek/reset.

- TickReplay Daily history paging: `daily-chart.mjs` owns DOM-free request admission, stale/cooldown/exhaustion state, page normalization, deduplication, SMA rebuild planning, and final canonical commit; `app.js` owns user-interaction arming and transactional four-series plus live/saved logical-range application.

## 技術選定 (Tech Stack & Rationale)

<!-- Chosen technologies and why. Record alternatives considered. -->

| Area | Technology | Rationale | Alternatives Considered |
|------|------------|-----------|-------------------------|
| Tick replay minute-chart viewport | Lightweight Charts logical-range APIs plus a native ES-module history controller | Logical ranges preserve manual navigation and allow exact +N viewport compensation after prepending older bars; pure controller logic remains testable with node:test. | Per-frame setVisibleRange/setVisibleLogicalRange; eager full-history loading; a new backend paging endpoint |
| Tick replay order-book ladder | Bounded recycled DOM rows plus a DOM-free native ES module | A finite viewport-sized pool provides continuous navigation without DOM growth, while pure level/rebase/format calculations remain deterministic and testable with node:test. | Append rows indefinitely; keep all ladder calculations inside app.js; add a backend paging API. |
| TickReplay daily chart and indicators | FastAPI plus bounded DuckDB query, RequestCoordinator, a DOM-free native ES module, and a separate Lightweight Charts instance | Matches existing repository patterns while keeping data validation, request state, SMA math, and chart ownership testable and isolated. | New dependencies; client-side full-history aggregation; shared minute/daily chart series. |
| TickReplay Daily history paging | Existing /api/daily-context strict-before requests plus DailyChartSession paging state and Lightweight Charts logical ranges | The endpoint already supports arbitrary cutoffs and bounded limits; logical-range +N compensation preserves a user-selected viewport after prepend. | New backend pagination schema; eager full-history loading; reuse MinuteChartSession directly. |

## 制約 (Constraints)

<!-- Technical, organizational, regulatory, or resource constraints. -->

- 

- Tickreplay minute history remains best-effort: an empty response is session-local exhaustion, failures must not stop replay, and implementation is limited to `app.js`, `minute-history.mjs`, its Node test, and `docs/tick-replay.md`.

- Daily history is limited to 500 completed sessions, uses no paging or daily trade markers, reads no adjustment columns, and adds no dependency, migration, or existing API-contract change.

- The previous no-paging limit for Daily history is superseded by FR-TICKREPLAY-10: each request remains bounded (500 initial, 200 older), uses the unchanged strict-before API, and adds no dependency or migration.

## Key Decisions

<!-- Durable architectural/design decisions. Append-only log. -->

| Decision | Rationale | Alternatives Considered | Date |
|----------|-----------|------------------------|------|
| Reuse /api/minute-context for best-effort historical paging without changing the backend schema. | The existing endpoint already returns chronological bars strictly before an arbitrary cutoff and supports bounded limits up to 500. | Add a new pagination endpoint or extend the response with explicit exhaustion/error status | 2026-08-22 |
| Separate replay progression from minute-chart viewport control and isolate history calculations/controller state in minute-history.mjs. | The current per-frame minute range write causes the interaction lock; a testable controller also centralizes generation, single-flight, retry, merge, and programmatic-range suppression invariants. | Keep all logic in the oversized app.js or disable chart interaction while replaying | 2026-08-22 |
| Key unfilled order state by logical price level rather than by physical board row. | Infinite scrolling recycles physical rows. Repainting each visible row from its logical level keeps pending quantities at the correct order price after any number of rebases and prevents stale quantities from following a reused row. | Store pending quantities in DOM rows or discard off-screen order display state during rebase. | 2026-08-22 |
| Separate manual ladder navigation from current-price following and compensate every level rebase with the matching scroll offset. | The existing topLevel combines render origin and follow behavior. Explicit navigation state prevents replay from snapping a board-fixed manual viewport back to the current price and exact compensation avoids visible jumps. | Always recenter on replay updates or allow native scrolling only within the original fixed rows. | 2026-08-22 |
| Use one shared centerBoardOnCurrent operation for both the Central button and delegated price-cell double-click. | A single action guarantees identical state reset, price anchoring, and scroll positioning while keeping price-cell gestures separate from order submission. | Duplicate handlers or center on the clicked price. | 2026-08-22 |
| Use a board-specific formatter derived from the inferred tick instead of changing the global price formatter. | This produces uniform .0/.5 ladder output without changing tape, chart, or portfolio presentation outside the requested scope. | Change the global formatter or add exchange tick metadata to the backend. | 2026-08-22 |
| Use an explicit local_authoritative mode only when the DuckDB cache path is the same authoritative tree served by the file server | An existing file in that shared tree is already the origin object, so downloading or conditionally revalidating it through loopback can only rewrite the source and destabilize mtime-derived validators. Default remote caches must continue normal conditional revalidation and must never infer byte identity from Content-Length and Last-Modified metadata. | Generic adoption based on size and Last-Modified; hashing multi-gigabyte local files; unconditional self-download | 2026-08-22 |
| Serialize DuckDB operations by resolved cache destination across repository instances instead of sharing staged download results | A staged .part file is move-only and remains consumable until commit, so stage-only coalescing can leak conditional results or let multiple consumers overwrite or move the same artifact. A process-wide (resolved cache_dir, stem) repository lock protects revalidation, staging, commit, and query as one lifecycle while leaving different destinations independent. | Stem-only or request-identity download coalescing; ref-counted staged-file leases; request-unique staging artifacts | 2026-08-22 |
| Use a separate lazily created and retained Lightweight Charts instance for daily candles, volume, SMA25, and SMA200. | Structural ownership prevents replay and minute-history writes from corrupting daily data and preserves independent viewports. | Reuse the minute series with setData; recreate the chart on every switch. | 2026-08-30 |
| Use official per-stem stocks_daily files and a new strict-before /api/daily-context contract with an explicit available flag. | The source supplies enough history for SMA200, while available distinguishes valid empty history from missing or corrupt supplementary data. | Aggregate minute data; return an ambiguous empty bars response. | 2026-08-30 |
| Use raw daily OHLCV and raw replay ticks for point-in-time consistency. | Selected-day adjustment ratios require completed-day or future corporate-action information; raw values avoid that look-ahead. | Adjusted history with a selected-day ratio; future-informed adjusted series. | 2026-08-30 |
| Preserve replay and trading side effects in both chart modes and isolate only direct upper price-chart writes. | Whole-function mode guards around step, redrawAll, refreshMarkers, seek, or history completion would stop unrelated behavior. | Pause replay updates in daily mode; guard entire rendering functions. | 2026-08-30 |
| Precompute historical SMA25/SMA200 series and rolling-window state at daily-data load time. | The initial implementation rebuilt both arrays for every tick and made a 10,000-tick replay take about 22.6 seconds. Load-time precomputation keeps replay responsive. | Recompute full SMA arrays per tick; recompute full arrays once per frame. | 2026-08-30 |
| Use precomputed windows for only the live terminal SMA point, batched once per animation frame and once after seek/reset. | This preserves the approved replay-derived partial-day indicator without repeating historical work. | Exclude the partial day entirely; update the terminal SMA on every tick. | 2026-08-30 |
| Keep the initial 500-session Daily fetch for SMA200 warm-up, display only the newest 90 bars with 5 logical bars of right padding, and page older sessions through the existing strict-before endpoint. | Separating loaded history from the visible range preserves correct SMA200 context and avoids a backend contract change while matching the Minute viewport rule. | Fetch only 90 visible bars; fetch 90 plus a 199-bar warm-up; keep Daily limited to one 500-bar response with no paging. | 2026-08-30 |

## TODO / Open Questions

<!-- Open design questions and deferred decisions for this project. -->

- 

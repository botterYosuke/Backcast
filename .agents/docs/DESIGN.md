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

## 非機能要件 (Non-Functional Requirements)

<!-- Quality attributes: performance, availability, security, maintainability, etc.
     Prefer measurable targets in the Metric column. -->

| Category | Requirement | Metric / Target |
|----------|-------------|-----------------|
| Performance | | |
| Availability | | |
| Security | | |
| Maintainability | | |

## アーキテクチャ (Architecture)

<!-- High-level architecture: components, data flow, boundaries.
     Add a diagram or description here. -->

### Agent Roles

| Agent | Role | Responsibilities |
|-------|------|------------------|
| | | |

- Tickreplay minute history: `app.js` owns chart/lifecycle wiring, while `minute-history.mjs` owns DOM-free paging state and merge/range calculations. Both initial preload and older-page requests share one session generation/token and cancellable request kind. History is prepended to both `contextBars` and `bars`; the visible logical range is shifted by the unique prepend count without mutating replay or paper-trading state.

- Tickreplay order-book scrolling: `app.js` owns DOM and replay lifecycle wiring, while `board-ladder.mjs` owns DOM-free row-count, rebase, level mapping, scroll compensation, navigation-state, and tick-precision calculations. Physical rows are repainted atomically from logical price levels so quantities, pending orders, flashes, and order-entry prices remain aligned.

## 技術選定 (Tech Stack & Rationale)

<!-- Chosen technologies and why. Record alternatives considered. -->

| Area | Technology | Rationale | Alternatives Considered |
|------|------------|-----------|-------------------------|
| Tick replay minute-chart viewport | Lightweight Charts logical-range APIs plus a native ES-module history controller | Logical ranges preserve manual navigation and allow exact +N viewport compensation after prepending older bars; pure controller logic remains testable with node:test. | Per-frame setVisibleRange/setVisibleLogicalRange; eager full-history loading; a new backend paging endpoint |
| Tick replay order-book ladder | Bounded recycled DOM rows plus a DOM-free native ES module | A finite viewport-sized pool provides continuous navigation without DOM growth, while pure level/rebase/format calculations remain deterministic and testable with node:test. | Append rows indefinitely; keep all ladder calculations inside app.js; add a backend paging API. |

## 制約 (Constraints)

<!-- Technical, organizational, regulatory, or resource constraints. -->

- 

- Tickreplay minute history remains best-effort: an empty response is session-local exhaustion, failures must not stop replay, and implementation is limited to `app.js`, `minute-history.mjs`, its Node test, and `docs/tick-replay.md`.

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

## TODO / Open Questions

<!-- Open design questions and deferred decisions for this project. -->

- 

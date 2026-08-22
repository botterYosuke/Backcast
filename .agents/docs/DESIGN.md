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

## 技術選定 (Tech Stack & Rationale)

<!-- Chosen technologies and why. Record alternatives considered. -->

| Area | Technology | Rationale | Alternatives Considered |
|------|------------|-----------|-------------------------|
| Tick replay minute-chart viewport | Lightweight Charts logical-range APIs plus a native ES-module history controller | Logical ranges preserve manual navigation and allow exact +N viewport compensation after prepending older bars; pure controller logic remains testable with node:test. | Per-frame setVisibleRange/setVisibleLogicalRange; eager full-history loading; a new backend paging endpoint |

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

## TODO / Open Questions

<!-- Open design questions and deferred decisions for this project. -->

- 

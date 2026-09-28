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
| FR-NATIVE-TRACE-1 | Execute explicitly selected trusted Python strategy source in a configured native executor and return an immutable sparse decision trace for browser replay. | High | Default off; cursor-zero; no per-tick network; proposals only; existing paper broker remains authoritative. |
| FR-NATIVE-TRACE-2 | Preserve existing Pyodide strategy execution and ordinary replay when native execution is disabled, unavailable, cancelled, or fails. | High | No implicit mode fallback within an active run. |
| FR-BUNDLED-RL-1 | Run the selected _moomoo RL policy from assets and runtime code contained entirely in this repository. | High | No runtime read/import from _moomoo; exact checkpoint bytes and NumPy version are pinned and verified. |
| FR-BUNDLED-RL-2 | Select the bundled RL Native strategy by default without overwriting or automatically executing the user's saved custom strategy. | High | The user still performs explicit Load and Start; saved source remains available as Saved strategy. |
| FR-BUNDLED-RL-3 | Expose only an exact-preset, source-free fixed Native API for Bundled RL and reset obsolete strategy selections to Bundled RL. | High | This supersedes custom Native v1/v2 compatibility requirements; Custom Python remains browser-Pyodide only. |

## 非機能要件 (Non-Functional Requirements)

<!-- Quality attributes: performance, availability, security, maintainability, etc.
     Prefer measurable targets in the Metric column. -->

| Category | Requirement | Metric / Target |
|----------|-------------|-----------------|
| Performance | Bound daily history and avoid whole-history application materialization. | At most 500 completed sessions; LIMIT in DuckDB; replay updates only the partial candle and terminal SMA points. |
| Availability | | |
| Security | The FastAPI process never imports, compiles, evaluates, or executes submitted source; v1 native execution is trusted/local-only and is not represented as a sandbox. | Default-off and Cloud Run-disabled configuration; direct worker contract tests are the only normal tests that execute harmless fixture source. |
| Maintainability | | |
| Correctness | Reject stale daily data and preserve all minute/replay side effects during chart-mode changes. | Generation, full session identity, and request token checked before commits; mode gates apply only to leaf chart writes. |
| Replay performance | Precompute historical SMA25/SMA200 series and rolling-window state when daily history loads; never rebuild full SMA arrays per replay tick. | Historical SMA calculation once per accepted daily payload; at most one O(1) terminal-point update per animation frame and once after seek/reset completion. |
| Daily history performance | Keep initial Daily history and every older page bounded while preserving O(1) replay-tick SMA updates. | Initial request at most 500 completed sessions; older pages 200 sessions; full historical SMA recalculation only once per accepted load or page. |
| Browser Python lifecycle correctness | No stale Load publication, unresolved superseded request, or orphan runtime across concurrent Load/Stop/dispose. | Deferred admission/hash/invalidation and stale response/error/timer matrix; all promises settle; each discarded Worker terminates once and every created Worker terminates exactly once after final shutdown. |
| Browser Python transactional correctness | Accepted state remains finite and representable by every Worker ABI integer/price/minor-unit field, including no-fill valuation changes. | Boundary and boundary+1 tests for both sides and aggregates; typed rejection before swap/postMessage leaves portfolio, orders, fills, events, IDs, cursor, and ledgers unchanged. |
| Browser Python release verification | Qualify actual production Worker/Python/replay/broker behavior and preserve immutable release identities. | Real browser fixture proves event-i order fills only at i+1 with exact portfolio/cursor, cancellation, Stop/restart, Load-only execution, and Python error handling; retained-release byte integrity and new canonical reconstruction pass before merge/promotion. |
| Correctness | Trace identity, lifecycle, sparse decision ordering, and next-tick causality remain deterministic across cancellation, reload, and cache reuse. | Canonical SHA-256 identities; atomic verified publish; repeated byte-identical GET; observed tick i is eligible only at i+1. |
| Bundled RL correctness | Use actual committed broker position to select a causal next-tick target and fail closed for unsafe portfolio, order, runtime, or artifact state. | Only positions/targets 0 or 100; at most one market command; proposal at tick i can fill only at i+1; retained releases remain byte-identical. |
| Bundled RL contract simplicity | Maintain one Native trace schema and reject source, executor, mode, stale preset, and unknown fields before any repository, cache, job, lease, child, or artifact side effect. | No native_trace_v1/v2 dual dispatch or custom Native executor settings remain in active code; exact-preset negative tests are side-effect-zero. |

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

- Browser Python review remediation: workspace and controller reserve Load generations synchronously and keep candidate Worker/request/interrupt/finalization resources owned by each attempt. The broker/replay boundary validates the complete candidate portfolio and exact Worker snapshot before atomic state publication, including no-fill valuation. The UI describes immediate trusted-code execution at Load. Canonical runtime changes produce a new immutable release; actual production Worker integration is a required acceptance gate. The active implementation order and acceptance matrix are recorded in `.agents/docs/plans/pyodide-replay-strategy.md`, Review remediation (2026-09-14).

- Native Python strategy traces: the browser submits an immutable source/session identity once, FastAPI schedules a configured external worker without executing source, the worker consumes causal tick snapshots and emits a sparse proposal trace, and the browser replays the verified trace through the existing next-tick transactional paper broker.

- Bundled RL default strategy: a versioned read-only browser preset requests a content-addressed native_trace_v2 artifact. A fixed child process loads the repository-owned JSON checkpoint, computes causal position-target branches for flat and long states, and returns proposals only. After each broker commit, the browser selects the branch for the actual position and stages any target delta for the next tick. Custom Native v1 remains a separate unchanged compatibility path.

- Bundled RL final form (supersedes the earlier compatibility paragraph): the product selector is `Bundled RL | Custom Python`. Bundled RL is the default fixed Native strategy and uses the sole `bundled_rl_trace_v1` exact-preset API with no source submission. Custom Python is explicitly selected and remains entirely in browser Pyodide. The earlier custom Native v1 preservation and native_trace_v2 dual-path statements are historical and no longer apply.

## 技術選定 (Tech Stack & Rationale)

<!-- Chosen technologies and why. Record alternatives considered. -->

| Area | Technology | Rationale | Alternatives Considered |
|------|------------|-----------|-------------------------|
| Tick replay minute-chart viewport | Lightweight Charts logical-range APIs plus a native ES-module history controller | Logical ranges preserve manual navigation and allow exact +N viewport compensation after prepending older bars; pure controller logic remains testable with node:test. | Per-frame setVisibleRange/setVisibleLogicalRange; eager full-history loading; a new backend paging endpoint |
| Tick replay order-book ladder | Bounded recycled DOM rows plus a DOM-free native ES module | A finite viewport-sized pool provides continuous navigation without DOM growth, while pure level/rebase/format calculations remain deterministic and testable with node:test. | Append rows indefinitely; keep all ladder calculations inside app.js; add a backend paging API. |
| TickReplay daily chart and indicators | FastAPI plus bounded DuckDB query, RequestCoordinator, a DOM-free native ES module, and a separate Lightweight Charts instance | Matches existing repository patterns while keeping data validation, request state, SMA math, and chart ownership testable and isolated. | New dependencies; client-side full-history aggregation; shared minute/daily chart series. |
| TickReplay Daily history paging | Existing /api/daily-context strict-before requests plus DailyChartSession paging state and Lightweight Charts logical ranges | The endpoint already supports arbitrary cutoffs and bounded limits; logical-range +N compensation preserves a user-selected viewport after prepend. | New backend pagination schema; eager full-history loading; reuse MinuteChartSession directly. |
| Browser Python strategy editor | CodeMirror 6 with Python language support | Provides the line numbers, syntax highlighting, indentation, search, diagnostics integration, and keyboard editing needed for a usable single-file strategy workflow while keeping the editor modular and independent from the Pyodide execution Worker. | Plain textarea for a technical spike; Monaco Editor for a future multi-file or language-server-backed IDE; external editor plus Python file import/export only |
| Native strategy execution | Fixed-argv native Python worker with framed JSON plus content-addressed trace store | Keeps NumPy/model dependencies outside the web process and supports bounded cancellation, parent validation, and deterministic cache reuse. | Browser Pyodide adapter; ONNX Runtime Web; per-tick inference service |
| Bundled RL inference | Pinned NumPy 2.4.5, unchanged JSON MLP checkpoint, and a fixed-argv native_trace_v2 child worker | Matches the selected upstream model exactly while remaining small, deterministic, and independent of _moomoo at runtime. | Pyodide inference; ONNX conversion; Torch/SB3; per-tick HTTP inference. |

## 制約 (Constraints)

<!-- Technical, organizational, regulatory, or resource constraints. -->

- 

- Tickreplay minute history remains best-effort: an empty response is session-local exhaustion, failures must not stop replay, and implementation is limited to `app.js`, `minute-history.mjs`, its Node test, and `docs/tick-replay.md`.

- Daily history is limited to 500 completed sessions, uses no paging or daily trade markers, reads no adjustment columns, and adds no dependency, migration, or existing API-contract change.

- The previous no-paging limit for Daily history is superseded by FR-TICKREPLAY-10: each request remains bounded (500 initial, 200 older), uses the unchanged strict-before API, and adds no dependency or migration.

- Native Trace v1 is trusted/local-only, disabled by default, unavailable on Cloud Run, cursor-zero-only, mutually exclusive with manual/Pyodide execution, and does not support live brokerage or untrusted multi-tenant code.

- Superseding constraint (2026-09-28): Native Trace v1 compatibility, custom executor argv/trust configuration, and source-bearing Native requests are intentionally removed. Retained release directories remain immutable for integrity, not compatibility.

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
| Use CodeMirror 6 for the first user-facing browser Python strategy editor and keep Monaco Editor out of scope until multi-file navigation or language-server-backed IDE features are required. | The strategy workflow needs a practical code editor but not a full IDE. CodeMirror 6 supplies modular Python editing features with less worker, asset, CSP, and mobile integration complexity than Monaco, while the execution revision state remains editor-agnostic. | Ship a plain textarea; adopt Monaco Editor immediately; require all editing in an external IDE | 2026-09-10 |
| Browser Python Load uses last-synchronously-accepted-wins admission with attempt-local lifecycle ownership. | Reserve workspace/controller generations before asynchronous acquisition, hashing, or invalidation; Stop/dispose invalidates pending admission. Stale work must settle and finalize only its own Worker, requests, interrupt state, and timers without publishing newer state or orphaning a Worker. | Rely on disabled UI controls, reserve after awaits, or clean up through a shared mutable Worker field. | 2026-09-13 |
| Validate complete candidate portfolio and Worker ABI numeric closure before strategy transaction swap or message dispatch. | Individually valid orders can overflow aggregate position or derived minor-unit cash/P&L/equity. Price-only movement also needs validation. Reject with typed error and full transaction rollback, retaining manual-only accounting behavior and refusing invalid Start snapshots. | Validate only input quantities; clamp values; detect invalid portfolio after committing a fill; rewrite all legacy accounting. | 2026-09-13 |
| Make trusted-code and immediate Load execution warnings persistent and accessible at the execution controls. | Top-level Python executes before Start in a same-origin Worker with browser capabilities. Load and the active editor input must describe the trust boundary in enhanced and degraded modes while preserving plaintext-storage disclosure. | Runbook-only warning, storage-only notice, repeated confirmation dialogs, or representing a Worker as a security sandbox. | 2026-09-13 |
| Require new immutable release reconstruction and actual production Worker event-to-fill integration for Python remediation acceptance. | Canonical changes must generate a new content-addressed closure without modifying retained releases. Fake Worker and native Python checks cannot prove production Python serialization, host commit, and next-tick eligibility work together. | Edit retained release bytes; rely only on mocked Worker tests or positive asset hash checks. | 2026-09-13 |
| Use cache-first session batch execution and immutable sparse traces rather than per-tick HTTP or WebSocket inference. | Replay input is already complete; batch execution improves determinism, seek/reset handling, throughput, and auditability. | Pyodide-only inference; per-tick native RPC; server-owned broker simulation | 2026-09-28 |
| Keep fills, cash, positions, and portfolio mutation out of trace artifacts and retain replay-commit/order-broker as the sole execution authority. | A trace records proposals, while existing validated host transactions preserve next-tick eligibility and atomic rollback. | Replay recorded fills; let executor mutate portfolio; duplicate broker semantics in Python | 2026-09-28 |
| Ship native_trace_v1 as default-off trusted/local-only execution with a separately configured external executor. | A child process provides lifecycle isolation but is not a security sandbox; public or multi-tenant arbitrary-code execution requires a future isolation boundary. | Exec inside FastAPI; claim subprocess sandboxing; enable on Cloud Run | 2026-09-28 |
| Bundle the selected _moomoo NumPy checkpoint and run it only in a fixed native child process, while keeping FastAPI free of NumPy, model parsing, and submitted-source execution. | The selected checkpoint is a small JSON MLP and can be made repository-self-contained without Pyodide or the upstream training repository; the child boundary preserves the existing web-process trust boundary. | Load _moomoo at runtime; execute inference in Pyodide; import NumPy and the model in FastAPI. | 2026-09-28 |
| Add bundled-only native_trace_v2 position-target branches while preserving the complete custom native_trace_v1 path unchanged. | The RL policy depends on the broker-committed position, so the immutable trace must provide both valid branches and let the existing browser broker remain the sole fill and portfolio authority. | Record assumed fills in Python; wrap custom v1 commands in v2; duplicate broker semantics in the worker. | 2026-09-28 |
| Default fresh and unselected profiles to a read-only Bundled RL preset in Native mode while preserving saved custom source bytes and retaining explicit Load then Start actions. | The application should present RL by default without silently executing code on mount or overwriting the user's existing strategy source. | Replace the saved source record; auto-run on page load; silently fall back to Pyodide when native capability is unavailable. | 2026-09-28 |
| Supersede the custom Native v1/v2 compatibility design with one fixed bundled_rl_trace_v1 server contract; Native accepts exact preset identity and never accepts source, executor argv, or a client runtime fingerprint. | The user explicitly waived backward compatibility. A single fixed contract is safer and simpler, while Custom Python remains an explicit browser-Pyodide product strategy. | Preserve custom Native v1; maintain v1/v2 dual dispatch; keep source-bearing Native requests. | 2026-09-28 |
| Use a single strategy selector with Bundled RL as the reset/default and Custom Python as the explicit alternative; obsolete or malformed selection records reset instead of migrating. | Strategy and runtime are one product choice in the final form, and compatibility-only state machinery no longer adds value. | Separate strategy/runtime selectors; preserve every old selection record; overwrite the Custom Python source store. | 2026-09-28 |

## TODO / Open Questions

<!-- Open design questions and deferred decisions for this project. -->

- 

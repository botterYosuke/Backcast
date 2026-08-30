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

---

## Current Bug Fix: local-authoritative-cache-download
<!-- orchestra:block-id: local-authoritative-cache-download -->

### Context

- Incident: a fresh app process selected 285A, entered pending/status polling, and the user observed a DuckDB transfer despite an existing-cache claim.
- Root cause of operation entry: normal-cache policy (`local_authoritative=false` or unset) intentionally revalidates each stem once per fresh repository/process.
- Full-transfer cause: not uniquely recoverable after replacement; missing/unusable/validator-less sidecar is leading, with exact-path absence/mismatch and changed remote validator viable.
- Affected files: src/tickreplay/config.py, cache.py, repository.py, server.py, static/app.js, and request-coordinator.mjs.

### Fix Approach

- Preserve freshness semantics and add observability that distinguishes checking/304 from 200 body transfer, stale fallback, and corruption repair.
- Isolate focused tests from the ignored root .env.
- Defer strong-digest sidecar bootstrap as a protocol design; reject trusting every ordinary cache forever.

### Codex Validation

- One initial read-only Codex consultation completed and agreed with the two-stage conclusion.
- Later mandatory plan/risk/fix consultations hung past their bounds or received an incomplete Windows prompt and were interrupted; they are unverified and contribute no evidence.

### Regression Risks

- Reusing local_authoritative=true for a remote copy can permanently hide remote market-data updates.
- Status schema changes must preserve serverEpoch/operationId/revision stale-response guards and avoid exposing absolute paths, validators, or headers.
- During investigation .env changed concurrently from no key/default false to true; no diagnosis agent changed it. Tests must not inherit this developer setting.

### Decisions

- Treat operation entry as intended behavior, not a cache-miss defect.
- Treat the indistinguishable revalidation/download presentation as the safest fix target.
- Do not overwrite the concurrent .env=true change; the owner must confirm whether C:\cache is the authoritative served tree or only a remote copy.

---

## Current Feature: Daily chart and moving averages
<!-- orchestra:block-id: daily-chart-and-moving-averages -->

### Context

- Goal: Let the upper TickReplay price chart switch between minute and daily modes and show SMA25/SMA200 in daily mode without exposing future selected-day values.
- Key files: `src/tickreplay/daily_context.py`, `src/tickreplay/server.py`, `src/tickreplay/static/daily-chart.mjs`, `src/tickreplay/static/app.js`, `src/tickreplay/static/index.html`, `src/tickreplay/static/styles.css`, `cloud-run/main.py`, focused tests, and `docs/tick-replay.md`.
- Dependencies: existing DuckDB/httpx cache path, FastAPI, RequestCoordinator, Lightweight Charts 4.2.0, native ES modules, pytest, and node:test; no new dependency or migration.
- Complexity: COMPLEX.

### Architecture

- Daily data comes from official per-stem `stocks_daily/{stem}.duckdb` through a new bounded strict-before `/api/daily-context` with explicit availability.
- Daily mode owns a separate lazily created and retained chart, four series, time scale, and viewport; minute replay and history keep their existing ownership.
- The selected-day daily candle is rebuilt only from replayed raw ticks; raw historical closes drive SMA25/SMA200 for point-in-time consistency.
- Request generation, full `stem|code|actualDate` identity, and token checks protect cache/state commits; active mode additionally gates chart commits.

### Codex Validation

- The first plan validation returned NEEDS_REVISION for shared-chart ambiguity, replay side-effect guards, availability semantics, and missing boundary/race tests.
- The revised plan at `.agents/docs/plans/daily-chart-moving-averages.md` incorporated every finding and passed Codex re-validation with no blocking missing coverage.
- Codex CLI validation was unavailable before its 14:40 quota reset, so the PASS came from a read-only gpt-5.6-sol validation subagent; the CLI audit-log gap remains explicit.

### Integration Points

- `/api/session` supplies the actual session date/code before daily identity or fetching begins.
- `/api/daily-context` returns at most 500 completed sessions and distinguishes valid empty history from missing/corrupt/unreachable data.
- `daily-chart.mjs` owns validation, duplicate handling, partial-day aggregation, SMA math, and request/session state; `app.js` owns DOM/chart lifecycle wiring.
- `step()`, `redrawAll()`, `refreshMarkers()`, seek/reset, and minute-history completion preserve all non-price-chart side effects; only leaf chart writes are isolated by owner.

### Decisions

- Query the entire per-stem date series across legacy/current Code partitions; do not filter to the current Code.
- Use strict-before raw OHLCV and never read adjustment columns; split discontinuities in long SMAs are an accepted documented limitation.
- Treat `available=true,bars=[]` as valid empty history and `available=false,bars=[]` as supplementary-data failure; only valid responses are cached.
- Exclude daily markers, paging beyond 500 sessions, new dependencies, migrations, and existing API-contract changes.

---

## Current Bug Fix: daily-mode-pauses-replay
<!-- orchestra:block-id: daily-mode-pauses-replay -->

### Context

- Report: selecting Daily appears to stop replay.
- Root cause: current replay state does continue; the active daily view lacks perceptible liveness because one full-day candle changes sub-pixel, no-trade frames do not change it, and the coarse scrubber may stay on one integer.
- Affected files: src/tickreplay/static/app.js, daily-chart.mjs, index.html, styles.css, and focused browser/Node tests.

### Fix Approach

- Preserve the replay engine and independent daily viewport. Add a compact daily-panel play-state/time/progress indicator updated only when its displayed whole second or state changes. Add executable tab-switch continuity coverage and a real Chrome regression.

### Regression Risks

- Do not force the daily viewport to the live edge; that would break user pan/zoom. Avoid per-frame aria-live announcements and unnecessary layout work. Preserve gap skip, seek/reset, all speeds, tick/tape/board/paper side effects, and mobile layout.

### Decisions

- Treat the behavior as a perceptual liveness defect, not an intentional pause or proven replay-engine failure.
- Do not add a no-op playing-state restore in setChartMode.
- Codex initial/root-cause consultations were unusable; the Phase 3 validation response requested a plan despite receiving it, so no Codex verdict is used as evidence.

---

## Current Bug Fix: daily-mode-stops-tick-chart-and-tape
<!-- orchestra:block-id: daily-mode-stops-tick-chart-and-tape -->

### Context

- Error: Daily ready emits `Cannot add property zb, object is not extensible` and visibly freezes Tick/Tape.
- Root cause: frozen terminal SMA25/SMA200 points cross directly into mutating Lightweight Charts APIs before downstream replay ports.
- Affected files: `src/tickreplay/static/app.js`, `daily-chart.mjs`, focused tests, and Chrome regression artifacts.

### Fix Approach

- Preserve frozen canonical session state and clone `{time,value}` points only at SMA25/SMA200 `setData`/`update` chart boundaries.

### Regression Risks

- Incident skips Tick, Tape, orders, board, and position updates after cursor consumption. Fix must keep O(1) terminal work, exact SMA values, and immutable state.

### Decisions

- Require a fresh-browser check after Daily reaches ready, with zero runtime exceptions and visible Tick/Tape transitions.
- Codex consultations were unavailable; direct Chrome/code evidence is authoritative for this fix.

---

## Current Feature: Daily chart history paging
<!-- orchestra:block-id: daily-chart-history-paging -->

### Context

- Goal: Show the newest 90 Daily bars with five logical bars of right padding by default, then prepend older bars when user pan or zoom reaches the left edge without interrupting replay.
- Key files: `src/tickreplay/static/daily-chart.mjs`, `src/tickreplay/static/app.js`, `src/tickreplay/static/daily-chart.test.mjs`, `tests/test_tickreplay_daily_context.py`, and `docs/tick-replay.md`.
- Dependencies: Existing Daily context endpoint and Lightweight Charts logical-range API.
- Complexity: COMPLEX

### Architecture

- Use a session-scoped, user-armed, single-flight pager with a 10-bar left-edge threshold and 200-bar strict-before pages.
- Prepare immutable pages and recompute SMA25/SMA200 before the atomic four-series commit; preserve the visible logical range by shifting both edges by the unique prepend count.
- Commit inactive Daily completions to canonical state and the saved range only, then render them when Daily becomes active again.

### Codex Validation

- Two wrapper-based Codex CLI consultations produced no usable response; a `gpt-5.6-sol` fallback independently validated scope, architecture, plan, and revalidation as PASS.
- Independent quality, security, and test reviews were run after implementation; the quality review found and drove removal of an O(n) replay-frame snapshot.

### Integration Points

- Daily tab lifecycle and chart gestures in `app.js`; canonical Daily bars, SMA series, paging state, transaction, and rollback in `daily-chart.mjs`; strict-before paging contract in the existing Daily context API.

### Decisions

- Initial viewport is exactly newest 90 bars plus five right-padding bars, matching the Minute chart policy.
- Paging requires a real user wheel, pointer, or touch gesture so programmatic range changes cannot start requests.
- Failures use per-cutoff cooldown and a three-failure stop; empty pages mark history exhausted.
- Historical SMA arrays are computed only during initial load or page commit; replay-frame partial updates remain O(1).

### Validation

- Static JavaScript: 135/135 passed. Python integration subset: 133 passed, 1 skipped. Ruff and syntax checks passed.
- Fresh Chrome CDP verification passed exact 90+5 initialization, drag- and wheel-triggered 200-bar paging with exact +N range shifts, inactive completion and Daily return, SMA continuity, Tick/Tape/board/order/position continuity, and zero runtime exceptions.

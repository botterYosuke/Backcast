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

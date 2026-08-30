# Quality Review: Daily Chart and Moving Averages — Final Pass 3

## Decision

**PASS.** All High and Medium correctness, concurrency, retry, viewport, identity, and performance findings are resolved. The final limiter-wave amplification gap is closed by capturing request arrival before the dedicated worker limiter and passing that timestamp into the real loader. The two remaining Low findings are optional hardening/refactoring and are not release blockers.

## Current Finding Counts

- High: 0
- Medium: 0
- Low: 2
- Total: 2

## Low Findings — Optional, Non-blocking

### [Low, optional hardening] Full-identity history cache remains unbounded

- **File:** `src/tickreplay/static/daily-chart.mjs:358`
- **Evidence / impact:** Each successful identity retains up to 500 bars plus derived arrays for the lifetime of the page. A long multi-symbol browsing session grows memory monotonically.
- **Suggested improvement:** Add a small LRU/entry cap while retaining exact full-identity keys. This is not a current correctness blocker.

### [Low, optional refactor] Integration remains concentrated in a 2,092-line app module

- **File:** `src/tickreplay/static/app.js:1`
- **Evidence / impact:** The module remains above the coding-principles 800-line maximum and coordinates chart lifecycle, replay, minute history, board, trading, and session commits. Pure helpers reduce behavioral risk but not the file-level responsibility concentration.
- **Suggested improvement:** Extract the daily DOM/chart adapter and session integration after release. This is not a current correctness blocker.

## Final Re-audit

- **Resolved — limiter-wave origin amplification:** `_run_daily_context()` captures `request_started_at` before awaiting `anyio.to_thread.run_sync(... limiter=_DAILY_CONTEXT_LIMITER)` and passes it to `load_daily_context()` (`server.py:500-518`). The loader preserves a direct-call fallback capture (`daily_context.py:469-486`) and compares request arrival with negative-miss record time (`:194-207`). The integration test sends eight same-stem requests through the real loader and four-slot limiter, holds the first 404 until every arrival is captured, asserts exactly one origin GET for the burst, then asserts a later explicit request produces GET two (`test_tickreplay_server.py:514-594`).
- **Resolved — commit/status lifecycle:** `commitDailySession()` commits identity/phase and publishes the committed phase (`daily-chart.mjs:230-240`). Both zero/non-zero session branches use it, and tests execute `ready`, `empty`, `unavailable`, and `error` settling before commit.
- **Resolved — asynchronous viewport ownership:** nested `ChartViewportState.runProgrammatic()` acquisitions remain suppressed through the next task boundary and drain independently (`daily-chart.mjs:183-214`). Executed tests cover synchronous, microtask-delayed, and nested release behavior.
- **Resolved — failure retry semantics:** a negative miss coalesces only requests already started before it; the next explicit request removes the entry and retries. Both direct-loader concurrency and limiter-spanning integration tests verify this contract.
- **Preserved — SMA performance:** accepted daily history precomputes immutable SMA25/SMA200 arrays and rolling windows once (`daily-chart.mjs:150-158`, `:455`). Tick folding only updates partial OHLCV (`:496-498`); seek derives one terminal tail after its loop, and replay derives at most once per tick-bearing frame. The executed 10,000-tick test asserts `historyPrecomputations=1` and `terminalDerivations=1` after the single explicit tail derivation.
- **Preserved — atomic full identity:** requested metadata remains staged until new `state.meta`, typed arrays, and context are installed; exact `stem|code|actualDate` identity is committed before synchronous seek initialization, with no intervening await. Tick admission requires that exact committed identity.
- **Preserved — replay and hidden minute history:** `runReplayFrame()` keeps all non-chart side effects mode-independent, while chart writes remain leaf-owned. Hidden minute-history completion updates canonical arrays through a deferred target and flushes the real minute chart exactly once on return.

## Architecture and Regression Assessment

- Duplicate-date group SQL, strict-before raw OHLCV, bounded `LIMIT`/materialization, validated staging/repair, per-destination locking, conditional revalidation, and API empty/unavailable semantics remain correct.
- Requested versus actual date, generation/token/full identity, zero-tick omission, deterministic seek/reset/SMA-tail removal, separate chart ownership, resize, crosshair, and deferred history rendering remain compliant.
- No whole-function chart-mode guards were added; tick chart, tape, orders, board, position, clock, and scrubber behavior remains mode-independent in executed pure frame tests.

## Validation Evidence

- `node --test src/tickreplay/static/*.test.mjs` — 108/108 passed.
- `uv run pytest tests/test_tickreplay_daily_context.py -q` — 40/40 passed.
- `uv run pytest tests/test_tickreplay_server.py -q` — 31/31 passed; one Starlette deprecation warning.
- `uv run ruff check src/tickreplay/daily_context.py src/tickreplay/server.py tests/test_tickreplay_daily_context.py tests/test_tickreplay_server.py` — passed.
- Manual browser acceptance was not performed; the lifecycle and ownership invariants are covered by executable controller/integration tests.

## Codex Consultation

No retry was made during Final Pass 3. The feature’s mandated single read-only consultation remains the original attempt: wrapper exit 0 (`gpt-5.6-sol`, 10.674 s), but the response was unusable because only `# Objective` reached Codex. Per the original one-attempt instruction it contributed no evidence. Artifacts: `.agents/logs/codex/20260830T042535Z-quality-review-daily-chart-moving-averages.{prompt.md,md,err.log}`.

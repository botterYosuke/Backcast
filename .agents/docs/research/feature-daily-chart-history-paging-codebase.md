# Daily Chart Viewport and History Paging: Codebase Analysis

## Requested behavior

Daily mode should open with the same logical viewport policy as minute mode: the
latest 90 bars plus 5 logical bars of right-side padding. After a real user pan
or zoom, approaching the oldest loaded Daily bar should fetch and prepend an
older page without moving the bars currently visible to the user. Replay, Tick,
Tape, board, paper trading, and Minute history must remain independent.

## Current behavior and ownership

- `src/tickreplay/static/app.js` defines `MINUTE_VISIBLE_BARS = 90` and
  `MINUTE_RIGHT_PADDING_BARS = 5`; `followMinuteView()` sets
  `{from: lastIndex - 89, to: lastIndex + 5}`. Daily has no equivalent constants
  or follow function. `renderDailyChart()` calls `fitContent()` once through
  `updateChartViewport()`, so all 500 completed bars (and a replay-derived
  partial bar, when present) are initially fitted into the chart with no fixed
  five-bar padding.
- `src/tickreplay/static/daily-chart.mjs` owns Daily payload validation, session
  identity/generation, immutable history, partial-day aggregation, SMA
  precomputation, and viewport callback suppression. `DailyChartSession` caches
  one immutable payload per `stem|code|actualDate`; it has no older-page token,
  exhaustion state, retry policy, or history-merge operation.
- `src/tickreplay/static/minute-history.mjs` is the established paging pattern:
  a user interaction arms loading; `barsInLogicalRange(range).barsBefore <= 10`
  admits one request; the request is generation/identity guarded; empty means
  exhausted; failure has a five-second cooldown and three-attempt limit; unique
  prepends shift both logical range edges by the number added.
- `src/tickreplay/daily_context.py` and `src/tickreplay/server.py` already expose
  the required paging primitive. `/api/daily-context` accepts any valid
  exclusive `date` cutoff and `limit` from 1 through 500, performs `LIMIT` in
  DuckDB, and returns the newest eligible rows before that cutoff in ascending
  order. A second request whose cutoff is the oldest loaded bar date therefore
  returns the preceding page without a backend contract change.

## Recommended implementation boundary

### Initial view

Keep the initial Daily request at 500 completed bars. Treat 500 as the initial
warm-up/buffer and per-request API cap, not as the number displayed. Replace the
one-time `fitContent()` decision with an initial logical range helper:

```text
lastIndex = snapshot.bars.length - 1
from = max(0, lastIndex - 89)
to = lastIndex + 5
```

The replay-derived partial day, when present, is one of the 90 visible bars, as
the forming Minute candle is today. With fewer than 90 observations, show all
available bars and retain the same five-bar right padding. Rename
`hasFitContent` to an initial-viewport concept and clear stale saved Daily range
state on a new replay session. Mode switches within the same session must still
restore the saved Daily range.

### Older-page state and merge

Extend `DailyChartSession` (or a small Daily-specific controller) with page
size/edge policy, user-armed state, single-flight page token, exhaustion,
per-cutoff cooldown/failure count, and generation/full-identity checks. Avoid a
broad refactor of the proven Minute controller. The oldest
`historyBars[0].time` is the strict-before cutoff; never use the partial selected
day as the paging boundary.

Normalize each page with its requested page size, reject malformed or
non-empty-all-invalid payloads as retryable failures, and prepend only unique
ISO-date bars strictly older than the current boundary. `available=true` with
an empty page means session-local exhaustion. `available=false`, transport
failure, cancellation, or an application failure must preserve existing data;
failure is not exhaustion.

The existing `daily-context` request-coordinator kind can be reused because
paging starts only after the initial request is ready. `loadSession()` already
cancels that kind before its first await. Admission must occur before fetch and
the token, generation, full identity, and current `state.meta` identity must be
rechecked immediately after await and before mutation. Switching to Minute may
suppress chart writes, but a still-current page may update canonical Daily
state/cache and the saved Daily range for the next switch back.

### Viewport and SMA transaction

Before applying a page, capture the active logical range. After `N` unique bars
are prepended, rewrite all four Daily series and set
`{from: old.from + N, to: old.to + N}` inside the Daily programmatic-range guard.
Update `dailyViewport.savedRange` to the same shifted range because suppressed
callbacks cannot capture it. This prevents horizontal jumps and prevents
programmatic `setData`/range callbacks from recursively requesting another
page. Arm paging only from `wheel`, `pointerdown`, or `touchstart` on the Daily
chart, then evaluate the left-edge threshold in the visible-range callback.

Prepending changes the SMA availability before the former first SMA point.
Recompute immutable historical SMA25/SMA200 arrays once per accepted page and
refresh their rolling terminal windows; never rebuild them per replay tick.
Keeping the initial 500 bars ensures the initially visible 90 bars have valid
SMA200 warm-up. The last 199 closes are unchanged by an older prepend, so the
partial-day terminal SMA value should remain numerically unchanged. Continue
cloning frozen SMA points at every Lightweight Charts boundary via
`paintedLinePoint()` to avoid the previously fixed mutation crash. Prefer a
staged apply/rollback: commit canonical history/cache only after all active
chart writes and range restoration succeed; when Daily is hidden, commit the
canonical page and shifted saved range without touching its chart instance.

## Affected files and downstream contracts

| File | Expected change |
| --- | --- |
| `src/tickreplay/static/app.js` | Daily 90/5 constants, initial-range wiring, Daily interaction arming, left-edge callback, page request/apply lifecycle, session cancellation/reset integration. |
| `src/tickreplay/static/daily-chart.mjs` | Initial range helper; Daily page admission/merge/commit state; once-per-page SMA precompute; saved-range shift/transaction helpers. |
| `src/tickreplay/static/daily-chart.test.mjs` | Pure range, merge, SMA, stale/single-flight, mode-switch, exhaustion/failure, and source-wiring regressions. |
| `tests/test_tickreplay_server.py` | Optional explicit two-page strict-before regression; backend implementation should remain unchanged. |
| `docs/tick-replay.md` | Document 90+5 initial Daily view, edge threshold/page size, strict-before loading, and failure/exhaustion behavior. |
| `.agents/docs/DESIGN.md` | Remove/replace the explicit “at most 500 completed sessions” and “uses no paging” constraints. Retain a maximum of 500 rows per DuckDB query. |

`src/tickreplay/daily_context.py`, `src/tickreplay/server.py`, `cloud-run/main.py`,
the API response schema, and dependencies need no implementation change.
Existing consumers of `DailyChartSession.snapshot()` and the O(1) partial-day
update path must remain compatible.

## Test matrix and focused commands

Add tests for exactly 90 visible bars plus `to = lastIndex + 5` with 0, fewer
than 90, 90, 500, and 500-plus-partial observations; restore after mode switch;
no page request before a real interaction; zoom/pan threshold at 10/11 bars;
single-flight; stale stem/code/date/generation; mode switch while a response is
in flight; duplicate-only and empty exhaustion; unavailable/malformed retry;
two consecutive chronological prepends; exact `+N` logical-range and saved-range
compensation; unchanged visible SMA values and terminal SMA after prepend; and
zero impact on replay/Tick/Tape ordering.

```powershell
node --test src/tickreplay/static/daily-chart.test.mjs src/tickreplay/static/minute-history.test.mjs
node --check src/tickreplay/static/daily-chart.mjs
node --check src/tickreplay/static/app.js
uv run pytest tests/test_tickreplay_daily_context.py tests/test_tickreplay_server.py -q
```

Browser acceptance should verify the initial 90+5 range, repeated left-edge
pages without jumps, an in-flight Daily-to-Minute switch and return, continuous
replay/Tick/Tape updates during all requests, SMA25/SMA200 continuity, and zero
runtime exceptions.

## Existing risks and design conflict

The working tree already contains the uncommitted Daily/SMA feature and follow-up
replay fixes; they must be preserved. The current design explicitly caps total
Daily history at 500 and excludes Daily paging, so this requirement deliberately
supersedes that constraint. Without a separate client retention policy, the
loaded in-browser Daily array grows by page until the source is exhausted. This
is modest for trading-day history but should be stated as an accepted bound
(source history/session lifetime), or a separate retention/windowing design
must be approved before implementation.

# Daily Chart and Moving Averages: Codebase Analysis

## Scope and conclusion

TickReplay currently has one price chart: a minute candlestick/volume chart
whose historical preload comes from `stocks_minute/{stem}.duckdb` and whose
current-session candles are aggregated from replay ticks. There is no
TickReplay daily-bar API. The existing Cloud Run GraphQL query reads
`stocks_daily/mother.duckdb`, but returns ranking rows (`date`, `code`, `close`,
`sortValue`, `volume`, `rank`) rather than OHLC bars and is not a suitable chart
contract.

The smallest robust design is a per-symbol daily loader and REST endpoint,
paired with a small DOM-free frontend module for bar validation and rolling
averages. Keep the minute replay arrays and `MinuteHistorySession` intact, and
make chart rendering mode-aware.

## Existing chart construction and population

- `src/tickreplay/static/index.html` contains one `#minute-chart` container and
  a minute-card heading; it has no interval selector or MA legend.
- `src/tickreplay/static/app.js` creates `minuteChart`, `candleSeries`, and
  `volumeSeries` with Lightweight Charts 4.2.0. `state.contextBars` contains
  pre-session minute bars and `state.bars` contains those bars plus candles
  aggregated by `applyTick()` from tick replay data.
- `loadSession()` loads `/api/session`, then preloads 30 bars from
  `/api/minute-context`; `seekTo()` rebuilds candles from ticks. `step()` updates
  the candle and volume series incrementally. `redrawAll()` resets both minute
  series and calls `followMinuteView()`.
- `MinuteHistorySession` and `minute-history.mjs` fetch older pages of 200 bars
  near the left edge and transactionally prepend them while preserving the
  logical viewport. Switching to daily mode must prevent an in-flight minute
  page or replay frame from overwriting daily series data.
- Trading markers use minute timestamps and are installed on `candleSeries`.
  They should remain minute-only unless a separate, explicitly designed daily
  marker mapping is added.

## Daily data availability

Daily data already exists in both `stocks_daily/{stem}.duckdb` and the combined
`stocks_daily/mother.duckdb`. `cloud-run/main.py` uses the mother database only
for `/graphql` ranking; TickReplay's repository cache does not expose daily
bars. The Cloud Run file whitelist permits numeric daily stems and `mother`,
but currently rejects letter-bearing daily stems such as `285A`.

Read-only inspection of the configured local cache found 5,072 per-symbol daily
DuckDB files. The sampled files use table `stocks_daily`, with `Date TIMESTAMP`
as the primary key and raw plus adjusted OHLC/volume columns.

| Stem | Rows | Date range | Code partitions |
| --- | ---: | --- | --- |
| `285A` | 405 | 2024-12-18 to 2026-08-19 | `285A`: 212; `285A0`: 193 |
| `7203` | 6,277 | 2001-01-04 to 2026-08-19 | `7203`: 6,084; `72030`: 193 |

Thus the current dataset can supply at least 200 sessions for both examples,
including the default `285A`. However, filtering only by the current canonical
session code would return only 193 rows after the 2025-11-04 code-format change
and would make MA200 unavailable. A per-stem query must span all rows in the
file (or all codes listed by `stocks_daily_metadata`) and order/deduplicate by
`Date`. Newly listed securities can legitimately have fewer than 200 sessions;
their MA200 should simply be absent until enough closes exist.

For split-safe long-period lines, use adjusted values when present:
`COALESCE(AdjustmentClose, Close)` and the corresponding OHLC/volume columns.
This matters: the inspected `7203` file has 5,080 of 6,277 rows where
`AdjustmentClose <> Close`. The raw-versus-adjusted choice should be
confirmed as a product decision, but adjusted values are the safer default for
MA continuity.

## Recommended contracts and behavior

Add a best-effort endpoint analogous to minute context:

```text
GET /api/daily-context?stem=285A&date=2026-08-19&limit=500
-> {"bars":[{"time":"2026-08-18","open":...,"high":...,"low":...,"close":...,"volume":...}]}
```

Recommended semantics are chronological bars strictly before `date`, with an
upper bound of at least 500. The frontend can append an in-progress current-day
bar derived from replay ticks. Strict-before is important: returning the stored
selected-day High/Low/Close would reveal the end-of-day future while replay is
still at the open. MA25/MA200 should use closing values and emit a point only
after 25/200 valid sessions; 500 history bars provide enough warm-up for both
lines across a useful visible window.

Use a dedicated RequestCoordinator kind such as `daily-context`. Cancel or
generation-guard it on symbol/session changes. On the interval selector:

1. Daily mode loads/caches the current `{stem,date}` daily payload, applies
   daily time-scale formatting, renders candles/volume/MA25/MA200, and ignores
   minute-series writes while replay state continues to advance.
2. Minute mode restores `state.bars`, minute volume, markers, minute time-scale
   formatting, and the existing logical viewport behavior.
3. A seek/reset while in daily mode rebuilds the partial daily candle and its
   current MA points without resetting trading, tape, board, or minute-history
   state beyond existing behavior.

## Exact affected files

### Runtime and UI

- **New `src/tickreplay/daily_context.py`**: validate stem; locate/download
  `stocks_daily/{stem}.duckdb`; query adjusted, chronological daily OHLCV before
  a date across the code-format boundary; return typed `DailyBar` values.
- **`src/tickreplay/server.py`**: import the loader and add
  `GET /api/daily-context` using `repository.cache_dir` and
  `repository.http_client`. Existing `/api/session` and `/api/minute-context`
  contracts must remain unchanged.
- **New `src/tickreplay/static/daily-chart.mjs`**: DOM-free response
  normalization, rolling SMA calculation, and (if adopted) current-day tick or
  minute-bar aggregation. Follow the existing extracted-module pattern.
- **`src/tickreplay/static/app.js`**: add chart mode state, line series for MA25
  and MA200, daily loading/cancellation, mode-aware series writes, time-scale
  formatting, and restoration of minute bars/markers on switch-back.
- **`src/tickreplay/static/index.html`**: add an accessible minute/daily control
  and MA labels in the price-card header; consider renaming the heading and
  `#minute-chart` to price-chart terminology if references are updated
  atomically.
- **`src/tickreplay/static/styles.css`**: style the interval control and line
  legend for desktop and the existing `max-width: 768px` wrapped card header.
- **`cloud-run/main.py`**: widen `stocks_daily` per-symbol whitelist from
  digits-only to the validated alphanumeric stem form; existing
  `_stem_case_variants()` can then handle filesystem case differences.
- **`docs/tick-replay.md`**: document the daily source, strict-before/live-day
  behavior, MA basis, control, endpoint, source tree, and test commands.

### Tests

- **`tests/conftest.py`**: add a realistic `stocks_daily` schema/factory and
  daily remote-store branch to the shared `httpx.MockTransport`.
- **New `tests/test_tickreplay_daily_context.py`**: chronological/strict-before
  query, 500-bar bounding, old+new Code continuity, adjustment fallback,
  missing/corrupt/unreachable behavior, invalid stem, and cache reuse.
- **`tests/test_tickreplay_server.py`**: pin query validation and response JSON
  for `/api/daily-context`, while preserving the existing best-effort minute
  contract.
- **New `src/tickreplay/static/daily-chart.test.mjs`**: test normalization,
  25/200 boundary positions and values, insufficient history, and source seams
  that prevent replay/minute-history writes from replacing daily data.
- **`tests/test_cloud_run_main.py`**: prove numeric and letter-bearing daily
  files are served and absent/disallowed paths remain 404. Existing GraphQL
  ranking tests remain regression coverage.

No dependency, `pyproject.toml`, Dockerfile, or database migration change is
needed. `src/tickreplay/static/minute-history.mjs` should not need a behavior
change; its tests are mandatory regression gates because `app.js` integration
is affected.

## Conventions and verification

- Python: 4 spaces, required type hints, snake_case functions/variables,
  PascalCase dataclasses, frozen bar value objects with `as_dict()`, parameterized
  DuckDB queries, `uv` for all Python commands, Ruff line length 88.
- JavaScript: native ES modules, two-space indentation, single quotes,
  semicolons, camelCase identifiers, uppercase policy constants, `node:test`
  plus `node:assert/strict`. Pure calculations are extracted from the DOM-heavy
  `app.js`; several existing tests also inspect `app.js` source seams.
- API responses use lower camelCase at existing repository/session boundaries
  and simple `{"bars": [...]}` context payloads. Times are epoch seconds for
  minute data; daily bars should use ISO `YYYY-MM-DD`, which Lightweight Charts
  accepts as a business-day time and avoids timezone shifts.
- Focused gates after implementation:

```powershell
uv run pytest tests/test_tickreplay_daily_context.py tests/test_tickreplay_minute_context.py tests/test_tickreplay_server.py tests/test_cloud_run_main.py
node --test src/tickreplay/static/*.test.mjs
uv run ruff check src/tickreplay tests cloud-run/main.py
uv run ruff format --check src/tickreplay tests cloud-run/main.py
```

## Dependency map and risks

- Upstream data path: Cloud Run `/jp/stocks_daily/{stem}.duckdb` -> daily local
  cache/loader -> TickReplay `/api/daily-context` -> RequestCoordinator ->
  daily normalization/SMA -> chart series and interval control.
- Replay path: `/api/session` -> tick arrays -> minute candles and current-day
  daily aggregate. Chart mode must not change tape, board, portfolio, scrubber,
  or replay clock state.
- **Look-ahead risk:** selected-day stored daily OHLC leaks future replay data;
  use strict-before history plus a live aggregate.
- **Code-transition risk:** filtering `Code = session.code` breaks MA200 for
  current data; query the per-stem date series across metadata codes.
- **Corporate-action risk:** raw closes produce false MA jumps; prefer adjusted
  columns with raw fallback and document the basis.
- **Race risk:** session changes or mode switches can apply stale daily/minute
  responses. Reuse RequestCoordinator cancellation plus identity checks before
  every state or chart mutation.
- **Shared-series risk:** `step()`, `redrawAll()`, `refreshMarkers()`, and
  minute-history prepend currently write the sole candle series directly.
  Every path needs a mode guard or separate visible series; otherwise the daily
  view will be overwritten during replay.
- **Availability risk:** per-symbol daily files are supplementary and may be
  absent or have fewer than 200 bars. The UI should distinguish “daily data
  unavailable” from a valid chart whose MA200 is not yet defined.

## Unverified product assumptions

- The interval switch changes only the upper candlestick card; the tick chart,
  tape, board, and replay controls remain active.
- Moving averages are simple moving averages of adjusted close, not EMA and not
  raw close.
- Daily history must respect replay time and must not expose the selected day's
  final OHLC. If the product intentionally allows look-ahead, the implementation
  can be smaller, but that should be an explicit decision.

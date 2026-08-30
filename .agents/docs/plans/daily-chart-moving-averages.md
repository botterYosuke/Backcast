# Implementation Plan: Daily Chart and Moving Averages

## Purpose

Add an accessible minute/daily selector to TickReplay's upper price-chart card. Daily mode shows at most 500 completed sessions strictly before the actual replay session date, a selected-day candle derived only from ticks already replayed, and daily-only SMA25/SMA200 lines, without changing replay, trading, or minute-history behavior.

Daily history uses raw point-in-time OHLCV from the official per-stem `stocks_daily/{stem}.duckdb` source. The implementation must not expose the stored selected-day close or read any adjustment column.

## Scope

The feature is limited to the upper price-chart data path, its supporting API, and the tests/documentation needed to protect that integration.

### In scope

- A new best-effort `GET /api/daily-context` endpoint with the response shape `{"bars": [...], "available": true|false}`.
- Bounded, strict-before loading from an official per-stem daily DuckDB file across legacy/current `Code` partitions.
- Deterministic validation, duplicate-date handling, and chronological serialization of raw daily OHLCV.
- A separately mounted, lazily created Lightweight Charts daily chart with candle, volume, SMA25, and SMA200 series.
- A replay-derived partial selected-day candle and its current SMA tail points.
- An accessible minute/daily selector, availability status, legend, responsive sizing, independent viewports, and mode-safe request/session handling.
- Focused Python and native-ES-module tests, integration regressions, documentation, and manual browser acceptance.

### Out of scope

- Changes to the tick chart, board, tape, clock, replay cursor/speed semantics, paper-trading orders/fills/positions, or minute-history behavior.
- Daily trade markers or mapping minute markers onto business-day dates.
- Unlimited daily paging or loading more than the newest 500 completed sessions.
- Adjusted prices, split repair, or reads of `AdjustmentOpen`, `AdjustmentHigh`, `AdjustmentLow`, `AdjustmentClose`, `AdjustmentVolume`, or any other adjustment field.
- Changes to `/api/session` or `/api/minute-context` response contracts.
- New dependencies, database migrations, schema changes, or changes to `src/tickreplay/static/minute-history.mjs`.

## Architecture Contract

### Backend data and API contract

1. `src/tickreplay/daily_context.py` owns stem validation, official daily-file acquisition/cache use, DuckDB querying, row/date validation, duplicate resolution, and serialization into immutable typed daily bars.
2. `GET /api/daily-context?stem=<stem>&date=<YYYY-MM-DD>&limit=<1..500>` returns HTTP 200 for supplementary-data availability outcomes:
   - `{"bars": [...], "available": true}` means the file was reached, opened, and queried successfully. `bars` may legitimately be empty because no session exists before the cutoff or because no date group satisfies the data invariants.
   - `{"bars": [], "available": false}` means the per-stem file was missing, corrupt, unreachable, or could not be queried. `bars` must be empty in this state.
   - Malformed `stem`, `date`, or `limit` remains an ordinary client-validation error; it is not converted to `available=false`.
   - Because this is a new endpoint, adding the explicit `available` field creates no compatibility break; existing endpoint contracts remain byte-for-byte out of scope.
3. The query reads only raw `Date`, `Open`, `High`, `Low`, `Close`, and `Volume`. It spans the whole per-stem table and never filters to the current session `Code`, so legacy/current code partitions contribute to SMA warm-up.
4. Backend and frontend use the same OHLCV invariants: `open`, `high`, `low`, and `close` are finite and greater than zero; `volume` is finite and greater than or equal to zero; and `low <= min(open, close) <= max(open, close) <= high`.
5. Duplicate handling is date-group based and deterministic. A date is eligible only if every row in its group satisfies the invariants and all rows have one identical raw OHLCV tuple. Exact duplicates collapse to one bar. Any conflicting OHLCV tuple or any invalid row makes the entire date invalid and the date is omitted.
6. Bounding happens in DuckDB, not after loading all history in Python: filter to `CAST(Date AS DATE) < cutoff`, validate/group by date, order eligible groups descending, apply the parameterized `LIMIT`, then order that bounded result ascending for the response. Python materializes at most 500 bars.
7. Daily bar times are ISO `YYYY-MM-DD` business-day strings. The response is chronological and unique by date.

### Separate chart ownership and lifecycle

1. `index.html` contains distinct `#minute-chart` and `#daily-chart` DOM containers in the upper price-card chart stage. The existing tick-chart DOM is untouched.
2. The existing minute container retains its existing `LightweightCharts.createChart()` instance and minute candle/volume series. Daily mode uses a second, separate `createChart()` instance with exactly four daily-owned series: candlestick, volume histogram, SMA25 line, and SMA200 line.
3. The daily chart is created lazily by `ensureDailyChart()` on the first actual daily-mode activation, using the current measured chart-stage size. It is retained thereafter. Minute/daily switches only change visibility, `aria-hidden`, and pointer participation; neither chart nor its series is destroyed/recreated during a switch.
4. The two charts have independent time scales, formatting, crosshairs, and saved logical ranges. Initial daily load may fit its content once. Later switches restore the saved range and do not reset the other mode's viewport.
5. Both containers remain laid out in the same bounded chart stage rather than depending on a zero-sized `display:none` measurement. Resize logic applies the visible stage dimensions to the minute chart and, once created, the daily chart. Activation performs one final visible-size application before restoring that mode's range.
6. Minute history, minute markers, and all minute-series writes target minute-owned objects only. Daily history, partial-day data, and SMA writes target daily-owned objects only. There is no shared `candleSeries`, `volumeSeries`, or time scale between modes.

### Replay side-effect isolation

1. Do not add a mode-based early return around all of `step()`, `redrawAll()`, `refreshMarkers()`, `applyTick()`, seek/reset, or minute-history completion.
2. Every replay tick continues to advance `playing`/cursor/clock state, aggregate canonical minute bars, update the tick chart and tick points, update board and tape, evaluate paper-trading fills/orders/positions, and preserve existing minute-history state, regardless of the visible price-chart mode.
3. Isolate only direct upper price-chart writes:
   - Individual minute candlestick/volume/follow-range calls run only against the minute instance and are skipped or deferred while daily is visible; canonical minute arrays still update, and switching back performs a deterministic minute redraw from those arrays.
   - Daily partial/SMA calls run only against the daily instance, only when daily mode and the current session identity match.
   - `refreshMarkers()` still computes/preserves marker state and applies markers only to the minute candlestick series; daily series never receive markers.
   - A minute-history prepend still commits canonical minute history and controller state. Only its minute-chart `setData`/range compensation is deferred when daily is active and replayed against the minute instance on return.
4. `redrawAll()` keeps all existing non-price-chart effects. Its minute rendering block and daily rendering block are explicit leaf operations, rather than a guard around the whole function.

### Daily session and request state machine

`state.daily` has an explicit generation, current identity, phase (`idle`, `loading`, `ready`, `empty`, `unavailable`, or `error`), cache keyed by identity, canonical history bars, partial bar, derived SMA data, saved logical range, and in-flight request identity/token. Session identity is the exact tuple `stem|code|actualDate`.

1. At the start of every session-load attempt, before the first `await`, cancel the `daily-context` request kind, increment the daily generation, clear identity/in-flight/history/partial/SMA state, and clear any displayed daily data/status. This prevents the old session tail from surviving while `/api/session` is pending.
2. Do not start a daily request from the requested date. Await `/api/session`; only after its returned metadata supplies the actual `meta.date` and `meta.code` may the app construct `stem|code|actualDate` and make that generation ready for daily loading. This covers server fallback where the actual date differs from the requested date.
3. Daily loading is lazy. If the user selects daily before session metadata is ready, record the desired mode and show loading without fetching. Once metadata is ready, start `/api/daily-context` with the stem and actual date. If a new session is loaded while daily remains selected, start the new fetch only after the new actual identity is known.
4. A cache hit is permitted only for the full identity. A well-formed `available=true` response can be normalized and cached after its request token/generation/identity checks even if the user switched to minute while it was in flight. Chart-series commit additionally requires `chartMode === 'daily'` and the same current session identity.
5. `available=false`, transport/HTTP failure, malformed JSON/schema, or a non-empty response whose bars are all rejected is not cached. It sets `unavailable` or `error` only if the generation/identity is still current. The next explicit daily selection retries.
6. A late response for a different stem, a different actual date on the same stem, a different code, or an invalidated generation cannot mutate current cache state, status, partial/SMA state, or either chart.
7. `available=true` with an originally empty `bars` array is a valid, cacheable `empty` history. It remains distinct from unavailable. A non-empty payload is normalized defensively with the backend invariants; exact duplicate dates collapse, conflicting/invalid dates are omitted, and if no date survives the non-empty payload is treated as a failed response rather than a valid empty history.
8. A session with zero replay ticks has no partial selected-day candle. Completed history may still render, and no placeholder/SMA observation is synthesized for the selected date.

### Partial selected-day and SMA contract

1. The partial selected-day candle uses only valid raw replay ticks through the current cursor: open is the first tick price, high/low are extrema, close is the last tick price, and volume is the sum of finite nonnegative raw tick quantities. No stored selected-day daily row is requested or read.
2. Every backward seek, reset, or session reload first removes the prior partial candle and its SMA25/SMA200 tail from canonical daily series data and the active daily chart. It then folds ticks from session start through the target cursor and replaces all four daily series deterministically. Incremental state from the previous cursor is never reused across a backward boundary.
3. Forward replay may update only the partial candle and affected terminal SMA points after canonical state is updated. A full deterministic rebuild and incremental replay through the same cursor must be equal.
4. SMA periods count valid daily observations, not calendar days. The partial day is one observation only when at least one tick exists. SMA25 begins exactly on observation 25; SMA200 begins exactly on observation 200. Thus 24 history bars plus a partial produce the first SMA25 point, and 199 plus a partial produce the first SMA200 point.

## Implementation Steps

Execute these steps in order. Backend and pure-JavaScript behavior begins with failing tests and follows the Red-Green-Refactor cycle before DOM-heavy integration.

### Step 1: Lock backend contracts and fixtures (TDD Red)

- Add realistic per-stem `stocks_daily` DuckDB construction and remote transport behavior to `tests/conftest.py`, including raw and adjustment columns, legacy/current Code rows, exact and conflicting duplicate dates, invalid OHLCV, empty history, 500+ valid rows, `285A`, and lowercase/case-variant storage.
- Create failing tests in `tests/test_tickreplay_daily_context.py` for input validation, strict-before semantics, SQL bounding, chronological output, Code-partition continuity, deterministic duplicate rules, identical data invariants, availability states, cache reuse, and raw-only column access.
- Add failing endpoint contract tests to `tests/test_tickreplay_server.py` and file-serving security/regression tests to `tests/test_cloud_run_main.py`.

**Verification:** The new focused tests fail for missing implementation and precisely distinguish `available=true,bars=[]` from `available=false,bars=[]`.

### Step 2: Implement the bounded daily loader (TDD Green/Refactor)

- Add immutable typed `DailyBar` and result/status types in `src/tickreplay/daily_context.py`.
- Reuse the repository's existing HTTP client, local cache-root, per-destination lock, staged download, DuckDB validation, and atomic replace patterns without changing `repository.py`.
- Implement the parameterized, raw-column-only CTE/group/descending-limit/ascending-result query described in the architecture contract. Do not fetch whole history into Python.
- Map missing, corrupt, unreachable, and query-failure outcomes to unavailable while allowing successful empty queries to remain available.
- Refactor only after the Step 1 tests pass, keeping validation and I/O responsibilities separate and fully typed.

**Verification:** Daily-loader tests pass, and SQL-capture tests prove the query has a bounded `LIMIT`, crosses Code partitions, and contains no adjustment-column identifier.

### Step 3: Expose the endpoint and secure Cloud Run serving (TDD Green/Refactor)

- Add `/api/daily-context` wiring to `src/tickreplay/server.py` using existing cache and HTTP-client injection; preserve `/api/session` and `/api/minute-context` unchanged.
- Return the explicit `bars` plus `available` status and retain FastAPI validation failures for malformed query input.
- In `cloud-run/main.py`, extend only the `stocks_daily` per-symbol filename rule to normalized 4- or 5-character ASCII alphanumeric stems; keep the exact `mother.duckdb` exception and existing case-variant lookup. Reject traversal, encoded traversal after framework decoding, extra extensions, separators, and six-character non-`mother` stems.
- Keep all other dataset whitelists and GraphQL mother behavior unchanged.

**Verification:** Server and Cloud Run tests pass for numeric, letter-bearing, lowercase/case-variant, missing, corrupt, `mother`, and rejection cases.

### Step 4: Lock pure frontend behavior (TDD Red)

- Create `src/tickreplay/static/daily-chart.test.mjs` before its implementation.
- Specify failing tests for response/status normalization, frontend OHLCV invariants, exact/conflicting duplicates, valid empty versus unavailable/invalid-all, partial raw tick OHLCV, zero ticks, deterministic seek/reset rebuilds, and SMA boundaries.
- Add pure state-machine tests for generation/identity transitions, requested-date versus actual-date handling, lazy fetch admission, cache/retry policy, and whether a chart commit is allowed.
- Add source-seam or mocked-chart tests proving separate containers/instances/four daily series, lazy creation, no mode-switch destruction, leaf-level chart-write isolation, and no whole-function mode guard around replay side effects.

**Verification:** Node tests fail for absent pure functions/controller behavior and identify every required boundary separately.

### Step 5: Implement pure daily normalization, aggregation, SMA, and state (TDD Green/Refactor)

- Add DOM-free immutable helpers and a small daily session/request controller to `src/tickreplay/static/daily-chart.mjs`.
- Implement response normalization and defensive duplicate/invalid-all behavior exactly as specified by the API contract.
- Implement partial candle fold/rebuild from raw ticks and rolling SMA25/SMA200 with terminal-point replacement.
- Implement the explicit generation, identity, cache, response-admission, retry, and chart-commit predicates without reading the DOM.
- Refactor after all Step 4 tests pass; do not add a dependency or move minute-history behavior into this module.

**Verification:** `daily-chart.test.mjs` passes at 24/25 and 199/200 boundaries both with and without a partial observation, plus empty, invalid-all, and seek/reset cases.

### Step 6: Add the separate daily DOM/chart and accessible presentation

- Modify `index.html` to add the separate daily container, labeled minute/daily control, non-blocking status region, and SMA legend while retaining the existing minute container and tick chart.
- Modify `styles.css` to layer the two price-chart containers in one stable-size stage, isolate pointer events/visibility, expose focus/selected states, and wrap controls/legend at the existing mobile breakpoint.
- In `app.js`, keep the minute chart construction intact and add `ensureDailyChart()` with a second `createChart()` and four daily series. Save/restore each chart's logical range independently and never destroy either instance during switching.
- Wire resize, crosshair, selector keyboard activation, and mode visibility so a daily chart first created after a hidden resize receives current dimensions.

**Verification:** Mocked Lightweight Charts tests/source seams prove two container IDs, two instances after first daily activation, four daily series, one-time lazy creation, no `remove()` on switches, independent ranges, and resize application.

### Step 7: Integrate session, requests, replay, seek/reset, and mode isolation

- At session-load entry, perform cancel/increment/clear before awaits; after `/api/session`, use the returned actual date/code to establish identity and admit lazy daily loading.
- Connect the dedicated `daily-context` request kind and full-identity cache. Permit valid same-session caching after a mode switch, but gate chart commits on active daily mode plus current identity. Do not cache unavailable or failed responses.
- Integrate partial-day maintenance with replay while preserving all existing tick/minute/board/tape/clock/paper-trading side effects.
- Refactor direct series writes inside `step()`, `redrawAll()`, `refreshMarkers()`, seek/reset, and minute-history callbacks into explicit minute/daily leaf blocks; do not guard or skip the containing operation.
- On daily activation, render canonical history, current partial if any, and SMA data into the daily instance. On minute activation, deterministically redraw any deferred minute series/marker/range work into the minute instance.
- On backward seek/reset, remove old daily tails before rebuilding from ticks; on zero ticks, omit the partial day.

**Verification:** Node integration tests cover stale identities, switching, failure/retry, replay invariants, backward seek removal, reset equality, and preserved minute-history behavior.

### Step 8: Document and validate the integrated feature

- Update `docs/tick-replay.md` with the selector, separate chart lifecycle, endpoint schema/status, official source, strict-before cutoff, actual-session-date identity, raw OHLCV, duplicate/validation behavior, partial-day construction, SMA warm-up, cache/retry behavior, and known split discontinuities.
- State clearly that daily markers, adjustment columns, unlimited paging, new dependencies, and migrations are not part of the feature.
- Run focused tests first, then the complete JavaScript and relevant Python/lint/format regressions, followed by the manual acceptance matrix.

**Verification:** All validation commands below pass, or any unrelated baseline failure is recorded with its exact command, output, and blast radius before handoff.

## File Changes

| File | Change type | Planned change |
| --- | --- | --- |
| `src/tickreplay/daily_context.py` | New runtime | Typed raw daily loader, SQL validation/deduplication/bounding, cache acquisition, availability result. |
| `src/tickreplay/static/daily-chart.mjs` | New runtime | Pure normalization, duplicate policy, tick aggregation, SMA calculation, session/request/cache controller. |
| `tests/test_tickreplay_daily_context.py` | New test | Loader contract, SQL, validation, availability, security, raw-column, cache, and boundaries. |
| `src/tickreplay/static/daily-chart.test.mjs` | New test | Pure JS data/SMA/state-machine tests and chart integration/source seams. |
| `src/tickreplay/server.py` | Modify runtime | Add `/api/daily-context` without changing existing endpoint schemas. |
| `src/tickreplay/static/app.js` | Modify runtime | Separate chart instance/lifecycle, mode UI wiring, session/request integration, leaf-level write isolation, partial rebuild. |
| `src/tickreplay/static/index.html` | Modify runtime UI | Separate daily chart DOM, accessible selector, status, and legend. |
| `src/tickreplay/static/styles.css` | Modify runtime UI | Stable layered chart stage, visibility/focus/status/legend/mobile rules. |
| `cloud-run/main.py` | Modify runtime | Safely serve validated alphanumeric per-stem daily files while preserving `mother`. |
| `tests/conftest.py` | Modify test support | Daily DuckDB fixtures and mock remote-store branch. |
| `tests/test_tickreplay_server.py` | Modify test | Endpoint query/response, unavailable/empty distinction, static `.mjs` integration regressions. |
| `tests/test_cloud_run_main.py` | Modify test | Daily whitelist, traversal/extension/length/case, numeric/letter/mother regressions. |
| `docs/tick-replay.md` | Modify documentation | User/API/data-source/SMA/lifecycle/error/limitation documentation and commands. |

No change is planned for `src/tickreplay/static/minute-history.mjs`, its public behavior, `repository.py`, `pyproject.toml`, lock files, Dockerfiles, database files/schemas, `/api/session`, or `/api/minute-context`.

## Test Matrix

| Area | Required cases and assertions |
| --- | --- |
| Backend validation | Finite positive OHLC; finite nonnegative volume; relationship invariant; invalid row invalidates its entire date group; invalid-all successful DB query yields `available=true,bars=[]`. |
| Duplicate dates | Exact raw OHLCV duplicates collapse once; any conflicting OHLCV, including cross-Code conflict, omits the full date deterministically. |
| SQL/query | Strict `Date < actualDate`; newest bounded window in SQL; ascending response; limits 1 and 500; 501+ source rows return newest 500; no Python whole-history materialization. |
| Source continuity | `285A` spans legacy/current Code rows without `Code` filter; numeric stem regression; exact raw values returned. |
| Adjustment prohibition | Adjustment fields contain deliberately different sentinel values; returned values remain raw; captured SQL never names an adjustment column. |
| Availability response | Missing, corrupt, unreachable, and query failure => `available=false,bars=[]`; valid no-prior-row/invalid-only DB => `available=true,bars=[]`; malformed client input remains validation error. |
| Frontend payloads | `available=false`, valid empty, malformed shape, non-finite/negative/impossible OHLCV, invalid-all non-empty, exact duplicates, conflicts, sort/uniqueness, 1-23/24/25/199/200/500 valid bars. |
| SMA boundaries | 24 observations => no SMA25; 25 => first SMA25; 199 => no SMA200; 200 => first SMA200; 23+partial remains below SMA25 and 24+partial produces it; 198+partial remains below SMA200 and 199+partial produces it; zero-tick partial does not count. |
| Partial raw ticks | Known tick prices/quantities assert exact O/H/L/C/V; no ticks omits date; forward replay equals full fold; backward seek removes old candle/SMA tails; reset output equals fresh rebuild at reset cursor. |
| Chart lifecycle | Separate DOM containers, second lazy `createChart()`, four daily series, separate time scale/logical range, repeated switches do not destroy/recreate, no daily markers. |
| Session races | Cancel/increment/clear occurs before await; same stem/different date stale response; different stem stale response; different code; requested date differs from server actual date; stale result changes neither status/cache/current chart. |
| Cache/retry | Valid same-identity response is cached even after daily-to-minute switch; no daily chart commit while minute is active; daily-to-minute-to-daily uses cache; unavailable/transport/malformed/invalid-all is not cached and next daily selection retries. |
| Replay invariants | Before/after mode change and daily failure, assert unchanged `playing`, cursor progression, selected speed, orders, positions, board state, tape entries, clock behavior, tick points, canonical minute bars, and minute-history controller/pages except for normal replay-driven changes shared with a minute-mode baseline. |
| Write isolation | `step()`, `redrawAll()`, and `refreshMarkers()` are never skipped wholesale; minute-history commit still occurs; mocked calls prove only the intended price-chart instance receives each series/range/marker write. |
| UI/responsive | Selector role/name/selected state, keyboard activation/focus, status distinction, legend colors, hidden/visible resize, independent crosshair behavior, desktop and mobile wrapping/touch targets. |
| Cloud Run security | Encoded traversal, decoded separators, extra extension, and a six-character non-`mother` stem are rejected; 7203, `285A`, lowercase case variant, missing file, and exact `mother.duckdb` behavior are covered. |
| Regressions | Existing request-coordinator, minute-history, board/tape, paper-trading/server, GraphQL mother, static MIME, and replay tests remain green. |

## Dependencies

- Existing DuckDB dependency and typed Python conventions.
- Existing repository HTTP client, cache directory, download/locking/atomic-replacement patterns.
- Existing `RequestCoordinator` with a dedicated `daily-context` kind.
- Existing Lightweight Charts 4.2.0 runtime.
- Existing native ES modules and Node `node:test`/`node:assert/strict` stack.
- Existing FastAPI and `httpx.MockTransport` test infrastructure.
- No new package, dependency version, migration, or persistent schema change.

Implementation order is backend test contract -> backend loader -> API/Cloud path -> pure JS tests -> pure JS logic -> DOM/chart lifecycle -> app integration -> documentation/full validation. Steps 3, 5, 6, and 7 depend on the contracts established before them.

## Validation Commands

```powershell
uv run pytest tests/test_tickreplay_daily_context.py -v
uv run pytest tests/test_tickreplay_server.py tests/test_cloud_run_main.py -v
uv run pytest tests/test_tickreplay_minute_context.py -v
node --test src/tickreplay/static/daily-chart.test.mjs
node --test src/tickreplay/static/*.test.mjs
node --check src/tickreplay/static/daily-chart.mjs
node --check src/tickreplay/static/app.js
uv run ruff check src/tickreplay/daily_context.py src/tickreplay/server.py cloud-run/main.py tests
uv run ruff format --check src/tickreplay/daily_context.py src/tickreplay/server.py cloud-run/main.py tests
uv run pytest -v
```

Plan-document gates before implementation handoff:

```powershell
uv run python .agents/skills/_shared/validate_doc.py --contract plan-doc --file .agents/docs/plans/daily-chart-moving-averages.md
uv run python .agents/skills/_shared/workspace.py --skill plan --slug daily-chart-moving-averages --verify
```

## Manual Acceptance

- Start replay, create pending orders/positions, change speed, and capture cursor, board, tape, clock, tick points, and minute viewport. Switch minute -> daily -> minute repeatedly; replay and trading continue and state matches normal progression, with each price-chart viewport restored independently.
- Enter daily before a new session response completes. Confirm no request uses the requested date; the request starts only after `/api/session` returns and uses its actual `meta.date`/`meta.code` identity.
- During a delayed daily fetch, switch to minute. Confirm the valid response is cached but does not paint the hidden daily chart; switch back and confirm it renders without refetch. Repeat with a failed response and confirm the next daily selection retries.
- Change to the same stem on a different date, then to a different stem while requests are delayed. Confirm stale data/status never appears.
- At x1 and x500, verify tick chart, board, tape, clock, orders/fills/positions, cursor, and minute-history continue while daily is visible.
- Seek backward after the partial candle and SMA tail exist; confirm the old high/low/close/volume and terminal SMA points disappear before the rebuilt values appear. Reset and compare with a fresh load at the same cursor.
- Use a zero-tick session and confirm completed history may display but no selected-day candle or SMA observation is synthesized.
- Resize while minute is visible and while daily is visible, including creating daily only after a resize. Confirm both charts fill the stage and retain independent logical ranges and crosshairs after switching.
- Operate the selector entirely by keyboard and verify focus, selected state, status announcement, and no chart keyboard/crosshair interference. Check narrow/mobile wrapping and touch targets at and below 768px.
- Confirm valid empty history reports an empty available chart, unavailable history reports a distinct non-blocking state, short history omits only the not-yet-defined SMA, and minute replay remains usable in all cases.
- Confirm `285A` loads enough cross-partition history for SMA200 when 200 observations exist, raw values match replay scale, and documented split discontinuities remain unadjusted.

## Risks and Mitigations

### Risks & Considerations

- **Look-ahead leakage:** SQL is strict-before the actual session date, and the selected day comes only from replay ticks. Tests use a stored selected-day row with conspicuously different values to prove it is never returned.
- **Replay regressions from mode guards:** Guard only leaf price-chart writes. Snapshot/baseline tests prove `step()`, `redrawAll()`, marker computation, minute-history commits, board/tape/clock, and paper trading continue.
- **Shared-series corruption:** Separate DOM containers, chart instances, series objects, time scales, and viewports make cross-mode writes structurally impossible; mocked ownership tests enforce this.
- **Session races:** Cancel/increment/clear before awaits, derive full identity from actual server metadata, and require generation/token/identity checks before cache/state work plus active-mode identity checks before chart work.
- **Unavailable/empty ambiguity:** The explicit `available` flag distinguishes successful empty history from supplementary-data failure. Only `available=true` payloads are cacheable.
- **Duplicate/corrupt data:** Validate before date-group selection; collapse only exact tuples and omit a date on any conflict/invalid row. Frontend revalidates defensively.
- **Memory/query cost:** Perform descending limit then ascending presentation in SQL and materialize no more than 500 bars; no paging or full-history Python list is introduced.
- **Corporate actions:** Raw point-in-time prices are required for replay-scale consistency. SMA discontinuities around splits are accepted and documented; no adjustment column is touched.
- **Seek tail leakage:** Clear partial and both terminal SMA tails before a deterministic fold through the target cursor; compare incremental and rebuilt outputs in tests.
- **Hidden sizing and viewport drift:** Use a stable-size layered stage, update both instantiated charts on resize, and restore per-mode logical ranges only after applying current dimensions.
- **Cloud file exposure:** Limit the widened route to 4-5 ASCII alphanumeric daily stems plus exact `mother`; test encoded traversal, extra suffixes/extensions, lowercase variants, and six-character rejection.
- **Large `app.js` integration surface:** Put normalization/state/math in the pure module, keep app changes to lifecycle/wiring and explicit chart-write leaves, and run all existing JS/minute-history regressions.

## Fixed Decisions

- The official source is per-stem `stocks_daily/{stem}.duckdb`; `mother.duckdb` remains only an existing Cloud Run/GraphQL regression path, not the TickReplay daily source.
- Historical daily prices and SMA closes are raw point-in-time values. Adjustment columns are never read.
- History is strictly before the actual `/api/session` date; the selected-day candle is replay-derived from ticks through the cursor.
- Daily uses a separate, lazily created, retained chart instance and four daily-owned series.
- SMA25/SMA200 are simple moving averages in daily mode only and start exactly at 25/200 valid observations, including a non-empty partial day as one observation.
- Daily charts have no trade markers.
- Daily history is bounded to the newest 500 completed sessions and has no paging.
- Valid empty data is available; missing/corrupt/unreachable data is unavailable. Failures are not cached and daily selection retries.
- Existing `/api/session` and `/api/minute-context` contracts and minute-history behavior are preserved.
- Tick chart, board, tape, clock, paper trading, replay speed/cursor semantics, orders, and positions remain in operation in both modes.
- No new dependency, migration, or database schema change is allowed.
- Implementation route after approval is `/team-execute` with slug `daily-chart-moving-averages`.

### Open Questions

None. All product and architecture decisions needed for implementation are fixed above.

# Minute-chart interaction and historical loading: codebase scan

Date: 2026-08-22  
Scope: planning only; no application or test changes made by this investigation.

## Decision summary

The minute chart is not configured as non-interactive. Lightweight Charts 4.2.0
has mouse/touch scrolling and scaling enabled by default in the vendored build.
The apparent lock is caused by application code: while replay is active,
`step()` calls `followViews()` on every animation frame, and `followViews()`
unconditionally writes a new minute-chart logical range. A user's pan or zoom
therefore takes effect briefly and is overwritten on the next frame.

Older bars can be paged without a new backend endpoint. The existing
`GET /api/minute-context` contract already returns up to `limit` chronological
minute bars strictly before an arbitrary date/time cutoff. The frontend can call
it again with the oldest loaded bar as the next cutoff. The main work is safe
frontend coordination: stop the per-frame minute range write, detect movement
near the left edge, serialize/cancel history requests, prepend to both canonical
bar arrays, and restore the visible logical range after prepending.

## Decision-relevant findings

### 1. The replay loop, not chart options, locks the minute viewport

- `src/tickreplay/static/app.js:203-212` creates the minute chart without
  disabling `handleScroll` or `handleScale`.
- The vendored `src/tickreplay/static/vendor/lightweight-charts.standalone.production.js`
  is v4.2.0. Its local defaults enable wheel/drag/touch scrolling, wheel/axis/
  pinch scaling, and `shiftVisibleRangeOnNewBar`; it exposes
  `getVisibleLogicalRange`, `setVisibleLogicalRange`,
  `subscribeVisibleLogicalRangeChange`, and `barsInLogicalRange`.
- `app.js:869-891` defines `followViews()`. It computes a fixed 90-bar minute
  window plus five logical bars of right padding and calls
  `minuteChart.timeScale().setVisibleLogicalRange(...)` unconditionally.
- `app.js:935-983` runs `step()` through `requestAnimationFrame`; while playing,
  `step()` calls `followViews()` at line 975 even in frames that contain no new
  trade. Thus any manual time-scale pan/zoom is replaced as often as the display
  refreshes.
- The paused path returns before `followViews()`, explaining why the same chart
  interactions work when replay is stopped.

The smallest behavioral correction is to separate tick-chart following from
minute-chart initial/reposition behavior. Keep the tick range update in the
animation loop, but set the minute logical range only on session initialization
and explicit seek/reset. Let Lightweight Charts' existing
`shiftVisibleRangeOnNewBar` behavior follow new candles while the view remains at
the right edge; once the user scrolls away, the library does not need an
application-level per-frame override.

### 2. Current minute data has two sources and one canonical rebuild path

The relevant frontend flow is:

1. `bootstrap()` / controls call `loadSession()` (`app.js:1064-1111`).
2. `RequestCoordinator.fetchUntilReady('session', ...)` loads one trading day's
   ticks from `/api/session` and fills typed arrays (`t`, `price`, `qty`) plus
   `type`.
3. `loadMinuteContextBars()` (`app.js:1045-1062`) performs one direct
   `getJson()` call to `/api/minute-context`, with the first tick minute as its
   cutoff and `MINUTE_CONTEXT_BARS = 30`.
4. The response becomes `state.contextBars`, then `seekTo()`
   (`app.js:920-931`) shallow-copies it into `state.bars` and re-aggregates all
   replay ticks up to the target virtual time.
5. `redrawAll()` (`app.js:896-917`) bulk-replaces both candle/volume series;
   normal playback subsequently uses `series.update()` only for the current/new
   replay-derived minute bars.

Consequences for lazy history:

- Loaded older pages must be prepended to both `state.contextBars` and the
  current `state.bars`. Updating only `state.bars` loses the history on the next
  `seekTo()`/reset because `seekTo()` reconstructs from `contextBars`.
- Prepending requires `setData`, not `series.update`: Lightweight Charts rejects
  updates older than the current last data time.
- The merge must remain strictly chronological and deduplicate by `time`, even
  though the current strict-before contract normally produces non-overlapping
  pages.
- A history response may arrive while new replay bars are being appended. Merge
  into the latest arrays at completion rather than a snapshot taken when the
  request began.
- The newly added paper-trading code calls `refreshMarkers()` from `redrawAll()`
  and derives minute marker bounds from `state.bars`; a history-specific redraw
  should preserve/rebuild markers as well, or use the existing `redrawAll()`
  conventions without resetting replay/trading state.

### 3. The existing backend contract can page older bars unchanged

`src/tickreplay/server.py:468-494` exposes:

```text
GET /api/minute-context?stem=&code=&date=&time=&limit=
```

`limit` is validated by FastAPI as 1..500. The endpoint returns
`{"bars": [...]}` and deliberately does not use the trades download
`pending/operationId` protocol.

`src/tickreplay/minute_context.py:140-196` implements the query:

- `Code = ?` uses a bound parameter.
- The cutoff is strict: rows satisfy `Date < before_date` or the same date with
  `Time < before_time`.
- Results are selected newest-first with `LIMIT ?`, then reversed in Python, so
  the response is oldest-first.
- Each bar is `{time, open, high, low, close, volume}`; `time` is integer epoch
  seconds under the application's UTC-interpreted wall-clock convention.

Therefore the next page can use the earliest loaded bar's UTC-derived
`YYYY-MM-DD` and `HH:MM` as its cutoff. No `server.py`, `minute_context.py`,
repository, cache, or cloud-file-server change is required for the minimum
feature.

The backend path is:

```text
app.js
  -> /api/minute-context
  -> server.read_minute_context()
  -> minute_context.bars_before()
  -> <cache_dir>/stocks_minute/<stem>.duckdb
     (downloaded once from /jp/stocks_minute/<stem>.duckdb if absent)
  -> parameterized DuckDB query
```

### 4. Left-edge loading needs explicit trigger, request, and viewport guards

The vendored chart API supports the standard trigger:

```text
timeScale.subscribeVisibleLogicalRangeChange(range)
  -> candleSeries.barsInLogicalRange(range).barsBefore
  -> load when barsBefore is below a small threshold
```

However, `setVisibleLogicalRange()` and `setData()` can also emit visible-range
changes. The handler needs at least:

- a manual-interaction/initialized guard so initial range setup does not
  immediately page repeatedly when the default 30 bars start at logical index
  zero;
- a per-session `loading` flag so one gesture cannot start parallel pages;
- an `exhausted` or retry/cooldown policy for empty responses;
- a session generation (or equivalent identity check) so a response for an old
  date cannot mutate the newly selected session;
- preservation of the visible window after prepend: capture the current logical
  range, prepend `N` unique bars with `setData`, then restore
  `{from: old.from + N, to: old.to + N}`. Without this offset, the same logical
  indexes point at different (older) bars and the user's viewport jumps.

Manual interaction can be armed from the existing `#minute-chart` element's
`pointerdown`/wheel/touch path, while the logical-range subscription decides
whether the left edge is close enough. This avoids treating the initial
programmatic range as user navigation.

### 5. Existing request coordination should be reused, but session identity is a gap

`src/tickreplay/static/request-coordinator.mjs:92-252` already provides the
project convention for cancellable, latest-wins frontend requests:

- `fetchLatest(kind, url, {stem})` aborts an older request of the same kind.
- `selectStem(stem)` aborts active requests belonging to a different symbol.
- `cancel(kind)` explicitly aborts one channel.

Use a dedicated kind such as `minute-history` instead of raw `getJson()` for
history pages. At the beginning of `loadSession()`, explicitly cancel that kind
and reset paging state. Symbol selection alone is insufficient because changing
the date while retaining the same symbol does not make `selectStem()` cancel the
old request. A monotonically increasing session generation checked before merge
is the safest small guard.

The current initial `loadMinuteContextBars()` bypasses `RequestCoordinator` and
catches every error as `[]`. Bringing the initial preload and subsequent pages
through the same request kind would remove an existing same-symbol/session race;
if cancellation is caught, it must remain distinguishable from a genuine empty
history response rather than being converted to `[]` and applied to stale state.

### 6. The backend's best-effort semantics create one product decision

`minute_context.py` intentionally maps missing files, unreachable servers,
corrupt downloads, bad SQL, and genuinely exhausted history to the same empty
list. This preserves the current guarantee that optional minute context can
never break a replay load, but the client cannot know whether `[]` means "no
older candles exist" or "history is temporarily unavailable."

Minimal compatible choice: retain best-effort behavior, treat an empty page as
exhausted for the current session, and retry after the next session load. This
prevents a stationary left-edge viewport from causing a request loop.

If the requirement means guaranteed/retryable history rather than optional
context, the API must grow an explicit status (`exhausted`, `unavailable`, and
possibly an error reason). That would expand the affected surface to
`server.py`, `minute_context.py`, and their tests and should be a deliberate
scope decision, not an incidental frontend change.

### 7. Test coverage and documentation do not currently cover this behavior

- `src/tickreplay/static/request-coordinator.test.mjs` has ten Node tests for
  request races/status polling and one extracted `app.js` XSS regression. It has
  no chart viewport, visible-range subscription, prepend, deduplication, or
  stale-history-response test.
- `tests/test_tickreplay_minute_context.py` covers chronological order, strict
  cutoff, missing/unreachable/corrupt data, invalid stem, and local cache reuse.
- `tests/test_tickreplay_server.py:266-302` covers a successful context response
  and the empty-list degradation contract.
- `docs/tick-replay.md:127` is already stale: it states that `stocks_minute` is
  not read, even though the frontend now preloads it. Its API table also omits
  `/api/minute-context`.
- The frontend uses native ES modules and Node's built-in test runner; there is
  no configured DOM/browser chart test harness. Pure helpers are easiest to unit
  test in a small `.mjs` module, while one real-browser acceptance pass is still
  needed for gesture behavior.

## Affected-file and dependency map

| File | Current responsibility | Minimum change? |
| --- | --- | --- |
| `src/tickreplay/static/app.js` | Chart creation, replay loop, viewport following, state, initial minute-context fetch, series redraw | Yes: stop per-frame minute range forcing; add left-edge paging orchestration and safe merge |
| `src/tickreplay/static/request-coordinator.mjs` | Latest-wins requests, abort/cancellation, stem selection | Reuse unchanged unless a higher-level session key is intentionally added |
| `src/tickreplay/static/request-coordinator.test.mjs` | Existing native-Node frontend regression pattern | Add focused tests if helpers remain in `app.js`; otherwise keep unchanged and add a dedicated test module |
| `src/tickreplay/static/minute-history.mjs` (new, recommended) | Pure trigger/merge/range-shift/session-state helpers | Recommended because `app.js` is already 1,248 lines, well beyond the shared 800-line maximum |
| `src/tickreplay/static/minute-history.test.mjs` (new, recommended) | Paging trigger, dedupe/order, logical-range preservation, stale-generation tests | Recommended |
| `src/tickreplay/server.py` | `/api/minute-context` HTTP validation/serialization | No for best-effort reuse; yes only for richer status semantics |
| `src/tickreplay/minute_context.py` | Download/reuse minute DuckDB and query bars before cutoff | No for best-effort reuse; yes only for richer status semantics/performance changes |
| `tests/test_tickreplay_minute_context.py` | Loader/query/failure contract | Existing coverage is adequate for API reuse; optionally add consecutive-page non-overlap |
| `tests/test_tickreplay_server.py` | HTTP contract/static serving | Existing coverage is adequate for API reuse; update only if response schema changes |
| `docs/tick-replay.md` | User/operator behavior and API documentation | Yes: correct stale `stocks_minute` statement and document lazy paging/interaction |
| `cloud-run/main.py` | File-server allowlist includes `stocks_minute/<numeric>.duckdb` | No for numeric JPX stems; see risk below |

## Recommended minimal change surface

### Recommended quality-complete minimum

1. `src/tickreplay/static/app.js`
   - Split `followViews()` so the animation loop follows only the tick chart.
   - Apply the minute's initial logical range from session load/explicit seek,
     not on every replay frame.
   - Subscribe to visible logical range changes and invoke a single serialized
     older-page load after manual navigation approaches the left edge.
   - Route requests through `RequestCoordinator.fetchLatest('minute-history',
     ..., {stem})`; cancel/reset on every `loadSession()` and check session
     generation before merging.
   - Merge unique chronological bars into `contextBars` and current `bars`, bulk
     update candle/volume series, refresh markers, and restore the offset logical
     range.

2. `src/tickreplay/static/minute-history.mjs` and
   `src/tickreplay/static/minute-history.test.mjs`
   - Keep trigger, cutoff conversion, dedupe/merge, and range-shift calculations
     pure and testable. This avoids growing the already oversized `app.js` and
     avoids needing a browser DOM merely to test state transitions.

3. `docs/tick-replay.md`
   - Update data-source, operation, and API sections.

No backend/runtime contract change is required. If strict file-count minimalism
is preferred, the pure helpers and tests can instead be appended to `app.js`
and `request-coordinator.test.mjs` using the existing `extractFunction()` /
`runInNewContext()` pattern, but that worsens the existing file-size violation
and couples chart-history tests to source extraction.

## Tests and acceptance commands

Automated tests to add:

1. User/manual mode is not overwritten by replay-frame following (the minute
   range setter is not called from the steady-state `step()` path).
2. A visible range near the left edge requests exactly one older page while a
   load is active.
3. Prepended bars are oldest-first, duplicate timestamps are removed, and both
   canonical arrays retain them across a seek rebuild.
4. Prepending `N` bars shifts both logical range endpoints by exactly `N`.
5. Empty history stops request storms under the chosen best-effort policy.
6. A response from an old session/date is ignored, including same-symbol date
   changes.
7. Subsequent left navigation pages from the new earliest bar cutoff.

Concrete commands (explicit paths are intentional on Windows; do not pass a
literal `*.test.mjs` path glob):

```powershell
node --check src/tickreplay/static/app.js
node --check src/tickreplay/static/minute-history.mjs
node --test --test-isolation=none src/tickreplay/static/request-coordinator.test.mjs src/tickreplay/static/minute-history.test.mjs src/tickreplay/static/paper-trading.test.mjs
uv run pytest tests/test_tickreplay_minute_context.py tests/test_tickreplay_server.py -q
```

Current observed baseline on 2026-08-22:

- `node --check src/tickreplay/static/app.js`: passed after the concurrent
  paper-trading integration.
- Node explicit-path suite: 21/21 passed (10 request-coordinator + 11
  paper-trading tests).
- `uv run pytest tests/test_tickreplay_minute_context.py
  tests/test_tickreplay_server.py -q`: 27/27 passed, with one existing
  Starlette/httpx deprecation warning.

Manual browser acceptance remains necessary:

1. Start replay, drag the minute chart left and zoom in/out while the clock and
   trades continue; the chosen viewport must remain stable.
2. Pan near the oldest loaded candle; an older page should appear without a
   horizontal jump, duplicates, or playback pause.
3. Continue left across two or more pages and then reset/seek; already loaded
   history should remain available.
4. Change dates (including the same symbol) during an in-flight history fetch;
   the old response must not contaminate the new chart.
5. Simulate unavailable minute history; replay must continue and the client
   must not issue an unbounded request loop.

## Risks and open questions

1. **Auto-follow re-entry semantics:** after manual navigation, should explicit
   reset/seek/session load return the minute chart to live follow? The minimum
   recommendation says yes; no new UI button is required. If users need to
   return to live without reset/seek, add a small "latest" action later.
2. **Empty response ambiguity:** current best-effort `[]` cannot distinguish
   exhausted history from transient failure. Decide whether per-session
   exhaustion is acceptable or whether the API response must become explicit.
3. **Viewport preservation:** prepending changes logical indexes. Failure to
   offset the captured range by the unique prepend count produces a visible
   jump and can retrigger paging recursively.
4. **Concurrency:** `RequestCoordinator` keys selection by stem, not date.
   Cancellation plus a session generation check is required for same-symbol
   day changes and for responses racing with live replay updates.
5. **Performance:** each page opens the local minute DuckDB and performs an
   ordered cutoff query. A bounded page (for example 200-500 bars), one in-flight
   request, and threshold/cooldown are important; repeatedly materializing tens
   of thousands of bars may eventually affect browser memory and `setData`
   cost.
6. **Symbol coverage:** the repository accepts 4-5 character alphanumeric stems,
   but `cloud-run/main.py` currently allows remote `stocks_minute` paths only for
   numeric stems. Existing minute context already silently fails for an
   alphabetic stem; the feature should either document numeric-only history or
   widen and test the file-server allowlist in a separate scope.
7. **Concurrent working tree:** while this scan was running, another task
   modified `app.js`, `index.html`, and `styles.css` and added untracked
   `paper-trading.mjs` / `paper-trading.test.mjs`. Implementation must start from
   the integrated current files and must not overwrite or delete those changes.


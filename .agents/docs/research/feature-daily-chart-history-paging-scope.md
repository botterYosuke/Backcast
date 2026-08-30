# Daily Chart History Paging Scope Decision

## Scope Assessment

The requested behavior has two separable parts: set Daily's initial logical
viewport to the newest 90 observations plus 5 logical bars of right padding,
and fetch older Daily pages only after a real user pan or zoom approaches the
left edge. The replay-derived partial day counts as one of the 90 observations.
With fewer than 90 observations, show all available observations and retain the
five-bar right padding.

Two initial-fetch strategies were considered:

- **A - Keep the existing 500-bar initial fetch, display only 90+5, then page
  older.** This preserves the current endpoint, client cache, and load-time SMA
  precomputation. The newest visible 90 bars have at least 199 prior closes for
  SMA200 whenever the source has sufficient history. The unused initial bars
  are a useful left-pan buffer and only affect a small bounded JSON payload.
- **B - Fetch 90 visible bars plus SMA warm-up, then page.** Correct SMA200 for
  all 90 visible completed bars needs up to 199 preceding observations, so this
  still requires roughly 289 completed bars (or 288 plus a partial day). It
  saves at most 211 rows initially but adds separate visible/warm-up accounting,
  more boundary cases, and cache semantics without improving the viewport.

**Decision: choose A.** Keep `limit=500` for the initial request and use a
Daily-specific page size of 200 for older requests. The API cap remains a
per-query cap, not a total client-history cap.

## Complexity Classification

**COMPLEX.** The behavior crosses at least five files and combines asynchronous
request state, chart mutation, cached canonical data, SMA recomputation, and
viewport preservation. The likely implementation touches two runtime modules,
JavaScript tests, documentation, and the durable design contract; an additional
Python regression test is recommended even though the backend implementation
does not need to change.

## Integration Points

- `app.js` should replace Daily's one-time `fitContent()` path with an initial
  logical range of `{from: max(0, lastIndex - 89), to: lastIndex + 5}`. A mode
  switch within the same session restores the saved Daily range; a new replay
  session resets it and applies the initial range once after valid Daily data is
  ready.
- Arm Daily paging only on `wheel`, `pointerdown`, or `touchstart` on the Daily
  chart. The visible-logical-range callback may request a page only while Daily
  is active, the current session is ready, the callback is not programmatic,
  and `barsInLogicalRange(range).barsBefore <= 10`.
- Page using `/api/daily-context?stem=<stem>&date=<oldestHistoryDate>&limit=200`.
  The cutoff must be `historyBars[0].time`, never the selected-day partial bar.
  The current backend already supports arbitrary exclusive cutoffs and bounded
  chronological results, so neither `daily_context.py`, `server.py`, the API
  schema, nor `cloud-run/main.py` requires an implementation change.
- Admit before fetch. Each page token must carry generation, full
  `stem|code|actualDate` identity, and cutoff; immediately after `await` and
  before mutation, recheck the token, session readiness, full identity, and
  current `state.meta`. Maintain one in-flight page, session-local exhaustion,
  and per-cutoff failure state (5-second cooldown, maximum 3 failures), matching
  the proven Minute policy.
- Normalize each page with its requested maximum. `available=true,bars=[]` and
  a valid duplicate-only page mean exhaustion. Transport errors, cancellation,
  `available=false`, malformed payloads, and non-empty all-invalid payloads
  preserve current data; non-cancellation failures remain retryable under the
  cooldown policy. Accept only unique ISO-date bars strictly older than the
  current boundary, sort ascending, and retain existing bars on collisions.
- On `N` unique prepends, recompute frozen historical SMA25/SMA200 and terminal
  rolling windows once for the new canonical history. Older data may create SMA
  points before the former first point, but SMA values whose complete window
  was already loaded, as well as the partial-day terminal SMA, must remain unchanged.
  Replay ticks must continue to update only the O(1) terminal point. Continue
  cloning frozen SMA points at Lightweight Charts write boundaries.
- Apply an active-chart page transactionally: capture the latest logical range,
  write all four series, and set `{from: old.from + N, to: old.to + N}` inside
  the programmatic guard. Update `dailyViewport.savedRange` explicitly because
  suppressed callbacks cannot capture the shifted range. If the user switches
  to Minute while the request is in flight, a still-current response may update
  canonical Daily state/cache and the shifted saved range without writing the
  hidden chart; render it on return. A new session cancels the request and
  invalidates its generation.

## Affected Files

Required implementation and contract changes:

- `src/tickreplay/static/app.js` - Daily 90/5 policy, interaction arming,
  left-edge callback, page request/apply wiring, reset/cancellation, and hidden
  mode handling.
- `src/tickreplay/static/daily-chart.mjs` - initial-range helper and
  Daily-specific paging state/normalization/merge/commit/SMA transaction logic.
- `src/tickreplay/static/daily-chart.test.mjs` - controller, range, merge, SMA,
  stale-response, and source-wiring coverage.
- `docs/tick-replay.md` - Daily initial view, paging policy, retries,
  exhaustion, and viewport/SMA behavior.
- `.agents/docs/DESIGN.md` - supersede the explicit total-500/no-paging
  constraint while retaining the 500-row per-query backend bound and load-time
  SMA rule.

Recommended contract-only test change:

- `tests/test_tickreplay_daily_context.py` (or
  `tests/test_tickreplay_server.py`) - one two-page strict-before regression
  proving adjacent pages are chronological, non-overlapping, and exhaustive.

No runtime change is expected in `src/tickreplay/daily_context.py`,
`src/tickreplay/server.py`, or `cloud-run/main.py`.

## Risks and Concerns

- `DailyChartSession.cache` currently stores one frozen initial result per
  identity. Every accepted prepend must replace that identity's cached value;
  otherwise reusing the same session cache discards paged history.
- A chart-write failure must not commit only some series or advance the paging
  boundary. Stage the merged history/SMA/range, restore prior series/range on
  failure, and commit canonical state/cache only after a successful active
  chart transaction. Hidden-mode commits avoid chart writes entirely.
- A response can arrive after further user panning or a mode switch. Shift the
  latest range at apply time, not a range captured when the request began.
- Programmatic `setData` and range restoration can synchronously or
  asynchronously emit visible-range callbacks. The Daily page controller and
  `ChartViewportState` guards must overlap until scheduled release so the apply
  does not recursively load another page.
- Loaded Daily history grows by 200-bar pages until source exhaustion. This is
  acceptable for the finite per-symbol trading-day dataset and session lifetime,
  but it deliberately replaces the current total-500 client bound; adding a
  retention window is a separate design requiring SMA and viewport eviction
  semantics.
- The working tree already contains user-owned uncommitted Daily/SMA and replay
  fixes. Implementation must be based on that state and must not reset, stage,
  or rewrite unrelated changes.

## Recommended Approach

1. Add pure Daily initial-range, page-normalization, merge, and paging-controller
   tests first, reusing policy values and invariants from `MinuteHistorySession`
   without refactoring the proven Minute module.
2. Extend `DailyChartSession` with separate initial-request and older-page state,
   page-token guards, exhaustion/failure tracking, immutable cache replacement,
   and once-per-page history/SMA recomputation.
3. Add a transactional Daily page-apply helper that rewrites all four series,
   preserves the logical range by exactly `+N`, and handles active versus hidden
   Daily mode explicitly.
4. Wire Daily user gestures and visible-range admission in `app.js`, reuse the
   existing `daily-context` request-coordinator kind sequentially, and reset the
   controller before the first session-load await.
5. Update focused docs/design, then run browser acceptance with replay active at
   both x1 and x500.

## Validation

Focused automated checks:

```powershell
node --test src/tickreplay/static/daily-chart.test.mjs src/tickreplay/static/minute-history.test.mjs
node --check src/tickreplay/static/daily-chart.mjs
node --check src/tickreplay/static/app.js
uv run pytest tests/test_tickreplay_daily_context.py tests/test_tickreplay_server.py -q
```

Tests must cover 0, fewer-than-90, exactly-90, 500, and 500-plus-partial initial
observations; exact 90+5 range; no request before a user gesture; threshold
10/11; single-flight; cooldown and maximum retries; empty and duplicate-only
exhaustion; malformed/unavailable/all-invalid retry; two ordered pages; full
identity/generation staleness; an in-flight Daily-to-Minute switch; exact `+N`
active and saved-range compensation; rollback on every chart-write stage;
unchanged previously complete SMA values and terminal SMA; and no change to
Tick, Tape, board, order, position, or Minute replay ordering.

Browser acceptance should confirm the initial Daily view shows 90 bars and five
right-padding bars, repeated left-edge pans/zooms add older bars without a
horizontal jump, switching modes during a request is safe, replay/Tick/Tape
continue at x1 and x500, SMA25/SMA200 remain continuous, and runtime exceptions
stay at zero.

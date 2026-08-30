# Daily Chart History Paging Architecture

## Architecture Design

Daily history paging remains a client-side extension of the existing Daily
aggregate. The server contract already provides the required primitive:
`/api/daily-context` returns at most 500 valid bars strictly before any supplied
date in ascending order. The initial request continues to use the actual replay
session date and `limit=500`; older requests use the oldest completed Daily bar
as the exclusive cutoff and `limit=200`. No backend, schema, dependency, or
Minute-history change is required.

The architecture separates four concerns:

1. `DailyChartSession` owns canonical completed bars, the identity cache,
   precomputed SMA state, initial-request state, and older-page request state.
2. Pure helpers in `daily-chart.mjs` calculate the initial logical range,
   normalize and merge older pages, shift a logical range, and build immutable
   page plans without touching the chart.
3. A transactional chart helper applies a prepared plan to candle, volume,
   SMA25, and SMA200 series, restores the prior chart on failure, and preserves
   the viewport by the unique prepend count.
4. `app.js` owns DOM gestures, Lightweight Charts callbacks, request I/O, mode
   checks, and replay-session lifecycle wiring.

The existing initial 500-bar fetch is a warm-up and navigation buffer, not the
visible-bar count. Daily's first non-empty snapshot receives this range:

```text
lastIndex = barCount - 1
from = max(0, lastIndex - 89)
to = lastIndex + 5
```

The replay-derived partial day is included in `barCount`. If the initial
history is empty, the session remains uninitialized until the first partial-day
tick creates a bar; that first bar then receives `{from: 0, to: 5}`. Once set,
normal replay, seek/reset rebuilds, page prepends, and mode switches must not
reapply the initial range.

## Module Structure

### `src/tickreplay/static/daily-chart.mjs`

Keep Daily paging in the existing module rather than importing or refactoring
`minute-history.mjs`. The two controllers share policy values and invariants,
but not mutable state or response contracts. This module owns:

- `DAILY_HISTORY_DEFAULTS`: immutable policy values for `visibleBars: 90`,
  `rightPaddingBars: 5`, `pageSize: 200`, `edgeThresholdBars: 10`,
  `failureCooldownMs: 5000`, and `maxFailuresPerCutoff: 3`.
- Pure viewport, URL, merge, and range-shift helpers.
- Initial and page payload validation through the existing Daily OHLCV rules.
- `DailyChartSession`, including older-page admission and prepared-plan commit.
- A DOM-free transaction helper that writes all four chart series through
  injected ports and performs rollback.
- Existing partial-day and O(1) terminal-SMA behavior without changing its
  public snapshot contract.

Private helpers should keep history derivation pure: precompute SMA25/SMA200,
the trailing 24/199-close windows, and terminal points from an explicit
`historyBars + partialBar` input. Metrics increment only when a prepared page is
committed, not when a failed chart transaction merely calculated a plan.

### `src/tickreplay/static/app.js`

`app.js` remains the integration layer. Add no Daily paging calculations here.
It owns:

- applying the initial logical range after the first non-empty Daily snapshot;
- arming history from Daily `wheel`, `pointerdown`, and `touchstart` gestures;
- converting visible-range callbacks into page admission decisions;
- calling `requests.fetchLatest('daily-context', ...)` after admission;
- rechecking replay metadata after `await`;
- selecting active-chart transaction versus hidden-mode canonical commit;
- canceling `daily-context`, resetting the Daily generation, and clearing
  `dailyViewport.savedRange` before the first session-load await.

### Tests and documentation

- `daily-chart.test.mjs` owns pure controller and transaction coverage plus
  source-level wiring assertions for `app.js`.
- `test_tickreplay_daily_context.py` should add one two-page strict-before
  contract regression; production Python files remain unchanged.
- `docs/tick-replay.md` documents the 90+5 viewport and 200-bar paging policy.
- `DESIGN.md` replaces the total-client-500/no-paging constraint with a
  per-query maximum of 500 and finite source-lifetime client growth.

## Interface Design

The names below are the intended implementation contract. Return objects should
be frozen when they enter session state or cross an asynchronous boundary.

### Pure exported helpers

```js
dailyInitialLogicalRange(barCount, options = {})
// -> null when barCount <= 0
// -> { from: number, to: number }

dailyContextUrl(identity, options = {})
// options: { beforeDate?: string, limit?: number }
// -> string | null
// With no options, preserves the current actualDate/limit=500 URL.

mergeOlderDailyBars(existingBars, olderBars, { boundary } = {})
// -> { bars, added, addedBars }
// `bars` is ascending; only unique dates strictly before boundary are added.

shiftDailyLogicalRange(range, added)
// -> { from, to } | null

applyDailyHistoryPage({ session, plan, viewport, ports, timeScale })
// -> { committed: true, added, shiftedRange }
// Throws after best-effort rollback if any series/range/commit stage fails.
```

`ports` has the same four owners used by `renderDailySnapshot`:

```js
{
  candle: { setData(data) },
  volume: { setData(data) },
  sma25: { setData(data) },
  sma200: { setData(data) },
  paintCandle(bar),
  paintVolume(bar),
}
```

The transaction helper captures `timeScale.getVisibleLogicalRange()` at apply
time, not request time. It renders `plan.nextSnapshot`, sets the shifted range
inside `viewport.runProgrammatic`, commits the session plan, and finally calls
`viewport.replace(shiftedRange)`. On failure it restores `plan.previousSnapshot`
and the previous live range inside the same programmatic guard. A failed
rollback is reported as an `AggregateError`; no canonical page commit is
allowed before all chart writes and range restoration succeed.

### `DailyChartSession` additions

Keep the existing `request` for the initial payload. Add independent state:

```js
historyPageRequest  // token | null
historyArmed        // boolean
historyExhausted    // boolean
historyFailures     // Map<cutoff, { count, retryNotBefore }>
hasInitialViewport // replaces hasFitContent
historyPageSize
historyEdgeThresholdBars
```

Public methods:

```js
armHistory(identity = this.identity)
// -> boolean; true only for the current ready identity.

canLoadOlder({ identity, barsBefore, programmatic })
// -> boolean; checks ready identity, armed, finite threshold, not exhausted,
//    not single-flight, and not programmatic.

admitOlderPage(cutoff, { identity = this.identity } = {})
// -> token | null
// token: { id, generation, identity, cutoff }

isCurrentOlderPage(token)
// -> boolean; checks token object identity, generation, full identity, cutoff,
//    current canonical identity, and the single-flight slot.

prepareOlderPage(token, normalizedPayload)
// -> one of:
// { kind: 'stale' }
// { kind: 'failure', token }
// { kind: 'exhausted', token }
// { kind: 'page', token, added, previousSnapshot, nextSnapshot, nextCached }

commitOlderPage(plan)
// -> { accepted: boolean, added: number }

completeOlderExhausted(token)
// -> boolean

failOlderPage(token)
// -> boolean; clears single-flight and records cutoff cooldown/failure count.

abortOlderPage(token)
// -> boolean; clears only the matching single-flight, records no failure.

markInitialViewport()
// -> boolean; sets the flag only for a current non-empty Daily snapshot.
```

`prepareOlderPage` is side-effect free. Valid empty payloads and valid
duplicate-only merges return `exhausted`. Unavailable, malformed, and non-empty
all-invalid payloads return `failure`. A `page` plan contains the exact unique
prepend count and a fully precomputed replacement cache record. The plan is
accepted only while its token remains current.

`commitOlderPage` atomically replaces the identity cache entry and applies the
same cached history to live fields, derives terminal SMA from the current
partial bar, clears the single-flight, clears that cutoff's failure record, and
increments history-precomputation metrics once. It does not change the public
Daily phase from `ready`; page loading and retry failures are supplementary and
must not replace a successfully loaded chart with `loading`, `error`, or
`unavailable`.

`resetSession`, `failSession`, and a generation change clear page request,
arming, exhaustion, failures, and initial-viewport state. Reusing an identity's
cache retains all accepted paged bars, but page exhaustion may reset for the
new replay-session lifecycle.

## Data Flow

### Initial Daily view

1. `loadSession` cancels `daily-context`, resets `DailyChartSession`, and clears
   `dailyViewport.savedRange` before awaiting `/api/session`.
2. The actual `stem|code|actualDate` identity is staged and committed through
   the existing atomic session flow.
3. The existing initial request fetches up to 500 completed bars before
   `actualDate`, validates them, and precomputes historical SMA state once.
4. `renderDailyChart` writes the snapshot. If a saved same-session range exists,
   it restores it. Otherwise, if `hasInitialViewport` is false and the snapshot
   is non-empty, it sets `dailyInitialLogicalRange(snapshot.bars.length)` and
   marks the initial viewport.
5. If initial history is empty, the first partial tick follows the same
   one-time initialization after its candle/volume/SMA updates.

### User-triggered older page

1. A real gesture on `els.dailyChart` calls
   `state.daily.armHistory(currentIdentity)`. Programmatic range writes never
   arm the controller.
2. The Daily visible-logical-range callback first captures the range in
   `dailyViewport`. It exits unless Daily is active, identity/session phase are
   current, `dailyViewport.programmaticDepth === 0`, and candle
   `barsInLogicalRange(range).barsBefore <= 10`.
3. `loadOlderDailyBars` reads `historyBars[0].time`, calls `admitOlderPage` before
   any fetch, and requests the same endpoint with that cutoff and page size 200.
4. Immediately after `await`, it verifies `isCurrentOlderPage(token)`, the full
   `DailyChartSession.identity`, and `dailySessionIdentity(state.meta)`. No
   normalization, chart write, status write, or state mutation precedes these
   checks.
5. It normalizes at most `historyPageSize` bars and asks the session to prepare
   a result. Failure and exhaustion use their matching completion methods.
6. For a prepared page while Daily is active, `applyDailyHistoryPage` captures
   the latest live range, rewrites all four series, sets live range `+N`, commits
   canonical/cache state, and explicitly replaces the saved range with `+N`.
7. If the user switched to Minute after the request began, no hidden chart port
   is touched. The caller shifts the latest `dailyViewport.savedRange` by `N`,
   commits the still-current plan, and stores the shifted saved range. Returning
   to Daily renders canonical data and restores that range.

Only wheel/pointer/touch activity arms paging. Keyboard tab switching, replay
frames, `setData`, initial range setup, restore, seek/reset, resize, and page
range compensation do not arm it. Programmatic callbacks are suppressed until
the scheduled `ChartViewportState` release, covering asynchronous Lightweight
Charts range notifications.

## Error Handling Strategy

- **Stale response:** generation, token, identity, or current `state.meta`
  mismatch returns without chart, cache, phase, failure-count, or exhaustion
  mutation. A later matching session cannot adopt the response.
- **Cancellation:** `abortOlderPage` releases only the matching request and does
  not consume a retry. New-session reset independently invalidates old tokens.
- **Unavailable, transport, malformed, or all-invalid response:** preserve the
  chart and canonical data, call `failOlderPage`, wait five seconds before the
  same cutoff can retry, and stop admitting that cutoff after three failures.
  Other cutoffs remain independently tracked, although a valid strict-before
  flow normally advances only after success.
- **Valid empty or duplicate-only response:** clear the request and mark the
  current session exhausted so range callbacks cannot loop. Exhaustion clears
  on session reset.
- **Partial chart apply:** restore all four prior datasets and the prior live
  range. Leave canonical history, identity cache, oldest cutoff, saved range,
  and SMA rolling state unchanged; record the apply as a retryable failure.
- **Rollback failure:** throw an `AggregateError` containing the original and
  rollback failures. The request loader catches it, records one page failure,
  and replay continues because no error escapes into the animation frame.
- **Inactive mode:** a request admitted while Daily was active may complete and
  commit canonical Daily state, but cannot write the hidden chart. Minute,
  Tick, Tape, board, orders, positions, and replay timing remain outside the
  page transaction.
- **Cache consistency:** accepted pages replace the existing cache record for
  the same full identity. A reload or same-identity cache reuse therefore cannot
  revert to the original 500 bars.

## Test Strategy

### Pure Node tests in `daily-chart.test.mjs`

- `dailyInitialLogicalRange`: 0, 1, 89, 90, 500, and 501 including a partial;
  assert newest 90 and exact `to = lastIndex + 5`.
- URL construction: default actualDate/500 compatibility and oldestDate/200
  page URL validation.
- Merge: unordered input, incoming duplicates, collision with existing bars,
  dates at/after cutoff, invalid rows, duplicate-only exhaustion, and two
  consecutive chronological pages.
- Admission: no gesture, threshold 10/11, single-flight, programmatic guard,
  generation/identity/cutoff staleness, cooldown boundary, three-failure limit,
  cancellation, success clearing failures, and session-local exhaustion.
- Plan/commit: side-effect-free preparation; cache replacement; exact added
  count; phase remains ready; partial day remains last; one SMA precomputation
  per committed page; terminal SMA and already complete historical SMA values
  remain unchanged after prepend.
- Transaction: exact live and saved `+N` compensation; capture latest range at
  completion; mutable clones at SMA chart boundaries; rollback for candle,
  volume, SMA25, SMA200, range, and commit failure; AggregateError on rollback
  failure; canonical state unchanged on every failed stage.
- Hidden mode: no chart-series call, canonical/cache commit succeeds, saved
  range shifts once, and returning to Daily restores the shifted range.
- Existing replay-order tests remain unchanged and prove Tick, Tape, order,
  board, position, clock, scrubber, and liveness ports still run in both modes.

### Source-wiring and Python contract tests

Source assertions should verify Daily has the three gesture listeners, uses
`barsInLogicalRange`, admits before `fetchLatest`, rechecks token and full
identity after `await`, uses the session page size for both URL and
normalization, cancels/resets before the first session await, and never calls
`redrawAll` for a page prepend.

Add one Python test that queries a dataset larger than one page twice: first
with the replay date, then with the first page's oldest date. Assert both pages
are ascending, every second-page date is strictly older than every first-page
date, there are no duplicates, and a subsequent query eventually returns a
valid empty result. No production Python change is expected.

Focused commands:

```powershell
node --test src/tickreplay/static/daily-chart.test.mjs src/tickreplay/static/minute-history.test.mjs
node --check src/tickreplay/static/daily-chart.mjs
node --check src/tickreplay/static/app.js
uv run pytest tests/test_tickreplay_daily_context.py tests/test_tickreplay_server.py -q
```

Browser acceptance must use a fresh page and verify initial 90+5 framing,
multiple left-edge pages without jumps, zoom-triggered paging, a Daily-to-Minute
switch during an in-flight page, mode return with preserved range, SMA25/SMA200
continuity, continuous Tick/Tape at x1 and x500, and zero runtime exceptions.

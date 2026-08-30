# Daily Chart History Paging Plan Validation

## Validation Result (PASS / NEEDS_REVISION)

**PASS.** The revised plan is dependency-ordered, bounded to the approved
files, backwards compatible, and independently testable. Both previous
blockers are now explicit:

1. Step 4 orders the active transaction as four-series writes, live `+N`, saved
   `+N`, then `commitOlderPage(plan)` as the final state mutation. It requires
   complete chart/live/saved rollback on every earlier failure or rejected
   commit, and prohibits any state write after successful canonical commit.
2. Steps 5 and 6 require one shared initial-range helper for initial history
   and first partial day. It runs under `dailyViewport.runProgrammatic`, marks
   `hasInitialViewport` only after successful `setVisibleLogicalRange`, and
   explicitly tests failure followed by retry.

No blocking gap remains in scope, ordering, compatibility, or validation.

## Missing Coverage

No required coverage is missing. The revised RED tests explicitly include:

- saved-range replacement failure before canonical commit;
- rejected canonical commit after chart/live/saved writes, with complete
  rollback;
- event-order proof that no state mutation follows successful commit;
- initial-history and first-partial range-write failure, programmatic
  suppression, false initialization flag, and later successful retry.

The plan also covers:

- 0, fewer than 90, exactly 90, 500, and 500-plus-partial range math;
- first-load-only behavior, new-session reset, and same-session mode restore;
- real wheel/pointer/touch arming and programmatic/inactive rejection;
- token, generation, stem, code, actualDate, cutoff, and `state.meta` staleness;
- single-flight, cooldown, maximum failures, cancellation, and exhaustion;
- invalid, all-invalid, empty, duplicate, colliding, unordered, and multi-page
  payloads;
- current partial day excluded from cutoff but retained as the last observation;
- once-per-page SMA25/SMA200 recomputation and O(1) replay terminal work;
- Tick, Tape, board, orders, positions, clock, scrubber, and liveness continuity.

## Backward Compatibility Check

**Compatible.** The revised plan preserves these contracts:

- `dailyContextUrl(identity)` without options still emits actualDate with
  `limit=500`; only an optional `{beforeDate,limit}` form is added.
- `/api/daily-context` remains unchanged: strict exclusive date cutoff,
  ascending valid raw OHLCV, explicit `available`, and limits 1 through 500.
- Initial fetch remains 500 completed sessions, preserving SMA200 warm-up and
  the current cache behavior for existing callers.
- `DailyChartSession.snapshot()`, partial-day raw-tick aggregation,
  `deriveTerminal()`, `loadDailyRequest`, chart mode ownership, and
  `paintedLinePoint` cloning remain compatible.
- `available=true,bars=[]` keeps its existing initial-load meaning. In older-page
  context it additionally means session-local exhaustion without changing the
  public Daily phase.
- Minute paging files and policies, production Python, cloud file serving,
  dependencies, API schema, adjustment handling, and Daily markers remain out
  of scope.

The proposed Python adjacent-page test is sufficient to lock the existing
backend assumption. If it fails, the plan correctly requires diagnosis instead
of silently expanding scope to a backend change.

## Convention Compliance

The plan follows established repository conventions:

- It keeps DOM and I/O wiring in `app.js` and DOM-free Daily state/calculations
  in `daily-chart.mjs`.
- It mirrors the proven Minute policy without sharing mutable controllers or
  changing Minute behavior.
- It uses TDD with explicit RED and GREEN gates, preserves existing tests, and
  includes Node, syntax, Python, fresh-browser, diff, and review checks.
- It uses the existing `RequestCoordinator`, `ChartViewportState`, immutable
  canonical state, `paintedLinePoint` boundary copies, and actual-session full
  identity.
- It routes the durable constraint update through `design-tracker` rather than
  treating `DESIGN.md` as ordinary implementation state.
- It preserves user-owned uncommitted Daily/SMA and replay fixes and prohibits
  unproven production-backend edits.

The revised commit-last order now matches the repository's transactional
prepend convention and its own all-or-nothing guarantee.

## Integration Risks

- **Canonical/view divergence:** mitigated by the revised commit-last order and
  complete four-series/live/saved rollback tests.
- **First-partial recursion or lost retry:** mitigated by the shared
  programmatic initial-range helper and success-only initialization flag.
- **Same coordinator kind:** reusing `daily-context` is safe only because older
  paging is admitted after initial Daily readiness and new-session load cancels
  before its first await. Mode switching must continue to use the cache path and
  must not start a competing initial request.
- **Current-range timing:** user interaction can continue while the page is in
  flight. The plan correctly captures the live range at apply time, not request
  time; implementation tests must use two different ranges to prove this.
- **SMA cache replacement:** every accepted page must replace the full-identity
  cache record. Otherwise a later same-identity cache use drops paged history.
- **Finite client growth:** 200-bar pages accumulate until source exhaustion.
  This is an approved replacement of the total-500 client bound and must be
  documented, not silently capped during merge.
- **Gesture event ordering:** the first wheel event must arm before the visible
  range is evaluated. The fresh-browser test should explicitly verify a first
  gesture at the threshold, not only repeated gestures.

## Additional Test Cases Recommended

These remain useful non-blocking additions beyond the required revised tests:

1. Test a page containing both duplicate/out-of-bound dates and valid older
   dates; only valid unique older dates contribute to `N`, and the page must not
   be marked exhausted.
2. Test a partial day changing while a request is in flight and confirm the
   synchronously prepared/committed page derives its terminal SMA from the
   latest partial bar present after `await`.
3. Test cache reuse after two accepted pages: reset to the same actual identity,
   apply cached data, and assert both page prefixes remain.
4. Test a stale page completion after a new session has independently set its
   initial 90+5 range; the old completion must not alter either live or saved
   range.
5. Test an asynchronous visible-range callback fired before the scheduled
   `ChartViewportState` release after page apply; it must neither overwrite the
   shifted saved range nor admit a follow-up page.
6. In browser evidence, record the logical range or equivalent observable
   before and after prepend so the exact `+N` claim is executable evidence, not
   only a visual impression.

## Revised Steps

None. Steps 3, 4, 5, and 6 now contain the previously required revisions. The
plan may proceed to implementation through the approved `team-execute` route.

## Feature Brief: Daily chart history paging

### Current State
- Architecture: Daily mode owns a retained Lightweight Charts instance and a `DailyChartSession`; completed daily bars come from strict-before `/api/daily-context`, while the selected-day partial candle comes only from replayed ticks.
- Relevant files: `src/tickreplay/static/app.js`, `src/tickreplay/static/daily-chart.mjs`, `src/tickreplay/static/daily-chart.test.mjs`, `docs/tick-replay.md`, `.agents/docs/DESIGN.md`.
- Patterns: Minute history already provides user-armed left-edge paging, single-flight requests, generation/identity rejection, cooldown/exhaustion, deduplication, and logical-range compensation after prepend.

### Feature Goal
Show Daily mode initially with the newest 90 bars and 5 logical bars of right padding, matching Minute mode. When the user pans or zooms near the oldest loaded Daily bar, load and prepend older Daily bars without interrupting replay or moving the user's view.

### Scope
- Include: retain the initial 500 completed-session fetch for SMA200 warm-up; set an explicit Daily logical range of 90 visible bars plus 5 right-padding bars; request older pages in 200-bar batches; merge and deduplicate pages; recompute historical SMA state once per accepted page; preserve the viewport and all replay side effects; add focused tests and documentation.
- Exclude: backend/API schema changes, daily markers, adjustment-column handling, new dependencies, migrations, changes to Minute paging behavior, and per-tick full SMA recomputation.

### Complexity Classification (from Codex)
- Classification: COMPLEX
- Estimated files: 5-6 including tests, documentation, and design/state artifacts
- Estimated LOC: 180-320
- Implementation route: team-execute

### Integration Points
- Daily chart lifecycle: `app.js` must apply the initial `90 + 5` logical range only at the Daily session boundary and retain independent Daily viewport state across mode switches.
- User interaction: Daily wheel, pointer, and touch activity arms paging; visible-logical-range callbacks trigger only near the loaded left edge.
- Daily request/session state: `DailyChartSession` must admit one older-page request at a time, reject stale generation/identity/token results, track cooldown and exhaustion, and expose a safe page-commit result.
- Historical merge and indicators: accepted pages prepend unique chronological bars, rebuild SMA25/SMA200 and rolling windows once, and report the unique prepend count used to shift logical ranges.
- Existing API: `/api/daily-context?stem=...&date=<oldest>&limit=200` already supplies strict-before pages; no backend change is expected.

### Risks
- View jumps after prepend: shift the live/saved Daily logical range by the exact unique prepend count and suppress programmatic range callbacks.
- SMA discontinuity or excessive replay work: rebuild history only when a page is accepted, then preserve O(1) terminal updates during replay.
- Stale cross-session commits: gate request admission and completion with Daily identity, generation, and request token checks.
- Repeated requests from chart callbacks: require a real user interaction, single-flight state, a left-edge threshold, cooldown after failure, and exhaustion after a valid empty page.
- Partial apply across four series: stage the merged snapshot before committing canonical state and update candle, volume, SMA25, and SMA200 consistently.

### Success Criteria
- A newly loaded Daily view shows exactly the newest 90 data bars with 5 logical bars of right space.
- Real pan/zoom toward the left edge loads older Daily bars in bounded pages and does not jump the visible range.
- Programmatic range changes, inactive Daily mode, stale responses, duplicate pages, cooldown, and exhausted history do not issue or commit invalid loads.
- SMA25/SMA200 remain point-in-time correct and are not fully recalculated per replay tick.
- Daily replay continues to update Tick, Tape, board, orders, and positions during and after history loading.
- Focused Node tests, relevant Python API tests, and a fresh-browser interaction regression pass.

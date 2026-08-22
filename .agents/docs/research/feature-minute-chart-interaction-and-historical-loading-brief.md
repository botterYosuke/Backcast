## Feature Brief: Minute-chart interaction and historical loading

### Current State
- Architecture: `app.js` owns chart creation, replay state, viewport following, minute-context preload, seek/reset rebuilds, and series redraws. `RequestCoordinator` provides cancellable latest-wins fetches. `/api/minute-context` queries chronological bars strictly before a cutoff from the per-symbol minute DuckDB.
- Relevant files: `src/tickreplay/static/app.js`, `src/tickreplay/static/request-coordinator.mjs`, `src/tickreplay/minute_context.py`, `src/tickreplay/server.py`, the native Node frontend tests, and `docs/tick-replay.md`.
- Patterns: native ES modules with pure helpers tested by `node:test`; best-effort optional minute context; session data rebuilt from `state.contextBars` on seek/reset; logical ranges for minute-chart viewport sizing; latest-wins request cancellation. The current working tree also contains concurrent paper-trading changes that must be preserved.

### Feature Goal
Keep minute-chart pan and zoom usable while replay continues, and load older minute candles on demand when the user navigates near the oldest loaded candle, without interrupting replay or moving the user's viewport.

### Scope
- Include: separate tick follow from minute viewport control; switch the minute chart to manual mode after user pan/zoom; detect user navigation near the left edge; page older candles through the existing `/api/minute-context`; single-flight/cancel requests; reject stale same-symbol/date responses; merge and deduplicate older bars into both `state.contextBars` and `state.bars`; preserve logical viewport position after prepend; retain loaded history across seek/reset; prevent empty-response request storms; preserve paper-trading markers and state; add pure helper tests and update tickreplay documentation.
- Exclude: changing the minute-context HTTP schema; guaranteed retry semantics for unavailable history; backend/DuckDB query redesign; connection pooling; alphabetic-symbol file-server widening; an explicit “latest” UI button; unbounded eager history loading.

### Complexity Classification (from Codex)
- Classification: MODERATE
- Estimated files: 4 (`app.js`, new helper, new helper test, documentation)
- Estimated LOC: 180-300
- Implementation route: Codex + review

### Integration Points
- Replay loop: continue updating/following the tick chart without resetting the minute chart's user-selected logical range every animation frame.
- Lightweight Charts time scale: subscribe to logical-range changes, distinguish manual from programmatic changes, calculate proximity with `barsInLogicalRange`, and shift the saved logical range by the unique prepend count.
- Minute state: prepend chronological unique bars to both canonical arrays so `seekTo()` and reset retain history, then redraw candles, volume, and markers without altering replay/trading state.
- Request lifecycle: use a dedicated `minute-history` request kind, cancel it on every session load, and compare a monotonically increasing session generation before applying a response.
- Existing API: request `/api/minute-context` with the earliest loaded bar as the next strict-before cutoff; retain its current best-effort empty-list contract.

### Risks
- Programmatic `setData()`/range restoration can recursively trigger more page loads: suppress programmatic callbacks and keep a single in-flight request plus exhausted state.
- Same-symbol date changes can accept an old response: cancel explicitly and verify session generation before merge.
- Prepending changes logical indexes and can jump the viewport: restore `{from + N, to + N}` using the count of unique added bars.
- Updating only current bars loses history on seek/reset: update both `contextBars` and `bars` through a pure, chronological deduplicating merge.
- Empty response conflates history exhaustion and transient failure: treat it as exhausted for the current session only, preserving best-effort compatibility.
- Repeated full `setData()` grows with retained history: use bounded pages and single-flight loading; manually observe behavior with roughly 10,000 retained bars.
- Concurrent paper-trading changes overlap `app.js`: implement against the current working-tree file and preserve marker/order/position behavior and unrelated files.

### Success Criteria
- During active replay at low and high speeds, user pan and zoom on the minute chart are not overwritten, and replay continues advancing.
- Navigating near the oldest loaded candle issues at most one history request and can load at least two consecutive older pages without duplicates.
- Older candles appear without a horizontal viewport jump and remain after seek/reset.
- An in-flight response from an earlier date/session cannot mutate the newly selected session, including same-symbol date changes.
- Empty or failed best-effort history does not stop replay or create an unbounded request loop.
- Paper-trading markers, orders, positions, tick chart following, and the existing first-playback zoom fix remain intact.
- New native Node tests, existing frontend tests, relevant Python API/context tests, syntax checks, and the repository quality gate pass.

# Security Review: Daily Chart History Paging

## Verdict

**SHIP with non-blocking hardening follow-up.** No Critical or High vulnerability was found. The feature does not add an HTML injection sink, dynamic code execution, unsafe path construction, or an authorization boundary. Request and commit state are bound to the full session identity, generation, token object, and oldest-bar cutoff.

Unresolved counts: **Critical 0, High 0, Medium 1, Low 1**. Neither finding blocks shipment because both affect only resource availability in the requesting browser and the production API already bounds each response to 500 bars.

## Findings

### [Medium] Retained Daily history and identity cache have no total resource budget

- **File:** `src/tickreplay/static/daily-chart.mjs:636`, `src/tickreplay/static/daily-chart.mjs:751`, `src/tickreplay/static/daily-chart.mjs:843`, `src/tickreplay/static/daily-chart.mjs:877`
- **Evidence:** `DailyChartSession.cache` is an unbounded `Map`. Every accepted identity retains its history, and every page concatenates all prior bars, rebuilds SMA arrays over the full history, and replaces the cached entry. `resetSession()` does not evict older identities.
- **Impact:** Repeated session navigation and older-page loading can grow browser memory without a fixed ceiling. Cumulative full-history SMA rebuild and four-series materialization also increase CPU cost, so a sufficiently long-lived client can become unresponsive. This does not consume another user's client resources.
- **Recommended fix:** Add an explicit resource policy: LRU-limit cached identities and define a maximum retained-bar budget per active identity. Preserve the visible range when evicting, or virtualize older chart data if complete-history navigation must remain available.

### [Low] Client normalization bounds output but scans an arbitrarily large response array

- **File:** `src/tickreplay/static/daily-chart.mjs:164`, `src/tickreplay/static/daily-chart.mjs:176`, `src/tickreplay/static/daily-chart.mjs:197`
- **Evidence:** `normalizeDailyPayload()` iterates every member of `payload.bars` before slicing normalized output to `maxBars`. It does not reject a response whose input cardinality exceeds the requested limit.
- **Impact:** A faulty or compromised same-origin API could make the browser spend unbounded CPU/memory parsing and grouping a response despite a 200-bar page request. The current FastAPI/DuckDB implementation limits responses to at most 500 rows, so normal deployments are protected server-side.
- **Recommended fix:** Reject `payload.bars.length > maxBars` before grouping, treating the response as malformed and retryable. Keep the server-side `limit` validation as the primary bound.

## Validation

- URL construction validates identity shape, ISO dates, and `1..500` limits, then applies `encodeURIComponent` (`daily-chart.mjs:577-588`). The server uppercases and regex-validates the stem, validates the date, constrains `limit`, and parameterizes both DuckDB values (`daily_context.py:155-169`, `daily_context.py:453-455`, `server.py:521-542`). No SQL/path injection was found.
- Remote bars require a plain object, real ISO date, finite positive OHLC, finite non-negative volume, valid OHLC ordering, deterministic duplicate agreement, chronological sorting, and bounded accepted output (`daily-chart.mjs:62-91`, `daily-chart.mjs:164-207`).
- UI status writes use `textContent`; reviewed Daily data is passed only to chart APIs. No `innerHTML`, `eval`, `Function`, or equivalent injection sink appears in the reviewed Daily flow (`app.js:188-211`, `app.js:355-376`).
- Older-page completion rechecks request token, generation, full `stem|code|date` identity, and cutoff before state/chart commit (`daily-chart.mjs:805-888`, `app.js:550-606`). Synchronous commit leaves no await boundary for a cross-session interleave.
- User arming is consumed at admission; single-flight, programmatic-range suppression, cooldown, three-failure cutoff, exhaustion, and request cancellation prevent an automatic retry/tight-fetch loop (`daily-chart.mjs:787-915`, `app.js:618-631`, `app.js:2148-2158`).
- Transaction failure restores all four datasets, live and saved ranges, cached history, SMA windows, paging state, and metrics; rollback failure is surfaced as `AggregateError` and is not displayed with sensitive details (`daily-chart.mjs:386-499`, `app.js:608-615`).
- Independently executed `node --test src/tickreplay/static/daily-chart.test.mjs`: **56 passed, 0 failed, 0 skipped**. Parent-provided gates report JavaScript **131/131**, Python **133 passed / 1 skipped**, ruff pass, placeholders 0, and weakened tests 0.

## Residual Risks

- The API has no authentication in the reviewed scope. That is an existing deployment boundary, not introduced by paging; the endpoint still applies a four-thread I/O limiter and bounded queries/downloads.
- A configured data origin is trusted to provide legitimate market magnitudes. Individual values are finite-checked, but no business-range ceiling is imposed; extreme finite values remain a data-quality availability risk rather than an injection or privilege issue.
- Browser behavior with the eventual maximum retained history was not load-tested in this review. The Medium finding should be addressed before claiming a fixed client-memory bound.

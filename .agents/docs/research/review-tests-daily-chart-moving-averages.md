# Final Test Re-review Pass 3: Daily Chart and Moving Averages

## Decision

**PASS.** Fix Pass 3 closes the server-level concurrency/retry integration gap while preserving zero Critical/High automated-test findings. Final counts: **Critical 0 / High 0 / Medium 1 / Low 0**. The remaining Medium item is non-blocking browser/UI coverage.

## Coverage Status

- Feature-wide coverage: **not measured**. Python coverage tooling is not installed and `app.js` is not instrumented as an executable browser bundle by this Node command.
- Fresh focused result for `daily-chart.mjs`: **96.79% lines / 82.52% branches / 88.00% functions**.
- The same focused run imported only the exercised portion of `minute-history.mjs`; its combined 76.35% number is not a feature-wide or full minute-history measurement and is not used for acceptance.
- Artifact: `.agents/logs/coverage-daily-chart-node.txt`.

## Fix Pass 3 Acceptance Audit

| Acceptance area | Executed proof | Verdict |
| --- | --- | --- |
| Four terminal results after staged request | Parameterized ready/empty/unavailable/error test executes `commitDailySession`, asserts atomic identity install, then exactly one status publication with the terminal phase | Resolved |
| Zero-tick and normal load branches bind status publication | `loadSession` seam asserts exactly two `commitDailySession` calls and two `publishStatus: refreshDailyStatus` bindings | Resolved |
| No-tick/tick x minute/daily replay frame | `runReplayFrame` executes all four combinations; no-tick still calls follow/clock/scrubber, tick modes share tape/orders/board/position and differ only in chart ownership | Resolved |
| Complete app port binding | App seam asserts one `runReplayFrame` plus minute/defer, daily derive/update, ticks, tape, orders, board, position, follow, clock, and scrubber ports | Resolved |
| Daily-mode minute-history commit/defer | Real `applyMinuteHistoryPage` updates canonical `contextBars`/`bars` and logical range while recording zero real hidden-chart writes | Resolved |
| Minute return flush | Executed `flushDeferredMinuteChart` performs exactly one flush, clears the flag, does not repeat, and remains deferred while daily | Resolved |
| Cancellation | Recognized cancellation releases request, writes no cache or terminal error status, and permits immediate retry | Resolved |
| `failSession` | Executed test clears staged/current identities, request, history, partial candle, historical SMA arrays, and terminal SMA tails | Resolved |
| Async/nested viewport suppression | Scheduled releases execute out of order while captures remain suppressed until depth drains exactly to zero; subsequent user capture succeeds | Resolved |
| Backend `volume=0` | Real DuckDB loader returns one available bar preserving `0.0` | Resolved |
| Negative-cache time boundaries | Equal request-start and TTL boundaries are tested explicitly | Resolved |
| Negative-cache burst/retry | The direct loader test covers five threads, and the new AnyIO integration executes eight real `_run_daily_context` calls across the four-token limiter with the real loader/MockTransport origin: the burst makes exactly one GET and a separately awaited later explicit call makes GET two | Resolved |
| SMA performance | 500-bar history precomputes once; 10,000 ticks retain historical array identity and perform no historical rebuild/per-tick terminal derivation | Resolved |

## Finding

### [Medium] Browser lifecycle, accessibility, crosshair, and mobile behavior remain source/manual seams

- File/function: `daily-chart.test.mjs::separate DOM, lazy second chart...`; runtime `app.js::ensureDailyChart`, `setChartMode`, `applyChartStageSize`, keyboard handler; `styles.css` responsive rules.
- Remaining case: there is no real browser/DOM execution of repeated switches, exact resize dimensions, independent crosshairs, focus/ARIA transitions, or <=768px wrapping/touch targets.
- Consequence: a presentation-only regression could retain the expected source tokens and evade Node tests.
- Recommendation: retain the approved manual browser matrix or add a browser harness later. This does not block the automated ship gate because chart ownership, viewport suppression, deferred flush, request state, and replay invariants now have executable coverage.

## Test Quality

- Tests use clear arrange/act/assert phases, fresh state, controllable deferred promises, recording ports, real minute-history application, tmp-path DuckDB files, and bounded thread synchronization.
- Assertions prove behavior rather than only text for every previously blocking path. Source seams are now limited to verifying that `app.js` binds the already-executed pure contracts to every required port and to browser-only presentation structure.
- The new integration uses eight tasks (twice the four-token limit), captures arrival before limiter admission, and blocks the first real origin miss until all arrivals are observed. This makes the former second-wave regression deterministic; assertions require eight unavailable results, one burst GET, and exactly two GETs after a distinct later request.
- No skipped/weakened assertions or test-order dependency was found in the reviewed fixes.

## Test Execution Results

- `node --test src/tickreplay/static/daily-chart.test.mjs`: **33 tests; 33 passed, 0 failed, 0 skipped**, 0.449s.
- `node --test src/tickreplay/static/*.test.mjs`: **108 tests; 108 passed, 0 failed, 0 skipped**, 0.687s. This includes the 33 focused tests.
- `uv run pytest tests/test_tickreplay_daily_context.py -q`: **40 tests; 40 passed, 0 failed, 0 skipped**, 17.30s.
- Non-overlapping primary total: **148 tests; 148 passed, 0 failed, 0 skipped** (108 static Node + 40 backend Python).
- Coverage-only rerun: **33 passed, 0 failed**; module-limited percentages are listed above.
- Pass 3 focused server rerun (`test_daily_context_blocking_io_uses_a_dedicated_bounded_limiter` and `test_daily_context_limiter_waiters_coalesce_one_origin_miss_then_retry`): **2 tests; 2 passed, 0 failed, 0 skipped**, 0.77s; one third-party Starlette deprecation warning.

## Unverified Items

- Manual browser acceptance remains: repeated minute/daily switching, delayed-request presentation, keyboard-only interaction, responsive/mobile layout, independent crosshairs, and x1/x500 replay.
- Feature-wide coverage remains unmeasured; only the focused module coverage above is valid.

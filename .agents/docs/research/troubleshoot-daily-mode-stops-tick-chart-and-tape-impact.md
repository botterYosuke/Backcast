# Impact Assessment: Daily mode stops Tick chart and Tape

## Risk Summary

- **Incident impact: High.** The visible Tick chart and Tape stop, and the same exception skips limit-order matching, board updates, and position/P&L updates after the replay cursor has already advanced.
- **Recommended-fix regression risk: Low.** Keep canonical daily/SMA state frozen and clone only `{ time, value }` objects at the Lightweight Charts boundary.

## Git History and Origin

The defect is not in committed history. `git blame -L 450,465 -- src/tickreplay/static/app.js` identifies `app.js:456-462` as `Not Committed Yet` on 2026-08-30, and `daily-chart.mjs` plus its test are untracked. The latest committed `app.js` change is `f7a939eb247f0f071656fdbaf2f09476e5f784d9` (2026-08-23), before Daily/SMA existed. The introducing change is therefore the current uncommitted Daily chart/SMA feature: it created frozen terminal points in `daily-chart.mjs:209-215,556-560` and passed them directly to line-series `update()` in `app.js:461-462`. No introducing commit hash exists yet.

## Blast Radius

The trigger is Daily mode after history becomes ready with enough closes for a terminal SMA (normally 24 completed sessions plus the replay partial for SMA25). Before readiness or with shorter history, the terminal is null and the fault is delayed/absent. SMA200 has the identical unsafe call; SMA25 normally throws first and masks it.

`step()` consumes ticks and advances `state.cursor` before rendering (`app.js:1652-1677`). `commitReplayFrame()` then calls the Daily chart before Tick, Tape, order matching, board, and position ports (`daily-chart.mjs:291-300`). The exception therefore leaves canonical minute/daily price state and cursor ahead of:

- `tickSeries` and `state.tickPoints` (`app.js:1723-1737`);
- Tape DOM rows (`app.js:638-651`);
- pending-order fills and portfolio events (`app.js:1120-1231`);
- board quantity invalidation/flashes (`app.js:1091-1105`);
- position, P&L, and mobile trade-button refresh (`app.js:1316-1344`).

This is not only presentation damage: ticks skipped by `matchOrders(from,to)` are not replayed later, so a limit order may fill late, never fill, or retain the wrong timestamp. Switching back to Minute does not repair the missed interval. RAF is scheduled before the exception, and no-tick frames still update housekeeping, explaining why clock/liveness can move while these consumers remain frozen.

## Lightweight Charts Boundaries

- Candle/volume `setData()` and `update()` receive fresh objects from `paintedBar()` / `paintedVolume()` and are safe.
- SMA25/SMA200 incremental `update()` receive frozen canonical terminal objects directly. Vendored Lightweight Charts 4.2.0 mutates the input by adding internal `zb` metadata (`vendor/lightweight-charts.standalone.production.js:7`), producing the reproduced exception.
- SMA25/SMA200 full `setData()` also receive frozen arrays/points directly (`daily-chart.mjs:227-228`). The current 4.2.0 full-data converter does not mutate them, and browser loading succeeds, but this violates the same ownership boundary and is fragile across library changes.
- Frozen viewport ranges are already copied before `setVisibleLogicalRange()` (`daily-chart.mjs:269-275`); no other frozen daily object crosses into the chart library.

Recommended boundary policy: map both SMA arrays to fresh line points for `setData()` and clone both terminal points for `update()`. Do not remove `Object.freeze`, clone whole history per replay tick, reorder side effects, or catch-and-ignore chart errors.

## Test Gaps and Safeguards

The focused suite passes **37/37 while the browser bug exists**. Tests at `daily-chart.test.mjs:593-708` use collecting/non-mutating fake ports and prove order only. Source-seam tests at `:865-941` prove wiring tokens, not chart behavior.

The earlier browser check was a false pass because it sampled Daily from `loading` to `ready`, saw 53 Tick updates before the terminal existed, and judged only cursor/time/liveness (`daily-mode-pauses-replay-repro.mjs:264-321`). It did not fail on Tape stasis, Tick-canvas stasis, or runtime exceptions. The visual probe later proved 0/15 transitions for both and 252 exceptions.

Required safeguards:

1. A mutating fake chart port must attach metadata to every SMA object received by `setData()` and `update()`, while asserting canonical session points remain frozen and unchanged.
2. Preserve exact SMA25/SMA200 values, 25/200 observation boundaries, one history precompute, and O(1) terminal derivation.
3. Browser regression must wait until Daily phase is `ready`, clear prior events, then require zero runtime exceptions plus changing Tick canvas and visible Tape head.
4. Keep the existing mode-parity assertion for Tick/Tape/orders/board/position and add a failure-sensitive path so a real Daily chart adapter is exercised before those ports.
5. Prefer a deterministic pending-order-crossing browser/unit case to prove replay trading was not skipped.

## Compatibility and Performance

The object schema and APIs do not change. Full-series cloning is bounded by 500 history sessions and happens only on load/switch/seek redraw; incremental work is at most two tiny allocations per tick-bearing animation frame. It preserves load-time SMA precomputation and avoids any full-array rebuild during replay. No backend, endpoint, HTML/CSS, storage, dependency upgrade, or external research is required.

## Codex Status

One combined read-only regression-risk/fix-safety consultation was attempted with a 60-second bound. It produced no response (empty response artifact) and was interrupted; per instruction it was not retried and contributes no evidence. This assessment relies on git/blame, vendored 4.2.0 source, the 37/37 false-negative unit run, and reproduced Chrome exceptions/transitions.

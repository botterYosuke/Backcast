## Root Cause

The current implementation does not intentionally pause replay when Daily is selected, and the reported pause was not reproduced as a replay-state failure. The definitive defect demonstrated by the available evidence is a **perceptual liveness gap in the active daily view**: replay advances, but the dominant visible object is one partial-day candle plotted against as many as 500 completed sessions. Most tick-to-tick changes are sub-pixel, repeated prices do not alter the candle, and no-trade frames do not write the daily series at all. The coarse 0-1000 scrubber can also remain on the same integer while virtual time advances.

An independently saved daily viewport can amplify the symptom if the selected-day candle is off-screen. An already-open page can retain older JavaScript until reload, but no stale-asset evidence was captured. Neither is required to explain the fresh-browser observation.

## Evidence

- `app.js:484-529` changes chart ownership and DOM state only; it never calls `setPlaying()` or mutates `state.playing`.
- `app.js:1627-1695` schedules the next RAF before any early return, advances `state.vt`/`state.cursor`, and calls `runReplayFrame()` in both modes.
- `daily-chart.mjs:243-263` changes only the upper-chart leaf writes by mode; tick, tape, orders, board, position, clock, scrubber, and tick-follow effects remain shared.
- The fresh Chrome probe observed Daily mode with `playing=true`, cursor `961 -> 1451`, virtual time `+15.49s`, tick points `+250`, and partial-day volume `+118,300`. No console exception was observed.
- `updateDailyPartialChart()` updates a single partial candle and terminal SMA points only after ticks were consumed. This naturally produces quiet frames and low visible motion on a full-history price scale.
- An impact-investigator repeat independently observed Daily mode continue with cursor `+553`, virtual time `+15.99s`, tick points `+82`, and partial volume `+137,600`.

Actual pause/state mutation is therefore eliminated for the tested current build. RAF cancellation and a mode-switch exception are also unsupported. Low visual sensitivity is confirmed; no-trade intervals and a coarse scrubber are contributing conditions. Off-screen live data and stale already-open assets remain possible environment-specific amplifiers, not proven root causes.

## Alternatives

1. **Visible replay liveness in the daily panel.** Show replay state plus current virtual time/progress beside the daily legend and update it on every RAF, including no-tick frames. Add a real tab-switch browser regression that asserts `playing`, cursor, and clock continue. This preserves chart and viewport semantics while making progress observable.
2. **Force the daily viewport to follow the live candle.** Keep the selected-day candle at the right edge while playing. This makes candle updates easier to find, but conflicts with the established independent, user-controlled daily viewport unless follow state is explicitly user-controlled.
3. **Reassert `state.playing` in `setChartMode()`.** Snapshot and restore the flag around the switch. This is a no-op for current code and masks no demonstrated mutation, so it would treat the symptom rather than the cause.

## Recommended Fix

Use alternative 1: leave the replay engine and viewport policy unchanged; add an always-visible `Playing HH:MM:SS`/progress indicator inside the daily panel, driven by the existing per-frame clock path, and cover a real Daily-tab click with assertions that `state.playing`, cursor, virtual time, tick/tape/board, and the partial candle continue. Optionally expose an explicit Follow Live control later rather than overriding a panned viewport. This is the smallest correct fix for the demonstrated liveness defect and avoids introducing replay or navigation regressions.

## Codex Status

The mandatory read-only Codex flow/hypothesis consultation was invoked through `codex_consult.py` with a 90-second limit. It produced no response and was interrupted after exceeding the bound. Per the lead's instruction, no further consultations were attempted; execution-flow, hypothesis, fix-design, and correctness conclusions above are independently derived from code and two live-browser probes and are not Codex-validated.

## Artifacts

- Bug report: `.agents/docs/research/troubleshoot-daily-mode-pauses-replay-bug-report.md`
- Context analysis: `.agents/docs/research/troubleshoot-daily-mode-pauses-replay-context.md`
- Browser capture: `.agents/logs/troubleshoot-repro-daily-mode-pauses-replay-initial.log`
- Root-cause analysis: `.agents/docs/research/troubleshoot-daily-mode-pauses-replay-root-cause.md`


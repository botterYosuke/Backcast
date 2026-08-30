# Daily Mode Replay Liveness: Impact Assessment

## Blast Radius

- A chart-tab click affects only chart ownership and daily-history loading. `setChartMode()` does not write `state.playing`; the retained `step()` loop continues clock, scrubber, tick chart, tape, order matching, board, and portfolio updates in both modes.
- The reported symptom can affect every replay speed, but is strongest at x0.5/x1: many ticks do not move a full-day candle by a visible pixel. At x10 the real Chrome probe still advanced cursor by 553, virtual time by 15.99 seconds, and partial volume by 137,600 after switching tabs.
- Sparse intervals amplify the perception. The sampled session has a 3,600-second lunch gap and a 300-second closing gap. With gap skipping off, virtual time and clock advance while cursor, candle, tape, and board can remain static; with it on, replay jumps to the next tick.
- A user-panned daily viewport intentionally remains independent. The selected-day candle may update off-screen. Mobile users are more exposed because the transport clock is below the daily chart and may be outside the viewport.
- Session/date changes are distinct: `loadSession()` intentionally calls `setPlaying(false)`. Natural end, Reset, and the Play/Pause button also stop replay; a tab switch does not.

## Git Origin

- Repository HEAD is `f7a939e`; it has no daily mode. `daily-chart.mjs` and its tests are untracked, while the current working tree adds 387/removes 38 lines in `app.js`, adds 21/removes 5 in `index.html`, and adds 102/removes 1 in `styles.css`.
- Therefore no committed introducing hash or useful bisect exists. The perceived-liveness regression originates in the uncommitted daily feature, not in HEAD history.
- The behavior is not an intentional pause. The feature decision explicitly requires chart-mode switches to preserve replay and trading side effects. The implementation satisfies state continuity but lacks sufficiently prominent liveness feedback in the active daily view.

## Regression Risks

- **Changing the replay engine (high):** adding play/pause writes or mode-specific timing to `setChartMode()` could break all speeds, gap skipping, order fills, board/tape updates, seek/reset, and end-of-session behavior. No evidence justifies this change.
- **Unconditional daily auto-follow (medium-high):** forcing the selected-day candle to the right edge would violate the independent saved viewport contract and make historical pan/zoom unusable. If follow is desired later, it should be explicit or active only while already near the live edge.
- **Explicit liveness feedback (low):** a daily-panel clock/progress indicator is the safest fix. Do not update the existing `aria-live="polite"` status every animation frame; that could flood assistive technology. Throttle visible text to whole seconds, expose play state accessibly, and respect reduced motion for any pulse.
- **Changing scrubber resolution (medium):** HTML `max=1000`, `syncScrubber()` multiplication, and input division are a coupled contract. A partial change corrupts seek positions. More resolution still does not make an off-screen or price-static daily candle visibly move.
- **Rendering overhead (low-medium):** per-frame DOM text or layout work can hurt mobile/high-speed replay. Update only when the displayed second or state changes; retain O(1) daily candle/SMA updates.

## Safeguards

- Add a real Chrome tab-switch regression that starts playback, clicks Daily, then asserts `playing`, cursor/virtual time, clock, shared replay effects, and daily partial state continue. Run it at x0.5/x1/x10 and both gap-skip settings.
- Seek just before the 11:30 lunch gap. With skipping off, assert virtual clock/liveness advances while cursor may remain fixed; with skipping on, assert cursor reaches the next tick without changing play state.
- Pan daily history away from the live edge, switch modes, and assert the logical range remains unchanged while the internal partial candle advances and the liveness indicator remains visible. Test an explicit follow action separately if introduced.
- Exercise a 390x844 viewport and keyboard tab switching. Assert the indicator is visible, controls do not reflow over the chart, focus/ARIA tab state remains correct, and Pause still works.
- Keep existing DOM-free parity tests, but do not treat regex inspection as tab-switch coverage. If scrubber precision changes, derive both directions from one maximum and add 0/mid/max round-trip tests plus pointer-scrubbing tests.

## Codex Status

- Required regression and fix-safety consultations were attempted through the read-only wrapper with a 90-second cap. Both prompt-stdin attempts failed before Codex launched: the Windows pipe produced a leading surrogate that the wrapper could not write as UTF-8 (`UnicodeEncodeError`).
- The failure was not a timeout, but repeating the same unusable transport would add no evidence. Codex risk and safety verdicts are therefore unverified; the assessment rests on git/code tracing, session-gap measurement, the executable Chrome probe, and agreement with the Root Cause Analyst.

## Artifacts

- Browser probe: `.agents/logs/daily-mode-pauses-replay-repro.mjs`
- Initial capture: `.agents/logs/troubleshoot-repro-daily-mode-pauses-replay-initial.log`
- Context analysis: `.agents/docs/research/troubleshoot-daily-mode-pauses-replay-context.md`
- Impact assessment: `.agents/docs/research/troubleshoot-daily-mode-pauses-replay-impact.md`

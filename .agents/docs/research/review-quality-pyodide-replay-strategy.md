# Quality Review: Browser Python Remediation

## Verdict

PASS after re-review. High/Critical 0, blocking Medium 0, additional nonblocking findings 0. Original Q1–Q3 and the initial R6 reentrancy finding RQ1 are resolved in the final inspected working tree and release `fde82a53f12e81c6fbac68d116c2afc3fd8e0254907dd7b6e93cb9750a63f14d`. This is local code-quality acceptance, not production/browser-matrix promotion approval.

## Review Scope

Initially reviewed `.agents/logs/review-diff-pyodide-replay-strategy.patch`, canonical controller/workspace/Worker, broker/replay/paper-trading helpers, app integration, warning/source ingestion, build/fetch changes, and relevant regression/browser tests. Applied the active Review remediation plan, original quality Q1–Q3, security S1–S2, coverage obligations, coding/testing and delegation rules. Generated runtime copies were reviewed through canonical byte equality and release reconstruction rather than redundant source reading. Initial inspected release was `2914b610cc3391acc1552c7df6aa8ff805d44ef0ea120135838050499e6666ed`. Re-review inspected the current `git diff`, the new tests and canonical source, and final release `fde82a53f12e81c6fbac68d116c2afc3fd8e0254907dd7b6e93cb9750a63f14d`; the stored patch still described the initial candidate at re-review time, so Lead was asked to refresh the final audit artifact. The final verdict uses the actual current working tree, not that stale capture.

## Blocking Findings

None remaining.

## Resolved Initial R6 Finding

### RQ1 — Medium, RESOLVED: reentrant Load/Stop from the loading notification leaves an obsolete pending request

The following reproducer describes the initial candidate, not the final code. Closure evidence follows below.

- Locations: `src/tickreplay/static/strategy/strategy-controller.mjs:221`–`:224`, `:639`–`:652`, and invalidation at `:395`–`:399`.
- Current code publishes `loading` via `#setStatus`, which invokes the injected synchronous `onStateChange`, and then enters `#request` without rechecking its admission or captured Worker. Invalidation rejects whatever pending request exists at that instant, but at this notification boundary none exists yet. When the callback returns, the now-obsolete load creates a new pending request and posts it to its retiring Worker. Invalidation terminates that Worker but never rejects the newly created pending request. `#request` has a second callback boundary at `#emitState` and likewise does not recheck pending/current Worker ownership before posting.
- Concrete scenario A: call `controller.stop()` once from `onStateChange` when the first Load publishes `tag === 'loading'`. With inert controlled deadline callbacks and no interrupt buffer, await Stop and flush microtasks. The independent inline Node probe observed `tag='idle'`, `inFlight=true`, one terminated Worker with one posted request, and the Load promise still unsettled. In real deadline operation the obsolete pending request remains until the full Load deadline, not until Stop completion.
- Concrete scenario B: call replacement `controller.load(B)` from that same loading notification. B synchronously wins admission and waits for A's invalidation. A nonetheless installs its pending request after the invalidation rejection point. When B resumes, it creates its own Worker but `#request` rejects `single_flight` because A's pending request remains. Independently observed output: `replacement='single_flight'`, first Load unsettled, `tag='errored'`, `inFlight=true`, Worker states `[{messages:1,terminated:1},{messages:0,terminated:1}]`. Thus the last accepted Load cannot succeed.
- Impact: the public controller callback seam can leave a stopped controller manually blocked until timeout or make both Loads fail. This is the approved synchronous-reentrancy/settlement contract, not a hypothetical hostile-Worker claim. Existing tests cover reentrancy during `stopping` and pending rejection but omit the `loading` notification immediately before request admission.
- Required change: bind each request to its captured Worker/admission and validate ownership after synchronous host callbacks, before creating or posting a request. A superseded operation must settle without installing pending work or posting to a retiring Worker; cleanup must affect only its own request. Add the two probes as deterministic failing regression tests, plus Stop at the `inFlight=true` notification to prove no post occurs after settlement. Regenerate the immutable release after the canonical fix and revalidate its references.

## Nonblocking Findings

None newly identified. The same-origin trusted-code limitation and unexecuted deployment/browser-matrix gates remain documented scope limits, not new quality defects.

## Original-Finding Closure

| Original finding | Disposition | Evidence |
| --- | --- | --- |
| Q1 concurrent Load orphan | Resolved, including RQ1 | Synchronous `admission` reservation and invalidation-job reservation; Worker-keyed listeners/interrupt/finalization and retiring-Worker join. Request admission/posting and success publication now recheck ownership after notifications; independent loading-notification probes and regression matrix pass. |
| Q2 derived portfolio overflow | Resolved | `paper-trading.mjs:34` validates the same monetary/average-price projection used in dispatch; `order-broker.mjs:271` checks candidate IDs/orders/ledgers, including no-fill valuation; `order-broker.mjs:756` prepares batch and fill state privately; `replay-commit.mjs:169` validates the exact event before broker swap and replay cursor application. Boundary, aggregate, reversal, no-fill and full rollback tests pass. |
| Q3 final-LF source limit | Resolved | `strategy-source-store.mjs:68` excludes the trailing split sentinel; boundary tests cover 8192/8193 lines with final LF, BOM and CRLF against protocol. Native-runner parity tests are present and lead reports them passing. |
| Related S1/S2 | Resolved in inspected source | Persistent warning next to Load and both editor inputs' accessible description; metadata bound before `arrayBuffer`, with bounded BOM/CRLF overhead and source-preservation tests. |

Manual-only numeric handling remains characterized by unchanged legacy observable golden assertions; strict checks activate for live strategy ledgers, and invalid legacy Start is rejected before controller Start. The candidate transaction shares immutable historical collections but each mutating operation replaces arrays/maps or clones ledgers before publication. No alias mutation defeating rollback was found. Worker progress now carries identity, late completion is suppressed after messageerror, and nested proxy destruction guarantees the other proxy is attempted even if the first destructor throws. No new hard-coded shortcut, swallowed implementation error, unauthorized production deletion, test weakening, dependency change, or retained-release mutation was found in the reviewed diff.

## RQ1 Re-review and Final Release

- `strategy-controller.mjs:492` validates captured admission, current Worker, termination state and invalidation ownership. `:660` checks ownership before pending admission and again after the `inFlight` state notification, then posts through the captured Worker rather than the mutable shared field. `:696` settles the explicitly captured pending object, clearing shared state only if it is still that pending request. Response/error/deadline paths preserve that identity.
- Load and Start recheck ownership after ready/paused publication; Start also checks after its manual-entry gate callback. End checks admission after asynchronous cleanup/termination, after the manual gate callback, and after completion notification. An obsolete operation therefore cannot return stale success or publish completion/error over a synchronously admitted replacement. Malformed-message cleanup captures admission before notification and does not invalidate a replacement admitted by its settlement callback.
- The four regression combinations (loading notification with inFlight=false/true × Stop/replacement Load) pass with zero obsolete posts, deterministic old-promise rejection, winning replacement source, no remaining timers/listeners, and exactly-once Worker termination. Additional passing cases cover Start gate Stop, Load-ready and Start-paused success Stop, end gate replacement, and malformed-message replacement.
- Independent read-only inline probes of the original two scenarios now produce Stop `{settled:true,termination:[1],pending:false,timers:0}` and replacement Load `{settled:true,termination:[1,1],pending:false,timers:0}` after final shutdown, with B successful and source hash B retained. The first rerun of the replacement probe accidentally copied the request-only payload field into its synthetic response, so protocol validation correctly rejected that malformed fixture; removing the extra field made the valid probe pass. No production change was needed for this reviewer fixture error.
- `app.js:1416` now delegates invalidation whenever a controller exists, including ready/idle-owned resources. Focused host tests prove ready Stop, workspace dispose, pending Load Stop and retiring-resource joins, while preserving the active committer cleanup path.
- Re-review command `node --test src/tickreplay/static/strategy/strategy-controller.test.mjs src/tickreplay/static/strategy/strategy-workspace.test.mjs src/tickreplay/static/replay-commit.test.mjs tests/browser/strategy-release.test.mjs` passed **87/87**, exit 0. `node scripts/build_strategy_release.mjs --check` passed for final `fde82a53...f14d` (11 files, 9 edges, two reconstruction roots). `git diff --check` passed. App entry, browser fixture, release test and image metadata references agree; canonical copies match and retained-release identity tests pass.
- No additional Medium+ defect was found in this re-review. No broad verification or new nested Codex consultation was run; the prior consultation limitation remains below.

## Validation

- Reviewer-run focused Node command: `node --test src/tickreplay/static/strategy/strategy-controller.test.mjs src/tickreplay/static/order-broker.test.mjs src/tickreplay/static/replay-commit.test.mjs src/tickreplay/static/strategy/strategy-source-store.test.mjs src/tickreplay/static/strategy/strategy-workspace.test.mjs src/tickreplay/static/strategy/strategy-worker.test.mjs tests/browser/strategy-release.test.mjs` — **122 passed, 0 failed**, exit 0.
- Reviewer-run `node scripts/build_strategy_release.mjs --check` — PASS, 11 files, 9 edges, 2 reconstruction roots, release ID above.
- Reviewer-run `git diff --check` — PASS, exit 0.
- Reviewer-run read-only inline controller probes — reproduced RQ1 scenarios A and B as documented.
- Lead-supplied evidence, not rerun by this reviewer: full verify overall pass, 1300 pytest, 281 Node, browser 11 passed/1 expected skip, guardrail/ownership clean. Passing existing suites does not cover RQ1.
- Browser causal fixture was inspected: actual immutable production Worker/Python emits exact portfolio/fill/cursor observations through actual app replay/broker; owned cancellation and manual-order survival, Stop/reset/restart, Load-only marker, and Python callback failure are asserted. The reviewer did not run a separate browser matrix or production deployment.

## Codex Consultations

Read-only wrapper consultation requested deep admission/reentrancy, transactional closure, and manual-compatibility review using `.agents/logs/codex/prompt-quality-review-pyodide-remediation.md`, label `quality-review-pyodide-remediation`, timeout 240 seconds, medium effort. The wrapper returned exit 3, `ok=false`, `timed_out=true`, `duration_sec=240.01`, `response_chars=0`, `sandbox=read-only`, `write_access=false`. Response `.agents/logs/codex/20260913T232904Z-quality-review-pyodide-remediation.md` is empty; stderr is the same stem with `.err.log`. The consultation yielded no independent assessment and is not PASS. This verdict rests on source inspection, independent executable gates, and the two reproduced failure scenarios.

## Report and Work Log

- Report: `.agents/docs/research/review-quality-pyodide-replay-strategy.md`.
- Reviewer work log: `.agents/logs/agent-teams/team-execute-pyodide-replay-strategy/quality-reviewer-r6.md`.

# Security Review: Browser Python Remediation

## Verdict

PASS after focused re-review. Critical 0, High 0, unresolved Medium 0, Low 0.
Both independently reproduced lifecycle findings below are now resolved.
The warning, numeric transaction, import-size and build-failure remediations
remain supported by source inspection and targeted checks.

Reviewed the working-tree changes against HEAD, scoped by
`.agents/logs/review-diff-pyodide-replay-strategy.patch`, and the active Review
remediation in `.agents/docs/plans/pyodide-replay-strategy.md`. The inspected
final immutable release is
`fde82a53f12e81c6fbac68d116c2afc3fd8e0254907dd7b6e93cb9750a63f14d`.
Production/test files were not changed by this reviewer.

## Blocking findings

None remaining after the re-review documented below.

## Final re-review evidence

- **R6-S1 resolved:** `src/tickreplay/static/app.js:1418` now delegates to an
  existing controller independently of `isActive()`, retaining the active
  committer's cleanup path first. Real host-function extraction tests at
  `src/tickreplay/static/replay-commit.test.mjs:64` exercise ready Stop and the
  real workspace dispose function; repeated calls terminate the Worker exactly
  once and leave it unowned/idle. Additional tests at `:100` and `:116` cover
  pending Load settlement and idle retirement waiting.
- **R6-S2 resolved:** `strategy-controller.mjs:223` passes the captured Worker
  and admission into `#request`. Owner validation at `:662` precedes creation
  of pending work, and `:676` rechecks after the external state callback; the
  pending identity/settlement check at `:677` prevents a stale post. Settlement
  is bound to the original pending object. The new four-case matrix at
  `strategy-controller.test.mjs:142` covers Stop/replacement before and after
  pending publication without firing deadlines; old requests are never posted,
  callers settle, listeners/timers are empty and every Worker terminates once.
  Start/manual gate, successful Load/Start notification, end replacement, and
  error-settlement callback regressions also passed.
- The original independent inline synchronous-Stop probe now returns:

  ```json
  {"tag":"idle","inFlight":false,"loadResult":"invalidated","requests":0,"terminated":1,"timers":0,"listeners":0}
  ```

- Re-review command:
  `node --test --test-reporter=spec src/tickreplay/static/strategy/strategy-controller.test.mjs src/tickreplay/static/replay-commit.test.mjs tests/browser/strategy-release.test.mjs`:
  **73 passed, 0 failed/cancelled/skipped, exit 0**.
- `node scripts/build_strategy_release.mjs --check`: **ok=true**, final release
  `fde82a53f12e81c6fbac68d116c2afc3fd8e0254907dd7b6e93cb9750a63f14d`,
  11 files, 9 edges, 2 reconstruction roots. Current authored copies are
  byte-identical and all retained-release identity tests pass.
- `git diff HEAD --name-only -- src/tickreplay/static/strategy/releases` again
  returned no tracked modifications. The previous tracked immutable releases
  remain unchanged; only the new final release is added in the current diff.
- No further Medium-or-higher issue was established in the correction diff.

## Historical findings and original reproductions

These describe the initial R6 candidate (2914b610…666ed), not unresolved
defects in the final fde82a53…f14d release.

### R6-S1 — Medium, resolved: host Stop/dispose omits the ready Worker

- Location: `src/tickreplay/static/app.js:1418`, called by `stopStrategy` at
  `:1422` and workspace disposal at
  `src/tickreplay/static/strategy/strategy-workspace.mjs:754`.
- Scenario: Load valid Python successfully, do not Start, then invoke the
  public host Stop or dispose the mounted workspace. `invalidateStrategyRuntime`
  only delegates to the controller when `isActive()` is true. The controller
  excludes `ready` from its active tags and has no pending request after Load,
  so this branch returns null with a live Worker still owned by the controller.
  Workspace disposal cancels source admission but uses this same host function
  for runtime shutdown.
- Independent reproduction: an inline Node probe loaded the actual canonical
  `StrategyController` through its fake Worker seam, then evaluated the exact
  `invalidateStrategyRuntime` body extracted from current `app.js`. After
  `await hostInvalidate(controller, null, 'explicit_stop')`:

  ```json
  {"hostStop":{"tag":"ready","isActive":false,"terminated":0,"workerStillOwned":true}}
  ```

  A subsequent direct `await controller.stop()` returned:

  ```json
  {"directControllerStop":{"tag":"idle","terminated":1}}
  ```

- Impact: the app can retain a loaded Pyodide Worker after the requested
  shutdown/disposal. The expanded acceptance invariant explicitly covers all
  created Workers after final Stop/dispose, including Load-before-Start. This
  is not a claim of a sandbox escape or a browser OOM reproduction.
- Fix: route invalidation to an existing controller even when its UI activity
  tag is ready or idle; let controller admission/resource ownership decide what
  needs cancellation. Keep committer invalidation first when a run exists.
  Cover ready Stop, ready disposal, and a failed candidate already retiring
  with tests exercising the real app-host boundary, not a fake host that always
  calls controller.stop. Verify termination exactly once and no pending work.

### R6-S2 — Medium, resolved: synchronous Stop during loading state publication strands a pending request

- Location: `src/tickreplay/static/strategy/strategy-controller.mjs:221`–`:224`;
  related request publication at `:645`–`:650`.
- Scenario: an `onStateChange` callback synchronously calls `controller.stop()`
  on the first `loading` state. The callback advances admission and creates the
  invalidation job before a request exists, so its rejection finds no pending
  request. Returning to the older Load then unconditionally calls `#request`,
  posting the superseded source and arming a new timer. Invalidation terminates
  its Worker but does not reject that newly created pending request.
- Independent reproduction used the actual controller with controlled timers,
  a single fake Worker, and an armed loading-state callback. Immediately after
  awaiting Stop:

  ```json
  {"afterStop":{"tag":"idle","inFlight":true,"loadSettled":false,"workers":[{"terminated":1,"requests":1}]}}
  ```

  Only firing the captured deadline cleared the request and settled Load:

  ```json
  {"afterDeadline":{"tag":"idle","inFlight":false,"loadSettled":true}}
  ```

- Impact: Stop returns with a pending request against a dead Worker; retry can
  encounter `single_flight` until the 20/40-second Load deadline expires. The
  newly added tests cover reentrancy during stopping and pending rejection,
  but not this earlier publication boundary. The public callback seam is
  demonstrated; occurrence through current ordinary UI callbacks was not
  reproduced and is not asserted. It is blocking under the explicitly accepted
  synchronous-reentrancy and immediate settlement/resource-cleanup contract.
- Fix: check the captured admission after externally observable state callbacks
  and before creating/posting a request. During request publication, bind the
  Worker and pending object locally and recheck their identity/settlement after
  `#emitState` before posting. Regression tests should synchronously Stop and
  replace Load from both loading-state and pending-state callbacks, assert no
  superseded post, and assert Stop leaves no timer/pending request and every
  caller settles without firing a deadline.

## Nonblocking findings

None newly established in the reviewed diff. Unverified deployment/browser
qualification is separated below rather than counted as a code defect.

## Original-finding closure

- **S1 warning: resolved.** `index.html:184` persistently states trusted code,
  immediate top-level execution before Start, and the absence of a security
  sandbox with networking/same-origin capabilities. Load and source input use
  `aria-describedby`; the existing editor copies the description to its active
  input. The plaintext notice remains. Server, enhanced/degraded browser, and
  native Load-only marker assertions exercise the intended boundary. No new
  sandbox claim or server-native code execution was introduced.
- **S2 import allocation: resolved.** Workspace `:823` validates file name and
  raw size before `arrayBuffer()`. Source-store decoder independently checks
  raw byte length, allowing bounded BOM/CRLF overhead before canonical limits.
  Fake oversized/wrong-extension no-read assertions and canonical line parity
  tests were inspected.
- **Q1 original overlap and reviewed lifecycle boundary: resolved.** Admission
  is reserved before invalidation;
  cleanup captures its own Worker; listeners are detached and termination is
  idempotent per Worker. Deferred overlap/replacement/stale timer/interrupt tests
  pass. R6-S1 and R6-S2 were discovered during the first review and then repaired
  and independently revalidated as documented in Final re-review evidence.
- **Q2 numeric portfolio closure: resolved for the reviewed strategy path.**
  `paper-trading.mjs:37` supplies the shared checked projection for cash,
  position, average level, realized/unrealized P&L and equity. Broker candidate
  validation checks state and counters, validates each proposed fill and no-fill
  valuation, and publishes a privately prepared whole tick only after outgoing
  snapshot preflight. Replay cursor application follows that validation. Exact
  portfolio/ledger/ID/staged-batch rollback, both signed exposure boundaries,
  aggregate overflow, and invalid legacy Start tests pass. Manual-only accounting
  was not converted into a different arithmetic system.
- **Q3/T4 line parity: resolved.** Store excludes the trailing split sentinel;
  LF/BOM-CRLF and final-newline boundary tests align with protocol/native runner.
- **T5 production bridge: materially addressed.** Actual immutable production
  Worker fixtures cover Load-only execution, causal next-tick fill, cancellation,
  restart and Python failure. Canonical Worker negative tests cover malformed
  outcomes, transport failures and proxy destruction. Serial browser PASS is
  lead-provided evidence, not independently rerun by this security reviewer.
- **T6 build/fetch failures: addressed.** New mocked size/hash/body/allowlist,
  manifest/OID/path/subprocess, zero/ambiguous release and destination-preservation
  checks exercise the rejected-input paths. Inspection confirms reconstruction
  happens in a temporary staging tree before destination publication and image
  manifest construction precedes writing the output. These are fail-before-write
  proofs for rejected inputs, not a claim of crash-atomic disk writes.

## Initial-review validation

- Independently ran focused Node controller, workspace, Worker, broker, replay
  and immutable-release suites: 110 tests, exit 0.
- Independently ran `node scripts/build_strategy_release.mjs --check`: ok=true,
  release `2914b610cc3391acc1552c7df6aa8ff805d44ef0ea120135838050499e6666ed`,
  11 files, 9 edges, 2 reconstruction roots.
- `git diff HEAD --name-only -- src/tickreplay/static/strategy/releases`
  returned no tracked changes. The patch adds only the new release directory;
  the retained release integrity tests passed. Therefore retained release bytes
  are unchanged relative to HEAD and the new security-sensitive canonical files
  reconstruct correctly.
- Python build/fetch selection printed `15 passed in 0.18s`; final process exit
  had not returned at report writing. This is successful assertion output, not
  a claimed observed zero exit. The lead separately supplied full pytest 1300
  pass/one existing warning and overall verify.sh pass.
- Two inline read-only lifecycle probes produced the failure outputs above.
  An initial probe accidentally retained a request-only `payload` field in its
  fake response and correctly failed protocol exact-key validation. The corrected
  response removed that field and demonstrated R6-S1; no implementation change
  or weakened check was used.

## Unverified risks

- No independent full-browser run, pinned CI Chromium, Firefox/WebKit,
  physical-device accessibility, deployment, external dependency-vulnerability
  database scan, or exhaustive vendored implementation audit was performed.
- Same-origin trusted Python remains capable of browser API access and excessive
  resource use. The warning accurately communicates the accepted boundary; a
  Worker is not an adversarial-code security isolation mechanism.
- The final verdict includes the focused R6-S1/R6-S2 correction re-review above.
  No browser run was repeated by this reviewer after the final release change;
  actual production Worker acceptance against fde82a53…f14d remains owned by
  the lead. Further canonical changes require regeneration and revalidation.

## Report/work-log paths

- `.agents/docs/research/review-security-pyodide-replay-strategy.md`
- `.agents/logs/agent-teams/team-execute-pyodide-replay-strategy/security-reviewer-r6.md`

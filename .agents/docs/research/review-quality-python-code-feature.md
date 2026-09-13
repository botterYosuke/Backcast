# Quality Review: Browser Python Replay Strategies

## Verdict

NEEDS_REVISION. High 0, Medium 2, Low 1. Q1 and Q2 are blocking correctness
findings. Review scope is commit `26cc86ec` versus `main`, using
`.agents/logs/review-diff-python-code-feature.patch`; uncommitted preflight gate
fixes are excluded. No implementation files were changed.

## Findings

### Q1 — Medium, blocking: overlapping Load calls orphan a live Worker

- Locations: `src/tickreplay/static/strategy/strategy-controller.mjs:171`,
  `:196`, `:230`, `:647`, `:717`; UI admission at
  `src/tickreplay/static/strategy/strategy-workspace.mjs:529`.
- Current behavior: `load()` awaits invalidation before reserving the load
  operation. No-run invalidation does not publish an `invalidationJob`, and
  termination clears `this.worker` before its async caller resumes. Each load
  then installs its own Worker in the shared field. A single-flight failure
  terminates the current shared Worker, not necessarily that load's candidate.
- Reproduction: load a controller successfully into ready; invoke two further
  `load()` calls in the same turn without awaiting between them. The first
  suspends while disposing the ready Worker; the second sees no current Worker
  and starts a request on Worker B. The first resumes, installs Worker C and
  fails with `single_flight`, terminating C. B remains alive but is no longer
  current, so its messages are ignored. Calling Stop cannot terminate B.
  The workspace's controller acquisition and asynchronous source hash happen
  before a loading status disables controls, so concurrent host admissions
  are not prevented at the UI boundary either.
- Independently observed with the same FakeWorker seam as controller tests,
  disabled interrupt setup, and inert controlled timers:

  ```json
  {"workers":[{"requests":1,"terminated":1},{"requests":1,"terminated":0},{"requests":0,"terminated":1}],"status":"errored","inFlight":true,"firstError":"single_flight"}
  ```

  After `await controller.stop()` and settling both callers, the second caller
  rejects `invalidated`, but Worker B still has termination count zero.
- Impact: a normal repeated Load can fail both attempts, lose the newest source
  attempt, and retain an orphan Pyodide runtime beyond Stop. If its top-level
  Python continues running, the normal controller timeout/termination path no
  longer owns it. This is a lifecycle defect within the trusted-code model,
  not a claim that the Worker is an adversarial sandbox.
- Suggested change: reserve/serialize or supersede load attempts synchronously
  before the first await; bind response publication and disposal to an explicit
  attempt/generation and candidate Worker. Superseding must settle the old
  pending request and terminate its own candidate, not another operation's
  Worker. Also guard workspace load admission during hashing. Test concurrent
  loads from ready and loading, delayed hashing, and Stop after overlap; every
  discarded Worker must terminate, all promises must settle, and only the
  winning attempt may publish the loaded revision.

### Q2 — Medium, blocking: accepted strategy quantities can commit an invalid portfolio

- Locations: `src/tickreplay/static/order-broker.mjs:16`, `:164`, `:638`, `:652`;
  `src/tickreplay/static/replay-commit.mjs:26`; rejection occurs at
  `src/tickreplay/static/strategy/strategy-protocol.mjs:187`.
- Current behavior: each quantity is checked as a positive safe integer and
  the default maximum is `Number.MAX_SAFE_INTEGER`. Matching applies fills
  and swaps the portfolio/order/fill/event state without validating derived
  aggregate quantity or monetary values. Conversion into minor-unit ABI fields
  happens later, where the protocol requires safe integers.
- Reproduction: begin a strategy run and submit a valid single market buy with
  quantity `Number.MAX_SAFE_INTEGER`, observed tick 0 and eligible tick 1. Commit
  the batch at tick 1 and match it at price 100. Convert the resulting portfolio
  with `strategyPortfolioSnapshot(..., 100)` and validate the next event.
  The independently executed probe produced:

  ```json
  {"commit":"committed","filled":1,"position":9007199254740991,"cash_minor":-90071992547409900000,"cashSafe":false,"nextEventError":"invalid_integer"}
  ```

- Impact: an ABI-valid command mutates authoritative paper-trading state into
  a state the next strategy event cannot represent. The strategy then stops
  after the fill, leaving unsafe monetary arithmetic and a portfolio that also
  prevents a subsequent valid Start until the state is reset or reduced.
  Multiple individually safe quantities can similarly exceed aggregate bounds.
  No external/live brokerage execution is involved.
- Suggested change: define representable exposure/monetary bounds and enforce
  them before the strategy fill transaction swaps authoritative state. Market
  prices are only known at matching, so an individual quantity check alone is
  insufficient. Fail with a typed rollback-safe error and preserve the documented
  manual-trading behavior. Add quantity-at-limit, aggregate-position overflow,
  and monetary-conversion overflow tests that assert every authoritative
  component is unchanged when the proposed strategy fill is rejected.
- Scope distinction: floating-point paper-trading arithmetic predates this
  feature. The newly introduced defect is accepting these strategy quantities
  without maintaining the new safe-integer worker ABI after committing them;
  this finding does not request a general rewrite of existing manual accounting.

### Q3 — Low, nonblocking: final LF changes the source line-limit contract

- Locations: `src/tickreplay/static/strategy/strategy-source-store.mjs:65`,
  compared with `src/tickreplay/static/strategy/strategy-protocol.mjs:336` and
  `src/tickreplay/static/strategy/strategy-runner.py:389`.
- Current behavior: the source store counts the final empty split element as a
  line, while the protocol explicitly removes it and the runner uses splitlines.
- Reproduction: construct `"def on_event(e): return []\n" + "\n".repeat(8191)`.
  It has 8192 newline-terminated physical lines and is below all other source
  limits. `validateCanonicalSource` rejects `source_lines_limit`; a load request
  with exactly the same source passes protocol validation. Both results were
  independently observed in one inline Node probe.
- Impact: valid boundary-sized newline-terminated Python cannot be imported,
  saved, or loaded through the source-store path even though the runtime accepts
  it; editor/store and execution contracts disagree.
- Suggested change: share or exactly align the line-count definition, preserving
  longest-line validation. Test 8192 and 8193 physical lines both with and without
  a final LF across source store, protocol, and runner.

## Design and Implementation Assessment

The implementation generally follows the frozen design: explicit Load/Start,
source-only local persistence, worker-owned Python, structured versioned
requests with full source/session/run/sequence identity, bounded diagnostics,
single-flight event callbacks, next-real-tick order eligibility, and broker-owned
authoritative order state. Cancellation scopes and transaction fault injection
are substantive, not success-only stubs. Manual characterization and replay
commit tests protect the legacy projection and final-tick behavior. The above
findings are narrower than a rejection of that architecture.

Inspected canonical source, host app wiring, controller/worker/runner/protocol,
editor/workspace/source store, broker/replay commit, serving changes, build and
fetch scripts, pinned manifests, CI, docs and relevant tests. Generated/vendor
code was assessed through canonical equivalence, declared provenance, manifest
hashes and release reconstruction rather than a line-by-line vendor audit.
Four retained content-addressed release roots are intentional compatibility
artifacts; the current release matches canonical sources. Fix canonical code,
generate a new content-addressed release, and update the release entry point and
checks through the existing build procedure; do not mutate old release identities.

Existing testing covers sequential Load failure, callback single-flight, stale
response rejection, interrupt-before-termination, idempotent cleanup, batch
copy-on-write failure, order ownership, and source limits. It does not cover the
specific concurrent admission or derived numeric boundary scenarios above.
The source-store line test exercises limit rejection, not parity at a final-LF
boundary. Passing current tests therefore does not contradict these reproducers.

## Codex Consultations

The required read-only consultation was invoked with the project wrapper,
timeout 180 seconds, low effort, label `quality-review-python-code-feature`, and
prompt `.agents/logs/codex/prompt-quality-review-python-code-feature.md`.
Attempt 1 returned wrapper exit 3, `ok=false`, `timed_out=true`,
`exit_code=null`, `duration_sec=180.013`, `response_chars=0`,
`response_verified=true`. An empty verified artifact is not a verified review.
Response: `.agents/logs/codex/20260913T201513Z-quality-review-python-code-feature.md`;
stderr: the same stem with `.err.log`. Stderr included read-only sandbox git
temporary-cache and locale warnings; those warnings alone do not establish
the timeout's cause.

Attempt 2, requested by Lead with the same prompt/label/configuration, also
returned wrapper exit 3, `ok=false`, `timed_out=true`, `exit_code=null`,
`duration_sec=180.011`, `response_chars=0`, `response_verified=true`.
Response: `.agents/logs/codex/20260913T202026Z-quality-review-python-code-feature.md`;
stderr: the same stem with `.err.log`. Both response envelopes declared
`sandbox=read-only` and `write_access=false`. The mandatory consultation was
attempted twice, but no independent Codex quality assessment was obtained.
No Codex claim supports this verdict; it rests on the independently reproduced
defects and source inspection. This consultation shortfall remains explicit,
rather than treating timeout as approval.

## Residual Risks

No full vendor/CVE audit, fresh network dependency acquisition, production
deployment, or actual orphaned browser infinite-loop experiment was performed.
The concurrent lifecycle reproduction uses the actual controller with its
documented fake Worker seam; it does not measure browser process memory.
The security report separately records the missing visible trusted-code warning
and pre-size-check import allocation; these are not duplicated in quality counts.

## Validation

- Independent focused probes reproduced Q1, Q2 and Q3 as described above.
- Reused checks executed by this reviewer in the immediately preceding security
  role: 67 focused Node protocol/controller/broker/replay/release tests passed;
  21 native runner tests passed with exit 0; release reconstruction `--check`
  passed (11 files, 9 edges, 2 roots). Vendor manifest audit verified all 5
  CodeMirror and 8 Pyodide listed files with exact hashes and no extra files.
- Selected server tests printed 27 passed / 27 deselected; final tool-session
  exit was not observed, so no exit-zero claim is made for that command.
- No global suite rerun. No numeric coverage estimate is claimed.
- Reviewer work-log validation passed with exit 0, no missing sections or
  metadata, and no warnings. Report headings/findings were manually checked;
  no specialized quality-report contract exists in the shared validator.

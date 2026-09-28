# Security Review: Bundled RL Default Strategy

## Final Verdict

**PASS WITH NON-BLOCKING HARDENING — no Critical or High issue blocks shipment.**

The targeted remediation improves malformed-input rejection, storage-error
sanitization, cancellation responsiveness during slow artifact I/O, and failed
Load cleanup. The final browser release is internally consistent. The prior
Medium and two Low findings remain open, and the new lock/commit ordering adds
one Low lifecycle/resource finding.

Final finding counts: **Critical 0 · High 0 · Medium 1 · Low 3**.

## Scope Reviewed

- Feature-owned paths in
  `.agents/logs/ownership-team-execute-bundled-rl-default-strategy.json`.
- Targeted remediation in backend request validation, job locking/storage,
  process integration, frontend Load cancellation, and final release
  `51a39c13a038b423cffdab52cc4185bb1614a0ff6b5b44152c442cf7853b61e5`.
- Prior findings S-01 through S-03, plus regressions introduced at the same
  boundaries.
- Excluded as instructed: unrelated dirty files, `.codex/config.toml`, `.env`,
  retained releases, and prior-feature artifacts.

## Findings

### Medium S-01 (open): single-flight cancellation has no submitter ownership

**Evidence:** `src/tickreplay/bundled_rl_service.py:147-149` returns the same job
ID to every submitter with the same input identity. Lines 188-203 cancel that
whole job using only the shared ID. The HTTP DELETE at
`src/tickreplay/server.py:860-868` has no per-submitter lease. The final browser
controller still cancels by job ID alone at
`src/tickreplay/static/strategy/strategy-trace-controller.mjs:191-212`.

**Impact:** two legitimate tabs loading the same session and preset share one
running job. Stop, superseding Load, or cleanup in either tab can cancel the
other tab's computation. This is local cross-client availability and lifecycle
isolation, not code execution or broker authority expansion.

**Concrete fix:** return a random lease for every submit, including coalesced
submits. Require the exact `{jobId, leaseId}` pair for release and cancel the
underlying job only when its last active lease is released. Test two controllers
sharing one computation.

### Low S-02 (open): a failed Stop cancellation is not retryable

**Evidence:** the remediation now performs best-effort cancellation when Load
fails after acquiring a job ID
(`strategy-trace-controller.mjs:144-148`), which is a useful improvement. But
`invalidate()` still clears `attempt`, `run`, and `loaded` before DELETE and
unconditionally transitions to idle (`:191-198`). `#cancelJob` forgets a rejected
request (`:207-212`), leaving no retained job ID or bounded retry path.

**Impact:** after a transient loopback HTTP failure, Stop can appear complete
while the fixed worker continues until completion or backend timeout and may
publish an unused trace. Load is broker-inert, so the effect is limited to
temporary CPU and disk work.

**Concrete fix:** retain cancellation identities until a bounded retry succeeds,
or retry within Stop before settlement. Mark cancellation complete only after a
valid matching acknowledgement. Test both a lost request and a lost response.

### Low S-03 (open): release writing is not exclusive

**Evidence:** `scripts/build_strategy_release.mjs:248-252` recursively opens the
content-addressed destination and writes files with ordinary `writeFileSync`,
which truncates existing files. The `--write` path at lines 339-340 performs no
destination-absence or byte-equality check.

**Impact:** a mistaken or repeated trusted build command can mutate an already
published immutable directory before later verification catches it. The
checked-in final release currently verifies, and this path is not remotely
reachable from the application.

**Concrete fix:** create the final directory exclusively, or prepare a new
sibling and atomically rename only if the destination is absent. If it exists,
compare bytes and exit without writing; fail on any mismatch. Add a test proving
`--write` cannot repair a tampered retained release.

### Low S-04 (new regression): cancellation can leave a published artifact

**Evidence:** `src/tickreplay/bundled_rl_service.py:266-272` checks that the job
is still validating while holding the manager lock, then releases the lock.
`TraceStore.commit()` executes outside the lock at lines 273-281. A concurrent
cancel or timeout can make the job terminal during that commit; the second check
at lines 282-288 prevents `ready`, but cannot undo the already-created final
trace file. A deterministic blocking-store probe canceled after commit began and
observed:

```text
cancel_state=cancelled, final_state=cancelled,
files_after_cancel=[<trace-id>.json]
```

The responsiveness test at
`tests/test_tickreplay_bundled_rl_service.py:184-207` verifies prompt cancel and
terminal state, but does not assert that the store remains free of final files.

**Impact:** repeated local cancellation or timeout at this narrow phase can
accumulate unreachable immutable trace files. It cannot publish a ready job,
execute an order, alter trace bytes, or expose submitted source, so this is a
bounded disk-resource/lifecycle issue rather than an integrity or RCE issue.

**Concrete fix:** make publication conditional on a manager-owned commit token
that cancellation revokes, or add rollback for a newly-created target when the
post-commit state check fails (without deleting a target that pre-existed or is
referenced by another job). Add deterministic cancel-during-commit and
timeout-during-commit assertions that no newly published final artifact remains.

## Remediation and Controls Verified

- `src/tickreplay/server.py:650-685` now validates exact request shape, stem,
  bounded non-NUL code, lowercase hash identity, and canonical date before
  repository or manager access. Side-effect-zero rejection tests pass.
- `src/tickreplay/bundled_rl_service.py:255-264` maps prepare failures to a
  bounded public `artifact_store_failed` error. Commit errors are also
  sanitized; storage details and paths are not returned.
- Moving slow commit I/O outside the manager lock keeps cancellation responsive,
  and the second state/deadline check prevents a canceled or timed-out job from
  becoming `ready`. S-04 is the remaining cleanup gap.
- The process boundary remains fixed argv with `shell=False`, reduced
  environment, bounded framing/output/time, cancellation, and process-group
  termination. No process-boundary regression was found in the remediation.
- The Native API remains source-free and fixed-preset. Browser Load sends only
  preset/session identity, verifies the trace hash and schema, and remains
  broker-inert until explicit Start/replay.
- Failed Load now best-effort cancels its acquired job while preserving the
  original Load error; stale cleanup cannot overwrite a newer attempt.
- Final release `51a39…61e5` reconstructs deterministically, its authored copies
  match, `app.js` pins it, and retained-release verification passes.

## Targeted Validation

```text
uv run pytest -q tests/test_tickreplay_bundled_rl_service.py \
  tests/test_tickreplay_bundled_rl_runtime.py \
  tests/test_tickreplay_bundled_rl_process.py \
  tests/test_tickreplay_server.py -k 'bundled_rl'
35 passed, 56 deselected, 1 deprecation warning

node --test \
  src/tickreplay/static/strategy/strategy-trace-controller.test.mjs
20 passed, 0 failed

npm run check:strategy-release && \
  node --test tests/browser/strategy-release.test.mjs
release 51a39…61e5 verified; 3 passed, 0 failed

uv run python scripts/verify_bundled_rl_snapshot.py verify-retained \
  --phase security-rereview \
  --snapshot .agents/logs/bundled-rl-default-strategy-retained-releases.json \
  --releases src/tickreplay/static/strategy/releases \
  --expected-new-id 51a39c13a038b423cffdab52cc4185bb1614a0ff6b5b44152c442cf7853b61e5
ok: true
```

The commit/cancel race was additionally reproduced with an isolated temporary
store and fixed in-process worker adapter; it did not touch repository data.

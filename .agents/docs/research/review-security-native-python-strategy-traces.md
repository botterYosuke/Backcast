# Security Review: Native Python Strategy Traces

## Verdict

**FAIL — not ready to ship with Native Trace enabled.**

The implementation has strong trace-integrity controls, keeps source execution out of the FastAPI process, is disabled by default, and rejects Cloud Run enablement. However, the enabled arbitrary-code endpoint does not enforce its advertised local-caller boundary. A client that can reach the app can submit Python to run with the app user's privileges. This is a High-severity boundary failure even though enablement requires an explicit trust acknowledgement.

Finding counts: **Critical 0 · High 1 · Medium 3 · Low 1**.

## Scope Reviewed

- Complete feature diff in `.agents/logs/review-diff-native-python-strategy-traces.patch`, excluding the pre-existing/user-owned `.codex/config.toml` and prior spike notes.
- Live backend, worker, trace store, HTTP routes, browser trace protocol/controller, release builder, documentation, and focused tests.
- Threat areas: arbitrary-code entry, fixed argv and environment, process lifetime, framing and size bounds, trace identity/integrity, path traversal, concurrency, data leakage, default-off behavior, and Cloud Run behavior.

## Findings

### High S-01: `trustedLocalOnly` is descriptive, not an enforced caller boundary

**Evidence**

- `src/tickreplay/config.py:94-102` rejects Cloud Run and requires an operator acknowledgement, but does not constrain which clients may use the endpoint.
- `src/tickreplay/server.py:453-456` installs no host/origin/local-peer guard.
- `src/tickreplay/server.py:687-753` accepts arbitrary source and schedules it without authentication, an origin check, a Host allowlist, or a loopback-peer check.
- `src/tickreplay/server.py:670-684` reports `trustedLocalOnly: true` even though the server does not enforce that property.
- `run.py:22-38` defaults to loopback but permits `--host 0.0.0.0` or another non-loopback bind. A loopback bind alone also does not address DNS-rebinding attacks when Host is unrestricted.

**Impact**

Once Native Trace is enabled, any client that can reach the FastAPI service can execute submitted Python with the service account's filesystem and network privileges. This converts an accidental LAN bind, reverse-proxy exposure, or DNS-rebinding path into host-level code execution. Default-off and the warning reduce accidental enablement but do not protect an enabled service.

**Required fix**

Add a shared server-side guard for all `/api/native-strategy/*` routes, with the state-changing job route treated as mandatory:

1. Reject non-loopback client addresses for v1, failing closed if the peer address is missing or unparsable.
2. Reject non-local Host values to prevent DNS rebinding; allow only explicit loopback literals and `localhost` with the active port.
3. For browser state-changing requests, require an absent Origin only for a documented local non-browser client, or require Origin to exactly match the validated local origin. Prefer an unguessable per-process CSRF capability if non-browser and browser callers must coexist behind anything other than direct loopback.
4. Refuse Native Trace startup when the deployment/bind mode cannot guarantee direct local access. Do not trust forwarded headers for this decision.
5. Add API tests for a remote peer, hostile Host, hostile Origin, and valid IPv4/IPv6 loopback calls.

### Medium S-02: Parent stdout/stderr collection is unbounded before frame validation

**Evidence**

- `src/tickreplay/native_strategy.py:114-130` calls `process.communicate()` in a thread and stores both complete byte strings in memory.
- The 4 MiB response limit is checked only after collection at `src/tickreplay/native_strategy.py:184-189`.
- Any stderr at all is rejected only after complete collection at `src/tickreplay/native_strategy.py:149-151`.
- The bundled worker redirects ordinary user streams, but this parent adapter is also the trust boundary for a configured external executor; a defective executor, native extension, or code that deliberately reopens descriptors can still write without bound.

**Impact**

A misbehaving child can exhaust FastAPI-process memory before the advertised frame limit is applied. The framed protocol is bounded logically but not at the OS pipe boundary.

**Required fix**

Read stdout and stderr through capped drains. Terminate the process group immediately when stdout exceeds `4 MiB + 4` or stderr exceeds a small documented diagnostic cap, while still draining/closing both pipes without deadlock. Never retain bytes beyond `limit + 1`. Add real-subprocess tests that emit oversized stdout and stderr and assert bounded parent memory, termination, and sanitized failure.

### Medium S-03: Windows cancellation kills only the worker, not its descendants

**Evidence**

- `src/tickreplay/native_strategy.py:100-108` creates a new session only when `os.name != "nt"`.
- POSIX cancellation targets the process group at `src/tickreplay/native_strategy.py:164-178`, while Windows uses only `process.terminate()` / `process.kill()` at `src/tickreplay/native_strategy.py:167-179`.
- The focused tests exercise a successful real subprocess but do not create a descendant and cancel/timeout it (`tests/test_tickreplay_native_strategy.py:280-302`).

**Impact**

On Windows, strategy or ML-library child processes can survive Stop, timeout, and application shutdown. They can continue consuming CPU/GPU, holding files, or running trusted code after the UI and job state claim termination.

**Required fix**

Own the complete process tree with a Windows Job Object configured for kill-on-close, or fail closed and mark Native Trace unsupported on Windows until equivalent tree termination exists. Add platform-specific cancellation, timeout, and shutdown tests with a spawned descendant. Also test POSIX process-group cleanup.

### Medium S-04: The HTTP source limit is applied only after FastAPI buffers/parses the body

**Evidence**

- `src/tickreplay/server.py:687-696` accepts an untyped `Body(...)`, so Starlette/FastAPI reads and decodes the request before `_native_job_request` runs.
- The 256 KiB source limit is enforced later by `NativeStrategyManager._validate_source` at `src/tickreplay/native_strategy.py:343-354`.
- There is no route or middleware request-byte cap in `src/tickreplay/server.py:453-456`.

**Impact**

A reachable client can force allocation and JSON parsing of a body far larger than the advertised source limit. Combined with S-01, this is a remotely triggerable memory/CPU denial of service whenever Native Trace is enabled.

**Required fix**

Enforce a conservative encoded-body limit before JSON decoding, accounting for JSON escaping at the maximum valid source size. Reject oversized or missing/invalid framing with `413`. Add Content-Length and chunked/streamed over-limit tests; Content-Length alone is not sufficient.

### Low S-05: Competing timeout owners can report an actual timeout as `execution_failed`

**Evidence**

- The manager starts a timer at `src/tickreplay/native_strategy.py:411-417` and marks `timed_out` at `src/tickreplay/native_strategy.py:509-521`.
- The subprocess adapter independently computes a deadline at `src/tickreplay/native_strategy.py:131-146` and raises `TimeoutError` after killing the child.
- `_run` catches every executor exception through one generic branch at `src/tickreplay/native_strategy.py:419-431`, which maps the result to `failed/execution_failed` if the adapter deadline wins the scheduling race.

**Impact**

Timeout status is nondeterministic under scheduler load. The child is still killed and no artifact is published, so this is not a code-execution escape, but it weakens auditability and operational response.

**Required fix**

Use one timeout owner, or catch a dedicated executor-timeout exception and atomically transition to `timed_out`. Add a real sleeping-child regression that repeats enough times to exercise the race and always observes `timed_out`.

## Controls Verified

- FastAPI does not import, compile, evaluate, or execute submitted source; source execution occurs in `native_strategy_worker.py`.
- Executor argv is passed as a sequence with `shell=False` (`src/tickreplay/native_strategy.py:100-108`), so submitted source cannot inject shell arguments.
- Child environment inheritance is reduced to a short allowlist (`src/tickreplay/native_strategy.py:93-98`); submitted source and raw stderr are not returned through public job errors.
- Default enablement is false, explicit non-sandbox trust acknowledgement is required, and `K_SERVICE` rejects Cloud Run enablement (`src/tickreplay/config.py:75-102`).
- Trace IDs are SHA-256 of canonical validated bytes, names are strict lowercase 64-hex, writes use exclusive temporary files plus atomic replace, and reads rehash/revalidate (`src/tickreplay/strategy_trace.py:418-498`). No path-traversal route was found.
- Parent validation binds the artifact to input, source, authoritative session data hash/count, runtime fingerprint, protocol, and run config (`src/tickreplay/native_strategy.py:442-507`).
- Trace schema rejects unknown fields and excludes dedicated source/fill/cash/portfolio/stderr fields; browser rehashes fetched bytes and revalidates identity before replay.
- Replay keeps fill and portfolio authority in the existing broker/commit path; no live-broker integration was introduced.

## Accepted Trust-Boundary Notes

These are not additional findings because v1 explicitly accepts only trusted code and clearly says it is not a sandbox:

- Submitted Python can access the worker's filesystem/network, import modules, allocate memory, and attempt to spawn or detach processes. Environment stripping and a subprocess boundary do not make it safe for untrusted code.
- The worker inherits its current directory, and the runtime fingerprint is operator-supplied rather than measured from executable/model bytes. Operators must keep the executor environment private and update the fingerprint whenever runtime/model/config bytes change.
- Job metadata and trace files have no retention/GC quota. Advanced retention is explicitly out of v1 scope, but long-lived enabled installations must monitor disk and process-local job growth.

## Validation Performed

```text
uv run pytest -q tests/test_tickreplay_strategy_trace.py \
  tests/test_tickreplay_native_strategy.py \
  tests/test_tickreplay_native_strategy_worker.py \
  tests/test_tickreplay_config.py \
  -k 'native or trace or worker or store or subprocess or cloud'

62 passed, 30 deselected
```

The passing suite confirms the implemented positive controls. It does not cover the High local-caller boundary, pre-parse body cap, oversized parent pipes, descendant cleanup, or the real-subprocess timeout race described above.

---

## Final Remediation Review — 2026-09-28

This section is the authoritative final verdict for the remediated implementation. The original review above is retained as finding history.

### Final Verdict

**FAIL — two lifecycle-safety fixes are still required before merge.**

All five original findings are resolved. No Critical or High finding remains. The remediated implementation now enforces the direct-loopback HTTP boundary, caps request and subprocess streams before unbounded buffering, owns the POSIX process group, fails closed on Windows, and deterministically maps executor timeouts to `timed_out`. The new two-phase publication and per-submitter leases are otherwise sound.

Two Medium issues remain: publication can win after the configured wall-clock deadline if the timer callback is delayed, and the browser permanently suppresses lease-release retries before it has received a valid acknowledgement. Both violate the requested timeout/cancellation safety contract around trusted native code.

Final unresolved finding counts: **Critical 0 · High 0 · Medium 2 · Low 0**.

### Resolution of Original Findings

- **S-01 resolved.** `src/tickreplay/server.py:698-712` fails closed unless the actual socket peer is loopback, exactly one Host is an allowed local authority, and any Origin exactly matches the validated local origin. Forwarded headers are not trusted. Every native capability/job/lease/trace route calls the guard (`src/tickreplay/server.py:763-785`, `851-877`). This is a direct-loopback boundary; operators must not place an untrusted Host-rewriting/Origin-stripping reverse proxy in front of it.
- **S-02 resolved.** `src/tickreplay/native_strategy.py:148-206` incrementally drains nonblocking pipes, retains at most the documented stdout/stderr caps plus one byte, and terminates immediately on overflow or any stderr. `communicate()` is no longer used.
- **S-03 resolved.** POSIX workers start in their own session and termination targets their process group (`src/tickreplay/native_strategy.py:109-117`, `226-255`). Native execution fails closed on Windows until equivalent process-tree ownership exists (`src/tickreplay/config.py:94-103`).
- **S-04 resolved.** `src/tickreplay/server.py:715-760` checks the local boundary and Content-Length, streams into a bounded bytearray, rejects overflow with `413`, and only then decodes/parses strict JSON. The native POST route uses this dependency rather than FastAPI `Body` (`src/tickreplay/server.py:781-784`).
- **S-05 resolved.** `src/tickreplay/native_strategy.py:545-554` catches the executor's dedicated `TimeoutError` and converges on the manager's atomic `timed_out` transition. Repeated focused tests observe deterministic timeout classification.

### Medium S-06: Timer scheduling delay can publish an artifact after the job deadline

**Evidence**

- The timer is started at `src/tickreplay/native_strategy.py:537-543`, but no absolute deadline is stored with the job.
- Validation and `TraceStore.prepare()` execute outside the manager lock at `src/tickreplay/native_strategy.py:571-591`.
- Immediately before `TraceStore.commit()`, the publication gate checks only `current.state == "validating"` (`src/tickreplay/native_strategy.py:593-600`). It does not recheck `time.monotonic()` or the cancellation event.
- Publication then cancels the timer and sets `ready` at `src/tickreplay/native_strategy.py:610-618`. Therefore, if the timer callback has not been scheduled by the wall-clock deadline, the late publication wins permanently.
- An adversarial scheduler-delay probe reproduced the issue: with `timeout_seconds=0.01`, a valid executor result, `prepare()` delayed by `0.05` seconds, and the timer callback delayed by `0.25` seconds, the terminal state was `ready` with a fetchable trace ID.

**Exact deterministic regression**

In `tests/test_tickreplay_native_strategy.py`, save the real `threading.Timer`, monkeypatch `native_strategy.threading.Timer` to construct that saved timer with `interval + 0.25`, use a `SlowPrepareStore` whose `prepare()` calls `super().prepare(trace)` and then sleeps `0.05`, and create the manager with `timeout_seconds=0.01`. Submit the normal valid `_artifact`, wait for a terminal state, and assert `timed_out`, `trace_id is None`, and an empty artifact directory. The current implementation deterministically returns `ready` and leaves `<trace_id>.json`; the probe output is recorded under Final Validation below.

**Impact**

The implementation does not uphold its stated invariant that work exceeding the configured job timeout becomes `timed_out` and never publishes. Scheduler delay under CPU pressure can turn expired work into an immutable ready artifact. The artifact remains schema- and identity-validated, so this is not an RCE expansion, but it is a lifecycle and publication-integrity failure.

**Required fix**

Store an absolute monotonic deadline when the job transitions to `running`. Under the manager lock, immediately before `store.commit(prepared)`, atomically reject publication when the deadline has elapsed or cancellation is set and perform the appropriate terminal transition. Keep the timer as a wake-up mechanism, not as the source of truth. The minimum code change is one per-job absolute deadline plus this pre-commit comparison; no store API change is required because the existing `finally: discard(prepared)` removes the temporary file.

### Medium S-07: Failed lease release is recorded as complete before acknowledgement

**Evidence**

- `src/tickreplay/static/strategy/strategy-trace-controller.mjs:475-479` inserts `jobId|leaseId` into `releasedLeases` before sending the DELETE request.
- A transport failure, malformed acknowledgement, or mismatched acknowledgement throws at `src/tickreplay/static/strategy/strategy-trace-controller.mjs:479-483`, but the set entry is never removed.
- All later release attempts return immediately at `src/tickreplay/static/strategy/strategy-trace-controller.mjs:477`, even when the server never received the first request.
- Stop clears the controller's job references and returns the UI to `idle` in a `finally` block (`src/tickreplay/static/strategy/strategy-trace-controller.mjs:400-429`), leaving no controller path to retry the still-active lease.
- The backend release operation is already idempotent (`src/tickreplay/native_strategy.py:502-528`), so retry after a lost response is safe and returns the recorded result.

**Exact deterministic regression**

In `strategy-trace-controller.test.mjs`, provide a transport whose submit/status path yields a non-terminal job plus lease, hold status polling so the attempt remains active, and make `cancel(jobId, leaseId)` reject its first call. Start `load()` without awaiting it, wait until the attempt has entered status polling, call `stop()`, then call `stop()` again after the first rejection. Assert the current defect: the controller is `idle` and only one DELETE-equivalent call occurred because both the deduplication key and cleared job reference suppress retry. The fixed regression should make `cancel` return the matching valid acknowledgement on its second call and assert two calls with the exact same job/lease pair, final `idle`, and no release of another coalesced submitter's lease.

**Impact**

A transient loopback HTTP failure can make Stop appear complete while trusted native Python continues until the backend timeout. This does not escape the backend's eventual timeout/process-tree controls, but it weakens the user-visible cancellation boundary for arbitrary native code.

**Required fix**

Track in-flight and acknowledged releases separately, or remove the deduplication key when DELETE/validation fails. Mark the lease released only after a valid matching acknowledgement. Because Stop currently clears its job reference, the minimum complete fix must also retain/enqueue the failed `{jobId, leaseId}` until a bounded retry succeeds (or make `#cancelJob` perform that bounded retry before Stop settles). Backend idempotence makes a retry safe, including the lost-response case.

### Lease and Publication Controls Otherwise Verified

- Every submission, including a coalesced one, receives an independent random lease; releasing one lease does not cancel shared work while another active lease remains (`src/tickreplay/native_strategy.py:423-455`, `502-528`). Wrong job/lease pairs fail closed and releases are idempotent.
- Submit versus last-release is serialized under the same manager lock. If last-release wins, `_by_input` is removed by the terminal transition and a later submit creates a new job; if submit wins, the added lease prevents cancellation.
- Two-phase storage validates and fsyncs a bounded canonical temporary artifact before publication, verifies store ownership/path form, uses content addressing and atomic replace, and revalidates bytes on read (`src/tickreplay/strategy_trace.py:462-548`). Cancellation/timeout observed before commit leaves neither a final artifact nor a temporary file.
- Fixed argv with `shell=False`, reduced child environment, sanitized public errors, artifact/source/session/runtime/protocol identity binding, default-off configuration, and Cloud Run rejection remain intact.

### Final Validation Performed

```text
uv run pytest -q tests/test_tickreplay_strategy_trace.py \
  tests/test_tickreplay_native_strategy.py \
  tests/test_tickreplay_native_strategy_worker.py \
  tests/test_tickreplay_config.py \
  tests/test_tickreplay_server.py \
  -k 'native or trace or worker or store or subprocess or cloud or null_trade'

99 passed, 83 deselected, 1 warning

node --test \
  src/tickreplay/static/strategy/strategy-trace-protocol.test.mjs \
  src/tickreplay/static/strategy/strategy-trace-controller.test.mjs \
  src/tickreplay/static/strategy/strategy-workspace.test.mjs

43 passed, 0 failed
```

The additional scheduler-delay probe produced:

```text
{'state': 'ready', 'trace_id': '464488eb0a5637d5def700004f4d325ab7028be4b5bf9259548885229380d03a',
 'configured_timeout_s': 0.01, 'prepare_delay_s': 0.05}
```

---

## Final Closure Re-review — 2026-09-28

This closure note supersedes the preceding remediation verdict while retaining the complete finding history.

### Closure Verdict

**PASS — S-06 and S-07 are resolved; no reproducible security blocker remains.**

Final unresolved finding counts: **Critical 0 · High 0 · Medium 0 · Low 0**.

### S-06 Closed: deadline is authoritative at the publication boundary

- `src/tickreplay/native_strategy.py:537-543` computes the absolute monotonic deadline when execution transitions to `running`; the timer remains only an asynchronous wake-up path.
- After validation and temporary-file preparation, the manager reacquires its lifecycle lock at `src/tickreplay/native_strategy.py:594-596`.
- Under that lock and immediately before `TraceStore.commit()`, it first requires `validating` state and then compares `time.monotonic()` with the captured deadline (`src/tickreplay/native_strategy.py:596-603`). An expired job transitions through `_time_out_locked()` and returns without commit.
- Cancellation, shutdown, and timer timeout also transition job state while holding the same manager lock (`src/tickreplay/native_strategy.py:502-528`, `655-668`, `701-715`). Therefore a job already made terminal cannot pass the pre-commit `validating` gate, and a timer cannot interleave a `timed_out` transition between the deadline check and commit.
- The existing `finally: self.store.discard(prepared)` at `src/tickreplay/native_strategy.py:623-624` removes the prepared temporary artifact on the timeout path.

The exact prior adversarial probe now returns:

```text
{'state': 'timed_out', 'trace_id': None, 'configured_timeout_s': 0.01,
 'prepare_delay_s': 0.05, 'json_artifacts': 0, 'temporary_artifacts': 0}
```

The deterministic regression at `tests/test_tickreplay_native_strategy.py:634-682` disables the timer callback, delays `prepare()` past the absolute deadline, and proves `timed_out`, no trace ID, no final JSON, no temporary file, and idempotence of a late timer callback.

### S-07 Closed: lease release records acknowledgement, not intent

- The controller retains outstanding lease identities independently of active/loaded UI state in `leaseReleases` (`src/tickreplay/static/strategy/strategy-trace-controller.mjs:179-180`, `501-509`). Stop remembers the current lease and drains all outstanding leases before settling (`src/tickreplay/static/strategy/strategy-trace-controller.mjs:402-432`, `511-514`).
- `#cancelJob()` records the lease in `releasedLeases` and deletes its retry record only after schema validation and exact job/lease identity validation succeed (`src/tickreplay/static/strategy/strategy-trace-controller.mjs:478-491`). Transport failure, invalid schema, and mismatched identity leave the record in `leaseReleases`.
- The `finally` path clears only the failed record's `inFlight` promise (`src/tickreplay/static/strategy/strategy-trace-controller.mjs:493-498`), making a later Stop retry the same job/lease pair.
- Concurrent callers return the same in-flight promise (`src/tickreplay/static/strategy/strategy-trace-controller.mjs:481-483`), so they produce one DELETE. After a valid acknowledgement, `releasedLeases` suppresses later duplicate DELETEs (`src/tickreplay/static/strategy/strategy-trace-controller.mjs:479-490`).
- Per-submitter identity remains intact: retry records are keyed by the exact `jobId|leaseId`, and the coalesced-controller test confirms one controller releases only its own lease.

Regression coverage is explicit at `src/tickreplay/static/strategy/strategy-trace-controller.test.mjs:236-303` for successful-ack deduplication, transient DELETE retry, invalid-ack retry, and concurrent-call coalescing, plus `:360-380` for coalesced submitter isolation.

### Closure Validation

```text
uv run pytest -q tests/test_tickreplay_native_strategy.py \
  -k 'deadline_expiry_during_prepare or cancel_or_timeout_during_slow_prepare or executor_timeout_exception'

4 passed, 32 deselected

node --test \
  --test-name-pattern='failed lease DELETE|invalid lease acknowledgement|concurrent Stop calls|Stop releases a loaded-ready lease|releases only its own lease' \
  src/tickreplay/static/strategy/strategy-trace-controller.test.mjs

5 passed, 0 failed

uv run pytest -q tests/test_tickreplay_native_strategy.py

36 passed

node --test src/tickreplay/static/strategy/strategy-trace-controller.test.mjs

19 passed, 0 failed
```

No implementation file was modified by this review.

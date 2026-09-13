# Test Coverage Review: Browser Python Replay Strategies

## Verdict

NEEDS_REVISION. High 0, Medium 4, Low 2 coverage gaps. T1, T2 and T3 are
blocking regression-test requirements for independently demonstrated quality
Q1/Q2 and security S1; they are not three additional implementation defects.
T4-T6 are nonblocking gaps. Missing coverage by itself is not reported as a
demonstrated runtime defect.

The existing tests are substantial and pass the selected Python and Node runs,
but cannot establish the complete feature's correctness: actual reproductions
have passed through the uncovered boundaries. The initial browser run also had
one performance assertion failure; its focused retry passed without changing
the test or threshold. This is not an all-browser-gates-pass result.

## Review Scope and Source Classification

Reviewed commit `26cc86ec` versus `main` using
`.agents/logs/review-diff-python-code-feature.patch`. Uncommitted preflight fixes
are excluded from findings. Executable checks ran on the repaired working tree,
not an isolated checkout of the original broken commit. In particular, measured
`server.py` includes the preflight `anyio.to_thread` import repair; percentages
are whole-file measurements, not commit-delta coverage.

| Class | Files / handling |
| --- | --- |
| Hand-written runtime Python | `src/tickreplay/server.py`; canonical `src/tickreplay/static/strategy/strategy-runner.py`; measured separately from release copies |
| Hand-written JS feature modules | `order-broker.mjs`, `replay-commit.mjs`, and canonical strategy controller/editor/protocol/source-store/worker/workspace; these eight modules form the reported JS feature denominator |
| Additional hand-written UI | `static/app.js`, `static/index.html`, `static/styles.css`, `frontend/strategy-editor-entry.mjs`; browser behavior inspected/tested, but no source-mapped execution coverage for these files |
| Hand-written build/fetch tools | `scripts/build_strategy_editor.mjs`, `scripts/build_strategy_release.mjs`, `scripts/build_strategy_images.py`, `scripts/fetch_pyodide_assets.py`; reproducibility checks separate from unit/branch test evidence |
| Generated release copies | Four `static/strategy/releases/<id>/` closures, including manifest/identity/generated entry and asset modules; excluded from coverage denominators and verified by content identity/current canonical parity |
| Vendored/generated bundles | `static/vendor/codemirror/<id>/` and `static/vendor/pyodide/0.27.6/<id>/`; excluded, including minified JS/WASM/stdlib/archive/licenses; pinned provenance and reconstruction verified |
| Test infrastructure and fixtures | `tests/browser/` spike host/worker/server, Playwright configuration/lock/decisions, `.test.mjs`, Python tests, frozen maximum-source/envelope/header fixtures; not product-source coverage |
| Documentation/config/metadata | Design/plan/runbook, package manifests/lock, CI, `.gitattributes`, `.gitignore`, image-input manifest; inspected as contracts or generated provenance, not counted as executable runtime |

No source, test, lockfile or dependency declaration was edited. `pytest-cov`
was acquired only in an ephemeral `uv run --with` environment. No browser was
installed and no coverage denominator includes vendor/release duplicates.

## Measured Coverage

### Python

Coverage.py 7.16.1, pytest-cov 7.1.0, Python 3.13.11; branch recording enabled.
JSON: `.agents/logs/coverage-python-code-feature.json`.
Configuration/data/log are alongside it with `.rc`, `.data`, `.log` suffixes.
The config includes four first-party files; the two build/fetch scripts are
intentionally visible as zero rather than silently disappearing.

| Scope | Covered statements / total | Statement coverage | Covered branches / total | Branch coverage |
| --- | ---: | ---: | ---: | ---: |
| `src/tickreplay/server.py` | 272 / 294 | 92.5170% | 55 / 64 | 85.9375% |
| Canonical `strategy-runner.py` | 257 / 298 | 86.2416% | 95 / 132 | 71.9697% |
| Runtime subtotal, above two files | 529 / 592 | 89.3581% | 150 / 196 | 76.5306% |
| `scripts/build_strategy_images.py` | 0 / 84 | 0% | 0 / 28 | 0% |
| `scripts/fetch_pyodide_assets.py` | 0 / 92 | 0% | 0 / 26 | 0% |
| Entire four-file JSON denominator | 529 / 768 | 68.8802% | 150 / 250 | 60.0000% |

The scripts' 0% means **not exercised by this instrumented pytest run**. Both
subsequently passed their real `--check` commands, but those uninstrumented
success checks do not cover negative paths or raise the test-coverage numbers.
The JSON's `percent_covered` is Coverage.py's combined statements-plus-branches
metric: runtime subtotal 86.1675%; all four files 66.6994%. It must not be
misreported as line or branch coverage. This is neither repository-wide nor
new-lines-only coverage.

### JavaScript

Node 24.15.0's native experimental test coverage was available and used. Raw
LCOV: `.agents/logs/coverage-js-python-code-feature.lcov`; native summary:
`.agents/logs/coverage-js-python-code-feature.log`.

| First-party feature module | Lines covered / total | Line % | Branches covered / total | Branch % |
| --- | ---: | ---: | ---: | ---: |
| `order-broker.mjs` | 706 / 763 | 92.53 | 171 / 207 | 82.61 |
| `replay-commit.mjs` | 238 / 251 | 94.82 | 45 / 50 | 90.00 |
| `strategy-controller.mjs` | 676 / 800 | 84.50 | 133 / 198 | 67.17 |
| `strategy-editor.mjs` | 210 / 235 | 89.36 | 51 / 78 | 65.38 |
| `strategy-protocol.mjs` | 497 / 555 | 89.55 | 141 / 193 | 73.06 |
| `strategy-source-store.mjs` | 327 / 350 | 93.43 | 100 / 123 | 81.30 |
| `strategy-worker.mjs` | 197 / 286 | 68.88 | 22 / 46 | 47.83 |
| `strategy-workspace.mjs` | 676 / 830 | 81.45 | 148 / 233 | 63.52 |
| Eight-module feature subtotal | 3527 / 4070 | **86.6585** | 811 / 1128 | **71.8972** |

Feature function coverage is 304 / 359 = 84.6797%. Node/V8 counts source lines
differently from Coverage.py's executable Python statements; these are not
cross-language-comparable denominators. The Node command also runs unchanged
board/daily/minute/paper-trading/request-coordinator tests. Its native all-file
summary is 89.38% lines / 77.75% branches, but that larger unchanged-code
denominator is **not** the feature subtotal above.

`app.js` is inspected largely as source text or small extracted functions by
Node tests; it is absent from the LCOV denominator. The frontend entry and
build scripts were not instrumented by the Node test run either. Absence is
**unmeasured**, not 100% or a claim they never run. Browser runs exercise app
and generated releases but do not collect source-mapped coverage; they cannot
be merged into this percentage. The project's default 80% coverage target is
not configured as an executable threshold here. Several branch values and
the worker's line value are below it, without a numeric gate enforcing them.

## Requirement and Failure-Mode Mapping

Paths in the middle column are relative to `src/tickreplay/static/` unless
prefixed with `tests/`.

| Requirement / failure mode | Existing test evidence | Limits / uncovered behavior |
| --- | --- | --- |
| Explicit Load/Start, stateful Python, function/object/async callbacks | `strategy/strategy-controller.test.mjs:111,138,154`; `tests/test_tickreplay_strategy_runner.py:48,76,102`; `tests/browser/application-strategy.mjs:14` | Browser production path reaches paused Start, not a real strategy order event |
| Step, causal i+1 submit/cancel/fill, replay speed independence | `replay-commit.test.mjs:79,109,166,213,318,349`; `order-broker.test.mjs:149,186,214` | Real app Step/order/portfolio bridge not end-to-end asserted; T5 |
| Stop/invalidation, cleanup, timeout/crash, late responses | `strategy/strategy-controller.test.mjs:196,214,235,253,271,285,309`; replay tests `:243,269`; application test `:49` | Predominantly fake Worker; concurrent Load absent (T1); canonical worker negative responses weak (T5) |
| Concurrent Load/hash or load-vs-stop | Workspace test `strategy-workspace.test.mjs:286` awaits each load/restart; controller failure test `:351` owns one candidate | No overlapping admission/termination-ownership assertion; demonstrated quality Q1 escapes |
| Protocol exact keys/version/identity, invalid commands, byte/depth/UTF-8 limits | `strategy/strategy-protocol.test.mjs:137,157,179,195`; runner tests `:190,201,240,257,282,302` | Missing derived numeric portfolio closure (T2); worker bridge failure shapes (T5) |
| Atomic commit, intent replay, ownership, caps, rollback, final discard | `order-broker.test.mjs:230,252,299,317,358,379,398,448`; `replay-commit.test.mjs:292` | Individual quantity/range checks do not test overflow after fill; T2 |
| Source canonicalization, identity, CAS/autosave, import/export/recovery | `strategy/strategy-source-store.test.mjs:37,42,62,108,146,194,221`; workspace tests `:216,226,261,286,327` | Exact final-LF 8192-line parity and oversized File no-read missing; T4 |
| Visible trusted-code and Load-executes warning | Recovery/storage UI asserted in `tests/test_tickreplay_server.py:642`; app accessibility/lazy checks in browser test `:14` | No assertion of trust/execution warning; existing tests pass security S1; T3 |
| Server headers/static MIME/cache/immutable release resolution/outer mount | `tests/test_tickreplay_server.py:73,632,642,659,706,718,739,750,774`; `tests/test_cloud_run_main.py:130,587,621`; spike-server tests | Good runtime integration coverage; current server branch misses include existing non-feature APIs, not all new defects |
| Canonical-to-release parity/vendor closure/reproducibility | `tests/browser/strategy-release.test.mjs:24,49,80`; asset tests `:25,54,77,93,117`; five build/fetch/config checks below | Positive integrity checks strong; synthetic corrupt/missing/hash/path-negative cases in build tools absent; T6 |
| Browser editor/maximum-payload/interrupt/performance/accessibility | `tests/browser/tickreplay-strategy.mjs:35,65,203,230,285,303` | Mostly spike-specific worker, not production runner; Firefox/WebKit and real assistive devices unrun; performance first-run fail below |

## Findings

### T1 — Medium, blocking: no overlapping Load regression test

Locations: `src/tickreplay/static/strategy/strategy-controller.test.mjs:351`,
`src/tickreplay/static/strategy/strategy-workspace.test.mjs:286`; affected
implementation `strategy-controller.mjs:171,196,230`.

Current tests await each load and test candidate termination with one Worker.
They still pass when concurrent admission or shared-field cleanup is broken.
Quality Q1 independently demonstrated live orphan Worker counts `[1,0,1]`
after overlapping calls, remaining alive after Stop. Add deferred invalidation
and hashing fixtures; issue two loads before either settles, from both ready
and loading; assert supersession policy, winning source identity, complete
promise settlement, and termination of every discarded candidate. Add Stop
during that overlap. These must fail on the reviewed implementation.

### T2 — Medium, blocking: accepted-order tests omit derived portfolio overflow

Locations: `src/tickreplay/static/order-broker.test.mjs:317,379`,
`src/tickreplay/static/replay-commit.test.mjs:149`; affected implementation
`order-broker.mjs:164,638,652` and `replay-commit.mjs:26`.

Atomicity tests inject preparation failures and invalid individual commands,
but do not ensure a valid strategy command leaves a representable next ABI
portfolio after matching. Quality Q2 demonstrated a maximum-safe quantity buy
at price 100 committing `cash_minor=-90071992547409900000`, followed by
`invalid_integer` on the next event. Add maximum and near-maximum quantity,
aggregate position, high price and minor-unit overflow cases. Assert the typed
rejection occurs before authoritative state swap, including order/fill/event
IDs and ledgers, and that the next event/Start remains valid.

### T3 — Medium, blocking: planned trust warning has no UI acceptance assertion

Locations: `tests/test_tickreplay_server.py:642`,
`tests/browser/application-strategy.mjs:39`; product `index.html:153,186` and
`strategy-runner.py:400` under `src/tickreplay/static/`.

Tests assert storage/recovery/accessibility and successful explicit Load, but
not that users are visibly told to run trusted code or that Load itself executes
top-level Python. Security S1 is a demonstrated missing product warning, not
just a coverage concern. Add a rendered, persistent, accessible trust/Load
execution warning assertion for enhanced and degraded modes, adjacent to the
controls. Pair with a harmless load-only runner assertion proving top-level
execution before Start. Do not substitute a comment/runbook or an incidental
localStorage warning for the actual warning contract.

### T4 — Low, nonblocking: source ingestion boundary tests miss early rejection and line parity

Locations: `src/tickreplay/static/strategy/strategy-workspace.test.mjs:327`,
`strategy-source-store.test.mjs:42,221`; implementation
`strategy-workspace.mjs:784`, `strategy-source-store.mjs:65,345`.

The fake import File has no size and always returns a small buffer. It cannot
detect reading/decoding an oversized file before enforcing the bound (security
S2). The line-limit test checks an over-limit source, not the exact 8192-line
final-LF mismatch demonstrated by quality Q3. Add oversized/wrong-extension
Files whose `arrayBuffer` must never be called, a decoder raw-byte bound, and
an unchanged prior source/hash/revision assertion on failure. Compare store,
protocol and runner at 8192/8193 physical lines with/without final LF and with
permitted BOM/CRLF canonicalization overhead.

### T5 — Medium, nonblocking: production event bridge is not exercised end to end

Locations: `tests/browser/application-strategy.mjs:46`,
`tests/browser/tickreplay-strategy.mjs:230`,
`src/tickreplay/static/strategy/strategy-controller.test.mjs:375`; unexecuted
failure branches in `strategy-worker.mjs:204,214,258` and controller log paths
at `strategy-controller.mjs:539`.

Production browser coverage stops after Start/pagehide; the maximum-event,
Python-error and busy-loop browser test uses `tests/browser/spike-worker.mjs`,
not the canonical runtime. The Node Worker seam returns prebuilt successful
runner JSON. It can stay green despite changes that break the actual production
Python event/response bridge. The worker measured 68.88% lines / 47.83% branches;
its diagnostic/failure/serialization-fallback paths are uncovered by this Node
run. This is a missing integration proof, not a reproduced new code defect.

Add a production-release browser strategy that submits on event i, Steps twice,
asserts the fill only at i+1 with exact portfolio/cursor, cancels, Stops, and
restarts. Add deterministic canonical-worker tests for runner ok=false,
loader/fetch/JSON conversion failure, messageerror, postMessage failure and
temporary-proxy destruction on errors. Controller tests should reject malformed
and stale log/progress/response identities while preserving bounded logs and
cleanup. Keep wall-clock benchmarks separate from these deterministic checks.

### T6 — Low, nonblocking: build/fetch safety failures lack unit coverage

Locations: `scripts/fetch_pyodide_assets.py:100,104,109`,
`scripts/build_strategy_images.py:46,55,72`.

Selected pytest execution measured zero statements/branches for both scripts.
Real reconstruction checks passed, but do not prove incorrect downloaded bytes,
invalid revision/path, zero/multiple matching releases or unexpected asset sets
fail safely. Add small temporary-tree/unit tests with a mocked URL response and
subprocess seam, verifying size/hash mismatch, allowlist mismatch, invalid Git
OID/path, and ambiguous/missing canonical release. Assert no destination mutation
on rejected inputs. Do not require network acquisition for these negative tests.

## Test Quality Assessment

Tests generally use clear behavior names and Arrange/Act/Assert structure with
isolated fake storage, Workers, clocks and per-test broker/runner instances.
Fault injection asserts complete state preservation; manual goldens and
speed-independent traces provide meaningful regression evidence. No weakened,
deleted or skipped assertions were introduced during review.

Important limitations: app source regex checks can survive a semantic wiring
bug; extracted functions do not give whole-app coverage; fake Pyodide success
payloads do not validate the production Python bridge. Native Python runner
tests validate semantics on Python 3.13, whereas the browser Pyodide runtime
contains Python 3.12.7. Existing real Load/Start coverage partly bridges this,
but T5 remains. The spike maximum-envelope fixture uses its own broad payload
shape and string order IDs, so its size benchmark is not a strict production
ABI acceptance test. The mobile cooperative-interrupt test returns early at
`tests/browser/tickreplay-strategy.mjs:286` instead of marking itself skipped;
its passing status does not mean mobile soft-interrupt behavior was exercised.

## Execution Results and Exact Commands

### Python tests and branch coverage

```sh
uv run --with pytest-cov pytest -q tests/test_tickreplay_strategy_runner.py tests/test_tickreplay_server.py tests/test_cloud_run_main.py tests/browser/test_spike_server.py --cov --cov-config=.agents/logs/coverage-python-code-feature.rc --cov-branch --cov-report=term-missing --cov-report=json:.agents/logs/coverage-python-code-feature.json > .agents/logs/coverage-python-code-feature.log 2>&1
```

149 passed, one existing Starlette/httpx deprecation warning, test summary in
65.24 seconds, final exit 0 observed. Process finalization continued after the
summary; a read-only `sample` showed atexit directory cleanup (`unlink/rmdir`),
not proof of a Worker deadlock. It later exited normally; no process was killed.
No full repository suite was rerun.

### Node native coverage

```sh
node --experimental-test-coverage --test-coverage-include='src/tickreplay/static/**' --test-coverage-include='scripts/*.mjs' --test-coverage-exclude='**/*.test.mjs' --test-coverage-exclude='**/releases/**' --test-coverage-exclude='**/vendor/**' --test-reporter=spec --test-reporter-destination=.agents/logs/coverage-js-python-code-feature.log --test-reporter=lcov --test-reporter-destination=.agents/logs/coverage-js-python-code-feature.lcov --test src/tickreplay/static/*.test.mjs src/tickreplay/static/strategy/*.test.mjs tests/browser/*.test.mjs
```

230 passed, 0 failed/skipped, exit 0. Feature subtotals were calculated by
summing LF/LH and BRF/BRH in the eight named module LCOV records, not averaging
percentages or including untouched helpers.

### Real browser tests

```sh
STRATEGY_SPIKE_USE_SYSTEM_CHROME=1 node_modules/.bin/playwright test --config tests/browser/playwright.config.mjs --project=chromium --project=mobile-chromium --output=.agents/logs/playwright-test-review-artifacts > .agents/logs/browser-python-code-feature.log 2>&1
```

Initial result: **18 passed, 1 skipped, 1 failed**, exit 1, 1.9 minutes. Failure
at `tests/browser/tickreplay-strategy.mjs:255`: mobile callback P95 **31.30 ms**,
required <=30 ms. The trace/error-context are under the named output directory.
The skipped case is desktop-only exclusion of the mobile layout assertion.

```sh
STRATEGY_SPIKE_USE_SYSTEM_CHROME=1 node_modules/.bin/playwright test --config tests/browser/playwright.config.mjs --project=mobile-chromium --grep 'Worker executes maximum-shape plain data' --output=.agents/logs/playwright-test-review-retry-artifacts > .agents/logs/browser-python-code-feature-retry.log 2>&1
```

Focused retry: **1 passed**, exit 0, 12.9 seconds; P95 **9.20 ms**, conversion
P95 4 ms, 128.60 events/second. No threshold/config/test changes. Retry success
does not erase the initial failure or establish its cause. The initial browser
run overlapped other read-only checks/cleanup; host load sensitivity is plausible
but unproven. Re-run the frozen acceptance workload in its pinned CI environment
before claiming that performance gate passes consistently.

Used existing system Chrome 152.0.7977.83, not the locked CI Chromium 153 image.
Firefox/WebKit/mobile-WebKit were **unrun** because the local Playwright browser
cache is absent; no downloads were performed. Safari/VoiceOver/TalkBack and real
devices were **unrun**. System-Chrome mobile emulation is not real-device proof.
No browser source-mapped coverage percentage was collected.

The configured JSON reporter resolves its relative output beneath
`tests/browser/.agents/logs/`; its retry JSON was moved to
`.agents/logs/browser-python-code-feature-retry.json`. Initial line log and
trace were preserved; the initial JSON was replaced by the reporter on retry
and is not claimed as retained evidence. No pre-existing artifact directories
were removed.

### Feature integrity checks

```sh
node scripts/build_strategy_editor.mjs --check
node scripts/build_strategy_release.mjs --check
node scripts/build_strategy_release.mjs --check-config tests/browser/strategy-assets.mjs
uv run python scripts/build_strategy_images.py --check
uv run python scripts/fetch_pyodide_assets.py --check
```

All five checks returned ok=true. Pyodide was freshly fetched to a temporary
directory and matched pinned bytes/hashes; no vendor output was rewritten.
Editor: 447508 raw / 149530 gzip bytes, same bundle identity. Current release:
`1a45c99b4a6b884fbfc17240ae65759f20601dc86267df3d2b9a160de63a45da`,
11 files / 9 edges / 2 reconstruction roots. All four retained release
identities and current canonical parity also passed the Node tests. Image
manifest status is local-input-manifest-only; this is not an image publish,
deployed rollback test, or Cloud Run acceptance claim.

## Residual Risks and Validation

The three blocking fixes need new failing-then-passing regression assertions;
the current green unit suites cannot substitute for them. Remaining branch
gaps, production event integration, non-Chrome engines, pinned performance,
and real-device accessibility are not claimed complete. Coverage was newly
measured rather than estimated or inferred from test counts.

Dependency/lockfile diff checks were empty. Existing preflight working-tree
changes were preserved. The reviewer work log passed the shared work-log
contract (exit 0; no missing sections/metadata or warnings). Report headings and exact file locations are
manually reviewed because no dedicated test-review-report contract exists.

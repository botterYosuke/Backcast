# Security Review: Browser Python Replay Strategies

## Verdict

NEEDS_REVISION. Critical 0, High 0, Medium 1, Low 1. S1 is blocking because
the implemented user interface omits the trust warning required by the feature's
accepted same-origin execution model. S2 is nonblocking input-size hardening.

Scope: commit `26cc86ec` against `main`, using
`.agents/logs/review-diff-python-code-feature.patch`. Uncommitted preflight fixes
are excluded from findings. Reviewed security-relevant authored runner, worker,
protocol, controller, workspace/source persistence, order broker/replay commit,
app integration, static serving and build/release boundaries. Generated copies
were verified against their identities and canonical sources. Vendored runtime
internals were not exhaustively audited.

## Findings

### S1 — Medium, blocking: Load executes same-origin Python without a visible trust warning

- Location: `src/tickreplay/static/index.html:153` and `:186`;
  `src/tickreplay/static/strategy/strategy-runner.py:400`.
- Reproduction: Open the Python strategy panel, import or paste a program with
  a top-level action, then choose **Load** without choosing **Start**. The panel
  help only explains raw source recovery and plaintext localStorage. Neither
  HTML nor the canonical workspace/editor provides a warning that only trusted
  code should be loaded, that this is not a security sandbox, or that Load
  already executes code. The worker load path reaches `load_strategy`, which
  executes the module before callback discovery or any Start invocation.
- Independent narrow evidence: invoking the same runner's `handle_json` with
  operation `load` and source `print("top-level code executed during Load")`
  followed by a valid `on_event` returned `ok=true` and that exact stdout. No
  Start operation was sent. Code execution can also occur before a later ABI
  validation failure; successful loading is not the security boundary.
- Impact: A user can reasonably interpret Load as preparation/validation and
  Start as the execution boundary. Loading code copied from elsewhere can execute
  it immediately with the browser Worker capabilities available to same-origin
  Pyodide. The accepted design explicitly acknowledges browser Web API and
  same-origin API access; `jsglobals` filtering and report-only CSP do not make
  this an untrusted-code sandbox. This is a missing product safety boundary,
  not a claim of server-native remote code execution or a demonstrated
  authentication exploit.
- Requirement evidence: `.agents/docs/plans/pyodide-replay-strategy.md:523`
  explicitly requires a product warning. `docs/tick-replay.md:518` documents the
  trust model, but documentation outside the execution surface does not supply
  that warning to a user about to Load an imported program.
- Suggested fix: Add persistent visible text beside the source/Load controls
  stating: only load code you trust; Load executes top-level Python immediately;
  the Worker is not a security sandbox and code may use browser networking and
  same-origin APIs. Keep the existing plaintext-storage warning. Make the text
  accessible from the source field/controls and retain it in degraded editor
  mode. A clear warning is sufficient for the already accepted trusted-user
  scope; this finding does not require a new origin-isolation architecture or
  repeated confirmation dialogs. Add a focused UI assertion covering the
  warning before Load is available.

### S2 — Low, nonblocking: Import materializes oversized files before checking the source limit

- Location: `src/tickreplay/static/strategy/strategy-workspace.mjs:784`;
  `src/tickreplay/static/strategy/strategy-source-store.mjs:345`.
- Reproduction: Select a very large local `.py` file with Import. The handler
  unconditionally awaits the entire `file.arrayBuffer()` before calling
  `decodePythonFile`; that decoder materializes the UTF-8 string before
  canonicalization and the 262,144-byte source check. Even the filename/type
  check is reached only after the read because JavaScript evaluates arguments
  first. No `file.size` gate exists in this path.
- Impact: The eventual source-limit rejection does not bound peak memory or
  decoding work in the browser UI process. An accidentally selected large file
  can stall or exhaust the tab before rejection. This requires local file
  selection and is not an unauthenticated remote attack; an actual OOM was not
  induced during review.
- Suggested fix: Validate name and a conservative raw-byte upper bound before
  `arrayBuffer()`, with allowance for canonicalized BOM/CRLF input. Bound the
  decoder input too if it remains independently callable. For a stricter
  canonical-size policy, use a bounded streaming decode. Add a fake oversized
  File test asserting its `arrayBuffer` is never invoked and the existing draft
  remains intact. Update the canonical module and generate a new immutable
  release rather than editing retained release bytes.

## Security Boundaries Verified

- **Execution placement and authority:** Python runs inside a module Worker;
  server endpoints do not accept Python source for native execution. Load and
  lifecycle calls are explicit; source restore/import/sample insertion does
  not automatically execute. The project intentionally supports trusted
  current-user code, not shared/adversarial strategy hosting. No automatic
  package-install or `loadPackagesFromImports` path was added. Standard-library
  shipping policy is not misrepresented here as a technical import/network
  sandbox.
- **Worker identity and lifecycle:** The controller checks worker object and
  generation, pending request identity, session, source hash/revision/load
  attempt, run and sequence before exposing commands. Single flight, timeout,
  invalidation and hard termination are present; cross-origin-isolated setups
  may use an interrupt buffer before bounded termination. Old-generation
  messages cannot commit current orders. Worker/runner exceptions become
  structured failure responses; malformed results invalidate the strategy.
- **Data and message validation:** Exact schemas reject unknown command fields,
  invalid sides, unsafe/negative integer quantities and price levels, duplicate
  intents, cycles, nonfinite values and oversized logical envelopes. Responses
  are checked again on the main side. Source, diagnostic/log and per-event/run
  command caps exist. Checks occur after structured clone/Python allocation;
  they do not enforce a hostile-worker memory quota.
- **Order authority:** The host broker owns simulated orders and portfolio
  state. It validates full batch/run identity, sequence, intent uniqueness,
  eligibility and capacity before copy-on-write state swap. A tick-i command
  becomes eligible at i+1; final-tick commands are discarded. Cancellation is
  scoped to the exact strategy run and preserves manual orders. Pausing,
  invalidation, error cleanup and stale-response handling retain the replay
  commit barrier. No live brokerage/payment capability was introduced.
- **Rendering and source persistence:** Diagnostic/log/status strings are
  rendered with `textContent`, not HTML interpretation. Source goes to the
  textarea/CodeMirror text model. Drafts remain plaintext localStorage under
  dedicated keys with explicit user export; the UI states plaintext storage.
  No credentials were added or intentionally emitted to logs. The documented
  same-origin trust model still requires users not to store secrets in source.
- **Serving and CSP:** Production code adds a route report-only CSP, not an
  enforced sandbox; COOP/COEP remains off on the ordinary route. Existing
  enforcement is not replaced with a weaker policy. Static files continue
  through Starlette's containment-aware serving layer, with explicit MIME,
  immutable content-addressed classes and no-store mutable modules. Committed
  gzip sidecars are direct provenance artifacts, not negotiated executable
  representations.
- **Supply and release consistency:** Pyodide 0.27.6 / Python 3.12.7 selected
  files pin HTTPS source URLs, sizes and SHA-256 digests; only the declared
  runtime/license closure is shipped. CodeMirror dependencies, lock integrity,
  build tools/recipe and CI images/actions are pinned. CI has read-only contents
  permissions and does not retain checkout credentials. Runtime imports capture
  one immutable release closure; current canonical files reconstruct that same
  identity. Retained releases verify their own content identities. Hash checks
  are build/provenance checks, not browser SRI guarantees against a compromised
  same-origin server.

## Validation

- `node scripts/build_strategy_release.mjs --check`: PASS; current release
  `1a45c99b4a6b884fbfc17240ae65759f20601dc86267df3d2b9a160de63a45da`,
  11 files, 9 edges, two independent reconstruction roots.
- Focused Node suites for vendor/release assets, protocol, controller, broker
  and replay commit: **67 passed, 0 failed**.
- `uv run pytest -q tests/test_tickreplay_strategy_runner.py`: **21 passed**,
  command exit 0.
- Targeted server header/MIME/cache/sidecar/recovery selection: pytest reported
  **27 passed, 27 deselected**, one dependency deprecation warning. At report
  time the tool session had not returned a final exit status despite no matching
  pytest process remaining in the process listing; this is test-output evidence,
  not a claimed observed zero exit for that invocation.
- Independent read-only vendor audit checked every manifest-listed byte count
  and SHA-256, plus exact directory allowlists: **CodeMirror 5 files; Pyodide
  8 files; no unexpected files; all match**.
- Native runner smoke demonstration confirmed top-level execution during Load
  before Start. UI/code inspection confirms no visible trust/Load warning.
- No full browser matrix, external vulnerability database scan, complete vendor
  implementation audit, production canary, or live Cloud Run rollback was run.
  Their absence is not represented as successful validation.
- Only this report and assigned work log were authored; no feature source,
  configuration, state, release, dependency or deployment was modified.

## Residual Risks

The same-origin Worker is not an adversarial-code boundary. Deliberately
noncompliant trusted code can attempt browser API access, monkeypatch runtime
state or allocate excessive memory; callback deadlines cannot guarantee recovery
from process-wide OOM or a wedged browser. Aggregate numerical extremes and
long-session broker performance merit quality review, but no unverified numeric
or timing hypothesis is counted as a security finding here. Deployment remains
subject to the documented pinned browser/header/retention and compatibility
gates; local artifact verification does not authorize promotion.

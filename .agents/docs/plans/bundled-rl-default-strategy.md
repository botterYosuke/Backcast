# Bundled RL Default Strategy — Breaking Final-Form Plan

## Status

- Mode: existing codebase
- Complexity: COMPLEX
- Route: `team-execute`
- User approval: granted 2026-09-28, explicitly waiving backward compatibility
- Codebase scan: `.agents/docs/research/feature-bundled-rl-default-strategy-codebase.md`
- Model inventory: `.agents/docs/research/bundled-rl-model-inventory.md`
- Breaking architecture consult: `.agents/logs/codex/20260928T041217Z-bundled-rl-breaking-architecture.md`
- The earlier compatibility-preserving validation is superseded and is not implementation evidence for this plan.

## Product Contract

The strategy UI has one product choice, not independent strategy and runtime axes:

1. **Bundled RL** — default, fixed native execution, read-only model information, explicit Load then explicit Start.
2. **Custom Python** — explicitly selected browser Pyodide editor/runtime.

Missing, obsolete, or malformed selection records reset to Bundled RL. No backward-compatible selection migration is required. The Custom Python source store remains because Custom Python remains a product feature, not to preserve an old Native contract.

Opening the page may probe capability but never loads a strategy, starts replay, submits a broker command, or executes user code. Load prepares the selected runtime; Start begins replay-driven decisions.

## Ideal Architecture

```text
Bundled RL (default)
  -> explicit Load
  -> exact preset identity request
  -> fixed Python child + pinned NumPy + bundled model
  -> immutable position-branch trace
  -> explicit Start
  -> broker commit for tick i
  -> choose branch using actual committed position
  -> stage at most one market order eligible only at tick i+1

Custom Python (explicit selection)
  -> existing browser Pyodide runtime
  -> existing replay/broker boundary
```

- Native has exactly one trace contract: `bundled_rl_trace_v1`.
- FastAPI never imports NumPy, parses model arrays, runs inference, or compiles/executes submitted source.
- The child argv is repository-owned and fixed. There is no custom Native executor, argv, trust flag, or client runtime fingerprint.
- The Native API accepts no source field. It accepts only the exact preset identity returned by capability plus authoritative session identity.
- Trace rows contain two ordered branches for committed positions 0 and 100. Each branch contains only target quantity 0 or 100. Traces contain no fill, cash, portfolio, or assumed execution state.
- Production `replay-commit.mjs` and `order-broker.mjs` remain the only fill/portfolio authority and are no-writer paths.
- Existing content-addressed release directories remain byte-immutable as an integrity property. A new release is published exclusively.

## Breaking Changes

Delete rather than preserve:

- custom `native_trace_v1` and planned `native_trace_v2` dual dispatch;
- `native_strategy.py` and `native_strategy_worker.py`;
- custom Native executor argv, trusted-local flag, and client-supplied runtime fingerprint;
- server-side arbitrary Python source submission/execution;
- canonical preset source/hash and `bundled-rl-preset.mjs`;
- custom-Native fixtures, E2E tests, and compatibility assertions;
- compatibility requirements for old selection/settings records.

Retain:

- browser Pyodide for explicitly selected Custom Python;
- the Custom Python source store and editor;
- authoritative session/hash generation;
- bounded job/lease/cancel/timeout behavior and immutable trace storage;
- broker atomicity and exact next-tick causality;
- dirty-worktree recovery snapshot and retained-release integrity checks.

## Native API and Validation Order

Routes:

```text
GET    /api/strategies/bundled-rl/capability
POST   /api/strategies/bundled-rl/jobs
GET    /api/strategies/bundled-rl/jobs/{job_id}
DELETE /api/strategies/bundled-rl/jobs/{job_id}
GET    /api/strategies/bundled-rl/traces/{trace_id}
```

Capability returns `available`, `contractVersion: bundled_rl_trace_v1`, and exact preset `{id, revision, digest}`. Submit accepts an exact-key object containing only that contract/preset identity and session `{stem, code, date, dataHash}`. `source`, `sourceHash`, executor fields, mode fields, and all unknown keys are invalid.

Validation order is mandatory:

1. Enforce body bound, UTF-8 JSON, duplicate-key rejection, and exact keys.
2. Match contract and preset identity exactly.
3. Reject every source/executor/unknown field.
4. Complete steps 1–3 before repository/cache lookup, job/lease allocation, child spawn, or artifact/temp write.
5. Re-read the authoritative session and verify its identity/hash.
6. Derive content identity from preset, model/provenance, runtime manifest, canonical session, and run contract.

Availability is based on fixed-worker self-check: supported platform/runtime, exact NumPy distribution version 2.4.5, bundled asset digests, and child handshake. `BACKCAST_NATIVE_STRATEGY_ENABLED=false` may explicitly disable the feature; no other custom Native configuration remains.

## Model and Causal Semantics

- Copy `_moomoo/models/rl_scalp_evolution_v1.json` unchanged: exactly 11,932 bytes and SHA-256 `528b9dc44928984c471731bb64ef82fa4a299a2ffd5707d1ce2a482223d28ef1`.
- Pin `numpy==2.4.5`; add no other upstream ML dependency.
- Network: `(7,16) -> tanh -> (16,16) -> tanh -> (16,2)`, float32.
- Inputs: tick sign, the three upstream **current-versus-lag cumulative** decision-mark returns, exchange-wall-clock sine/cosine, and position 0/1. If decision marks are `m0` (current), `m1`, `m2`, `m3`, the raw return features are `10000 * (m0/m1 - 1)`, `10000 * (m0/m2 - 1)`, and `10000 * (m0/m3 - 1)`, each divided by 30, clipped to `[-2,2]`, and staged as float32. A missing lag at a half-session start is zero. This deliberately follows `_moomoo.causal_features` rather than the superseded one-step interpretation found during plan review.
- Target 100 only when `Q[1] > Q[0] + 0.1`; otherwise target 0.
- Slots: every ten seconds during 09:01:00–11:24:30 and 12:31:00–14:49:30; reset history at lunch.
- Repository epochs already represent exchange wall-clock calendar fields; do not add nine hours. Clock sine/cosine use slot wall-clock seconds.
- For slot `s`, the current mark is the last canonical print with timestamp `<= s`; tick sign is `sign(mark(s) - mark(s-10s))`, not adjacent-tick direction. Return history uses decision marks from prior logical slots in the same half-session.
- Canonical session timestamps are nondecreasing and equal timestamps retain authoritative input order. The observation barrier for slot `s` is the **last tick in the first timestamp block whose timestamp is `>= s`**. This ensures every same-time print is known while all feature marks remain restricted to timestamps `<= s`.
- A proposal row records that barrier's index as `observedSessionTickIndex` and is eligible only at `observedSessionTickIndex + 1`. If no barrier exists, or the barrier is the final session tick, emit no row.
- When a gap maps multiple logical slots to the same observation barrier, evaluate each slot causally but retain only the latest slot's two-branch result for that observed index. Earlier slots never use the barrier tick's price when its timestamp is later than the slot. Future marks and final-tick rows are forbidden.

Start and every dispatch require a safe integer position exactly 0 or 100 and a complete, untruncated, empty open-order snapshot. Map 0→100 to one buy-100 market command, 100→0 to one sell-100 command, and equal targets to no command. Broker rejection is terminal for that run with complete atomic rollback and no implicit retry.

## Implementation Workstreams

### Coordinator bootstrap

The first application-code write is the complete final `scripts/verify_bundled_rl_snapshot.py`. Root writes it once, computes its SHA-256, and immediately captures all dirty/untracked files plus relevant planning logs into:

- `.agents/logs/bundled-rl-default-strategy-baseline-files.tar`
- `.agents/logs/bundled-rl-default-strategy-worktree-baseline.json`
- `.agents/logs/bundled-rl-default-strategy-retained-releases.json`

The snapshot records path, type, size, SHA-256 or symlink target, directory child sets, and each pre-existing Git dirty hunk. It protects `.codex/config.toml`, `.env`, credentials, market data, unrelated changes, and every retained release. No reset, checkout, clean, stash, or broad revert is permitted.

Root writes `.agents/logs/ownership-team-execute-bundled-rl-default-strategy.json` with separate `allowed_modify`, `allowed_create`, and `allowed_delete` arrays. `allowed_delete` contains only `src/tickreplay/native_strategy.py`, `src/tickreplay/native_strategy_worker.py`, `tests/test_tickreplay_native_strategy.py`, and `tests/test_tickreplay_native_strategy_worker.py`. Every other missing path, type change, unowned creation, or unowned modification fails closed.

Every dirty exact-owned path is also classified before delegation:

- `preserve_hunks`: existing unrelated/Pyodide/application hunks must remain present by baseline hunk fingerprint while new hunks may be added. This is the default for tracked UI/docs/build files and any mixed-purpose backend file.
- `replace_obsolete_native_hunks`: only the enumerated old Native route/config/trace hunks may disappear; the manifest stores their exact baseline patch fingerprints and Root reviews the generated baseline-to-current patch.
- `delete_obsolete_native_file`: only the four exact paths in `allowed_delete`.

The verifier rejects disappearance of an unclassified baseline hunk even inside an `allowed_modify` path and emits one per-owner baseline-to-current patch for Root review.

### A — Model and inference

Exact ownership:

```text
pyproject.toml
uv.lock
src/tickreplay/models/rl_scalp_evolution_v1.json
src/tickreplay/models/rl_scalp_evolution_v1.provenance.json
src/tickreplay/bundled_rl_inference.py
tests/fixtures/bundled-rl/model-oracle.json
tests/test_tickreplay_bundled_rl_inference.py
.agents/logs/agent-teams/team-execute-bundled-rl-default-strategy/model-inference-implementer.md
```

Implement strict asset/genome/shape/finite validation, float32 encoding/MLP, upstream golden Q values, both position targets, causal scheduler, lunch reset, gaps, same-time ordering, boundaries, timezone independence, and final-tick rejection. Prove operation from an unrelated cwd without `_moomoo` on `sys.path`.

### B — Fixed Native backend

Exact ownership:

```text
.env.example
src/tickreplay/config.py
src/tickreplay/repository.py
src/tickreplay/server.py
src/tickreplay/strategy_trace.py
src/tickreplay/native_strategy.py                         (delete)
src/tickreplay/native_strategy_worker.py                  (delete)
src/tickreplay/bundled_rl_runtime.py
src/tickreplay/bundled_rl_service.py
src/tickreplay/bundled_rl_worker.py
tests/fixtures/bundled-rl/native-contract.json
tests/test_tickreplay_config.py
tests/test_tickreplay_repository.py
tests/test_tickreplay_strategy_trace.py
tests/test_tickreplay_server.py
tests/test_tickreplay_native_strategy.py                  (delete)
tests/test_tickreplay_native_strategy_worker.py           (delete)
tests/test_tickreplay_bundled_rl_runtime.py
tests/test_tickreplay_bundled_rl_service.py
tests/test_tickreplay_bundled_rl_worker.py
tests/test_tickreplay_bundled_rl_process.py
.agents/logs/agent-teams/team-execute-bundled-rl-default-strategy/backend-implementer.md
```

Replace source-dependent Native code with fixed runtime identity, worker handshake, exact preset API, authoritative session verification, bounded jobs, atomic immutable publication, cancellation/timeout cleanup, and bounded diagnostics. Parent code treats model bytes as opaque and never imports inference/NumPy.

### C — UI, controller, release, and docs

Exact ownership:

```text
src/tickreplay/static/strategy/strategy-selection-store.mjs
src/tickreplay/static/strategy/strategy-selection-store.test.mjs
src/tickreplay/static/strategy/strategy-trace-protocol.mjs
src/tickreplay/static/strategy/strategy-trace-protocol.test.mjs
src/tickreplay/static/strategy/strategy-trace-controller.mjs
src/tickreplay/static/strategy/strategy-trace-controller.test.mjs
src/tickreplay/static/strategy/strategy-workspace.mjs
src/tickreplay/static/strategy/strategy-workspace.test.mjs
src/tickreplay/static/replay-commit.test.mjs
src/tickreplay/static/order-broker.test.mjs
src/tickreplay/static/index.html
src/tickreplay/static/styles.css
src/tickreplay/static/app.js
scripts/build_strategy_release.mjs
tests/browser/application-strategy.mjs
tests/browser/strategy-release.test.mjs
tests/browser/bundled-rl-real.mjs
tests/browser/bundled-rl.playwright.config.mjs
docs/bundled-rl-strategy.md
docs/tick-replay.md
docs/pyodide-strategy-release-runbook.md
.agents/logs/agent-teams/team-execute-bundled-rl-default-strategy/frontend-release-implementer.md
```

Implement the two-choice UI, default/reset semantics, complete removal of editor/source payload from Bundled RL, exact trace validation, actual-position branch selection, terminal failure behavior, deterministic intents, and real browser proof. Custom Python alone uses the existing editor/source store/Pyodide.

After two identical temporary reconstructions compute `<candidate-id>`, Root adds exact ownership **for stream C** for the candidate directory, the release-root direct-child addition of that ID only, and these 14 exact leaves: `identity.json`, `manifest.json`, `strategy-assets.mjs`, `strategy-controller.mjs`, `strategy-editor.mjs`, `strategy-protocol.mjs`, `strategy-release.mjs`, `strategy-runner.py`, `strategy-source-store.mjs`, `strategy-selection-store.mjs`, `strategy-trace-controller.mjs`, `strategy-trace-protocol.mjs`, `strategy-worker.mjs`, and `strategy-workspace.mjs`. Root only verifies the ID/destination and runs publication gates; C performs the exclusive publication. The destination must not exist.

Dependency order: A inference and C selection shell may begin in parallel; B consumes A's API; C protocol/controller consumes B's frozen contract fixture; A+B+C converge on real E2E, deterministic reconstruction, then exclusive release publication.

## No-Writer Paths

```text
.codex/config.toml
.env
run.py
src/tickreplay/static/replay-commit.mjs
src/tickreplay/static/order-broker.mjs
every release directory present in the retained baseline
all `_moomoo` files except the one read-only source checkpoint
```

## Acceptance Gates

1. No custom Native worker, custom executor env, submitted-source Native field, or v1/v2 dual dispatch remains.
2. Valid exact preset succeeds; missing/wrong preset, `source`, `sourceHash`, and unknown keys fail before repository/cache/job/lease/spawn/write.
3. Model byte count/hash, NumPy 2.4.5, shapes, finite values, and golden Q/targets match the upstream oracle; runtime never reads/imports `_moomoo`.
4. Fresh, obsolete, and malformed selection all display Bundled RL. Custom Python editor/Pyodide appears only after explicit selection. Mount performs no Load, Start, source execution, or broker dispatch.
5. Trace contains proposals only. Actual committed position selects the branch; holdings remain 0/100; open-order and numeric safety checks run at Start and dispatch.
6. The real browser fixture contains decision rows whose flat and long targets intentionally differ. It proves the branch selected after broker commit matches the actual post-commit position, for both entry and exit. Each observed tick `i` fills exactly at `i+1`; explicit negative assertions reject `i` and `i+2`; final-tick proposals are impossible.
7. Broker failure fully rolls back and terminally stops the run without implicit retry.
8. From an unrelated cwd, the production server launches the fixed child, completes capability → job → poll → trace, rejects source-bearing requests with zero side effects, and performs zero Native HTTP during replay after trace retrieval.
9. New release reconstructs identically twice, publishes exclusively, and all retained release paths/types/sizes/hashes/child sets remain unchanged.
10. `.codex/config.toml`, `.env`, unrelated dirty work, credentials, market data, and unrelated `_moomoo` assets remain byte-identical.
11. Public redistribution rights remain out of scope; provenance states the local-copy basis and unconfirmed redistribution status.

## Executable Gates

```text
shasum -a 256 scripts/verify_bundled_rl_snapshot.py
COPYFILE_DISABLE=1 uv run python scripts/verify_bundled_rl_snapshot.py capture --repo /Users/sasac/backcast --archive .agents/logs/bundled-rl-default-strategy-baseline-files.tar --snapshot .agents/logs/bundled-rl-default-strategy-worktree-baseline.json --retained-root src/tickreplay/static/strategy/releases --retained-snapshot .agents/logs/bundled-rl-default-strategy-retained-releases.json --ownership .agents/logs/ownership-team-execute-bundled-rl-default-strategy.json --self-sha256 <preceding-command-value>
uv run python scripts/verify_bundled_rl_snapshot.py verify --phase post-stream-a --snapshot .agents/logs/bundled-rl-default-strategy-worktree-baseline.json --archive .agents/logs/bundled-rl-default-strategy-baseline-files.tar --ownership .agents/logs/ownership-team-execute-bundled-rl-default-strategy.json --owner model-inference-implementer
uv run python scripts/verify_bundled_rl_snapshot.py verify --phase post-stream-b --snapshot .agents/logs/bundled-rl-default-strategy-worktree-baseline.json --archive .agents/logs/bundled-rl-default-strategy-baseline-files.tar --ownership .agents/logs/ownership-team-execute-bundled-rl-default-strategy.json --owner backend-implementer
uv run python scripts/verify_bundled_rl_snapshot.py verify --phase post-stream-c --snapshot .agents/logs/bundled-rl-default-strategy-worktree-baseline.json --archive .agents/logs/bundled-rl-default-strategy-baseline-files.tar --ownership .agents/logs/ownership-team-execute-bundled-rl-default-strategy.json --owner frontend-release-implementer
uv run python scripts/verify_bundled_rl_snapshot.py verify-retained --phase pre-publication --snapshot .agents/logs/bundled-rl-default-strategy-retained-releases.json --releases src/tickreplay/static/strategy/releases
uv run python scripts/verify_bundled_rl_snapshot.py verify-retained --phase post-publication --snapshot .agents/logs/bundled-rl-default-strategy-retained-releases.json --releases src/tickreplay/static/strategy/releases --expected-new-id <candidate-id>
uv run pytest -q tests/test_tickreplay_bundled_rl_inference.py tests/test_tickreplay_bundled_rl_runtime.py tests/test_tickreplay_bundled_rl_service.py tests/test_tickreplay_bundled_rl_worker.py tests/test_tickreplay_bundled_rl_process.py tests/test_tickreplay_strategy_trace.py tests/test_tickreplay_config.py tests/test_tickreplay_repository.py tests/test_tickreplay_server.py
node --test src/tickreplay/static/strategy/strategy-selection-store.test.mjs src/tickreplay/static/strategy/strategy-trace-protocol.test.mjs src/tickreplay/static/strategy/strategy-trace-controller.test.mjs src/tickreplay/static/strategy/strategy-workspace.test.mjs src/tickreplay/static/replay-commit.test.mjs src/tickreplay/static/order-broker.test.mjs
STRATEGY_SPIKE_USE_SYSTEM_CHROME=1 npx playwright test --config tests/browser/bundled-rl.playwright.config.mjs --project=chromium --grep 'Bundled RL real backend: Load is inert and Start fills entry and exit exactly on next ticks'
npm run check:strategy-config
npm run check:strategy-release
npm run test:strategy-assets
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run ty check src/
uv run pytest
bash .agents/skills/_shared/verify.sh
git diff --check
uv run python scripts/verify_bundled_rl_snapshot.py active-residue --repo /Users/sasac/backcast --exclude src/tickreplay/static/strategy/releases --exclude .agents/logs --forbid 'native_trace_v1' --forbid 'native_trace_v2' --forbid 'BACKCAST_NATIVE_STRATEGY_EXECUTOR_ARGV' --forbid 'BACKCAST_NATIVE_STRATEGY_TRUSTED_LOCAL_ONLY' --forbid 'native_strategy_worker' --forbid-native-source-fields
uv run python scripts/verify_bundled_rl_snapshot.py verify --phase final --snapshot .agents/logs/bundled-rl-default-strategy-worktree-baseline.json --archive .agents/logs/bundled-rl-default-strategy-baseline-files.tar --ownership .agents/logs/ownership-team-execute-bundled-rl-default-strategy.json --all-owners --expected-new-release-id <candidate-id>
```

The uniquely titled browser gate additionally snapshots cursor, orders, fills, portfolio, and broker-dispatch count before Load and after Load. All remain identical after Load except trace/readiness state. Only Start may begin dispatch. The fixture uses differing flat/long targets and asserts the post-commit position-selected branch plus exact `fillIndex === observedIndex + 1` for entry and exit.

Phase 2 runs independent security, quality, and test-coverage reviews. Critical/High findings are fixed and re-reviewed before completion.

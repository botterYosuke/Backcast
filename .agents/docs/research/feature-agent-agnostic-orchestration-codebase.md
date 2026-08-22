# Agent-Agnostic Orchestration Codebase Scan

## Executive Summary

The repository already has the right high-level shape for an agent-agnostic
orchestrator: root `AGENTS.md` is the shared bootstrap, `.agents/` is the
canonical policy/capability store, native directories are intended to contain
only adapter configuration, and cross-CLI execution goes through hardened
wrappers. Claude Code can remain the default without being the shared
architecture.

The main gap is that the declared active main agent is descriptive rather than
operational. `.agents/STATE.md` records `Claude Code`, and
`.agents/skills/context-loader/load_context.py` exposes that string, but no
runtime registry resolves it into capabilities, role bindings, native paths, or
validation requirements. Changing the value to `Codex` does not change the
Claude-specific subagent names, tool syntax, hooks, installer behavior, `/init`
bootstrap checks, or `.agents/check.sh` expectations.

The smallest coherent refactor is therefore not to remove Claude support. It is
to introduce one small runtime/capability registry, make shared routing and
skills refer to logical roles and capabilities, and move Claude event/tool
syntax behind a Claude adapter. Existing Claude names, `CLAUDE.md`, discovery
links, settings, and default behavior should remain compatibility aliases.

This is a **COMPLEX, migration-sensitive change**: approximately 30-40 files and
roughly 900-1,500 changed lines across contracts, workflow prose, adapter
configuration, scripts, and tests. It should be implemented in phases, with the
first phase preserving byte-for-byte default Claude behavior.

## Current Main-Agent Selection Flow

1. **Template default**: root `AGENTS.md` states that Claude Code is the default
   main agent. The seed `.agents/STATE.md` stores `Claude Code` under
   `## Main Agent`.
2. **Discovery**: Claude Code reaches the shared contract through `CLAUDE.md ->
   AGENTS.md`; its native `.claude/agents` and `.claude/skills` entries point to
   canonical `.agents/agents` and `.agents/skills`. Codex reads root
   `AGENTS.md` and receives two explicitly configured skills through
   `.codex/config.toml`.
3. **Loading**: `.agents/skills/context-loader/load_context.py` parses the first
   value under `## Main Agent` and returns it as `state.main_agent`. It accepts a
   missing or arbitrary value and does not resolve a runtime adapter.
4. **Changing**: `.agents/change_main.md` instructs the operator to update only
   the state value for a repository-local switch, then manually add the target
   runtime's minimum native configuration and validate a disposable session.
   There is no executable change-main command or atomic typed writer dedicated
   to this field.
5. **Runtime activation**: the runtime the user actually starts remains the
   runtime in control. The state value cannot transfer an in-flight session or
   prove that the selected runtime can satisfy the main-agent contract.
6. **Verification**: `.agents/skills/init/detect_stack.py` and
   `.agents/check.sh` unconditionally require Claude's entrypoint and discovery
   links. They do not select checks based on `state.main_agent`. Consequently a
   nominal Codex main still has to look like a complete Claude installation.

The result is a useful human convention, but not yet a selection mechanism.

## Claude-Specific Coupling Inventory

### Intentional default and native adapter behavior

These are valid and should remain when Claude Code stays the default:

- `AGENTS.md` default statement and `.agents/STATE.md` seed value.
- `CLAUDE.md` as Claude Code's discovery entrypoint to shared `AGENTS.md`.
- `.claude/settings.json`: Claude hook event names, matchers, permissions,
  environment variables, Agent Teams flag, and default subagent model.
- `.claude/agents` and `.claude/skills`: Claude-native discovery adapters.
- `.codex/config.toml`: Codex-native model, sandbox, and skill configuration.
- `.agents/workflows/antigravity/{feature,troubleshoot}.md`: explicitly
  experimental Antigravity adapter sketches.
- `.agents/skills/_shared/cli_consult.py` and
  `.agents/skills/_shared/codex_consult.py`: per-callee headless adapters with a
  shared logging/error contract. Their separation reflects real permission and
  output differences.

### Avoidable shared-layer coupling

| Area | Concrete paths | Coupling |
|---|---|---|
| Shared bootstrap/routing | `AGENTS.md`, `.agents/rules/delegation.md`, `.agents/rules/tiers.md`, `.agents/rules/codex-delegation.md` | Logical work types are bound directly to `general-purpose-sonnet`, `general-purpose-opus`, `codex-debugger`, and Claude model configuration instead of capability/role IDs. |
| Main-agent runbook | `.agents/change_main.md`, `.agents/rules/agent-state.md`, `.agents/skills/context-loader/{SKILL.md,load_context.py}` | The state value is free text; no supported-runtime list, capability resolution, readiness state, or adapter validation exists. |
| Workflow prose | `.agents/skills/{feature,spike,troubleshoot,team-execute,tdd,plan,research-lib,simplify,catchup}/SKILL.md` | Shared workflows name “Claude Lead,” `Task tool`, `AskUserQuestion`, `TodoWrite`, `WebSearch/WebFetch`, Claude Agent Teams, and specific Claude subagent types. Another main must translate the workflow manually. |
| Codex integration skill | `.agents/skills/codex-system/SKILL.md` | The wrapper guidance is portable, but the preflight includes `claude update`, the recommended pattern uses Claude `Task`, and plugin commands are Claude-plugin surfaces. These belong in a Claude adapter subsection. |
| Agent definitions | `.agents/agents/{general-purpose-sonnet,general-purpose-opus,codex-debugger,fable-advisor}.md` | Frontmatter (`tools`, `model`) and bodies are Claude-native executor definitions, although the directory is described as shared and normative. Semantic role contracts and Claude manifests are conflated. |
| Hooks | `.agents/hooks/*.py` plus `.claude/settings.json` | The hook implementations consume Claude payload fields (`tool_name`, `tool_input`, `tool_response`, `session_id`), emit `hookSpecificOutput`, use Claude event/tool names, and recommend Claude `Task(...)` syntax. Several algorithms are reusable, but the stdin/stdout envelope is a Claude adapter. |
| Session/history collection | `.agents/skills/catchup/collect_repo_state.py`, `.agents/skills/checkpointing/checkpoint.py` | Both default to `~/.claude` and Claude Agent Teams `teams/` and `tasks/` layouts. This is an optional Claude history source presented as a generic session collector. |
| Bootstrap detection | `.agents/skills/init/{SKILL.md,detect_stack.py}` | `agent_bootstrap` means AGENTS + STATE + Claude entrypoint + both Claude discovery links, regardless of active main runtime. |
| Install/update/check | `scripts/{install,update}.sh`, `.agents/check.sh` | Product name, version marker, native link manifest, settings merge, migration paths, exact native-directory allowlists, and model-coherence checks are hard-coded around Claude. `.codex/` is overwritten as one template-owned directory while Claude settings are diffed/preserved, creating an adapter-specific asymmetry. |

The hooks are physically centralized under `.agents/hooks`, which is good for a
single source of truth, but they are not tool-neutral merely because the path is
shared. They should be classified as Claude adapter code unless and until their
pure detection logic is separated from the Claude event envelope.

## Existing Agent-Neutral Foundations

- `AGENTS.md` is already the correct universal bootstrap filename and explicitly
  separates shared content from product-native configuration.
- `.agents/STATE.md`, `.agents/docs/DESIGN.md`, `PROGRESS.md`, and the typed
  writers are runtime-independent data contracts.
- Tier IDs `default`, `sol`, and `fable` in `.agents/rules/tiers.md` are more
  stable than vendor/model names and can anchor neutral routing.
- `.agents/rules/cli-execution.md` defines a symmetric caller/callee rule:
  whichever CLI is main verifies delegated results independently.
- `.agents/skills/_shared/cli_consult.py` already uses an adapter table for
  Claude and Gemini; `.agents/skills/_shared/codex_consult.py` supplies Codex's
  distinct sandbox contract. Both emit comparable JSON result envelopes.
- `.agents/hooks/log-cli-tools.py` already records the callee and wrapper rather
  than assuming that Claude is always the caller.
- `.agents/change_main.md` contains the right invariants: keep `.agents/`
  canonical, keep native settings native, preserve fallback runtimes, and avoid
  permission widening.
- `.agents/workflows/antigravity/` expresses workflows in tier terms and is a
  useful proof that the semantic phases can be described independently of a
  particular main runtime.
- `tests/test_validate_doc.py` accepts both `Claude Code` and `Codex` state
  values because the document contract checks structure, not a Claude-only
  value.

## Proposed Boundary: Shared Core vs Runtime Adapters

### Shared core

Keep the following concepts canonical and vendor-neutral:

- Mission, approvals, language, quality gates, state/doc ownership.
- Tiers and logical roles such as `routine-implementer`, `deep-investigator`,
  `debugger`, `planner`, `reviewer`, and `team-coordinator`.
- Capabilities such as `ask-user`, `spawn-agent`, `parallel-team`,
  `task-tracking`, `web-research`, `file-edit`, and `hook-events`.
- Skill phase logic, artifacts, acceptance checks, and verification rules.
- Cross-CLI prompt/result envelope and audit log.

Add one small machine-readable registry (for example
`.agents/runtimes.json`) plus a deterministic helper (for example
`.agents/skills/_shared/runtime_context.py`). The registry should contain:

- stable runtime ID (`claude-code`, `codex`, `antigravity`);
- display name and `default_main` (`claude-code`);
- supported capability set;
- bindings from logical roles to native executor names or wrapper routes;
- native entrypoint/settings/discovery paths;
- adapter-specific validation checks.

The helper should combine the registry with `.agents/STATE.md`, reject an empty
main value, warn on an unknown runtime ID, and emit a resolved runtime context
for installers, `/init`, `context-loader`, and `.agents/check.sh`. Keep the
human display value compatible during migration, but make the stable ID the
machine key.

### Runtime adapters

- **Claude adapter**: `CLAUDE.md`, `.claude/*`, Claude agent frontmatter,
  Claude hook envelopes, Agent Teams/session-home collector, and mappings from
  logical roles to the existing `general-purpose-*` names.
- **Codex adapter**: `.codex/config.toml`, `codex_consult.py`, Codex sandbox and
  approval semantics, and mappings from logical roles to available Codex
  orchestration/subagent mechanisms.
- **Antigravity adapter**: keep the current workflow skeleton inactive until
  official discovery/configuration and equivalent capabilities are known.
- **Generic CLI adapter**: keep `cli_consult.py` as the registry-backed adapter
  for headless peer CLIs; add new callees by spec rather than by shared-workflow
  prose.

Shared skills should say “invoke the `deep-investigator` role” or “use the
`ask-user` capability.” A short adapter appendix should show the concrete
Claude `Task`/Agent Teams syntax. This preserves the workflow while allowing a
Codex main to bind the same intent to its own primitives.

### Compatibility aliases

Do not rename the current Claude-facing files or agent names in the first
phase. Treat `general-purpose-sonnet`, `general-purpose-opus`,
`codex-debugger`, slash-skill names, `CLAUDE.md`, and `.claude/agents|skills`
as supported aliases that resolve to the new role/capability model. Remove an
alias only in a later major template migration.

## Affected Files and Change Types

### Shared core

| Paths | Likely change |
|---|---|
| `AGENTS.md`, `.agents/INDEX.md` | Rename the product-branded shared contract, retain Claude as the stated default, and document the core/adapter boundary. |
| `.agents/change_main.md`, `.agents/rules/{delegation,tiers,cli-execution,codex-delegation,agent-state}.md` | Define stable runtime IDs, capability/role routing, and adapter-aware validation while preserving existing tier semantics. |
| `.agents/skills/context-loader/{SKILL.md,load_context.py}` | Return resolved runtime context and surface unknown/missing/capability gaps. |
| `.agents/skills/{feature,spike,troubleshoot,team-execute,tdd,plan,research-lib,simplify,catchup}/SKILL.md` | Replace Claude tool syntax in normative phases with capability calls; retain Claude examples in an adapter section or reference. |
| `.agents/skills/codex-system/SKILL.md` | Separate portable Codex wrapper guidance from Claude plugin and `Task` examples. |
| New `.agents/runtimes.json` and `.agents/skills/_shared/runtime_context.py` (names illustrative) | Single runtime/capability registry and resolver; avoid duplicating runtime tables across Bash, Python, and Markdown. |

### Runtime adapters

| Paths | Likely change |
|---|---|
| `CLAUDE.md`, `.claude/{settings.json,agents,skills}` | Preserve as the default Claude adapter; consume registry values where practical. |
| `.codex/config.toml` | Preserve Codex-native policy; decide which shared skills a Codex main must expose beyond the current two. |
| `.agents/agents/*.md` | Retain Claude discovery compatibility while separating semantic role requirements from Claude `model`/`tools` frontmatter. |
| `.agents/hooks/*.py` | Classify as Claude adapter entrypoints; optionally extract reusable detectors/loggers into shared modules with normalized event input. |
| `.agents/skills/{catchup/collect_repo_state.py,checkpointing/checkpoint.py}` | Rename `--claude-home` to a neutral source option or add a collector interface, retaining `--claude-home` as an alias. |
| `.agents/workflows/antigravity/*.md` | Update references to the neutral role/capability contract; keep inactive. |
| `.agents/skills/_shared/{cli_consult,codex_consult}.py` | Mostly retain; optionally source supported-callee metadata from the registry while preserving their current CLI and JSON contracts. |

### Installer/updater/checking

| Paths | Likely change |
|---|---|
| `scripts/install.sh` | Generalize native adapter manifests, keep Claude installed/enabled by default, preserve existing backup and settings-merge behavior. |
| `scripts/update.sh` | Move version metadata out of the Claude namespace or support the legacy path; make adapter sync policy explicit instead of overwriting all `.codex/` while diffing `.claude/settings.json`. |
| `.agents/check.sh` | Validate shared core always, default Claude adapter always, active-main adapter conditionally, and optional installed adapters independently. |
| `.agents/skills/init/{SKILL.md,detect_stack.py}` | Report `shared_bootstrap` separately from per-runtime adapter readiness; do not define bootstrap completeness solely as Claude symlinks. |

## Test and Verification Surface

### Tests that intentionally preserve Claude as default

- `tests/test_agent_model_routing.py`: retain coverage for Sonnet/Opus Claude
  bindings and `CLAUDE_CODE_SUBAGENT_MODEL`, but test them as the Claude adapter
  mapping rather than universal role names.
- `tests/test_cli_consult.py`: retain per-callee argument, permission, resume,
  output, and failure tests. Add registry/binding coverage without weakening
  the wrapper contract.
- `tests/test_post_bash_check.py`: retain Claude hook-envelope tests under the
  Claude adapter; add pure normalized-event tests if logic is extracted.

### Tests that currently enforce avoidable Claude coupling

- `tests/test_orchestration_contract.py`: requires `Claude Code` in the shared
  bootstrap, exact Claude symlinks, exact native-directory contents, and the two
  current Codex skill paths. Split into shared-contract, Claude-default-adapter,
  and active-runtime tests.
- `tests/test_install_script.py`: extensively asserts `.claude` paths,
  `CLAUDE.md`, `.claude/orchestra-version`, Claude legacy migration, and exact
  `.codex` contents. Preserve these as default-install compatibility tests and
  add non-default/optional-adapter fixtures.
- `tests/test_detect_stack.py`: currently treats missing Claude discovery links
  as global bootstrap failure. Change expectations to distinguish shared-core
  failure from Claude-adapter failure.
- `tests/test_load_context.py`: add stable-ID normalization, unknown runtime,
  missing main value, and capability resolution cases.
- `tests/test_collect_repo_state.py` and `tests/test_checkpoint.py`: generalize
  Claude-home fixtures while retaining the legacy option and Agent Teams
  collector tests.

### Related regression gates

- `tests/test_validate_doc.py`: preserve structural validation and add a
  runtime-ID/value rule only if the state schema adopts one.
- `tests/test_shared_script_contract.py`: keep the wrapper-only cross-CLI rule.
- `.agents/check.sh`: run as a first-class acceptance gate after its checks are
  split by shared core/default adapter/active adapter.
- Full relevant suite: at minimum the tests named above, followed by the
  repository's existing full test command and a real disposable startup probe
  for Claude Code and the selected alternative main.

## Backward-Compatibility and Migration Risks

1. **Default behavior drift**: changing shared role names could stop Claude
   native discovery or route different models. Preserve existing names as
   adapter aliases and snapshot the default resolved bindings.
2. **State schema drift**: existing repositories store display strings such as
   `Claude Code` or `Codex`. Normalize these to stable IDs without rewriting
   unrelated `.agents/STATE.md` content; unknown values should warn rather than
   be silently coerced.
3. **Installer data loss**: existing `.claude/agents`, `.claude/skills`, and
   settings are user-owned in some downstream repositories. Retain current
   conflict detection, backups, and merge-candidate behavior.
4. **Update compatibility**: `.claude/orchestra-version` is the installed
   version source today. Read it as a legacy fallback if metadata moves to a
   neutral path, and write both for at least one compatibility window.
5. **Hook semantics**: other runtimes may not have equivalent pre/post tool
   events or may use different allow/block semantics. Capabilities must be
   optional; absence of hooks must not pretend that equivalent enforcement ran.
6. **Permission widening**: mapping a logical role to Codex or another runtime
   must not inherit `danger-full-access` accidentally. Keep read/write access
   explicit at each adapter boundary and preserve independent verification.
7. **Partial capability support**: Antigravity is explicitly inactive, and
   Codex currently configures only `context-loader` and `design-tracker` as
   skills. A main-runtime switch should fail readiness or report degraded
   capabilities rather than claim parity.
8. **Cross-platform discovery links**: in this Windows checkout, `CLAUDE.md`,
   `.claude/agents`, and `.claude/skills` are Git mode `100644` files containing
   link targets, while POSIX tests and scripts require real symlinks. The
   refactor must decide whether this is a supported checkout representation or
   an existing environment defect before making adapter checks stricter.

## Complexity Estimate

- **Classification**: COMPLEX (cross-cutting contracts, migration behavior,
  permission semantics, and 5+ files).
- **Likely footprint**: 30-40 modified/new files.
- **Likely changed LOC**: 900-1,500, mostly Markdown workflow normalization,
  registry/resolver code, installer/check branching, and tests rather than
  application code.
- **Suggested delivery slices**:
  1. Add registry/resolver and tests; preserve all current Claude outputs.
  2. Split shared bootstrap checks from Claude-adapter checks.
  3. Convert routing and the highest-use skills (`context-loader`, `feature`,
     `troubleshoot`, `team-execute`) to roles/capabilities.
  4. Classify/extract hooks and session collectors.
  5. Add a disposable Codex-main readiness test, then update remaining skills.

## Open Questions

1. Does “agent-agnostic” require Codex to be a fully supported main immediately,
   or only require the shared core to permit future adapters while Claude stays
   the sole production main?
2. Should installed-but-inactive adapters be fully validated, or should
   `.agents/check.sh` gate only the default and active main adapters?
3. Should model-specific roles remain user-visible compatibility names, or
   become entirely adapter-internal after one deprecation window?
4. Which capabilities are mandatory for every main runtime? In particular, is
   parallel teammate communication required, or may a runtime implement the
   same workflow with isolated subagents and lead-side integration?
5. Should runtime metadata/version move from `.claude/orchestra-version` to a
   neutral `.agents/` path, and what is the compatibility window?
6. Is the regular-file representation of the three intended symlinks on
   Windows supported by policy, or should installation/checking require a
   symlink-capable environment?

## Feature Brief: Agent-Agnostic Orchestration

### Current State
- Architecture: Root `AGENTS.md` and canonical `.agents/` content are intended to be tool-neutral, while product-native directories expose that content to individual runtimes. Cross-CLI wrappers and stable Tier IDs already provide a partial neutral execution layer.
- Relevant files: `AGENTS.md`, `.agents/STATE.md`, `.agents/change_main.md`, `.agents/rules/`, `.agents/skills/`, `.agents/agents/`, `.agents/hooks/`, `.agents/check.sh`, `scripts/install.sh`, `scripts/update.sh`, `.claude/`, `.codex/`, and orchestration contract tests.
- Patterns: Shared state and design documents use deterministic typed writers; cross-CLI calls use wrapper-owned JSON envelopes; delegated writes require independent gates and diff review; native runtime configuration remains outside the canonical shared core.
- Gap: `## Main Agent` is descriptive free text. Shared routing, major workflows, hooks, bootstrap detection, distribution scripts, and checks still encode Claude-native roles, tools, event envelopes, and paths.

### Feature Goal
Make the orchestration framework runtime-neutral at its shared core while keeping Claude Code as the default, fully supported main runtime. Runtime-specific discovery, permissions, hooks, executor bindings, and readiness checks must live behind explicit adapters so Codex can operate as a validated alternative main and future runtimes can be added without rewriting shared workflow semantics.

### Scope
- Include: Add a single machine-readable runtime/capability registry and deterministic resolver with stable runtime IDs, display-name aliases, logical-role bindings, capability declarations, native paths, and readiness states.
- Include: Preserve `Claude Code` as the default and keep existing Claude files, agent names, skills, hooks, symlinks/pointer compatibility, and model routing as first-migration compatibility aliases.
- Include: Change shared routing and normative workflow phases from Claude-native executor/tool names to logical roles and capabilities, with Claude syntax retained in adapter-specific guidance.
- Include: Split bootstrap, install/update, and validation behavior into shared-core, default-adapter, active-adapter, and optional-adapter checks without widening permissions or overwriting user-owned runtime configuration.
- Include: Provide and validate a Codex-main adapter path. Represent unsupported or incomplete runtimes as `degraded` or `unsupported` instead of claiming parity.
- Include: Classify Claude hook envelopes and Claude session-history collection as adapter behavior; extract only genuinely reusable logic into the shared layer.
- Include: Add migration and regression tests for default Claude compatibility, runtime resolution, adapter readiness, installer/updater preservation, portable Windows discovery representation, and a disposable Codex-main startup flow.
- Exclude: Removing Claude Code as the default, renaming or deleting current Claude-facing compatibility names in the first migration, changing application/business code, external library research, claiming full Gemini/Antigravity main support without native validation, or weakening existing permission and completion-verification rules.

### Complexity Classification (from Codex)
- Classification: COMPLEX
- Estimated files: 30-40 modified or new files
- Estimated LOC: 900-1,500 changed lines
- Implementation route: `team-execute` with phased implementation and parallel security, quality, and test-coverage review

### Integration Points
- Main-agent state: `.agents/STATE.md` display values resolve through stable registry IDs and capability/readiness metadata without rewriting unrelated state blocks.
- Context loading: `context-loader` returns resolved runtime context and surfaces missing, unknown, degraded, and unsupported adapter states.
- Routing: `.agents/rules/delegation.md` and workflow skills route logical roles such as routine implementer, deep investigator, debugger, planner, reviewer, and team coordinator through the active adapter.
- Native discovery: `CLAUDE.md`, `.claude/*`, `.codex/config.toml`, and future runtime files remain adapter-owned entrypoints rather than shared-core semantics.
- Cross-CLI execution: Existing `cli_consult.py` and `codex_consult.py` contracts remain compatible and may consume registry metadata without weakening explicit read/write permissions.
- Distribution and readiness: installer, updater, `/init`, and `.agents/check.sh` distinguish shared-core health from default, active, and optional adapter health.
- Tests: Existing Claude-specific expectations become default-adapter compatibility tests; new tests cover resolver behavior, adapter preservation, capability gaps, and alternative-main cold starts.

### Risks
- Default Claude behavior drift: Snapshot current bindings and installer output first; keep all existing names and native paths as compatibility aliases in the initial migration.
- Permission widening across adapters: Keep read/write authority explicit per wrapper and adapter; readiness must fail or degrade when an equivalent permission boundary cannot be proven.
- User configuration loss: Define adapter-owned versus project-owned files and preserve existing backup, conflict, and merge behavior before generalizing updater logic.
- False parity: Require runtime readiness states and capability diagnostics; do not label the framework fully agent-agnostic until at least Claude and Codex main flows pass disposable startup tests.
- State migration ambiguity: Accept existing display strings such as `Claude Code` and `Codex`, normalize them to stable IDs, and reject or clearly warn on unknown values.
- Windows discovery representation: Decide and test whether pointer files are supported alongside symlinks before tightening adapter checks.
- Gemini/Antigravity damage: Do not install or remove native files for runtimes whose adapter contract is not yet supported; specifically prevent updater cleanup from deleting a future `.gemini` adapter.

### Success Criteria
- With no configuration changes, Claude Code resolves as `claude-code`, reports ready, preserves current role/model bindings, and passes the existing default-runtime gates.
- Existing `## Main Agent` display values resolve deterministically to stable runtime IDs; empty or unknown values produce an actionable diagnostic and never silently select a different runtime.
- Shared routing and high-use workflows express normative behavior through logical roles/capabilities, with no requirement for another main runtime to interpret Claude-only `Task`, `AskUserQuestion`, `TodoWrite`, or Agent Teams syntax.
- `.agents/check.sh`, `/init`, installer, and updater separately report shared-core, default-adapter, active-adapter, and optional-adapter status and preserve user-owned native configuration.
- A disposable Codex-main repository session can load context, resolve roles/capabilities, produce a plan, delegate through supported wrappers, and run independent verification; unsupported capabilities are reported as degraded rather than emulated unsafely.
- Claude hook and session-history behavior continues to work under the Claude adapter, while the shared core makes no claim that other runtimes enforce hooks they do not provide.
- Focused orchestration, resolver, installer/updater, context-loader, and compatibility tests pass on supported platforms; Windows symlink/pointer behavior is covered explicitly.
- No application code changes, no weakened tests, no silent permission expansion, and no removal of existing Claude compatibility surfaces occur in the first migration.

### Assumptions Requiring User Approval
- Codex is the first alternative main runtime that must be validated end to end; Gemini and Antigravity remain adapter-ready or unsupported until their native contracts are researched and tested.
- Parallel teammate communication is an optional capability. A runtime may remain supported with isolated subagents plus lead-side integration if the workflow declares the degraded execution mode.
- The migration is delivered in phases, but the approved feature scope covers the complete shared-core/adapter split rather than only documentation wording.

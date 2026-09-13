## Executive Conclusion

The preflight failures have independent causes: GNU-only path normalization blocks 18 installer/update tests; three committed regular-file link placeholders violate two repository-contract tests; a mutable DESIGN fixture breaks one writer test; and an unused import, two formatting mismatches, and implicit AnyIO submodule lookup fail the static gates. The authoritative result is **21 failed / 1208 passed**, not 18 total failures (`.agents/logs/verify.log:4-35,248-269`). A second macOS incompatibility, GNU `sed -i`, is already covered by an updater test but hidden behind the installer failure (`scripts/update.sh:667`; `tests/test_install_script.py:436-459`).

Recommend portable path/settings handling, restoring the three actual relative links, explicit blank-template test input, and mechanical imports/format repairs. No implementation or full-suite rerun was performed. Four bounded Codex consultations returned answers; final correctness verdict **INCOMPLETE** identifies implementation requirements rather than verified success.

## Execution Flows

1. `.agents/skills/_shared/verify.sh:163,174,187,200` runs Ruff check, Ruff format, Ty on `src/`, and pytest independently. Outer command exits 2, each gate exits 1 (`.agents/logs/troubleshoot-repro-preflight-quality-gate-failures-initial.log:1-24`).
2. Installer tests call `bash install.sh --yes <temporary-project>` (`tests/test_install_script.py:17-27`); update tests first install a copied template (`:30-59,399-405`). Installer validates before writes (`scripts/install.sh:450-455`). Parent links are refused at `:173-175`; GNU `realpath -m --` at `:179` fails, stopping 18 scenarios before their intended behavior (`.agents/logs/verify.log:95-221`).
3. Once installation succeeds, the migration scenario writes legacy hook settings and requires canonical paths after update (`tests/test_install_script.py:436-459`). Update calls settings migration at `scripts/update.sh:789`; `sed -i` at `:667` uses a host-incompatible form. This is a masked second failure, not an extra observed failure in the original run.
4. Orchestration tests inspect actual entry types: `CLAUDE.md.is_symlink()` fails and native discovery enumeration finds real files (`tests/test_orchestration_contract.py:81-86,110-136`; `.agents/logs/verify.log:222-235`).
5. Writer fixture copies current DESIGN (`tests/test_update_design.py:42-48`); test assumes blank Performance (`:234-236`) although the row is populated (`.agents/docs/DESIGN.md:54`). Writer preserves populated content and appends a distinct NFR (`.agents/skills/_shared/update_design.py:331-349`), producing correct 8 versus erroneous expected 7 rows (`.agents/logs/verify.log:236-240`).
6. Ty follows `import anyio` at `src/tickreplay/server.py:35` to `anyio.to_thread.run_sync` at `:602`. AnyIO's fallback only exports a deprecated exception alias and is annotated `type[BrokenWorkerInterpreter]` (`.venv/lib/python3.13/site-packages/anyio/__init__.py:103-115`), explaining the diagnostic. The actual function is defined in `anyio/to_thread.py:27-33`. Passing runtime server tests (`.agents/logs/verify.log:82-84`) mean this is a proven static import defect, not a demonstrated runtime Daily regression.

## Hypotheses Evaluated

| Hypothesis | Evidence for | Against / limits | Verdict |
|---|---|---|---|
| H1 GNU shell portability | Eighteen identical `illegal option -- m` failures; same family at `scripts/install.sh:179,321`, `scripts/update.sh:324,611`, `.agents/check.sh:299`. | Host accepts `--` for realpath/readlink/dirname (probes below). | Confirmed for `-m`; blanket delimiter claim eliminated. |
| H2 symlink drift | Contract requires three links (`AGENTS.md:121-128`); HEAD modes and filesystem are regular files. | `core.symlinks=false` alone does not explain already committed `100644` modes. | Confirmed repository type defect; checkout configuration separate. |
| H3 mutable fixture | Live-copy setup and blank-row assumption disagree (`tests/test_update_design.py:47,235`; `DESIGN.md:54`). | Writer intentionally appends distinct populated-label NFR (`update_design.py:331-349`). | Confirmed fixture defect; production writer defect eliminated for this failure. |
| H4 imports/format | Exact diagnostics in `.agents/logs/verify.log:4-35`; AnyIO function exists explicitly. | No evidence warrants pins, ignores, or gate exclusions. | Confirmed local static defects. |
| H5 initial Codex timeout | Bug Report Initial Hypotheses item 5 records 600s timeout, zero answer. | Timeout says nothing about source correctness; later calls succeeded. | Operational failure, never causal evidence. |

## Root Causes

### Shell portability and safety boundary

The installer relies on GNU missing-path resolution at a security boundary. Dropping `-m` rejects fresh missing destinations; removing resolution weakens containment (`scripts/install.sh:150-184`). Updater cleanup likewise checks resolved destinations before deletion (`scripts/update.sh:315-339`). Existing canonical-link identity does not need missing-path normalization (`scripts/install.sh:317-327`; `scripts/update.sh:605-618`; `.agents/check.sh:298-302`).

Captured host probes:

```text
$ /usr/bin/readlink -- /var
private/var
$ /bin/realpath -- /Users/sasac/backcast
/Users/sasac/backcast
$ /usr/bin/dirname -- /Users/sasac/backcast/CLAUDE.md
/Users/sasac/backcast
$ /usr/bin/sed -i 's/a/b/' /dev/null
sed: 1: "/dev/null": extra characters at the end of n command
```

The sed probe fails option/script parsing before file processing. These results correct the supplied context's broader unsupported-`--` claim. Keep supported delimiters. The incompatible settings migration is covered by the existing updater scenario (`tests/test_install_script.py:436-459`).

### Symlink repair is both repository content and generated local-state repair

Captured inventory:

```text
$ git ls-tree HEAD CLAUDE.md .claude/agents .claude/skills
100644 blob 4c8a5fc93dd07a2f87261d230b24fa63ddaa0c4f .claude/agents
100644 blob 2b7a412b8fa0fb7e985b0793321bd4e698f2b6cd .claude/skills
100644 blob 47dc3e3d863cfb5727b87d785d09abf9743c0a72 CLAUDE.md
$ git config --show-origin --get core.symlinks
file:.git/config false
$ git log -1 --format='%H %s' -- CLAUDE.md .claude/agents .claude/skills
9e52cb54179bde31ca2528a966b8e1489a34df80 init
```

Exact contents are `AGENTS.md` (9 bytes), `../.agents/agents` (17), and `../.agents/skills` (17), without newline (`CLAUDE.md:1`; `.claude/agents:1`; `.claude/skills:1`; byte inspection). They contain no additional user instructions or agent files. Recheck exact contents/type immediately before repair; preserve unexpected content instead of assuming this inventory remains current.

Installer/updater generate these links in installed targets (`scripts/install.sh:317-342`; `scripts/update.sh:605-655`), so there they are generated local state. This repository tracks them incorrectly: restoring them also creates a reviewable regular-file-to-symlink content/type change. The index remains `100644` until normal staging/commit records `120000`; no staging is necessary for this diagnostic task or for presenting an unstaged implementation diff. Local `core.symlinks=true` can enable supported checkout/type handling but does not itself repair entries. Do not change global configuration.

Keep installer conflict refusal/backup (`scripts/install.sh:223-250,274-282`) and updater discovery backup (`scripts/update.sh:636-646`). Do not run broad updater on the live repository merely for these three links: it overwrites other managed directories (`scripts/update.sh:389-439`).

### Fixture, Ruff, and Ty

Correct the test's input while preserving unchanged-row-count/content assertions and production writer behavior (`tests/test_update_design.py:234-253`; `update_design.py:331-349`). Remove only unused import and format only the named files (`.agents/logs/verify.log:4-23`). Explicit AnyIO module import retains the function, dedicated limiter, and cancellation defaults (`src/tickreplay/server.py:592-602`; `anyio/to_thread.py:27-33`). Existing tests verify the default pool remains available (`tests/test_tickreplay_server.py:519-539`); they do not monkeypatch `run_sync`, contrary to preliminary first-consultation wording.

## Fix Approaches

| Area | Approach A | Approach B | Recommendation |
|---|---|---|---|
| Missing-path resolver | Small Python stdlib component walker, early compatible-interpreter preflight, missing-only tolerance, error propagation. No coreutils dependency; more safety tests. | Require/discover GNU coreutils. Smaller source change but new stock-macOS installation requirement. | A with concrete fail-closed handling, not naive non-strict realpath. |
| Existing CLAUDE identity | Bash `-L` plus `-ef`. Minimal; same-target identity. | General resolver everywhere or exact target-text comparison. More work or stricter behavior. | A; link creation remains exact relative target. |
| Settings migration | Same-directory temp transform, preserve metadata, replace only after success, cleanup on error. No platform branches. | Detect GNU/BSD sed and select proper `-i` spelling. Smaller transformation change, platform detection burden. | A if source metadata/error semantics are preserved; B acceptable if safer/minimal in implementation. |
| Fixture | Give one blank-row test explicit minimal valid DESIGN. Small, retains other live-document integration coverage. | Freeze all writer fixtures and keep separate live-document integration test. Better broad isolation, larger scope. | A for current task. |
| Links | Repair verified exact placeholders and expose type diff for normal commit. | Full updater or relaxed contract tests. Broad writes or weakened contract. | A only. |
| AnyIO | Explicit module import, same limiter/call. | Dependency pin/suppression or function alias. Pins/ignores miss cause; alias valid but less minimal. | Explicit module import. |

A shell-only recursive resolver must solve the same missing/relative/cyclic-link cases with less readable code. Non-strict `os.path.realpath`/`Path.resolve` alone can suppress ELOOP/EACCES/ENOTDIR and is not the recommended implementation. The initial design consultation understated that risk; final review identified it. Startup subprocess overhead is insignificant relative to install/update copying, but performance has not been measured.

## Recommended Fix Set

1. Add a small Python resolver compatible with the bootstrap interpreter or enforce its minimum before mutation. Concrete design: walk absolute components with `os.lstat`; normalize `.`/`..`; follow `os.readlink` with a queue (absolute targets reset root, relative targets retain parent); tolerate only `FileNotFoundError`; propagate other failures; bound link traversal to terminate cycles. Avoid newer-only `ALLOW_MISSING`. A shared helper in `.agents/skills/_shared/` is already distributed by managed skills directories (`scripts/install.sh:10`; `scripts/update.sh:32`). This design still needs executable tests.
2. Replace missing-path GNU calls in installer validation and updater cleanup; preserve parent-link refusal, strictly-inside-root check, prewrite validation, backup, and swap rollback. Reject unsafe resolution errors. Use `-L` plus `-ef` at three existing canonical-link checks; keep supported delimiters.
3. Make settings migration portable with original bytes/metadata retained on transform/replacement failures. Existing migration test passes unchanged.
4. Restore only three verified exact placeholders to relative links. Verify types, target text, resolved target, and working-tree type diff. Normal commit later records Git modes; report any repository-local configuration change. Preserve unexpected content.
5. Construct explicit minimal valid DESIGN input with `| Performance | | |` in the single blank-row test; preserve assertions and assert writer exit zero.
6. Remove unused `pytest` from `tests/test_tickreplay_minute_context.py`; format it and `tests/test_collect_repo_state.py`; explicitly import `anyio.to_thread` in `src/tickreplay/server.py`.
7. Focused tests: installer/update including rollback/migration, orchestration, writer, affected minute/server limiter, Ruff/Ty. Add meaningful boundaries for missing leaves/intermediates, inside/outside/dangling/multihop/cyclic links, non-directory intermediate, permission error, prefix-confusion roots, spaces, and interpreter absence before mutations. Check outside sentinels and preserved project content. Then lead independently runs full original preflight. No weakened/skipped tests or gate exclusions.

## Codex Consultations

All four calls used `uv run python .agents/skills/_shared/codex_consult.py`, explicit `--sandbox read-only`, `--timeout 180`, and `--config model_reasoning_effort=low`. This report was staged as the prompt file; wrapper saved immutable prompt snapshots. Every call returned `ok: true`, `timed_out: false`, `response_verified: true`.

| Label | Duration | Response | Assessment |
|---|---:|---|---|
| preflight-root-flow | 22.698s | `.agents/logs/codex/20260913T115310Z-preflight-root-flow.md` | Main flows agreed; corrected 18-total wording (21 overall), checkout-mode causation implication, and preliminary monkeypatch claim. |
| preflight-root-hypotheses | 21.130s | `.agents/logs/codex/20260913T115351Z-preflight-root-hypotheses.md` | H1-H4 confirmed; blanket delimiter incompatibility eliminated. Checkout causation speculation not accepted as history evidence. |
| preflight-root-fix-design | 32.561s | `.agents/logs/codex/20260913T115427Z-preflight-root-fix-design.md` | Preferred portable Python and local fixtures/imports; non-strict resolver sketch strengthened above. |
| preflight-root-fix-verify | 26.433s | `.agents/logs/codex/20260913T115532Z-preflight-root-fix-verify.md` | **INCOMPLETE**: fail-closed resolver details, interpreter compatibility, unstaged-versus-committed mode persistence, metadata/error preservation, boundary tests. Incorporated as mandatory implementation requirements, not executable approval. |

Initial 600s/zero-answer timeout is recorded by supplied Bug Report and contributes no evidence. No consultation here timed out. First two replies used Japanese; durable findings are recorded in English and later prompts explicitly requested English. Wrapper answers were read and independently checked against cited code/logs; inaccurate suggestions are not silently accepted.

## Remaining Unknowns

- No implementation or independent executable validation yet. Final consultation is not PASS; implementation must close its specific gaps.
- Resolver design, bootstrap version, packaging path, and all error/boundary behavior need focused tests. System `python3` failed the existing context loader's modern union annotations; `uv run python` succeeded. Avoid assuming system and project interpreters are equivalent.
- Linux execution and filesystems without symlink support are unverified; current tests explicitly require actual links (`tests/test_orchestration_contract.py:81-86,125-136`). No contract redefinition.
- Existing shell checks are not a proven defense against concurrent hostile filesystem changes; preserve boundaries without claiming a new guarantee. Settings metadata/error behavior needs implementation-specific review.
- No source, tests, symlinks, lockfiles, .env, Git config, or index was changed by this agent. Only assigned report/work log and wrapper artifacts were written. Feature review and coverage remain paused.

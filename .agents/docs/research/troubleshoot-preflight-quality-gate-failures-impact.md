# Preflight Quality Gate Failures — Impact Investigation

## Executive Conclusion

The 21 pytest failures and three static-check failures are pre-existing portability, repository-link representation, test-isolation, and import/style defects. They were exposed on macOS by the full preflight run; none of their introducing lines belong to Python strategy feature commit `26cc86ecd268f3165d575fd1c54387fedc73908d`. The largest regression risk is changing path resolution used before writes and deprecated-path deletion. Preserve containment, backup, and rollback behavior while removing the actual GNU-only assumptions.

The initial context overstates the shell problem: `realpath -m` is incompatible on this host, but `/bin/realpath -- /Users/sasac/backcast` and `/usr/bin/readlink -- /var` both succeed. Do not remove all `--` delimiters. A separately masked defect is `scripts/update.sh:667`: BSD sed requires an extension argument for `-i`.

Investigation was read-only except this report, the assigned work log, and wrapper-generated consultation artifacts. No full verifier or global pytest rerun was performed. Captured baseline: 21 failed / 1,208 passed / 1 warning, with Ruff check, format, and Ty also failing (`.agents/logs/verify.log:4`, `:18`, `:25`, `:247`).

## Git History

| Origin / relevant commit | Evidence and interpretation |
|---|---|
| `9e52cb54179bde31ca2528a966b8e1489a34df80` — 2026-08-20, initial commit | Introduced GNU `realpath -m` in `scripts/install.sh:179`, `:321`, `scripts/update.sh:324`, `:611`, `.agents/check.sh:299`; introduced `sed -i` hook migration at `scripts/update.sh:667`. No earlier known-good local history exists for these paths. |
| `9e52cb54179bde31ca2528a966b8e1489a34df80` | `git ls-tree` at this commit and HEAD both show `100644` for `CLAUDE.md`, `.claude/agents`, `.claude/skills`. The target-text placeholders were committed as regular files from the outset, despite the generated-link contracts in `scripts/install.sh:317` and `:329` and tests at `tests/test_orchestration_contract.py:81`, `:110`. This is not solely checkout materialization. |
| `9e52cb54179bde31ca2528a966b8e1489a34df80` | Introduced fixture copying live DESIGN (`tests/test_update_design.py:42`) and the blank Performance-row assumption (`:234`). The flaw remained latent while the document was blank. |
| `0bdf8682d1e0997f56388f0fdfee28c09a7d14fb` — 2026-08-30 | Intentionally populated Performance (`.agents/docs/DESIGN.md:54`), activating the brittle fixture assumption. The same commit introduced `anyio.to_thread.run_sync` at `src/tickreplay/server.py:602` for bounded daily I/O. |
| `fb1f17d809d883d4c48c0eeab797f317d9bbbbbf` — 2026-08-21 | Locked AnyIO 4.14.2 (`uv.lock:24`); Ty 0.0.58 version already existed in initial commit (`uv.lock:604`). The failing import was added later against this dependency baseline; no evidence supports blaming the latest dependency sync for an upgrade. |
| `ebada788ed61b29f07bb30481930bc4fa7eb5fca` — 2026-08-21 | Added the Windows cp932/UTF-8 regression test whose nested signature needs formatting (`tests/test_collect_repo_state.py:95`). Preserve this test and its encoding assertions. |
| `6fe850714deb7e24ce81c52af0bc30c520b706e1` — 2026-08-22 | Added the unused pytest import and unformatted minute-context data (`tests/test_tickreplay_minute_context.py:14`, `:19`). |
| `26cc86ecd268f3165d575fd1c54387fedc73908d` — 2026-09-11 | Python strategy feature changes server static serving and adds CodeMirror decisions. `git diff main -- src/tickreplay/server.py .agents/docs/DESIGN.md` leaves the failing AnyIO call and populated Performance row unchanged. The captured context additionally establishes identical main/HEAD objects for the installer, affected tests, writer, and lockfile. |

History attribution uses `git log`, `git blame`, `git ls-tree`, and the main comparison. No checkout or bisect mutation was needed. The date when local `.git/config` acquired `core.symlinks=false` cannot be reconstructed from Git commit history.

## Blast Radius

| Surface | Callers and downstream effects |
|---|---|
| Installer destination validation | `main()` at `scripts/install.sh:450` invokes validation before project-file checks, conflict collection, backup, copying, entrypoint repair, discovery-link creation, settings and version handling. Every existing installer/update test enters this installer, so one early portability error masks all 18 scenarios (`tests/test_install_script.py:17`, `:30`, `:91`). |
| Updater deprecated cleanup | `scripts/update.sh:281` resolves paths before `git rm` / `rm`; managed deprecated entries include `.gemini`, old native directories, and `.codex/skills` (`:57`). A weakened resolver can widen deletion scope. Existing outside-root and root-itself checks must remain (`:325`, `:329`). |
| Bootstrap/discovery repair | Installer and updater create `CLAUDE.md -> AGENTS.md` and `.claude/{agents,skills} -> ../.agents/{agents,skills}` (`scripts/install.sh:317`, `:329`; `scripts/update.sh:605`, `:623`). Updater preserves real native content in unique backups before replacing (`:636`). `.agents/check.sh:287` checks these invariants but does not repair them. |
| Legacy settings migration | `scripts/update.sh:660` rewrites hook paths; migration executes after safe-directory/file sync and link repair (`:785`). A late sed failure can leave an incomplete but already partly updated installation. Preserve settings content and permissions, report errors, clean temporary artifacts, and make retries safe. |
| Installer/update delivery | README documents Bash install and update (`README.md:69`, `:450`); both scripts distribute/replace themselves and `.agents/check.sh` (`scripts/install.sh:34`, `:39`; `scripts/update.sh:40`, `:45`). Any new helper must be shipped before its first use and survive updater self-replacement. |
| Daily API thread offload | `read_daily_context()` calls `_run_daily_context()` (`src/tickreplay/server.py:605`, `:618`), which passes a dedicated limiter (`:52`, `:602`). Fixing the import must preserve bounded I/O and the default pool's availability. The browser's Daily chart and the cloud-run mounted app consume this API. |
| DESIGN writer fixture | Writer `_place_row()` correctly fills same-label placeholders and otherwise appends (`.agents/skills/_shared/update_design.py:331`, `:348`). Its design-tracker/checkpointing consumers must retain those semantics. This failure warrants a fixture correction, not changing production insertion behavior. |
| Ruff | Only two named test files require style cleanup; the UTF-8 regression and minute-context best-effort behavior remain valid tests. |

Host expectations: GNU/Linux behavior is the compatibility baseline implied by the current commands; native Darwin execution is required by the captured test run. The scripts require Bash and Unix utilities, with no native PowerShell installer implementation discovered. Windows application support is evidenced by the UTF-8 regression and `.mjs` MIME history, but it does not prove native Windows shell support. Windows checkouts without real symlink support cannot satisfy the current orchestration contract; do not weaken those assertions to emulate support. No explicit shell host matrix was found in README, DESIGN, or searched workflow configuration.

## External Research

- GNU documents `-m` as allowing missing/unavailable components, so replacing it with plain `realpath` can break fresh installs whose managed directory trees do not yet exist. The official search extract was accessible; repeated direct-page fetches timed out. [GNU realpath manual](https://www.gnu.org/software/coreutils/manual/html_node/realpath-invocation.html).
- BSD sed's `-i` takes an extension. This matches the host's installed `man sed` synopsis and option section; the current GNU form consumes the replacement program as that extension. Use a tested portable transform instead of assuming the suffix-free GNU invocation. [FreeBSD sed manual](https://man.freebsd.org/cgi/man.cgi?apropos=0&manpath=FreeBSD+4.10-RELEASE&query=sed&sektion=1). The cited archive documents the syntax; host support was independently checked in the installed manual.
- Python's default `os.path.realpath()` may return a partly unresolved path after a missing component, symlink loop, or other OS error. `strict=True` rejects missing components too; `ALLOW_MISSING` is version-specific. A substitute used for deletion safety must distinguish intended absence from loop/permission/non-directory failures rather than silently accepting the default result. [Python 3.13 os.path documentation](https://docs.python.org/3.13/library/os.path.html#os.path.realpath).
- AnyIO's public example explicitly imports `to_thread` and calls `to_thread.run_sync`. The same documentation explains custom limiters and cancellation shielding. This supports an import-only fix while retaining the existing limiter and default cancellation behavior. [AnyIO worker-thread documentation](https://anyio.readthedocs.io/en/stable/threads.html).
- AnyIO 4.14.2's initializer does not explicitly re-export `to_thread`, and its fallback `__getattr__` is typed as returning `type[BrokenWorkerInterpreter]`. The installed version shows the same signature at `.venv/lib/python3.13/site-packages/anyio/__init__.py:103`; this explains Ty's otherwise surprising diagnostic. [AnyIO 4.14.2 initializer](https://raw.githubusercontent.com/agronholm/anyio/4.14.2/src/anyio/__init__.py).
- Git documents that `core.symlinks=false` materializes tracked links as small target-text files. Here the Git objects themselves also have regular-file modes; both the durable representation and checkout behavior matter. [Git core.symlinks documentation](https://git-scm.com/docs/git-config#Documentation/git-config.txt-coresymlinks).

No dependency upgrade, external issue workaround, or newly installed GNU utility is required by this evidence. Failed web fetches were not used as evidence beyond accessible official search extracts.

## Existing Coverage

Existing installer tests cover complete install and project VERSION preservation (`tests/test_install_script.py:91`), legacy CLAUDE state preservation (`:139`), default refusal and force-backup (`:156`, `:173`, `:194`, `:217`), correct links (`:238`), native-content updater backup (`:256`), settings preservation (`:281`), outside and inside parent-link rejection (`:302`, `:316`), nonregular project-file rejection (`:332`), namespaced version state (`:346`), link generation (`:373`), old-native migration (`:399`), legacy state migration (`:471`), no swap debris (`:504`), and injected mid-swap rollback (`:534`).

The existing old-native migration test already exercises the masked sed failure: it changes hook strings to `.claude/hooks/` at `tests/test_install_script.py:436` and verifies both removal of old paths and presence of new paths at `:457`. The failure is not missing happy-path coverage; the early installer abort prevented reaching it.

No explicit tests for symlink loops, dangling leaf links escaping the target, deprecated-cleanup containment, resolver permission failure, or fresh clone link modes were found in this installer suite. Parent-link tests preserve important safeguards but do not substitute for those edge cases when the resolver changes.

Server tests assert strict-before daily data (`tests/test_tickreplay_server.py:355`), valid-empty versus unavailable (`:443`), client errors (`:486`), dedicated bounded limiter independence (`:492`), and coalescing/retry (`:549`). The limiter test patches `daily_context.load_daily_context`, not `to_thread.run_sync`; no mock-identity requirement was found for choosing the explicit import form.

Writer tests cover placeholder filling (`tests/test_update_design.py:234`), duplicate IDs (`:256`), idempotency (`:301`), hash conflicts (`:398`), validation before atomic replacement (`:436`), malformed tables (`:473`), and missing headings/documents (`:491`, `:502`). Keep these assertions; provide the failing placeholder test with the exact blank fixture it claims to test. Whole-suite coverage percentage was not measured in this investigation.

## Regression Risks

1. **High impact — path containment and deletion.** A lexical-only or permissive resolver can miss symlink escapes, accept unresolved loops, or mishandle prefix siblings. Keep physical resolution, boundary-aware containment, root-itself refusal, symlink-parent rejection, and explicit error handling. Preserve missing-component support needed by clean installs. Existing check/use races are not fixed merely by changing a utility.
2. **Medium — bootstrap runtime dependency.** Installer/updater currently have no Python invocation or explicit Python prerequisite. A Python helper adds a prerequisite; the host's `python3` is 3.9.6 while `uv run python` is 3.13.11. Do not introduce union-annotation or recent-stdlib startup failures into shell bootstrap. Use compatible syntax, an early clear prerequisite/fallback policy, and ship a helper before use.
3. **Medium — settings and update recoverability.** Replacing sed with temp-file/rename must preserve permissions, unrelated settings, failure reporting, and repeatability. Test write/rename failure if new mutation paths are added; retain existing stage-swap rollback assertions.
4. **Medium — durable links and user content.** Repair only exact known placeholders locally; do not overwrite different user content. Existing updater backup behavior is part of the contract. Confirm resulting file modes in the diff and eventual committed tree, not only `Path.is_symlink()` in the current working directory. A symlink-incapable checkout remains a separate host constraint.
5. **Low — AnyIO import.** Keep the call and explicit limiter unchanged; replacing the threading API or changing cancellation defaults is unnecessary and could affect daily request responsiveness/coalescing.
6. **Low — fixture and formatting.** Isolate the one blank-template scenario or create a deterministic fixture that still satisfies all writer contracts. Do not clear the real DESIGN, overwrite populated Performance rows, remove UTF-8 assertions, or skip failing tests.

## Codex Risk Analysis

Consultations used `gpt-5.6-sol` through the read-only shared wrapper with distinct labels and 180-second limits. The wrapper persisted prompts even when supplied through stdin. The lead verified CLI 0.154.0 matched the registry preflight.

- `preflight-impact-regression`: produced a HIGH assessment in `.agents/logs/codex/20260913T115323Z-preflight-impact-regression.md`; the first tool orchestration retained the response but did not retain the session completion envelope, so a bounded repeat was requested for explicit completion verification. Its first prompt overgeneralized `readlink --` incompatibility; host probes supersede that premise. The response also mistakenly described the checker as repairing links and loosely attributed regular files to checkout configuration; those statements are rejected based on source and Git evidence.
- `preflight-impact-fix-safety`: `ok=true`, exit 0, no timeout, 44.888 seconds; CAUTION. Full response: `.agents/logs/codex/20260913T115350Z-preflight-impact-fix-safety.md`. Verified against source: dependency introduction, path error handling, preservation of user files/settings modes, limiter semantics, and meaningful fixture coverage. The suggestion that settings replacement may alter unrelated strings is a prospective concern; the proposed fix should preserve the current exact hook-prefix substitution scope.
- `preflight-impact-regression-confirm`: `ok=true`, exit 0, no timeout, 31.617 seconds. This repeated the corrected safety prompt, returning CAUTION, rather than the original regression prompt; it is corroborating safety analysis, not a separate regression protocol result. Response: `.agents/logs/codex/20260913T115537Z-preflight-impact-regression-confirm.md`.
- `preflight-impact-regression-verified`: `ok=true`, exit 0, no timeout, 28.491 seconds; MEDIUM overall with high-impact containment/deletion risk. Full response: `.agents/logs/codex/20260913T115632Z-preflight-impact-regression-verified.md`. This explicitly completed the original regression protocol. Its repeated loose attribution of recorded file mode to `core.symlinks=false` is rejected; its additional advice not to delete modified deprecated paths is not treated as an existing product contract. Scope remains preserving current validated cleanup behavior. Risk rankings differ, but both regression responses identify the same principal path-safety safeguards.

The lead's earlier 600-second initial-analysis timeout produced no usable response and contributes no evidence. Advisory claims above were independently checked; no consultation claims that an unimplemented fix has passed tests.

## Recommended Safeguards

Implement in bounded groups: shell portability and safety regressions; exact link restoration; import-only change; isolated fixture and mechanical formatting. Keep independent source ownership and do not update dependencies or the live project design to satisfy these failures.

For path resolution, the Root Cause Analyst recommends a compatible stdlib helper that permits missing components but fails closed on other errors for destination validation and cleanup, plus `-L` and Bash `-ef` for existing CLAUDE/AGENTS identity. This reduces how many sites need missing-path resolution. The bootstrap prerequisite concern must be addressed explicitly in implementation.

Required verification after implementation: all existing installer/update scenarios including late sed migration and injected rollback; exact symlink targets and diff modes; writer placeholder regression plus adjacent writer tests; daily limiter/coalescing and API tests; Ruff/Ty; `.agents/check.sh`; and one final full preflight run owned by the lead. Add focused resolver cases for absent descendants, internal/external links, dangling escapes, loops, sibling-prefix paths, and relevant error handling. Test Darwin and GNU/Linux utility behavior where executable environments are available; do not claim a host was verified from documentation alone.

## Remaining Unknowns

- No real Linux or Windows shell execution was performed by this investigator. A native Windows support guarantee is not established by current shell docs.
- The exact final portable implementation and helper prerequisite policy remain for implementation; this report assesses candidate approaches, not completed code.
- Current Git history does not reveal why symlink placeholders were initially committed or when local `core.symlinks=false` was set.
- File permission/rename-failure and path-loop behavior needs executable verification against the chosen resolver/settings implementation.
- Python feature review and test coverage evaluation remain paused at the user's direction.

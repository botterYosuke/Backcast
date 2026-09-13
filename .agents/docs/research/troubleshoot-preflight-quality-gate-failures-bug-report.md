## Bug Report: Preflight quality gate failures

### Error
- Message: `bash .agents/skills/_shared/verify.sh` returns exit 2 with `overall: fail`; Ruff check, Ruff format, Ty, and pytest all fail. Pytest reports `21 failed, 1208 passed, 1 warning`.
- Location: the first common installer failure occurs at `scripts/install.sh:179`; the other independent failures originate at `src/tickreplay/server.py:602`, `tests/test_orchestration_contract.py:81-86`, `tests/test_orchestration_contract.py:110-136`, and `tests/test_update_design.py:234-253`.
- Stack trace: no CPython traceback was extracted. The pytest assertion/subprocess details are recorded in `.agents/logs/verify.log`; the command-level reproduction is in `.agents/logs/troubleshoot-repro-preflight-quality-gate-failures-initial.log`.

### Reproduction
- Steps:
  1. Run `uv sync --all-extras`.
  2. Run `bash .agents/skills/_shared/verify.sh` from the repository root.
  3. Observe Ruff check/format and Ty exit 1, followed by 21 pytest failures.
- Reproducibility: always on the current macOS checkout; the installer subgroup is environment-specific because Darwin `/bin/realpath` does not implement GNU `-m` or the `--` form used by the script.

### Immediate Context
- Failing code: `scripts/install.sh:179` calls `realpath -m --` before all 18 failing install/update test scenarios reach their intended branch. Related GNU-style option forms exist in `scripts/install.sh`, `scripts/update.sh`, and `.agents/check.sh` and may become the next macOS failure after the first line is fixed.
- Call chain: `verify.sh` -> `uv run pytest -q` -> `tests/test_install_script.py` -> installer/update subprocess -> `scripts/install.sh:179`. Independent chains are `verify.sh` -> Ruff over changed/test files, `verify.sh` -> Ty -> `src/tickreplay/server.py:602`, and `verify.sh` -> orchestration/update-design assertions.
- Recent changes: Python strategy feature commit `26cc86ec` is compared with `main`/`origin/main` at `93c239d5`; the failing lines are unchanged from main. Commit `9e52cb54` introduced the older GNU-dependent installer/link handling. The failures are therefore baseline defects exposed by running the full gate, not changes introduced by the current feature.

### Affected Area
- Files involved: `scripts/install.sh`, `scripts/update.sh`, `.agents/check.sh`, `CLAUDE.md`, `.claude/agents`, `.claude/skills`, `tests/test_install_script.py`, `tests/test_orchestration_contract.py`, `tests/test_update_design.py`, `tests/test_collect_repo_state.py`, `tests/test_tickreplay_minute_context.py`, and `src/tickreplay/server.py`.
- Related tests: 18 tests in `tests/test_install_script.py` fail from the shared `realpath` entry failure; `test_claude_md_is_symlink_to_root_agents_md` fails because `CLAUDE.md` is a regular file; `test_native_runtime_directories_only_keep_native_settings` fails because `.claude/agents` and `.claude/skills` are regular files; `test_an_unfilled_template_row_is_completed_in_place` fails because its fixture copies the live, populated DESIGN document while asserting that a blank Performance row exists. The captured Python strategy feature tests pass.

### Initial Hypotheses (informed by Codex analysis)
1. **Confirmed by reproduction and broad-context analysis: shell portability defect.** The installer assumes GNU coreutils option behavior that macOS utilities do not provide. A safe fix must cover the full related option family, not only `scripts/install.sh:179`, or failures will move to the next incompatible command.
2. **Confirmed repository-state contract drift.** `AGENTS.md` requires `CLAUDE.md` and `.claude/{agents,skills}` to be discovery symlinks, while the current checkout contains regular files. The repair should restore the contract without overwriting user content.
3. **Confirmed test isolation defect.** `test_an_unfilled_template_row_is_completed_in_place` depends on mutable live `.agents/docs/DESIGN.md`; the test should create an explicit minimal template fixture containing the blank row it intends to exercise.
4. **Confirmed mechanical static-analysis defects.** Ruff finds one unused `pytest` import and two unformatted test files. Ty requires an explicit `anyio.to_thread` import at the application boundary.
5. **Codex status:** the mandatory read-only initial consultation timed out after 600 seconds and returned zero response characters. No Codex conclusion is used as evidence; the hypotheses above rely on the captured reproduction and independently verified context analysis.

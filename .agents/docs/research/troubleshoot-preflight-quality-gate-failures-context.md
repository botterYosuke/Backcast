# Preflight Quality Gate Failure Context

## Reproduction Summary

- The authoritative reproduction was `bash .agents/skills/_shared/verify.sh`; it
  exited 2 and reported all four configured gates as failed
  (`.agents/logs/troubleshoot-repro-preflight-quality-gate-failures-initial.log:1-24`).
- The gate runner executes `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run ty check src/`, and `uv run pytest -q`
  (`.agents/skills/_shared/verify.sh:155-200`). The synchronized environment was
  macOS/Darwin with Python 3.13.11, pytest 9.0.2, and AnyIO 4.14.2
  (`.agents/logs/verify.log:40-46`; `uv.lock:24-33`).
- Pytest collected 1,229 tests and ended with 21 failed / 1,208 passed / 1
  warning in 470.73 seconds (`.agents/logs/verify.log:40-46,247-269`). No full
  gate or global pytest rerun was performed during this investigation.

## Gate Failure Classification

| Gate | Classification | Exact evidence | Immediate action surface |
|---|---|---|---|
| `ruff check` | One deterministic lint defect | Unused `pytest` import at `tests/test_tickreplay_minute_context.py:14`; Ruff reports F401 at `.agents/logs/verify.log:4-16`. | Remove the unused import; no behavior change is involved. |
| `ruff format --check` | Two deterministic formatting defects | The captured gate names `tests/test_collect_repo_state.py` and `tests/test_tickreplay_minute_context.py` (`.agents/logs/verify.log:18-23`). The first mismatch is the long nested `emit_utf8` signature at `tests/test_collect_repo_state.py:95`; the second starts with long tuple literals at `tests/test_tickreplay_minute_context.py:19-22` and recurs at lines 91, 124-125, 134-135, and 144. | Run Ruff formatting only on the two named tests. |
| `ty check src/` | One application/import typing defect exposed by the AnyIO package API | `src/tickreplay/server.py:602` accesses `anyio.to_thread.run_sync`; Ty resolves `anyio.to_thread` as `type[BrokenWorkerInterpreter]` and reports no `run_sync` attribute (`.agents/logs/verify.log:25-35`). | Make the `to_thread` submodule/function import explicit at the application boundary. |
| `pytest` | Four root symptoms across 21 tests, of which one symptom accounts for 18 tests | Full list is in `.agents/logs/verify.log:248-268`; grouping and flows are below. | Fix portability, restore required repository symlink types, and isolate the writer unit-test fixture from the mutable project design document. |

The one pytest warning is not a gate cause: Starlette/FastAPI reports a future
`httpx2` deprecation from installed package code
(`.agents/logs/verify.log:241-246`).

## Pytest Failure Groups

### Group 1: 18 installer tests abort before reaching their assertions

All 18 tests in `tests/test_install_script.py` fail with the same stderr:
`realpath: illegal option -- m` (`.agents/logs/verify.log:95-221`), and all 18
names are enumerated in the summary (`.agents/logs/verify.log:248-265`). The
individual expected exit codes vary (0 or 2), but the installer returns 1 before
the tested behavior runs (`.agents/logs/verify.log:95-221`).

Execution flow:

1. `run_install()` invokes `bash scripts/install.sh --yes ...`
   (`tests/test_install_script.py:17-27`). Update-oriented cases first use the
   same installer through `build_template_repo()` and `run_install()`
   (`tests/test_install_script.py:30-59,256-263,346-355,399-405,471-476,504-512,534-540`).
2. `main()` calls `validate_destination_paths()` before conflict handling,
   copying, or the behavior-specific assertion paths
   (`scripts/install.sh:450-468`).
3. The first managed destination reaches GNU-specific `realpath -m --` at
   `scripts/install.sh:179`; Darwin's `/bin/realpath` rejects `-m`, producing
   the captured exit 1 before any intended success/refusal/update behavior.

This is a cross-platform shell defect, not 18 independent behavioral
regressions. There is a second-wave portability surface that the current early
abort hides: the same GNU-only `realpath -m --` pattern exists at
`scripts/install.sh:321`, `scripts/update.sh:324`, `scripts/update.sh:611`, and
`.agents/check.sh:299`. BSD-incompatible `readlink --`, `rm ... --`, `cp ... --`,
and `mv ... --` forms also occur at `scripts/install.sh:232,290,311,324,335,338`
and `scripts/update.sh:336-370,615,632-645`. A fix limited to
`scripts/install.sh:179` is therefore likely to expose the next macOS failure
rather than clear the installer/update contract.

### Group 2: `CLAUDE.md` is not the required symlink

`tests/test_orchestration_contract.py:81-86` requires `CLAUDE.md` to be a
symlink resolving to root `AGENTS.md`; the captured failure is the first
`is_symlink()` assertion at line 84 (`.agents/logs/verify.log:222-227`). The
current `CLAUDE.md:1` is instead a regular, nine-byte file containing the text
`AGENTS.md`. This also violates the declared repository boundary in
`AGENTS.md` and bypasses the repair behavior implemented at
`scripts/install.sh:317-327` / `scripts/update.sh:605-617`.

### Group 3: native discovery entries are regular files, not symlinks

`tests/test_orchestration_contract.py:110-136` permits only native settings as
real `.claude/` files and requires `agents`/`skills` to be the exact discovery
symlinks. The failure identifies both as unexpected real files
(`.agents/logs/verify.log:228-235`). Their line-1 contents are merely link-target
text (`.claude/agents:1` contains `../.agents/agents`; `.claude/skills:1`
contains `../.agents/skills`) rather than filesystem links. The intended links
are defined at `scripts/install.sh:22-31` and created at
`scripts/install.sh:329-342` / `scripts/update.sh:620-646`.

### Group 4: the update-design test assumes mutable repository state is still blank

The failing assertion expects the NFR table row count not to increase
(`tests/test_update_design.py:234-253`; failure at
`.agents/logs/verify.log:236-240`). Its fixture copies the live repository
`.agents/docs/DESIGN.md` for every test (`tests/test_update_design.py:26-48`),
while its docstring assumes the copied file still contains
`| Performance | | |` (`tests/test_update_design.py:234-236`). In reality,
`.agents/docs/DESIGN.md:54` already contains a fully populated Performance row;
the remaining placeholders have non-matching category labels at lines 55-57.

The writer behaves consistently with its contract: `_place_row()` only fills a
same-label row when all cells after the label are empty
(`.agents/skills/_shared/update_design.py:331-336`), otherwise fills a wholly
blank row (`.agents/skills/_shared/update_design.py:343-346`) or appends
(`.agents/skills/_shared/update_design.py:348-349`). Because no blank
Performance placeholder exists, the supplied new Performance row is appended
through `_apply_table_rows()` (`.agents/skills/_shared/update_design.py:444-477`),
making 8 rows from 7. The immediate defect is the test fixture/assumption, not
the row-placement implementation demonstrated by this failure.

## Immediate Causes and Execution Flow

1. **Installer portability:** every installer test is stopped by the first
   GNU-only path-resolution call at `scripts/install.sh:179`; no tested install
   branch is reached (`scripts/install.sh:450-468`; `.agents/logs/verify.log:95-221`).
2. **Repository metadata/type drift:** the three paths mandated as symlinks are
   committed/materialized as ordinary files, directly triggering the two
   orchestration assertions (`tests/test_orchestration_contract.py:81-86,110-136`;
   `.agents/logs/verify.log:222-235`).
3. **Mutable-fixture coupling:** `test_update_design.py` copies the evolving
   project design at fixture setup (`tests/test_update_design.py:42-48`) but
   asserts an original blank-template condition (`tests/test_update_design.py:234-253`).
4. **Implicit AnyIO submodule access:** application code imports only `anyio`
   (`src/tickreplay/server.py:35`) and later relies on `anyio.to_thread` being
   available (`src/tickreplay/server.py:602`). AnyIO's public initializer does
   not statically export `to_thread`; its fallback `__getattr__` is annotated to
   return `type[BrokenWorkerInterpreter]`, which explains Ty's exact inferred
   type in the captured diagnostic (`.agents/logs/verify.log:28-35`). The
   `run_sync` function itself lives in AnyIO's `to_thread` module; the code
   should import that module/function explicitly rather than depend on incidental
   package-submodule loading.
5. **Mechanical lint/format drift:** the F401 and formatting mismatches are
   confined to the exact unchanged test lines listed in the gate classification
   (`.agents/logs/verify.log:4-23`).

## Regression vs Baseline Assessment

These failures are baseline/platform/orchestration defects, not regressions
introduced by commit `26cc86ec` (`feat(tickreplay): add browser Python replay
strategies`), based on the following repository evidence:

- The HEAD and `main` Git object IDs are identical for
  `scripts/install.sh`, `tests/test_install_script.py`,
  `tests/test_orchestration_contract.py`,
  `.agents/skills/_shared/update_design.py`, `tests/test_update_design.py`,
  `tests/test_collect_repo_state.py`,
  `tests/test_tickreplay_minute_context.py`, and `uv.lock`. Thus the 18
  installer failures, two orchestration failures, writer test failure, Ruff
  failures, and locked AnyIO/tool versions all exist unchanged on `main`.
- The feature does modify `src/tickreplay/server.py`, but not the failing AnyIO
  import or call: `import anyio` remains at `src/tickreplay/server.py:35`, and
  `_run_daily_context()` still reaches the pre-existing call at line 602. The
  feature diff adds strategy static-serving code elsewhere in that file.
- The feature modifies `.agents/docs/DESIGN.md`, but only adds the CodeMirror
  technology choice and decision (`.agents/docs/DESIGN.md:93,128`). The already
  populated Performance row responsible for the brittle test is also present
  on `main` at `.agents/docs/DESIGN.md:54`.
- The captured run shows the feature-facing Python suites themselves passing:
  browser spike tests, cloud-run tests, tickreplay server tests, and all 21
  strategy-runner tests complete without a failure marker
  (`.agents/logs/verify.log:48-55,82-85`). This does not substitute for the
  postponed feature review, but it argues against attributing the 21 preflight
  failures to the Python-strategy behavior.
- The symlink representation predates the feature: all three entries have been
  regular-file Git objects since the initial repository commit, matching the
  captured contract failures (`.agents/logs/verify.log:222-235`). The local Git
  configuration also has `core.symlinks=false`; both the committed file modes
  and checkout configuration must be considered when repairing and verifying
  them.

## Related Patterns

- The installer and updater intentionally share the same entrypoint/discovery
  invariants (`scripts/install.sh:317-342`; `scripts/update.sh:605-646`). Their
  path comparison and command-option portability should be corrected as one
  family, and `.agents/check.sh:299-320` should use the same portable convention
  so its own contract check does not disagree by platform.
- `tests/test_tickreplay_server.py:520,533-534` also uses
  `anyio.to_thread` through the package object. Those tests are outside the Ty
  scope (`src/` only, `.agents/skills/_shared/verify.sh:177-188`), so they do not
  currently fail the type gate; they demonstrate the same implicit-submodule
  pattern and are useful focused regression coverage for thread-pool behavior.
- Other tests avoid mutable project documents by constructing the exact
  document shape needed for the assertion; for example,
  `tests/test_collect_repo_state.py:251-278` writes its own minimal DESIGN
  content. The update-design placeholder test should follow that pattern rather
  than depend on today's live `.agents/docs/DESIGN.md` contents.
- Ruff's configured target and rule set are repository-wide
  (`pyproject.toml:39-60`), and the feature's only Ruff configuration change is
  the explicit exclusion of the content-frozen generated fixture at
  `pyproject.toml:43-45`; it does not suppress or create the reported test-file
  findings.

## Dirty Worktree Risks

- Before this research artifact was created, `git status --porcelain=v2
  --branch` reported only branch metadata (`feat/code` at `26cc86ec`) and no
  tracked, staged, or untracked worktree changes. `uv sync --all-extras` did not
  modify `pyproject.toml` or `uv.lock`.
- `.env` exists but is ignored by `.gitignore:7`. It was not read or modified.
  The retained state explicitly records a concurrent developer change and says
  tests must not inherit it (`.agents/STATE.md:127-137`); any fix/retest should
  preserve and isolate that file.
- The three regular-file link placeholders are tracked baseline content, not
  dirty changes. Repairing them changes Git object modes/content and must not be
  confused with cleanup of unrelated local work.

## Unknowns

- The captured run cannot show which later BSD/GNU portability failure appears
  first after `scripts/install.sh:179` is corrected; the related option patterns
  above should be addressed or covered together before declaring the installer
  portable.
- The intended supported host matrix for `scripts/install.sh`,
  `scripts/update.sh`, and `.agents/check.sh` is not explicitly stated in
  `.agents/docs/DESIGN.md`, which is still partially an uninitialized template
  (`.agents/docs/DESIGN.md:10-27`). The current pytest suite nevertheless runs
  those scripts on the host and therefore operationally requires Darwin support.
- It remains to be decided whether repository repair should also set/require a
  symlink-capable Git checkout configuration or merely repair the current three
  entries. The existing tests require actual filesystem symlinks
  (`tests/test_orchestration_contract.py:81-86,125-136`).
- Feature code review and coverage analysis remain intentionally paused; this
  artifact only diagnoses the preflight baseline that blocked that review.

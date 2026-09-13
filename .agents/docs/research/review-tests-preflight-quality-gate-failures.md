# Test Review: Preflight Quality Gate Fixes

## Verdict

PASS WITH NOTES. No ship-blocking coverage gap found. Gap counts: High 0,
Medium 0, Low 3. Coverage: not measured; the supplied gather-diff value is null
and no numerical coverage estimate is inferred from test counts.

This sequential reviewer role reused the earlier security/quality reviewer
because the runtime rejected additional threads. It is a separate review
perspective, not an additional independent agent identity.

## Findings

### T1 — Low, nonblocking: Explicit destination-link merge boundaries are absent

- File/function: `tests/test_path_safety.py:44`,
  `test_legacy_merge_preserves_conflicts_and_copies_missing_entries`.
- Current coverage: regular-file conflict, nested real directories, hidden and
  missing entries, a dangling source symlink, backup contents, and copy failure.
- Missing cases: a nested destination symlink to an outside directory with a
  sentinel, and a dangling destination symlink. Assert each link is unchanged,
  its referent receives no writes, and the conflicting source entry is not
  copied through it. A directory-source/regular-file-destination conflict would
  also pin the existing skip semantics.
- Why nonblocking: `scripts/update.sh:405,407` explicitly refuses recursion
  through source/destination links and treats dangling links as existing;
  inspected code is correct, but these new branches deserve direct regression
  protection.

### T2 — Low, nonblocking: Sed failure is only injected during capability probing

- File/function: `tests/test_install_script.py:737`,
  `test_settings_sed_capability_selection`.
- Missing case: a shim that succeeds on the disposable probe but exits nonzero
  before modifying the real settings file. Assert nonzero script status,
  unchanged bytes for this injected pre-write failure, and absence of later
  completion/version-update effects where integration permits.
- Why nonblocking: the unchanged fixed transformation and `set -e` propagation
  are straightforward, and both valid argument forms plus idempotence and basic
  metadata are tested. The current broken flavor cannot detect accidentally
  swallowed production edit errors because it exits at the probe. This does
  not justify claiming universal native-sed storage-failure atomicity.

### T3 — Low, nonblocking: Cycle tests lack a subprocess deadline

- File/function: `tests/test_path_safety.py:33`, `check`, as used by
  `test_symlink_chains_and_dangling_targets` at line 113.
- Missing condition: a finite `subprocess.run(..., timeout=...)` boundary for
  the helper subprocess. If a regression removes or breaks cycle termination,
  these cases can hang the suite instead of producing a bounded failure.
- Suggested: give the helper subprocess a short but practical deadline; retain
  the existing exact exit-2 and empty-output assertions. Do not replace the real
  cycle with a mocked successful result.
- Why nonblocking: current implementation bounds link traversal correctly and
  lead's complete suite terminated successfully. This strengthens detection of
  a future regression rather than fixing present behavior.

## Coverage Evidence and Mutation Sensitivity

| Surface | Concrete coverage | Broken behavior the assertions detect |
|---|---|---|
| Path decisions | `test_path_safety.py:94,106,113,132,149` | Allowing root/outside/prefix siblings, missing-path rejection, unsafe controls, symlink escape/cycle, or swallowing EACCES/ENOTDIR/ELOOP changes exact expected statuses. |
| Bootstrap failures | `test_install_script.py:642` | Missing/corrupt/noisy/NUL/unknown/always-safe helper and missing/old/broken interpreter must fail both install and update without changing target snapshots. |
| Root path transport | `test_install_script.py:613`, `test_path_safety.py:19` | Physical newline root, including installer alias path, must be refused before mutation or trailing-newline normalization can target another directory. |
| Actual cleanup | `test_install_script.py:697` | Outside/root links and outside sentinel must survive while a safe deprecated directory is removed; indiscriminately deleting or skipping everything fails. |
| Helper provenance | `test_install_script.py:719` | Removing the installed helper still permits update and safe cleanup, exposing accidental dependence on the old installed copy. |
| Sed portability | `test_install_script.py:737,408` | GNU/BSD argv shims still call the host sed; exact content and unrelated setting preservation prevent a no-op or constant result from passing. Existing end-to-end migration reaches the real settings call. |
| Conflicts/backups | `test_install_script.py:165,182,203,226,265` | Default refusal and backup-before-replace preserve original data; generated links must have correct types/targets. |
| Swap failure | `test_install_script.py:543` | A real second-rename failure must return nonzero, restore the listing/content and remove stage/swap debris. |
| Repository links | `test_orchestration_contract.py:81,110` | Actual link types, resolved content and exact discovery targets reject text placeholders or incorrect links; no assertion was weakened. |
| Helper contract | `test_shared_script_contract.py:292` | Status-only protocol is separately checked for inside/root/outside/relative/bare/help/unknown calls, exact stdout, and no writes; generic safety/doc checks retain the helper. |
| DESIGN fixture | `test_update_design.py:234` | Explicit blank input retains both unchanged row count and inserted requirement text; an append-only or no-op writer fails. |
| Import/runtime | `test_tickreplay_server.py:492,549` plus Ty | Dedicated limiter/coalescing tests exercise real AnyIO offload and concurrency behavior. Ty covers the explicit submodule import boundary; no redundant import-spelling unit test is needed. |

## Test Quality

Tests generally follow arrange/action/assert with descriptive names. Installer
and updater cases use isolated temporary Git repositories and local template
origins; PATH shims inject precise external-command failures. Permission-error
injection avoids privilege-dependent chmod tests, while real filesystem cases
cover links and nondirectory components. Sed shims emulate argv parsing but use
the host implementation to transform bytes, so they do not stub the result.

The corrected DESIGN case owns its necessary blank fixture and keeps production
writer assertions. Other writer tests intentionally continue integration with
the copied repository design. Existing minute-context tests use MockTransport,
verify chronological/strict-before data, cache reuse, and best-effort remote
failure behavior; the diff only reformats their data/calls and removes an unused
import. The UTF-8 test retains encoding/error assertions. No added skips,
deleted assertions, blanket gate exclusion or fixed success shortcut was found.

Function extraction via splitting the update script before `main` couples two
focused tests to the script's entrypoint text, but exercises its actual function
definitions. Existing end-to-end updater tests cover wiring omitted by those
drivers. Snapshot helper intentionally excludes `.git` and records bytes/link
targets rather than mode/ACL/xattr metadata; it proves its stated content
preservation, not arbitrary filesystem metadata invariance.

## Test Execution Results

Lead-provided independent gate evidence: 1,275 tests passed; Ruff check and
format passed; Ty passed; check.sh passed 8/8. No pytest or global suite was
rerun by this reviewer. Coverage: not measured.

Performed only the following independent, read-only system-Python smoke probes:

| Command | Result |
|---|---|
| `/usr/bin/python3 --version` | `Python 3.9.6`, exit 0 |
| `/usr/bin/python3 -I .agents/skills/_shared/path_safety.py /Users/sasac/backcast /Users/sasac/backcast/scripts` | exit 0, empty captured output |
| `/usr/bin/python3 -I .agents/skills/_shared/path_safety.py /Users/sasac/backcast /Users/sasac` | exit 1, empty captured output |
| `/usr/bin/python3 -I .agents/skills/_shared/path_safety.py /Users/sasac/backcast relative` | exit 2, empty captured output |

These establish Python 3.9 startup/basic status compatibility independently of
the implementer's self-report. They do not establish the entire edge-case suite
under explicitly pinned 3.9. `uv run pytest` may prepend the project interpreter
to PATH, so a test's unqualified `python3` call alone does not establish the
subprocess version. Full native Linux execution and unsupported link filesystems
remain unverified, not silently emulated by the GNU/BSD shims.

## Validation

Assigned work log validated with the shared `work-log` contract, reviewer
variant, no missing sections/metadata and no warnings. Report was checked against
the assigned scope and exact test functions; no dedicated test-review contract
exists in the document validator. Only the report and assigned work log were
written during this role.

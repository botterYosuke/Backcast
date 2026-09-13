# Security Review: Preflight Quality Gate Fixes

## Verdict

PASS for the supplied gate-fix diff. No new blocking security defect was found.
New findings: Critical 0, High 0, Medium 0, Low 0. This is a scoped change review,
not certification that the entire existing updater is safe against hostile
filesystem changes or that every failure leaves the whole project unchanged.

Reviewed the complete supplied patch, including all four diagnostic research
documents, STATE, check.sh, shared README/helper, the three link conversions,
installer/updater, server import, and all six changed/new test files. Current
surrounding mutation paths were also inspected to assess the actual boundaries.

## Findings

No introduced blocking or nonblocking security findings. Existing limitations
and evidence gaps are classified separately below; they do not require expansion
of this portability repair before resuming the feature review.

## Security Boundaries Verified

- `.agents/skills/_shared/path_safety.py:25` rejects relative paths and ASCII
  control characters; line 48 applies the same restriction to symlink targets.
  `resolve_missing` follows relative and absolute symlinks using `lstat` and
  `readlink`, tolerates only missing components, bounds link traversal at 40,
  and rejects a nondirectory with remaining components (lines 39, 45, 54).
  Root equality and `commonpath` at line 67 prevent root deletion and sibling
  prefix confusion. Errors return status 2 rather than an apparently safe path.
- `scripts/install.sh:78` and `scripts/update.sh:129` pass quoted arguments to
  an isolated Python interpreter. They capture raw stdout to a file and reject
  any nonempty output, including newlines and NUL bytes, and any status other
  than 0 or 1. No path is reconstructed from shell output, evaluated as shell
  code, or interpolated into the sed expression. The root/child self-tests
  reject a trivial always-safe helper.
- `scripts/update.sh:139` obtains the helper from the already fetched template,
  not the installed copy. Main calls helper and sed preflight before the first
  project migration (lines 850–852). The installer likewise preflights before
  destination validation and conflict handling (`scripts/install.sh:498`).
  Interpreter absence/incompatibility and the tested helper corruptions stop
  before target content changes.
- Installer parent-symlink rejection remains at `scripts/install.sh:218`;
  project-file type rejection and conflict refusal/backup remain intact.
  Deprecated cleanup preserves its lexical restrictions and checks containment
  at `scripts/update.sh:382` before `git rm` or `rm` at lines 389–392. Errors
  abort; known outside/root results skip the unsafe deletion.
- `scripts/update.sh:405` descends only through two real directories; it never
  recurses through a nested source or destination symlink. Existing destinations,
  including dangling links, are preserved at line 407. Missing entries are
  copied with `cp -a --`, preserving links as links and quoting filenames.
  Existing backup creation precedes the merge at line 437. Copy errors are
  not hidden by a blanket `|| true`.
- GNU/BSD sed selection edits only a disposable fixture and verifies exact
  expected bytes (`scripts/update.sh:158`). Its array preserves BSD's required
  empty argument. The production expression is unchanged and fixed, with the
  settings path quoted (`scripts/update.sh:735`). No input becomes sed code.
- The three repository conversions have the exact intended relative targets
  and supplied patch modes of 120000; current filesystem inspection confirmed
  real links. Existing runtime discovery conflicts are still copied before
  replacement (`scripts/update.sh:712`). Existing CLAUDE identity uses `-L`
  plus `-ef`; it does not transport a potentially malformed resolved path.
- check.sh changes retain checks while replacing GNU-only syntax. Server change
  is an explicit submodule import only. Test fixture/format changes preserve
  assertions and production behavior. No new dependency, secret, credential,
  external request, or sensitive-content logging was introduced by the diff.

## Test Adequacy

The tests exercise real subprocess failure results and filesystem sentinels,
not just implementation text. `tests/test_path_safety.py:94` covers missing
descendants, root/outside/prefix boundaries and relative input; line 113 covers
multihop/dangling/escaping/cyclic links; line 132 covers nondirectory components;
line 149 injects EACCES, ENOTDIR and ELOOP. Injection is appropriate for permission
errors whose physical reproduction depends on execution privilege.

`tests/test_install_script.py:642` checks nine helper/interpreter failure classes
against snapshots for both update and install. Deprecated cleanup tests preserve
outside sentinels and unsafe links while confirming real safe deletion (line
697). The fetched-helper test removes the installed helper (line 719), so it
would expose incorrect helper provenance. The status-only contract exception
retains generic safety checks and adds explicit output/status/input assertions
(`tests/test_shared_script_contract.py:292`); it is not a blanket exclusion.

The merge regression checks existing data, hidden/missing entries, a dangling
source link, backup content and a propagated copy error. The sed tests exercise
both argument forms via shims, exact settings bytes, basic mode/uid/gid,
idempotence and no backup debris. They do not establish a complete native Linux
matrix, ACL/xattr/hardlink preservation, or late physical write-failure behavior.
Line references identify current files; passing full-suite evidence is supplied
by the lead, not an additional suite run performed by this reviewer.

## Residual Risks

1. **Medium, pre-existing and nonblocking for this diff:** The updater does not
   validate containment for every destination before its first mutation.
   `scripts/update.sh:424` handles top-level canonical migration paths, and
   line 469 constructs swap paths through existing parent directories without
   installer-style parent-symlink refusal. An already hostile `.agents` or
   `.claude` parent can affect paths outside the project. The newly validated
   cleanup boundary must not be described as a complete updater sandbox.
2. **Medium, explicitly accepted design limitation:** Resolution and subsequent
   shell mutation are separate operations (`path_safety.py:5`,
   `scripts/update.sh:382` and `:389`). Concurrent hostile rename/symlink swaps
   are not prevented. The fetched template and executable search path also
   remain trusted; status self-tests validate protocol, not cryptographic
   provenance or arbitrary malicious helper behavior.
3. **Low, existing durability/evidence limit:** Capability-selected sed preserves
   the existing in-place editing mechanism (`scripts/update.sh:735`); it does
   not introduce all-or-nothing update rollback or prove unchanged settings
   bytes on every storage failure. The broken-sed test fails at capability
   selection, not after production editing begins. Report these semantics
   accurately rather than claiming universal failure atomicity. Native Linux,
   symlink-incapable checkouts, and coverage percentage were not established by
   this security review.

## Validation

- Source/diff review and read-only current-link inspection completed.
- Lead-supplied evidence: verifier PASS, 1,275 tests passed, check.sh 8/8, no
  placeholder/weakened-test/out-of-scope findings from delegation collection.
- No global suite, installer, updater, network operation, or source mutation was
  run by this reviewer. Only this report and its assigned work log were written.
- Reviewer work log validated using the shared `work-log` document contract.

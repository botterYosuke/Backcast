# Quality Review: Preflight Quality Gate Fixes

## Verdict

PASS WITH NOTE. No blocking code defect found in the supplied complete gate-fix
patch. Findings: High 0, Medium 0, Low 1 (documentation). Review was performed
sequentially by the earlier security reviewer because the runtime rejected
additional reviewer threads; the independent Codex consultation is recorded below.

## Findings

### Q1 — Low, nonblocking: Current state overstates settings failure guarantees

- Location: `.agents/STATE.md:310` and `.agents/STATE.md:317`.
- Current: The active fix block says settings migration will be failure-atomic
  and preserve original bytes on every failure. The approved diagnosis instead
  deliberately retains existing native sed semantics. The implementation at
  `scripts/update.sh:735` capability-selects the GNU/BSD in-place form; it adds
  neither full-update rollback nor an independently validated universal
  storage-failure guarantee. `tests/test_install_script.py:737` covers broken
  capability selection and successful transformations, not late production I/O
  failures. Future handoffs could mistake the stronger prose for verified behavior.
- Suggested: Describe capability-selected native sed, unchanged existing
  durability semantics, pre-mutation capability failure, and the metadata/error
  cases actually verified. Update this as part of final state recording rather
  than adding a new settings-write engine.
- Blocking: No. This is a documentation correction; no settings behavior
  regression was reproduced and the transformation is unchanged.

## Correctness and Maintainability

- `path_safety.py` uses postponed annotations and Python-3.9-compatible stdlib
  calls. `resolve_missing` distinguishes missing components from EACCES, ENOTDIR
  and ELOOP. It resolves symlinks before subsequent dotdot components, correctly
  resets the component stack on absolute link targets, and bounds link traversal.
  Nondirectory components with a suffix (including slash or dotdot) fail rather
  than being normalized away. Root existence/type and boundary-aware containment
  are checked separately. Exit values are the documented contract, not mocked
  success substituted for resolution.
- The shell transport never parses path text from command substitution. A raw
  stdout capture rejects even otherwise invisible newline/NUL output, and exact
  status validation plus root/child self-tests rejects common corrupt helpers.
  The installer uses its source helper; the updater uses the fetched helper
  before replacing installed skills or beginning migration. Missing helper and
  incompatible interpreter errors remain actionable and precede target writes.
- Parent-link restrictions, project-file type checks, installer conflict refusal,
  backup and updater stage/swap rollback remain in place. The changed deprecated
  cleanup calls the helper and preserves meaningful safe deletion, rather than
  skipping cleanup to make tests pass. Existing updater-wide containment and
  concurrency limitations are not claimed to be solved.
- `merge_missing_entries` is a scoped replacement for BSD cp-n's nonzero
  conflict status. It includes dotfiles, preserves any existing destination,
  recurses only through real directory pairs, copies links without dereferencing
  them, and propagates copy failure. It preserves the preceding backup and
  re-runnable partial-merge semantics; it does not promise whole-merge rollback.
- GNU/BSD sed selection checks actual behavior on disposable files. An array
  preserves the BSD empty argument, the transformation is fixed, and a failed
  capability probe stops before project mutation. Duplicating the small helper
  adapter in two standalone bootstraps is reasonable here; comments explicitly
  identify their alignment requirement. Extracting another sourced shell
  bootstrap would increase delivery-order complexity without clear benefit.
- check.sh replaces nonportable regex/find forms without suppressing checks.
  Existing-link identity uses `-L` plus `-ef` while generated repository links
  retain exact relative targets. The three supplied file-mode conversions are
  100644 to 120000. AnyIO's explicit module import leaves offload/limiter calls
  unchanged. Formatting/import cleanup and the explicit blank DESIGN fixture
  are appropriately local.
- The shared-script status-only exception is named, documented and accompanied
  by output/status/input/no-mutation tests. Generic safety/documentation checks
  still include the helper. Existing assertions are retained; no broad skipped
  tests, success stubs, swallowed errors or hardcoded bypasses were found.

## Codex Consultation

Required bounded read-only consultation completed successfully with label
`quality-review-preflight-fix`, timeout 180, and low reasoning effort. Model:
`gpt-5.6-sol`; duration 136.057 seconds; exit 0; `ok=true`, `timed_out=false`,
`response_verified=true`; no write access. Full response:
`.agents/logs/codex/20260913T195919Z-quality-review-preflight-fix.md`.

Codex returned PASS with zero introduced code findings. Independently verified
against the current implementation: Python 3.9-compatible component resolution,
missing-only tolerance, strict status/no-stdout handling, fetched-helper and sed
preflight ordering, missing-entry merge and backup preservation, narrow contract
test exception, fixture isolation, and current relative-link types. Re-ran its
reported Bash syntax check successfully. Its existing updater containment,
concurrency and sed-transaction limitations match the reviewed code. This review
retains Q1 as an additional documentation finding that Codex did not report.

## Residual Risks

- Native Linux and symlink-incapable checkout execution are not established by
  this review. Shims prove argument selection, not the entire host matrix.
- The preflight resolver is not a defense against concurrent hostile filesystem
  changes. It does not retrofit installer-style parent validation into every
  existing updater mutation. Fetched template contents and PATH remain trusted.
- Native sed keeps its existing metadata/durability semantics; tests establish
  mode/uid/gid and no ordinary backup debris, not ACL/xattr/hardlink or every
  low-level I/O failure guarantee. Coverage percentage was not measured here.

## Validation

Complete supplied patch and relevant surrounding code inspected. `git diff
--check` and `bash -n scripts/install.sh scripts/update.sh .agents/check.sh`
passed. Lead-provided gate evidence: Ruff check/format PASS, Ty PASS,
1,275 pytest tests PASS, check.sh 8/8, ownership reconcile clean, guardrails
actionable 0. No global suite or source mutations performed by this reviewer.
Reviewer work log passed the shared `work-log` validator with reviewer variant,
no missing sections or metadata, and no warnings. No dedicated quality-review
report contract exists in `validate_doc.py`; its content was checked against the
assigned review format and exact cited files instead.

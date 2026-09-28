# Bundled RL default source scan

## Summary

The current application has two copies of a no-op strategy, always starts in
Pyodide mode, and restores a saved browser draft over the authored default.
Changing only the HTML or workspace constant will not meet the request for an
existing browser profile. The reported `print(event)` is not present in the
authored default, so it must be treated as a saved/user-modified draft rather
than assumed to be an exact legacy template.

Make the bundled RL strategy the initial selected strategy and Native Trace the
initial execution mode. Preserve the existing custom draft, with a visible way
to reopen it. Couple any automatic preparation to an actual loaded session and
its generation; mount alone must not submit an invalid or stale session.

## Current Behavior

- `src/tickreplay/static/index.html:173` contains the no-op textarea source.
- `strategy/strategy-workspace.mjs:42` exports the same
  `SAMPLE_STRATEGY_SOURCE`; mount uses the textarea value first, falling back to
  this constant only for an empty value (`:178`). The Sample action (`:909`)
  also uses that constant.
- `restoreStoredState()` (`:422`) replaces the source with any available stored
  record, including its recorded hash, parent ID, and saved revision. The new
  tab's draft revision remains zero. Restoration does not write storage.
- The source key is `tickreplay.strategy.source.v1`. Its exact-key record schema
  is `{schemaVersion, writerId, recordId, parentRecordId, draftRevision,
  sourceHash, source, updatedAt}`. Adding metadata to it without a versioned
  reader change makes it corrupt to current readers.
- `saveSource()` compares expected parent before writing and confirms the new
  record via read-back. This is best-effort conflict detection, not an atomic
  cross-tab lock. Writes must preserve this path, not call `localStorage.setItem`
  from ad hoc migration code.
- Source edits increment tab-local draft revision and autosave after 750 ms.
  Saved/running source revisions remain independent. Restart uses the last
  loaded source, never a newer unsaved draft.
- `tickreplay.strategy.settings.v1` is independent and currently stores only
  `panelExpanded`; unknown settings fields are permitted by its validator, but
  the current workspace writer replaces settings with `{panelExpanded}`.
- `executionMode` is initialized to `pyodide` (`workspace:199`), is not restored
  or persisted, and HTML also places Pyodide first. Native option starts disabled.
- Mount asynchronously calls `ensureCapability()`. A successful capability
  enables Native; it does not select it. Capability failure is latched for this
  mount. The current exact capability shape is `{available, contractVersion,
  trustedLocalOnly, limits}`: protocol validation rejects extra fields.
- Expanding the panel lazily loads the editor. Only explicit Load creates a
  controller; only explicit Start starts a run. Mount does not load/start Python.
- `app.js:1369` requires `strategySessionIdentity()` before Load, adds session
  `dataHash` and tick count, and passes those to the controller. Native Start
  requires cursor zero. Any auto-preparation must run after session completion,
  not merely after strategy-workspace bootstrap (`app.js:2402`).
- Mode changes invalidate the prior controller and loaded identity. Seek, reset,
  and session change clear a native loaded identity. Manual/native/Pyodide runs
  must remain mutually exclusive.
- Future source schemas are preserved read-only/export-or-clear. Corrupt storage
  is retained; storage exceptions leave an in-memory editable draft. Bootstrap
  failure leaves the HTML textarea selectable for recovery.

## Recommended Migration

Prefer a versioned built-in preset plus a separate preserved custom draft rather
than silently rewriting the one existing source record:

1. Add a stable preset ID such as `bundled_rl_v1` and one canonical packaged
   source template. Use the same bytes for the HTML recovery textarea and release
   workspace default; add an equality assertion to prevent future drift.
2. On a profile without an explicit selection under the new preset/settings
   version, select bundled RL and Native Trace. Keep the existing
   `tickreplay.strategy.source.v1` record byte-for-byte and expose it as
   `Saved strategy`/custom. This directly fixes the user's `print(event)` profile
   while preserving all previously authored work.
3. Keep separate persistence for an edited bundled-strategy draft if it is
   editable. It can reuse the same validated record/CAS implementation with an
   explicit storage-key option. Do not allow editing the RL preset to autosave
   over a hidden pre-existing custom draft. Avoid multiplying template copies.
4. Persist explicit preset/mode choices, merging known settings on writes.
   Default old custom drafts to Pyodide because no prior native mode was saved.
   A chosen RL preset always requires Native; do not infer runtime from an
   arbitrary substring in user source.
5. Preset switching invalidates an active controller using existing lifecycle
   hooks and preserves draft/loaded/run revision semantics. A preset selection
   is a source change, not a mutation of an already loaded snapshot.
6. Auto-prepare only the shipped built-in strategy (if automatic execution is
   included in the parent plan). Use the loaded session identity/generation,
   cursor zero, and a single pending attempt. A session change, Stop, mode/preset
   edit, or disposal cancels that attempt. Do not automatically execute an old
   arbitrary saved custom draft just because Native now defaults on.

Smaller alternative: upgrade only an exact canonical legacy no-op record via
CAS and show a `Use bundled RL` button for edited/custom drafts. This is safer
than overwriting but does **not** by itself satisfy the reported profile: the
user added `print(event)`. If a one-time source replacement is used instead of
preset separation, preserve and verify a versioned backup before replacement,
provide restore/export in the UI, and abort replacement on any storage or CAS
conflict. A migration marker written before successful backup/save is unsafe.

Do not merely clear localStorage, increment the source key and forget the old
one, add fields to the exact-key v1 record, or broaden matching to any source
containing `on_event`/`return []`.

## Failure/Fallback UX

- While capability is pending, display RL/Native as the intended selection and
  prevent Load/Start until readiness is known; a fast click must not load Pyodide.
- If native is unavailable or malformed, keep the RL source visible, report a
  concise actionable unavailable state, and disable its Load/Start. Do not run
  the RL imports in Pyodide or silently return no orders.
- Permit explicit selection of a browser-compatible saved/custom strategy and
  Pyodide. Capability false must leave manual replay usable.
- Distinguish native unavailable from model/bootstrap failure using bounded
  diagnostics. Any extra capability metadata requires coordinated Python and
  strict JS protocol changes.
- Preserve future/corrupt/unavailable browser storage without blocking the
  shipped in-memory default. Recovery affordances must still expose original
  user bytes rather than hiding them behind the new preset.
- Native default configuration must remain local-only; current Cloud Run policy
  and unavailable-platform gates belong to the backend/bootstrap workstream.

## Tests

Meaningful cases for the new default behavior:

- Fresh profile shows the exact bundled template, chooses Native when available,
  never imports Pyodide for RL, and only submits with a valid current session.
- Profile with the exact old no-op and profile with `print(event)` both see the
  shipped RL selected; previous source remains recoverable byte-for-byte.
- Arbitrary saved custom strategy is preserved and recoverable, never executed
  implicitly. Editing the RL draft does not replace that saved custom record.
- Explicit preset/mode selection survives reload; old settings containing only
  panel expansion migrate predictably and panel writes preserve the selection.
- Future source schema, corrupt source, unavailable storage, backup/save failure,
  two tabs changing the same draft, late hash completion, and read-back conflict
  remain non-destructive.
- Capability pending/unavailable/rejected prevents accidental Pyodide execution;
  choosing custom Pyodide remains possible. Capability completion after disposal
  or a manual mode choice must not overwrite newer intent.
- Auto-prepare/start, if enabled: session before workspace and workspace before
  session, session change during generation, Stop during generation, reset/seek,
  cursor not zero, and repeated session-ready notifications.
- Real model execution produces a validated trace for a deterministic market
  fixture; a real browser consumes it through the broker with tick i+1 causality
  and zero native HTTP during replay. Existing mocked browser test alone cannot
  demonstrate copied model usability.
- Retained immutable releases remain byte-identical; current release reconstructs
  from canonical authored sources and every fixed entry/test/doc reference agrees.

Existing targeted commands:

```sh
node --test src/tickreplay/static/strategy/strategy-source-store.test.mjs src/tickreplay/static/strategy/strategy-workspace.test.mjs src/tickreplay/static/strategy/strategy-trace-controller.test.mjs src/tickreplay/static/replay-commit.test.mjs
npm run build:strategy-release
npm run check:strategy-release
npm run test:strategy-assets
STRATEGY_SPIKE_USE_SYSTEM_CHROME=1 npm run test:strategy-browser -- --project=chromium --grep 'Native Trace'
```

The final browser command currently starts `tests/browser/spike_server.py` and
the Native Trace application test mocks the API; add an actual backend/model
integration fixture for proof of self-contained RL execution.

## Files

Required frontend implementation surfaces:

- `src/tickreplay/static/strategy/strategy-workspace.mjs` and its test: source
  selection, restore, preset/mode persistence, capability admission, UI state.
- `src/tickreplay/static/strategy/strategy-source-store.mjs` and its test if
  introducing per-preset draft keys or a migration helper.
- `src/tickreplay/static/index.html`: initial recovery source, selector/default,
  saved-draft recovery affordance, useful RL-facing labels.
- `src/tickreplay/static/app.js`: new release pointer and any session-ready
  auto-preparation coordination; never submit before session identity exists.
- `scripts/build_strategy_release.mjs`: include any new authored preset module
  in `AUTHORED_FILES`, plus generation changes if applicable.
- `tests/browser/application-strategy.mjs`: fresh/existing profile behavior and
  release ID; Pyodide tests should explicitly select their intended mode.
- `tests/browser/strategy-release.test.mjs`: current ID and authored closure list.
- `src/tickreplay/static/replay-commit.test.mjs`: current release assertion.
- `docs/tick-replay.md` and `docs/pyodide-strategy-release-runbook.md`: actual
  default/run flow, preservation behavior, current release/manifest hashes.
- New `src/tickreplay/static/strategy/releases/<new-id>/` generated closure.

Current active release is
`c9456aff2a649e11d9617134f5f49b81053e2876b8309668cc2793f7997d33e6`.
The release builder writes a new directory but does not update all callers. Its
check reconstructs the closure twice and checks import/worker/runner edges.
Existing release directories are retained and must not be edited in place.

Backend model copying/configuration/package dependency choices are intentionally
outside this frontend scan and must be integrated with the parallel inventory.

## Artifact Path

`.agents/docs/research/bundled-rl-default-source-scan.md`

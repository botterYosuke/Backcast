'use strict';

import {
  SETTINGS_STORAGE_KEY,
  SOURCE_STORAGE_KEY,
  SourceStoreError,
  canonicalizeSource,
  createAutosaveScheduler,
  createSourceIdentityService,
  createStrategySourceStore,
  decodePythonFile,
  validateCanonicalSource,
} from './strategy-source-store.mjs';

export const STRATEGY_WORKSPACE_SELECTORS = Object.freeze({
  panel: '[data-strategy-panel]',
  toggle: '[data-strategy-action="toggle"]',
  source: '[data-strategy-source]',
  editor: '[data-strategy-editor]',
  status: '[data-strategy-status]',
  revision: '[data-strategy-revision]',
  startTarget: '[data-strategy-start-target]',
  diagnostics: '[data-strategy-diagnostics]',
  logs: '[data-strategy-logs]',
  importInput: '[data-strategy-import]',
  save: '[data-strategy-action="save"]',
  load: '[data-strategy-action="load"]',
  start: '[data-strategy-action="start"]',
  stop: '[data-strategy-action="stop"]',
  restart: '[data-strategy-action="restart"]',
  exportSource: '[data-strategy-action="export"]',
  sample: '[data-strategy-action="sample"]',
  clearSource: '[data-strategy-action="clear-source"]',
  reloadLatest: '[data-strategy-action="reload-latest"]',
  overwrite: '[data-strategy-action="overwrite"]',
  downloadDiagnostics: '[data-strategy-action="download-diagnostics"]',
});

export const SAMPLE_STRATEGY_SOURCE = `def on_start(state):
    return []

def on_event(event):
    # Orders returned here first become eligible on the next replay tick.
    return []

def on_end(summary):
    return None
`;

const DIAGNOSTIC_LIMITS = Object.freeze({ entries: 200, bytes: 262_144, messageBytes: 2_048 });
const ACTIVE_CONTROLLER_STATES = new Set(['loading', 'starting', 'running', 'paused', 'stopping']);

export class StrategyWorkspaceError extends Error {
  constructor(code, message = code, detail = null) {
    super(message);
    this.name = 'StrategyWorkspaceError';
    this.code = code;
    this.detail = detail;
  }
}

function defaultSafeCall(_label, operation, onError) {
  try {
    return Promise.resolve(operation()).catch(onError);
  } catch (error) {
    onError(error);
    return Promise.resolve(null);
  }
}

function boundedText(value, maximumBytes = DIAGNOSTIC_LIMITS.messageBytes) {
  const text = String(value ?? '');
  const encoder = new TextEncoder();
  if (encoder.encode(text).byteLength <= maximumBytes) return text;
  const suffix = '…';
  const scalars = Array.from(text);
  while (scalars.length && encoder.encode(scalars.join('') + suffix).byteLength > maximumBytes) {
    scalars.pop();
  }
  return scalars.join('') + suffix;
}

function requireElements(root) {
  const elements = {};
  for (const [name, selector] of Object.entries(STRATEGY_WORKSPACE_SELECTORS)) {
    elements[name] = root.querySelector?.(selector) ?? null;
  }
  for (const required of ['panel', 'toggle', 'source', 'editor', 'status', 'revision', 'startTarget']) {
    if (!elements[required]) throw new StrategyWorkspaceError('missing_dom', `missing ${required}`);
  }
  return elements;
}

function setText(element, value) {
  if (element) element.textContent = String(value ?? '');
}

function setExpanded(elements, expanded) {
  elements.panel.hidden = !expanded;
  elements.toggle.setAttribute?.('aria-expanded', expanded ? 'true' : 'false');
  elements.toggle.textContent = expanded ? '戦略を閉じる' : '戦略を開く';
}

function sanitizeFilename(value) {
  const base = String(value || 'strategy')
    .replace(/\.py$/i, '')
    .replace(/[^A-Za-z0-9._-]+/g, '-')
    .replace(/^[-.]+|[-.]+$/g, '')
    .slice(0, 80);
  return `${base || 'strategy'}.py`;
}

function browserDownload(name, content, type = 'text/plain;charset=utf-8') {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = sanitizeFilename(name);
  anchor.hidden = true;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

function snapshotController(host) {
  return host.getControllerState?.() ?? null;
}

function sourcePositionToOffset(source, position) {
  if (!position || !Number.isSafeInteger(position.line) || !Number.isSafeInteger(position.column)) {
    return null;
  }
  const lines = source.split('\n');
  if (position.line < 1 || position.line > lines.length || position.column < 0) return null;
  const scalars = Array.from(lines[position.line - 1]);
  if (position.column > scalars.length) return null;
  return lines
    .slice(0, position.line - 1)
    .reduce((length, line) => length + line.length + 1, 0)
    + scalars.slice(0, position.column).join('').length;
}

export function mountStrategyWorkspace({
  root,
  host,
  release,
  storage = globalThis.localStorage,
  subtle = globalThis.crypto?.subtle ?? null,
  writerId = null,
  makeId,
  now,
  safeCall = defaultSafeCall,
  confirmAction = (message) => globalThis.confirm(message),
  download = browserDownload,
  windowObject = globalThis.window,
  documentObject = globalThis.document,
  performanceClass = 'desktop',
} = {}) {
  if (!root || !host || !release) throw new StrategyWorkspaceError('invalid_bootstrap');
  if (typeof release.loadEditor !== 'function' || typeof release.loadController !== 'function') {
    throw new StrategyWorkspaceError('invalid_release_boundary');
  }
  const elements = requireElements(root);
  const identity = createSourceIdentityService({ subtle });
  const storeOptions = { storage, writerId };
  if (makeId) storeOptions.makeId = makeId;
  if (now) storeOptions.now = now;
  let store = null;
  let source = canonicalizeSource(elements.source.value || SAMPLE_STRATEGY_SOURCE);
  validateCanonicalSource(source);
  let draftRevision = 0;
  let sourceHash = null;
  let expectedParentRecordId = null;
  let savedDraftRevision = null;
  let savedAt = null;
  let dirty = false;
  let superseded = false;
  let persistenceFailed = false;
  let futureSourceSchema = false;
  let expanded = false;
  let disposed = false;
  let rawRecovery = false;
  let editorFailure = null;
  let controllerFailure = null;
  let editor = null;
  let editorPromise = null;
  let controller = null;
  let controllerPromise = null;
  let loadedSnapshot = null;
  let loadAttempt = 0;
  let composition = false;
  let beforeUnloadInstalled = false;
  let diagnosticsBytes = 0;
  let diagnosticsTruncated = 0;
  let diagnosticCursor = -1;
  const diagnostics = [];
  const cleanups = [];

  function emitStatus(message, tone = 'info') {
    setText(elements.status, boundedText(message));
    if (elements.status?.dataset) elements.status.dataset.tone = tone;
  }

  function appendDiagnostic(input) {
    const entry = Object.freeze({
      at: new Date().toISOString(),
      phase: boundedText(input?.phase ?? 'workspace', 128),
      severity: boundedText(input?.severity ?? 'error', 128),
      category: boundedText(input?.category ?? input?.code ?? 'workspace_error', 128),
      message: boundedText(input?.message ?? input),
      source_hash: input?.source_hash ?? null,
      draft_revision: input?.draft_revision ?? null,
      loaded_revision: input?.loaded_revision ?? null,
      running_revision: input?.running_revision ?? null,
      load_attempt_id: input?.load_attempt_id ?? null,
      run_id: input?.run_id ?? null,
      sequence: input?.sequence ?? null,
      range: input?.range ?? null,
    });
    const bytes = new TextEncoder().encode(JSON.stringify(entry)).byteLength;
    if (bytes > DIAGNOSTIC_LIMITS.bytes) {
      diagnosticsTruncated += 1;
      return;
    }
    diagnostics.push(entry);
    diagnosticsBytes += bytes;
    while (
      diagnostics.length > DIAGNOSTIC_LIMITS.entries
      || diagnosticsBytes > DIAGNOSTIC_LIMITS.bytes
    ) {
      const removed = diagnostics.shift();
      diagnosticsBytes -= new TextEncoder().encode(JSON.stringify(removed)).byteLength;
      diagnosticsTruncated += 1;
    }
    if (elements.diagnostics) {
      const suffix = diagnosticsTruncated ? `\n(${diagnosticsTruncated} older entries omitted)` : '';
      elements.diagnostics.textContent = diagnostics
        .map((item) => `${item.severity}: ${item.message}`)
        .join('\n') + suffix;
    }
    editor?.publishDiagnostics?.(diagnostics, { sourceHash, draftRevision });
  }

  function shouldWarn() {
    return dirty || superseded || persistenceFailed;
  }

  function beforeUnload(event) {
    event.preventDefault();
    event.returnValue = '';
  }

  function syncBeforeUnload() {
    const needed = shouldWarn() && !disposed && !rawRecovery;
    if (needed === beforeUnloadInstalled) return;
    beforeUnloadInstalled = needed;
    const method = needed ? 'addEventListener' : 'removeEventListener';
    windowObject?.[method]?.('beforeunload', beforeUnload);
  }

  function currentControllerState() {
    return snapshotController(host) ?? { tag: controller ? 'idle' : 'unavailable', loadedRevision: 0 };
  }

  function render() {
    const state = currentControllerState();
    const active = ACTIVE_CONTROLLER_STATES.has(state.tag);
    setText(
      elements.revision,
      `Draft r${draftRevision}${dirty ? ' modified' : ''}; saved ${savedDraftRevision ?? '—'}; loaded ${state.loadedRevision ?? 0}; running ${state.runningRevision ?? '—'}`,
    );
    setText(
      elements.startTarget,
      loadedSnapshot
        ? `Start target: loaded r${loadedSnapshot.loadedRevision} (${loadedSnapshot.sourceHash.slice(0, 12)})`
        : 'Start target: none',
    );
    const disableAll = disposed || rawRecovery;
    for (const name of ['save', 'exportSource', 'sample', 'clearSource', 'reloadLatest', 'overwrite']) {
      if (elements[name]) elements[name].disabled = disableAll;
    }
    if (futureSourceSchema) {
      for (const name of ['save', 'sample', 'reloadLatest', 'overwrite']) {
        if (elements[name]) elements[name].disabled = true;
      }
    }
    if (elements.reloadLatest) {
      elements.reloadLatest.disabled = disableAll || futureSourceSchema || !superseded;
    }
    if (elements.overwrite) {
      elements.overwrite.disabled = disableAll || futureSourceSchema || !superseded;
    }
    if (elements.load) {
      elements.load.disabled = disableAll || futureSourceSchema || Boolean(controllerFailure)
        || ['loading', 'starting', 'stopping'].includes(state.tag);
    }
    if (elements.start) elements.start.disabled = disableAll || state.tag !== 'ready';
    if (elements.stop) elements.stop.disabled = disableAll || !active;
    if (elements.restart) elements.restart.disabled = disableAll || !loadedSnapshot || active;
    if (elements.importInput) elements.importInput.disabled = disableAll || futureSourceSchema;
    if (elements.downloadDiagnostics) {
      elements.downloadDiagnostics.disabled = disableAll || (
        diagnostics.length === 0 && !(host.getDiagnostics?.().length) && !host.getLogs?.()
      );
    }
    if (elements.logs) {
      const logs = host.getLogs?.();
      elements.logs.textContent = logs ? `stdout:\n${logs.stdout}\nstderr:\n${logs.stderr}` : '';
    }
    syncBeforeUnload();
  }

  function acceptSource(nextSource, {
    origin = 'unknown',
    markDirty = true,
    forceRevision = false,
  } = {}) {
    const canonical = validateCanonicalSource(canonicalizeSource(nextSource));
    if (canonical === source && !forceRevision) return false;
    source = canonical;
    draftRevision += 1;
    sourceHash = null;
    dirty = markDirty;
    persistenceFailed = false;
    elements.source.value = source;
    if (origin !== 'codemirror') editor?.replaceSource?.(source);
    void identity.identifyCurrent(source, draftRevision).then((result) => {
      if (!result.current || disposed) return;
      sourceHash = result.sourceHash;
      editor?.publishDiagnostics?.(diagnostics, { sourceHash, draftRevision });
      render();
    }).catch((error) => {
      appendDiagnostic({ phase: 'identity', category: error.code, message: error.message });
      render();
    });
    if (markDirty && !superseded && store) sourceAutosave.schedule({ source, draftRevision });
    render();
    return true;
  }

  async function saveSnapshot(snapshot, { overwrite = false } = {}) {
    if (!store) throw new StrategyWorkspaceError('storage_unavailable', 'Draft persistence is unavailable');
    const identified = await identity.identifySnapshot(snapshot.source, snapshot.draftRevision);
    const result = store.saveSource({
      ...identified,
      expectedParentRecordId,
      overwrite,
    });
    if (result.status === 'conflict' || result.status === 'unconfirmed') {
      superseded = true;
      persistenceFailed = result.status === 'unconfirmed';
      sourceAutosave.cancel();
      emitStatus(
        result.status === 'conflict'
          ? '別のタブの保存を検出しました。最新版を再読込するか、この下書きで上書きしてください。'
          : '保存後の read-back を確認できませんでした。下書きは保持されています。',
        'error',
      );
      render();
      return result;
    }
    expectedParentRecordId = result.record.recordId;
    savedDraftRevision = result.record.draftRevision;
    savedAt = result.record.updatedAt;
    if (source === snapshot.source && draftRevision === snapshot.draftRevision) {
      sourceHash = result.record.sourceHash;
      dirty = false;
    }
    persistenceFailed = false;
    superseded = false;
    emitStatus('下書きを保存し、read-back を確認しました。', 'success');
    render();
    return result;
  }

  const sourceAutosave = createAutosaveScheduler({
    save: (snapshot) => saveSnapshot(snapshot).catch((error) => {
      persistenceFailed = true;
      appendDiagnostic({ phase: 'save', category: error.code, message: error.message });
      emitStatus('自動保存に失敗しました。下書きはこのタブに保持されています。', 'error');
      render();
      return null;
    }),
  });
  const settingsAutosave = createAutosaveScheduler({
    save: ({ panelExpanded }) => {
      if (!store) return null;
      try {
        return store.saveSettings({ panelExpanded });
      } catch (error) {
        appendDiagnostic({ phase: 'settings', category: error.code, message: error.message });
        return null;
      }
    },
  });

  function restoreStoredState() {
    try {
      store = createStrategySourceStore(storeOptions);
      const savedSource = store.readSource();
      if (savedSource.status === 'available') {
        source = savedSource.record.source;
        sourceHash = savedSource.record.sourceHash;
        expectedParentRecordId = savedSource.record.recordId;
        savedDraftRevision = savedSource.record.draftRevision;
        savedAt = savedSource.record.updatedAt;
      } else if (savedSource.status === 'future_schema') {
        futureSourceSchema = true;
        superseded = true;
        try {
          source = validateCanonicalSource(canonicalizeSource(savedSource.rawRecord.source));
        } catch {
          // An unrecognized record without canonical text remains clear-only.
        }
        emitStatus('新しい形式の保存済みソースは上書きしません。Export または Clear を使用してください。', 'error');
      } else if (savedSource.status === 'corrupt') {
        persistenceFailed = true;
        emitStatus('保存済みソースを読み取れません。現在の下書きを保持します。', 'error');
      }
      const settings = store.readSettings();
      if (settings.status === 'available') expanded = Boolean(settings.record.settings.panelExpanded);
      else if (settings.status !== 'missing') {
        appendDiagnostic({ phase: 'settings', category: settings.status, message: 'Saved workspace settings were ignored' });
      }
    } catch (error) {
      store = null;
      persistenceFailed = true;
      appendDiagnostic({ phase: 'storage', category: error.code, message: error.message });
      emitStatus('ブラウザ保存を利用できません。編集と実行は継続できます。', 'error');
    }
    elements.source.value = source;
    elements.source.readOnly = futureSourceSchema;
    if (futureSourceSchema) elements.source.setAttribute?.('aria-readonly', 'true');
    else elements.source.removeAttribute?.('aria-readonly');
  }

  function workspaceCallbacks() {
    return {
      onStateChange(controllerState) {
        if (disposed || rawRecovery) return;
        if (controllerState.tag === 'errored') emitStatus('戦略ランタイムが停止しました。診断を確認してください。', 'error');
        render();
      },
      onDiagnostic(diagnostic) {
        if (!disposed && !rawRecovery) appendDiagnostic(diagnostic);
        render();
      },
      onCleanup(detail) {
        appendDiagnostic({
          phase: 'cleanup', severity: 'info', category: 'strategy_cleanup',
          message: `Strategy orders cleaned: ${(detail.canceledOrderIds ?? []).join(', ') || 'none'}`,
        });
      },
      onPauseReplay() { render(); },
      onManualGateChange() { render(); },
    };
  }

  async function ensureEditor() {
    if (editor || editorFailure || rawRecovery || futureSourceSchema) return editor;
    if (editorPromise) return editorPromise;
    editorPromise = (async () => {
      try {
        const loaded = await release.loadEditor();
        if (disposed || rawRecovery) return null;
        editor = loaded.createStrategyEditor({
          container: elements.editor,
          textarea: elements.source,
          toolkit: loaded.toolkit,
          getCanonicalSource: () => source,
          onSourceChange: (value, detail) => acceptSource(value, detail),
          onDegraded: (error) => {
            editorFailure = error;
            emitStatus('CodeMirror を利用できないため textarea 編集に切り替えました。', 'error');
            render();
          },
          sourceHash,
          draftRevision,
          diagnostics,
          lineWrapping: windowObject?.matchMedia?.('(max-width: 768px)').matches ?? false,
        });
        if (root.dataset) root.dataset.strategyMode = 'enhanced';
        return editor;
      } catch (error) {
        editorFailure = error;
        if (root.dataset) root.dataset.strategyMode = 'degraded';
        elements.source.hidden = false;
        elements.source.readOnly = false;
        elements.source.tabIndex = 0;
        elements.source.removeAttribute?.('aria-hidden');
        emitStatus('CodeMirror を読み込めないため textarea 編集を使用します。', 'error');
        appendDiagnostic({ phase: 'editor', category: 'editor_unavailable', message: error.message });
        return null;
      } finally {
        editorPromise = null;
      }
    })();
    return editorPromise;
  }

  async function ensureController() {
    if (controller) return controller;
    if (controllerFailure) throw controllerFailure;
    if (controllerPromise) return controllerPromise;
    controllerPromise = (async () => {
      try {
        const callbacks = host.controllerCallbacks(workspaceCallbacks());
        const candidate = await release.loadController({ callbacks, performanceClass });
        if (disposed || rawRecovery) {
          await candidate?.stop?.('explicit_stop');
          throw new StrategyWorkspaceError('workspace_disposed');
        }
        host.attachController(candidate);
        controller = candidate;
        return controller;
      } catch (error) {
        controllerFailure = error;
        appendDiagnostic({ phase: 'controller', category: error.code, message: error.message });
        emitStatus('戦略ランタイムを利用できません。ソース操作は継続できます。', 'error');
        render();
        throw error;
      } finally {
        controllerPromise = null;
      }
    })();
    return controllerPromise;
  }

  async function loadDraft(snapshot = { source, draftRevision }) {
    await ensureController();
    const identified = await identity.identifySnapshot(snapshot.source, snapshot.draftRevision);
    const request = {
      source: identified.source,
      sourceHash: identified.sourceHash,
      draftRevision: identified.draftRevision,
      loadAttemptId: `workspace-load-${++loadAttempt}`,
      filename: 'strategy.py',
      performanceClass,
    };
    if (source === identified.source && draftRevision === identified.draftRevision) {
      sourceHash = identified.sourceHash;
    }
    const result = await host.load(request);
    loadedSnapshot = Object.freeze({ ...request, loadedRevision: result.loadedRevision });
    emitStatus(`戦略を loaded revision ${result.loadedRevision} として読み込みました。`, 'success');
    render();
    return result;
  }

  async function startStrategy() {
    const result = await host.start();
    emitStatus(`戦略 run ${result.runId} を開始しました。`, 'success');
    render();
    return result;
  }

  async function restartStrategy() {
    if (!loadedSnapshot) throw new StrategyWorkspaceError('no_loaded_source');
    const snapshot = { source: loadedSnapshot.source, draftRevision: loadedSnapshot.draftRevision };
    await loadDraft(snapshot);
    return startStrategy();
  }

  function checkExternalSource() {
    if (!store || rawRecovery) return;
    let result;
    try { result = store.readSource(); } catch (error) {
      persistenceFailed = true;
      appendDiagnostic({ phase: 'storage', category: error.code, message: error.message });
      render();
      return;
    }
    if (result.status === 'available' && result.record.recordId !== expectedParentRecordId) {
      superseded = true;
      sourceAutosave.cancel();
      emitStatus(
        `別タブの保存を検出しました (${result.record.sourceHash.slice(0, 12)}, ${result.record.updatedAt})。`,
        'error',
      );
    } else if (result.status === 'future_schema') {
      futureSourceSchema = true;
      superseded = true;
      sourceAutosave.cancel();
      emitStatus('新しい schema の保存済みソースを検出しました。上書きしません。', 'error');
    }
    render();
  }

  async function reloadLatest() {
    if (!store) throw new StrategyWorkspaceError('storage_unavailable');
    const result = store.readSource();
    if (result.status !== 'available') throw new StrategyWorkspaceError('latest_unavailable');
    const changed = acceptSource(result.record.source, {
      origin: 'conflict-resolution',
      markDirty: false,
      forceRevision: true,
    });
    if (!changed) elements.source.value = source;
    sourceHash = result.record.sourceHash;
    expectedParentRecordId = result.record.recordId;
    savedDraftRevision = result.record.draftRevision;
    savedAt = result.record.updatedAt;
    dirty = false;
    superseded = false;
    persistenceFailed = false;
    emitStatus('別タブの最新版を読み込みました。', 'success');
    render();
  }

  function exportDiagnostics() {
    const payload = {
      schemaVersion: 1,
      exportedAt: new Date().toISOString(),
      workspace: state(),
      controllerState: snapshotController(host),
      diagnostics: [...(host.getDiagnostics?.() ?? []), ...diagnostics],
      diagnosticsTruncated,
      logs: host.getLogs?.() ?? null,
      terminalOutcome: host.getTerminalOutcome?.() ?? null,
    };
    download('tickreplay-strategy-diagnostics.json', JSON.stringify(payload, null, 2), 'application/json');
  }

  function navigateDiagnostic(direction) {
    if (editor?.navigateDiagnostic?.(direction)) return true;
    const mapped = diagnostics
      .filter((item) => (
        item.source_hash === sourceHash
        && item.draft_revision === draftRevision
        && item.range
      ))
      .map((item) => ({
        item,
        from: sourcePositionToOffset(source, item.range.start),
        to: sourcePositionToOffset(source, item.range.end),
      }))
      .filter(({ from, to }) => from !== null && to !== null && to >= from);
    if (mapped.length === 0) return false;
    diagnosticCursor = (
      diagnosticCursor + (direction < 0 ? -1 : 1) + mapped.length
    ) % mapped.length;
    const selected = mapped[diagnosticCursor];
    elements.source.setSelectionRange?.(selected.from, selected.to);
    elements.source.focus?.();
    emitStatus(`${selected.item.severity}: ${selected.item.message}`, 'info');
    return true;
  }

  function enterRawRecovery(error) {
    if (rawRecovery) return;
    rawRecovery = true;
    sourceAutosave.dispose();
    settingsAutosave.dispose();
    editor?.dispose?.();
    elements.editor.hidden = true;
    elements.source.value = source;
    elements.source.hidden = false;
    elements.source.readOnly = true;
    elements.source.tabIndex = 0;
    elements.source.removeAttribute?.('aria-hidden');
    if (root.dataset) root.dataset.strategyMode = 'raw-recovery';
    emitStatus(`戦略機能を停止しました。ソースの選択とコピーのみ利用できます: ${boundedText(error?.message)}`, 'error');
    try { void host.invalidate?.('explicit_stop'); } catch { /* already unavailable */ }
    render();
  }

  function invoke(label, operation, { fatal = false } = {}) {
    return safeCall(label, operation, (error) => {
      appendDiagnostic({ phase: 'workspace', category: error?.code, message: error?.message ?? error });
      if (fatal) enterRawRecovery(error);
      else {
        emitStatus(`${label} に失敗しました: ${boundedText(error?.message ?? error)}`, 'error');
        render();
      }
      return null;
    });
  }

  function listen(target, type, label, operation, options) {
    if (!target?.addEventListener) return;
    const listener = (event) => invoke(label, () => operation(event), options);
    target.addEventListener(type, listener);
    cleanups.push(() => target.removeEventListener(type, listener));
  }

  function state() {
    return Object.freeze({
      source,
      sourceHash,
      draftRevision,
      savedDraftRevision,
      savedAt,
      dirty,
      superseded,
      persistenceFailed,
      futureSourceSchema,
      expanded,
      editorMode: editor?.mode?.() ?? (editorFailure ? 'degraded' : 'uninitialized'),
      controllerUnavailable: Boolean(controllerFailure),
      loadedSnapshot,
      rawRecovery,
      diagnosticsCount: diagnostics.length,
      diagnosticsTruncated,
    });
  }

  function dispose() {
    if (disposed) return;
    disposed = true;
    sourceAutosave.dispose();
    settingsAutosave.dispose();
    for (const cleanup of cleanups.splice(0).reverse()) cleanup();
    editor?.dispose?.();
    try { void host.invalidate?.('explicit_stop'); } catch { /* teardown is best effort */ }
    if (beforeUnloadInstalled) windowObject?.removeEventListener?.('beforeunload', beforeUnload);
    beforeUnloadInstalled = false;
  }

  restoreStoredState();
  setExpanded(elements, expanded);
  if (root.dataset) root.dataset.strategyMode = 'degraded';

  listen(elements.toggle, 'click', 'panel toggle', async () => {
    expanded = !expanded;
    setExpanded(elements, expanded);
    settingsAutosave.schedule({ panelExpanded: expanded });
    if (expanded) await ensureEditor();
    render();
  });
  listen(elements.source, 'input', 'source edit', () => {
    const attempted = elements.source.value;
    try { acceptSource(attempted, { origin: 'textarea' }); } catch (error) {
      elements.source.value = source;
      throw error;
    }
  });
  listen(elements.source, 'compositionstart', 'composition start', () => { composition = true; });
  listen(elements.source, 'compositionend', 'composition end', () => { composition = false; });
  listen(root, 'keydown', 'strategy shortcut', (event) => {
    if (composition || event.isComposing || event.keyCode === 229) return;
    const modifier = event.ctrlKey || event.metaKey;
    let action = null;
    if (modifier && event.key.toLowerCase() === 's') action = () => saveSnapshot({ source, draftRevision });
    else if (modifier && event.key === 'Enter' && event.shiftKey) action = () => loadDraft();
    else if (modifier && event.key === 'Enter') action = startStrategy;
    else if (modifier && event.key === '.' && ACTIVE_CONTROLLER_STATES.has(currentControllerState().tag)) action = () => host.stop();
    else if (event.key === 'F8') action = () => navigateDiagnostic(event.shiftKey ? -1 : 1);
    if (!action) return;
    event.preventDefault();
    event.stopPropagation();
    return action();
  });
  listen(elements.save, 'click', 'Save Draft', () => saveSnapshot({ source, draftRevision }));
  listen(elements.load, 'click', 'Load', () => loadDraft());
  listen(elements.start, 'click', 'Start', startStrategy);
  listen(elements.stop, 'click', 'Stop', () => host.stop());
  listen(elements.restart, 'click', 'Restart', restartStrategy);
  listen(elements.reloadLatest, 'click', 'Reload latest', reloadLatest);
  listen(elements.overwrite, 'click', 'Overwrite', () => saveSnapshot({ source, draftRevision }, { overwrite: true }));
  listen(elements.sample, 'click', 'Sample', () => {
    if (dirty && !confirmAction('未保存の下書きをサンプルで置き換えますか？')) return;
    acceptSource(SAMPLE_STRATEGY_SOURCE, { origin: 'sample' });
  });
  listen(elements.clearSource, 'click', 'Clear saved source', () => {
    if (!confirmAction('保存済み戦略ソースを消去しますか？')) return;
    store?.clearSource({ allowFutureSchema: true });
    futureSourceSchema = false;
    elements.source.readOnly = false;
    elements.source.removeAttribute?.('aria-readonly');
    expectedParentRecordId = null;
    savedDraftRevision = null;
    savedAt = null;
    dirty = true;
    superseded = false;
    emitStatus('保存済みソースを消去しました。設定は保持されています。', 'success');
    render();
  });
  listen(elements.exportSource, 'click', 'Export', () => download('strategy.py', source));
  listen(elements.downloadDiagnostics, 'click', 'Download diagnostics', exportDiagnostics);
  listen(elements.importInput, 'change', 'Import', async () => {
    const file = elements.importInput.files?.[0];
    if (!file) return;
    if (dirty && !confirmAction('未保存の下書きを import で置き換えますか？')) return;
    const imported = decodePythonFile(file.name, new Uint8Array(await file.arrayBuffer()));
    acceptSource(imported, { origin: 'import' });
    elements.importInput.value = '';
  });
  listen(windowObject, 'storage', 'storage recheck', (event) => {
    if (event.key === SOURCE_STORAGE_KEY) checkExternalSource();
    if (event.key === SETTINGS_STORAGE_KEY && store) {
      const current = store.readSettings();
      if (current.status === 'available') {
        expanded = Boolean(current.record.settings.panelExpanded);
        setExpanded(elements, expanded);
        render();
      }
    }
  });
  listen(windowObject, 'focus', 'focus recheck', checkExternalSource);
  listen(documentObject, 'visibilitychange', 'visibility persistence', () => {
    if (documentObject.visibilityState === 'hidden') void sourceAutosave.flush();
    else checkExternalSource();
  });
  listen(windowObject, 'pagehide', 'pagehide persistence', () => {
    void sourceAutosave.flush();
    try { void host.invalidate?.('explicit_stop'); } catch { /* app boundary also invalidates */ }
  });
  listen(windowObject, 'pageshow', 'pageshow recovery', (event) => {
    if (!event.persisted) return;
    emitStatus('ページを復元しました。戦略を続けるには Restart が必要です。', 'info');
    render();
  });

  render();
  return Object.freeze({
    state,
    saveDraft: () => saveSnapshot({ source, draftRevision }),
    loadDraft,
    start: startStrategy,
    stop: () => host.stop(),
    restart: restartStrategy,
    reloadLatest,
    overwrite: () => saveSnapshot({ source, draftRevision }, { overwrite: true }),
    acceptSource,
    flush: () => sourceAutosave.flush(),
    flushSettings: () => settingsAutosave.flush(),
    enterRawRecovery,
    dispose,
  });
}

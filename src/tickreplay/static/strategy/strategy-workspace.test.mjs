import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import test from 'node:test';

import {
  SOURCE_STORAGE_KEY,
  SETTINGS_STORAGE_KEY,
  createSourceIdentityService,
  createStrategySourceStore,
} from './strategy-source-store.mjs';
import {
  STRATEGY_WORKSPACE_SELECTORS,
  mountStrategyWorkspace,
} from './strategy-workspace.mjs';

function eventTarget(extra = {}) {
  const listeners = new Map();
  const attributes = new Map();
  return Object.assign({
    hidden: false,
    disabled: false,
    readOnly: false,
    tabIndex: 0,
    value: '',
    textContent: '',
    dataset: {},
    files: [],
    addEventListener(type, listener) {
      const values = listeners.get(type) ?? [];
      values.push(listener);
      listeners.set(type, values);
    },
    removeEventListener(type, listener) {
      listeners.set(type, (listeners.get(type) ?? []).filter((item) => item !== listener));
    },
    async fire(type, event = {}) {
      const complete = Object.assign({
        key: '', keyCode: 0, ctrlKey: false, metaKey: false, shiftKey: false,
        isComposing: false, preventDefault() {}, stopPropagation() {},
      }, event);
      for (const listener of [...(listeners.get(type) ?? [])]) await listener(complete);
    },
    setAttribute(name, value) { attributes.set(name, String(value)); },
    removeAttribute(name) { attributes.delete(name); },
    getAttribute(name) { return attributes.get(name) ?? null; },
    listenerCount(type) { return (listeners.get(type) ?? []).length; },
  }, extra);
}

function workspaceDom(source = 'print(1)\n') {
  const values = {};
  for (const [name] of Object.entries(STRATEGY_WORKSPACE_SELECTORS)) values[name] = eventTarget();
  values.source.value = source;
  values.source.readOnly = true;
  values.panel.hidden = true;
  values.editor.hidden = true;
  const root = eventTarget({
    querySelector(selector) {
      const name = Object.entries(STRATEGY_WORKSPACE_SELECTORS)
        .find(([, expected]) => expected === selector)?.[0];
      return values[name] ?? null;
    },
  });
  return { root, values };
}

function memoryStorage() {
  const values = new Map();
  const writes = [];
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem(key, value) { values.set(key, String(value)); writes.push(key); },
    removeItem: (key) => values.delete(key),
    values,
    writes,
  };
}

function nodeSubtle() {
  return {
    async digest(_algorithm, bytes) {
      return createHash('sha256').update(bytes).digest();
    },
  };
}

function harness({ source = 'print(1)\n', storage = memoryStorage(), downloads = [], subtle = nodeSubtle() } = {}) {
  const { root, values } = workspaceDom(source);
  const windowObject = eventTarget({ matchMedia: () => ({ matches: false }) });
  const documentObject = eventTarget({ visibilityState: 'visible' });
  const calls = [];
  let controllerState = null;
  let downstream = null;
  const controller = {
    stop: async () => {},
  };
  const host = {
    controllerCallbacks(callbacks) {
      calls.push('controllerCallbacks');
      downstream = callbacks;
      return callbacks;
    },
    attachController(candidate) {
      assert.equal(candidate, controller);
      calls.push('attachController');
      controllerState = { tag: 'idle', loadedRevision: 0, runningRevision: null };
    },
    async load(request) {
      calls.push(['load', request]);
      controllerState = { tag: 'ready', loadedRevision: 1, runningRevision: null };
      downstream.onStateChange(controllerState);
      return { ok: true, loadedRevision: 1, sourceHash: request.sourceHash };
    },
    async start() {
      calls.push('start');
      controllerState = { tag: 'running', loadedRevision: 1, runningRevision: 1 };
      downstream.onStateChange(controllerState);
      return { ok: true, runId: 'run-1', runningRevision: 1 };
    },
    async stop() {
      calls.push('stop');
      controllerState = { tag: 'idle', loadedRevision: 1, runningRevision: null };
      downstream?.onStateChange(controllerState);
    },
    invalidate(cause) { calls.push(['invalidate', cause]); },
    getControllerState: () => controllerState,
    getDiagnostics: () => [],
    getLogs: () => null,
    getTerminalOutcome: () => null,
  };
  const fakeEditor = {
    mode: () => 'enhanced',
    publishDiagnostics() {},
    replaceSource(next) { calls.push(['editorReplace', next]); },
    dispose() { calls.push('editorDispose'); },
  };
  const release = {
    async loadEditor() {
      calls.push('loadEditor');
      return {
        toolkit: {},
        createStrategyEditor(options) {
          calls.push(['createEditor', options.getCanonicalSource()]);
          return fakeEditor;
        },
      };
    },
    async loadController({ callbacks, performanceClass }) {
      assert.equal(callbacks, downstream);
      assert.equal(performanceClass, 'desktop');
      calls.push('loadController');
      return controller;
    },
  };
  let nextId = 0;
  const workspace = mountStrategyWorkspace({
    root,
    host,
    release,
    storage,
    subtle,
    makeId: (prefix) => `${prefix}-${++nextId}`,
    windowObject,
    documentObject,
    confirmAction: () => true,
    download: (...args) => downloads.push(args),
  });
  return {
    workspace, root, values, windowObject, documentObject, storage, calls,
    host, release, controller, downloads,
    emitDiagnostic: (diagnostic) => downstream.onDiagnostic(diagnostic),
  };
}

test('bootstrap restores source as revision zero and never loads editor or runtime', async () => {
  const storage = memoryStorage();
  const store = createStrategySourceStore({
    storage,
    writerId: 'prior',
    makeId: () => 'prior-record',
    now: () => 1_700_000_000_000,
  });
  const saved = await createSourceIdentityService({ subtle: nodeSubtle() })
    .identifySnapshot('saved = True\n', 91);
  store.saveSource({ ...saved, expectedParentRecordId: null });
  storage.writes.length = 0;

  const app = harness({ storage });
  assert.equal(app.workspace.state().source, 'saved = True\n');
  assert.equal(app.workspace.state().draftRevision, 0);
  assert.equal(app.values.source.readOnly, false);
  assert.deepEqual(app.calls, []);
  assert.deepEqual(storage.writes, []);
  app.workspace.dispose();
});

test('first expansion loads only editor; first explicit Load constructs and attaches runtime', async () => {
  const app = harness();
  await app.values.toggle.fire('click');
  assert.deepEqual(app.calls, ['loadEditor', ['createEditor', 'print(1)\n']]);
  assert.equal(app.values.panel.hidden, false);

  await app.workspace.loadDraft();
  assert.deepEqual(app.calls.slice(2, 5), [
    'controllerCallbacks', 'loadController', 'attachController',
  ]);
  const loadCall = app.calls[5];
  assert.equal(loadCall[0], 'load');
  assert.equal(loadCall[1].source, 'print(1)\n');
  assert.match(loadCall[1].sourceHash, /^[0-9a-f]{64}$/);
  assert.equal(loadCall[1].draftRevision, 0);
  assert.equal(app.workspace.state().loadedSnapshot.loadedRevision, 1);
  app.workspace.dispose();
});

test('every accepted source change mirrors raw textarea before editor side effects', () => {
  const app = harness();
  app.workspace.acceptSource('next = "😀"\r\n', { origin: 'sample' });
  assert.equal(app.workspace.state().source, 'next = "😀"\n');
  assert.equal(app.values.source.value, 'next = "😀"\n');
  assert.equal(app.workspace.state().draftRevision, 1);
  assert.equal(app.workspace.state().dirty, true);
  app.workspace.dispose();
});

test('Save uses CAS/read-back and a divergent storage record becomes superseded', async () => {
  const app = harness();
  app.workspace.acceptSource('draft = 1\n');
  const result = await app.workspace.saveDraft();
  assert.equal(result.status, 'confirmed');
  assert.equal(app.workspace.state().dirty, false);
  const ownRecord = JSON.parse(app.storage.values.get(SOURCE_STORAGE_KEY));
  app.storage.values.set(SOURCE_STORAGE_KEY, JSON.stringify({
    ...ownRecord,
    writerId: 'other',
    recordId: 'other-record',
    sourceHash: 'b'.repeat(64),
    updatedAt: '2026-09-10T00:00:00.000Z',
  }));
  await app.windowObject.fire('storage', { key: SOURCE_STORAGE_KEY });
  assert.equal(app.workspace.state().superseded, true);
  assert.equal(app.values.reloadLatest.disabled, false);
  await app.workspace.reloadLatest();
  assert.equal(app.workspace.state().superseded, false);
  assert.equal(app.workspace.state().draftRevision, 2);
  app.workspace.dispose();
});

test('settings writes are independent and do not change draft state', async () => {
  const app = harness();
  const before = app.workspace.state();
  await app.values.toggle.fire('click');
  await app.workspace.flushSettings();
  assert.equal(app.storage.writes.includes(SOURCE_STORAGE_KEY), false);
  assert.deepEqual(app.storage.writes, [SETTINGS_STORAGE_KEY]);
  assert.equal(app.workspace.state().draftRevision, before.draftRevision);
  assert.equal(app.workspace.state().dirty, before.dirty);
  app.workspace.dispose();
});

test('a future source schema is export-or-clear only and is never overwritten', async () => {
  const storage = memoryStorage();
  storage.values.set(SOURCE_STORAGE_KEY, JSON.stringify({
    schemaVersion: 99,
    source: 'future = True\n',
    opaque: 'preserve-until-clear',
  }));
  const app = harness({ storage });

  assert.equal(app.workspace.state().futureSourceSchema, true);
  assert.equal(app.workspace.state().source, 'future = True\n');
  assert.equal(app.values.source.readOnly, true);
  assert.equal(app.values.save.disabled, true);
  assert.equal(app.values.load.disabled, true);
  await app.values.exportSource.fire('click');
  assert.equal(app.downloads[0][1], 'future = True\n');
  assert.equal(JSON.parse(storage.values.get(SOURCE_STORAGE_KEY)).schemaVersion, 99);

  await app.values.clearSource.fire('click');
  assert.equal(storage.values.has(SOURCE_STORAGE_KEY), false);
  assert.equal(app.workspace.state().futureSourceSchema, false);
  assert.equal(app.values.source.readOnly, false);
  app.workspace.dispose();
});

test('Load snapshots are immutable and Restart reloads last loaded source, not newer draft', async () => {
  const app = harness();
  await app.workspace.loadDraft();
  app.workspace.acceptSource('newer = True\n');
  await app.workspace.restart();
  const loads = app.calls.filter((call) => Array.isArray(call) && call[0] === 'load');
  assert.equal(loads.length, 2);
  assert.equal(loads[1][1].source, 'print(1)\n');
  assert.equal(app.calls.at(-1), 'start');
  app.workspace.dispose();
});

test('common shortcuts map Load, Start, Stop and diagnostics without composition leaks', async () => {
  const app = harness();
  await app.root.fire('keydown', { key: 'Enter', ctrlKey: true, shiftKey: true });
  assert.equal(app.calls.filter((call) => Array.isArray(call) && call[0] === 'load').length, 1);
  await app.root.fire('keydown', { key: 'Enter', ctrlKey: true });
  assert.equal(app.calls.includes('start'), true);
  await app.root.fire('keydown', { key: '.', ctrlKey: true });
  assert.equal(app.calls.includes('stop'), true);

  await app.values.source.fire('compositionstart');
  const loadCount = app.calls.filter((call) => Array.isArray(call) && call[0] === 'load').length;
  await app.root.fire('keydown', { key: 'Enter', ctrlKey: true, shiftKey: true });
  assert.equal(app.calls.filter((call) => Array.isArray(call) && call[0] === 'load').length, loadCount);
  await app.values.source.fire('compositionend');

  const current = app.workspace.state();
  app.emitDiagnostic({
    protocol_version: 1,
    phase: 'load', severity: 'error', category: 'syntax', message: 'go here',
    source_hash: current.sourceHash, draft_revision: current.draftRevision,
    loaded_revision: 1, running_revision: null, load_attempt_id: 'x', run_id: null,
    sequence: 0,
    range: { start: { line: 1, column: 0 }, end: { line: 1, column: 5 } },
  });
  await app.root.fire('keydown', { key: 'F8' });
  assert.equal(app.values.status.textContent, 'error: go here');
  app.workspace.dispose();
});

test('new Load wins reversed hashing and Stop settles outstanding hash without publication', async () => {
  const hashes = [];
  const app = harness({ subtle: { digest: (_algorithm, bytes) => new Promise((resolve) => {
    hashes.push({ bytes, resolve });
  }) } });
  const first = app.workspace.loadDraft({ source: 'first = 1', draftRevision: 1 }).catch((error) => error);
  await new Promise((resolve) => setImmediate(resolve));
  const second = app.workspace.loadDraft({ source: 'second = 2', draftRevision: 2 });
  await new Promise((resolve) => setImmediate(resolve));
  hashes.at(-1).resolve(createHash('sha256').update(hashes.at(-1).bytes).digest());
  await second;
  hashes[0].resolve(createHash('sha256').update(hashes[0].bytes).digest());
  assert.equal((await first).code, 'load_superseded');
  assert.equal(app.workspace.state().loadedSnapshot.source, 'second = 2');
  const pending = app.workspace.loadDraft().catch((error) => error);
  await new Promise((resolve) => setImmediate(resolve));
  await app.workspace.stop();
  const outcome = await Promise.race([pending, new Promise((resolve) => setImmediate(() => resolve('unsettled')))]);
  assert.equal(outcome.code, 'load_superseded');
  app.workspace.dispose();
});

test('Stop and dispose before controller acquisition prevent any subsequent Load', async () => {
  for (const action of ['stop', 'dispose']) {
    const app = harness();
    let releaseController;
    app.release.loadController = () => new Promise((resolve) => { releaseController = resolve; });
    const pending = app.workspace.loadDraft().catch((error) => error);
    await app.workspace[action]();
    releaseController(app.controller);
    const result = await pending;
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(result.code, 'load_superseded');
    assert.equal(app.calls.some((call) => Array.isArray(call) && call[0] === 'load'), false);
    assert.equal(app.calls.includes('attachController'), false);
    assert.equal(app.workspace.state().loadedSnapshot, null);
    app.workspace.dispose();
  }
});

test('Load invalidates the prior controller before hashing a replacement', async () => {
  const app = harness();
  await app.workspace.loadDraft();
  app.calls.length = 0;
  const next = app.workspace.loadDraft();
  assert.deepEqual(app.calls[0], ['invalidate', 'explicit_load']);
  await next;
  app.workspace.dispose();
});

test('canceled UI Loads settle quietly and raw recovery cancels pending hashing', async () => {
  const app = harness({ subtle: { digest: () => new Promise(() => {}) } });
  const clicking = app.values.load.fire('click');
  await new Promise((resolve) => setImmediate(resolve));
  await app.workspace.stop();
  await clicking;
  assert.equal(app.values.status.textContent.includes('失敗しました'), false);
  const pending = app.workspace.loadDraft().catch((error) => error);
  await new Promise((resolve) => setImmediate(resolve));
  app.workspace.enterRawRecovery(new Error('recovery'));
  const outcome = await Promise.race([pending, new Promise((resolve) => setImmediate(() => resolve('unsettled')))]);
  assert.equal(outcome.code, 'load_superseded');
  app.workspace.dispose();
});

test('Import rejects oversized and wrong-extension files before reading and preserves identity', async () => {
  const app = harness();
  await app.workspace.loadDraft();
  for (const file of [{ name: 'huge.py', size: 300_000 }, { name: 'source.txt', size: 2 }]) {
    let reads = 0;
    const before = app.workspace.state();
    app.values.importInput.files = [{ ...file, arrayBuffer() { reads += 1; throw new Error('must not read'); } }];
    await app.values.importInput.fire('change');
    assert.equal(reads, 0);
    for (const key of ['source', 'sourceHash', 'draftRevision', 'loadedSnapshot']) {
      assert.deepEqual(app.workspace.state()[key], before[key]);
    }
  }
  app.workspace.dispose();
});

test('Import/export, diagnostics export, raw recovery, and disposal preserve source', async () => {
  const app = harness();
  app.values.importInput.files = [{
    name: 'INPUT.PY',
    async arrayBuffer() { return new TextEncoder().encode('\ufeffx = 2\r\n').buffer; },
  }];
  await app.values.importInput.fire('change');
  assert.equal(app.workspace.state().source, 'x = 2\n');
  await app.values.exportSource.fire('click');
  assert.deepEqual(app.downloads[0].slice(0, 2), ['strategy.py', 'x = 2\n']);

  app.workspace.enterRawRecovery(new Error('<host failed>'));
  assert.equal(app.workspace.state().rawRecovery, true);
  assert.equal(app.values.source.value, 'x = 2\n');
  assert.equal(app.values.source.readOnly, true);
  assert.equal(app.root.dataset.strategyMode, 'raw-recovery');
  assert.equal(app.values.status.textContent.includes('<host failed>'), true);
  assert.equal(app.values.load.disabled, true);
  app.workspace.dispose();
  app.workspace.dispose();
  assert.equal(app.windowObject.listenerCount('beforeunload'), 0);
});

import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import test from 'node:test';
import { createRequestEnvelope } from './strategy-protocol.mjs';

function validateSourceText(source) {
  return createRequestEnvelope({ phase: 'load', requestId: 'parity',
    session: { stem: '7203', code: '7203', actual_date: '2026-09-10', generation: 1 },
    runId: null, source: { source_hash: 'a'.repeat(64), strategy_revision: 1, load_attempt_id: 'parity' },
    sequence: 0, payload: { source, filename: 'strategy.py', assets: {
      loader_url: '/loader.mjs', index_url: '/pyodide/', runner_url: '/runner.py',
    } } });
}

import {
  AUTOSAVE_DELAY_MS,
  SOURCE_LIMITS,
  SOURCE_STORAGE_KEY,
  SETTINGS_STORAGE_KEY,
  SourceStoreError,
  canonicalizeSource,
  createAutosaveScheduler,
  createSourceIdentityService,
  createStrategySourceStore,
  decodePythonFile,
  validateCanonicalSource,
} from './strategy-source-store.mjs';

function memoryStorage() {
  const values = new Map();
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: (key) => values.delete(key),
    values,
  };
}

function nodeSubtle() {
  return {
    async digest(_algorithm, bytes) {
      return createHash('sha256').update(bytes).digest();
    },
  };
}

test('source canonicalization removes one BOM and normalizes every newline', () => {
  assert.equal(canonicalizeSource('\ufeffa\r\nb\rc'), 'a\nb\nc');
  assert.equal(canonicalizeSource('\ufeff\ufeffa'), '\ufeffa');
});

test('source limits reject each independent dimension without changing source', () => {
  assert.equal(validateCanonicalSource(''), '');
  assert.throws(
    () => validateCanonicalSource('x'.repeat(SOURCE_LIMITS.utf8Bytes + 1)),
    (error) => error instanceof SourceStoreError && error.code === 'source_bytes_limit',
  );
  assert.throws(
    () => validateCanonicalSource(`${'x\n'.repeat(SOURCE_LIMITS.lines)}x`),
    (error) => error.code === 'source_lines_limit',
  );
  assert.throws(
    () => validateCanonicalSource('x'.repeat(SOURCE_LIMITS.longestLineScalars + 1)),
    (error) => error.code === 'source_line_limit',
  );
  assert.throws(
    () => validateCanonicalSource('😀'.repeat(SOURCE_LIMITS.unicodeScalars + 1)),
    (error) => ['source_bytes_limit', 'source_scalars_limit'].includes(error.code),
  );
});

test('identity attempts publish only the latest source and snapshots stay immutable', async () => {
  const pending = [];
  const identity = createSourceIdentityService({
    subtle: {
      digest(_algorithm, bytes) {
        return new Promise((resolve) => pending.push({ bytes, resolve }));
      },
    },
  });

  const first = identity.identifyCurrent('first', 1);
  const second = identity.identifyCurrent('second', 2);
  pending[1].resolve(createHash('sha256').update(pending[1].bytes).digest());
  const secondResult = await second;
  pending[0].resolve(createHash('sha256').update(pending[0].bytes).digest());
  const firstResult = await first;

  assert.equal(secondResult.current, true);
  assert.equal(firstResult.current, false);
  assert.equal(identity.current().source, 'second');

  const captured = identity.identifySnapshot('saved', 3);
  identity.identifyCurrent('newer', 4).catch(() => {});
  pending[2].resolve(createHash('sha256').update(pending[2].bytes).digest());
  assert.deepEqual(await captured, {
    source: 'saved',
    draftRevision: 3,
    hashAttemptId: 3,
    sourceHash: createHash('sha256').update('saved').digest('hex'),
    current: false,
  });
});

test('missing or rejecting Web Crypto fails closed with hash_unavailable', async () => {
  await assert.rejects(
    createSourceIdentityService({ subtle: null }).identifySnapshot('x', 0),
    (error) => error.code === 'hash_unavailable',
  );
  await assert.rejects(
    createSourceIdentityService({
      subtle: { digest: async () => { throw new Error('unavailable'); } },
    }).identifySnapshot('x', 0),
    (error) => error.code === 'hash_unavailable',
  );
});

test('source save uses parent comparison and unique candidate read-back', async () => {
  const storage = memoryStorage();
  let nextId = 0;
  const store = createStrategySourceStore({
    storage,
    makeId: (prefix) => `${prefix}-${++nextId}`,
    now: () => 1_750_000_000_000,
  });
  const identity = await createSourceIdentityService({ subtle: nodeSubtle() })
    .identifySnapshot('print(1)\n', 1);

  const confirmed = store.saveSource({ ...identity, expectedParentRecordId: null });
  assert.equal(confirmed.status, 'confirmed');
  assert.equal(confirmed.record.parentRecordId, null);
  assert.equal(store.readSource().record.recordId, confirmed.record.recordId);

  storage.setItem(SOURCE_STORAGE_KEY, JSON.stringify({
    ...confirmed.record,
    recordId: 'other-tab',
  }));
  const conflict = store.saveSource({
    ...identity,
    draftRevision: 2,
    expectedParentRecordId: confirmed.record.recordId,
  });
  assert.equal(conflict.status, 'conflict');
  assert.equal(conflict.current.recordId, 'other-tab');

  const overwritten = store.saveSource({
    ...identity,
    draftRevision: 2,
    expectedParentRecordId: confirmed.record.recordId,
    overwrite: true,
  });
  assert.equal(overwritten.status, 'confirmed');
  assert.equal(overwritten.record.parentRecordId, 'other-tab');
});

test('read-back mismatch is unconfirmed and future schemas are never overwritten', async () => {
  const storage = memoryStorage();
  const identity = await createSourceIdentityService({ subtle: nodeSubtle() })
    .identifySnapshot('x', 1);
  const store = createStrategySourceStore({ storage, makeId: () => 'candidate' });
  const originalSet = storage.setItem;
  storage.setItem = (key, value) => {
    originalSet(key, value);
    if (key === SOURCE_STORAGE_KEY) {
      const record = JSON.parse(value);
      originalSet(key, JSON.stringify({ ...record, recordId: 'racing-tab' }));
    }
  };
  assert.equal(
    store.saveSource({ ...identity, expectedParentRecordId: null }).status,
    'unconfirmed',
  );

  storage.setItem = originalSet;
  storage.setItem(SOURCE_STORAGE_KEY, JSON.stringify({ schemaVersion: 99, source: 'future' }));
  assert.equal(store.readSource().status, 'future_schema');
  assert.throws(
    () => store.saveSource({ ...identity, expectedParentRecordId: null, overwrite: true }),
    (error) => error.code === 'future_schema',
  );
  assert.throws(() => store.clearSource(), (error) => error.code === 'future_schema');
  assert.equal(store.clearSource({ allowFutureSchema: true }).status, 'cleared');
});

test('settings use an independent key and clearing source preserves them', () => {
  const storage = memoryStorage();
  const store = createStrategySourceStore({ storage, makeId: () => 'settings-1' });
  assert.equal(store.saveSettings({ panelExpanded: true }).status, 'confirmed');
  assert.equal(storage.getItem(SOURCE_STORAGE_KEY), null);
  store.clearSource();
  assert.ok(storage.getItem(SETTINGS_STORAGE_KEY));
  assert.deepEqual(store.readSettings().record.settings, { panelExpanded: true });
});

test('storage exceptions are typed and preserve the other record', () => {
  const storage = memoryStorage();
  storage.setItem(SETTINGS_STORAGE_KEY, '{"schemaVersion":1}');
  storage.getItem = () => { throw new Error('blocked'); };
  const store = createStrategySourceStore({ storage });
  assert.throws(() => store.readSource(), (error) => error.code === 'storage_unavailable');
  assert.equal(storage.values.get(SETTINGS_STORAGE_KEY), '{"schemaVersion":1}');
});

test('autosave waits 750ms, resets on edits, flushes immediately, and disposes', async () => {
  const timers = new Map();
  const saved = [];
  let nextTimer = 0;
  const scheduler = createAutosaveScheduler({
    save: async (snapshot) => saved.push(snapshot),
    setTimer: (callback, delay) => {
      assert.equal(delay, AUTOSAVE_DELAY_MS);
      const id = ++nextTimer;
      timers.set(id, callback);
      return id;
    },
    clearTimer: (id) => timers.delete(id),
  });
  scheduler.schedule({ revision: 1 });
  scheduler.schedule({ revision: 2 });
  assert.equal(timers.size, 1);
  await scheduler.flush();
  assert.deepEqual(saved, [{ revision: 2 }]);
  scheduler.schedule({ revision: 3 });
  scheduler.cancel();
  assert.equal(timers.size, 0);
  scheduler.schedule({ revision: 4 });
  scheduler.dispose();
  assert.equal(timers.size, 0);
});

test('Python import accepts one strict UTF-8 .py file and is non-destructive on error', () => {
  const bytes = new TextEncoder().encode('\ufeffprint("日本語")\r\n');
  assert.equal(decodePythonFile('STRATEGY.PY', bytes), 'print("日本語")\n');
  assert.throws(() => decodePythonFile('strategy.txt', bytes), (error) => error.code === 'file_type');
  assert.throws(
    () => decodePythonFile('strategy.py', Uint8Array.from([0xc3, 0x28])),
    (error) => error.code === 'invalid_utf8',
  );
});

test('physical line boundaries match protocol with final LF, BOM and CRLF', () => {
  for (const count of [8192, 8193]) {
    for (const ending of ['', '\n']) {
      const source = Array(count).fill('#').join('\n') + ending;
      for (const raw of [source, `\ufeff${source.replaceAll('\n', '\r\n')}`]) {
        const canonical = canonicalizeSource(raw);
        if (count === 8192) {
          assert.equal(validateCanonicalSource(canonical), canonical);
          assert.doesNotThrow(() => validateSourceText(canonical));
          assert.equal(decodePythonFile('source.py', new TextEncoder().encode(raw)), canonical);
        } else {
          assert.throws(() => validateCanonicalSource(canonical), { code: 'source_lines_limit' });
          assert.throws(() => validateSourceText(canonical));
        }
      }
    }
  }
});

test('raw byte bound precedes decoding while bounded BOM and CRLF overhead is accepted', () => {
  assert.throws(() => decodePythonFile('source.py', new Uint8Array(270_340).fill(255)),
    { code: 'source_bytes_limit' });
  const canonical = `${'x'.repeat(31)}\n`.repeat(8192);
  assert.equal(new TextEncoder().encode(canonical).byteLength, SOURCE_LIMITS.utf8Bytes);
  assert.equal(decodePythonFile('source.py', new TextEncoder().encode(`\ufeff${canonical.replaceAll('\n', '\r\n')}`)), canonical);
});

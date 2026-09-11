'use strict';

export const SOURCE_SCHEMA_VERSION = 1;
export const SETTINGS_SCHEMA_VERSION = 1;
export const SOURCE_STORAGE_KEY = 'tickreplay.strategy.source.v1';
export const SETTINGS_STORAGE_KEY = 'tickreplay.strategy.settings.v1';
export const AUTOSAVE_DELAY_MS = 750;
export const SOURCE_LIMITS = Object.freeze({
  utf8Bytes: 262_144,
  unicodeScalars: 262_144,
  lines: 8_192,
  longestLineScalars: 4_096,
});

const HASH_PATTERN = /^[0-9a-f]{64}$/;
const SOURCE_RECORD_KEYS = Object.freeze([
  'schemaVersion', 'writerId', 'recordId', 'parentRecordId', 'draftRevision',
  'sourceHash', 'source', 'updatedAt',
]);

export class SourceStoreError extends Error {
  constructor(code, message = code, detail = null) {
    super(message);
    this.name = 'SourceStoreError';
    this.code = code;
    this.detail = detail;
  }
}

function freeze(value) {
  if (value && typeof value === 'object' && !Object.isFrozen(value)) {
    for (const child of Object.values(value)) freeze(child);
    Object.freeze(value);
  }
  return value;
}

function defaultId(prefix) {
  if (globalThis.crypto?.randomUUID) return `${prefix}-${globalThis.crypto.randomUUID()}`;
  throw new SourceStoreError('identity_unavailable', 'cryptographic random UUID is unavailable');
}

function utf8Bytes(value) {
  return new TextEncoder().encode(value).byteLength;
}

export function canonicalizeSource(value) {
  if (typeof value !== 'string') throw new SourceStoreError('invalid_source', 'source must be text');
  const withoutBom = value.startsWith('\ufeff') ? value.slice(1) : value;
  return withoutBom.replaceAll('\r\n', '\n').replaceAll('\r', '\n');
}

export function validateCanonicalSource(source) {
  if (typeof source !== 'string') throw new SourceStoreError('invalid_source', 'source must be text');
  if (source !== canonicalizeSource(source)) {
    throw new SourceStoreError('noncanonical_source', 'source must be canonical before validation');
  }
  if (utf8Bytes(source) > SOURCE_LIMITS.utf8Bytes) {
    throw new SourceStoreError('source_bytes_limit', 'source exceeds 262144 UTF-8 bytes');
  }
  const scalarCount = Array.from(source).length;
  if (scalarCount > SOURCE_LIMITS.unicodeScalars) {
    throw new SourceStoreError('source_scalars_limit', 'source exceeds 262144 Unicode scalars');
  }
  const lines = source.split('\n');
  if (lines.length > SOURCE_LIMITS.lines) {
    throw new SourceStoreError('source_lines_limit', 'source exceeds 8192 lines');
  }
  if (lines.some((line) => Array.from(line).length > SOURCE_LIMITS.longestLineScalars)) {
    throw new SourceStoreError('source_line_limit', 'source has a line over 4096 Unicode scalars');
  }
  return source;
}

function hexDigest(buffer) {
  return [...new Uint8Array(buffer)]
    .map((byte) => byte.toString(16).padStart(2, '0'))
    .join('');
}

export function createSourceIdentityService({ subtle = globalThis.crypto?.subtle ?? null } = {}) {
  let nextAttemptId = 1;
  let latestAttempt = null;
  let published = null;

  async function identify(source, draftRevision, publish) {
    const canonicalSource = validateCanonicalSource(canonicalizeSource(source));
    if (!Number.isSafeInteger(draftRevision) || draftRevision < 0) {
      throw new SourceStoreError('invalid_revision', 'draft revision must be a non-negative integer');
    }
    const attempt = freeze({
      source: canonicalSource,
      draftRevision,
      hashAttemptId: nextAttemptId++,
    });
    if (publish) latestAttempt = attempt;
    if (!subtle || typeof subtle.digest !== 'function') {
      throw new SourceStoreError('hash_unavailable', 'Web Crypto SHA-256 is unavailable');
    }
    let digest;
    try {
      digest = await subtle.digest('SHA-256', new TextEncoder().encode(canonicalSource));
    } catch (error) {
      throw new SourceStoreError('hash_unavailable', 'Web Crypto SHA-256 failed', error);
    }
    const current = Boolean(
      publish
      && latestAttempt?.source === attempt.source
      && latestAttempt?.draftRevision === attempt.draftRevision
      && latestAttempt?.hashAttemptId === attempt.hashAttemptId
    );
    const result = freeze({ ...attempt, sourceHash: hexDigest(digest), current });
    if (current) published = result;
    return result;
  }

  return freeze({
    identifyCurrent: (source, draftRevision) => identify(source, draftRevision, true),
    identifySnapshot: (source, draftRevision) => identify(source, draftRevision, false),
    current: () => published,
  });
}

function parseJson(raw, kind) {
  try {
    const value = JSON.parse(raw);
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('not an object');
    return value;
  } catch (error) {
    return { status: 'corrupt', kind, raw, error: String(error) };
  }
}

function validateSourceRecord(value) {
  if (value.schemaVersion > SOURCE_SCHEMA_VERSION) {
    return freeze({ status: 'future_schema', rawRecord: value });
  }
  const exactKeys = Object.keys(value).sort();
  if (
    value.schemaVersion !== SOURCE_SCHEMA_VERSION
    || JSON.stringify(exactKeys) !== JSON.stringify([...SOURCE_RECORD_KEYS].sort())
    || ![value.writerId, value.recordId, value.updatedAt].every(
      (item) => typeof item === 'string' && item.length > 0,
    )
    || !(value.parentRecordId === null || typeof value.parentRecordId === 'string')
    || !Number.isSafeInteger(value.draftRevision)
    || value.draftRevision < 0
    || typeof value.source !== 'string'
    || value.source !== canonicalizeSource(value.source)
    || !HASH_PATTERN.test(value.sourceHash)
  ) return freeze({ status: 'corrupt', rawRecord: value });
  try {
    validateCanonicalSource(value.source);
  } catch {
    return freeze({ status: 'corrupt', rawRecord: value });
  }
  return freeze({ status: 'available', record: value });
}

function validateSettingsRecord(value) {
  if (value.schemaVersion > SETTINGS_SCHEMA_VERSION) {
    return freeze({ status: 'future_schema', rawRecord: value });
  }
  if (
    value.schemaVersion !== SETTINGS_SCHEMA_VERSION
    || typeof value.writerId !== 'string'
    || typeof value.recordId !== 'string'
    || typeof value.updatedAt !== 'string'
    || !value.settings
    || typeof value.settings !== 'object'
    || Array.isArray(value.settings)
  ) return freeze({ status: 'corrupt', rawRecord: value });
  return freeze({ status: 'available', record: value });
}

export function createStrategySourceStore({
  storage = globalThis.localStorage,
  writerId = null,
  makeId = defaultId,
  now = Date.now,
} = {}) {
  const resolvedWriterId = writerId ?? makeId('writer');

  function read(key, kind, validate) {
    let raw;
    try {
      raw = storage?.getItem(key);
    } catch (error) {
      throw new SourceStoreError('storage_unavailable', `${kind} storage is unavailable`, error);
    }
    if (raw === null) return freeze({ status: 'missing' });
    const parsed = parseJson(raw, kind);
    if (parsed.status === 'corrupt') return freeze(parsed);
    return validate(parsed);
  }

  function readSource() {
    return read(SOURCE_STORAGE_KEY, 'source', validateSourceRecord);
  }

  function readSettings() {
    return read(SETTINGS_STORAGE_KEY, 'settings', validateSettingsRecord);
  }

  function write(key, record, kind) {
    const serialized = JSON.stringify(record);
    try {
      storage.setItem(key, serialized);
    } catch (error) {
      throw new SourceStoreError('storage_write_failed', `${kind} could not be saved`, error);
    }
  }

  function saveSource({
    source,
    sourceHash,
    draftRevision,
    expectedParentRecordId,
    overwrite = false,
  }) {
    validateCanonicalSource(source);
    if (!HASH_PATTERN.test(sourceHash)) throw new SourceStoreError('invalid_hash');
    const currentResult = readSource();
    if (currentResult.status === 'future_schema') {
      throw new SourceStoreError('future_schema', 'newer source data will not be overwritten');
    }
    if (currentResult.status === 'corrupt' && !overwrite) {
      return freeze({ status: 'conflict', current: currentResult });
    }
    const current = currentResult.status === 'available' ? currentResult.record : null;
    if (!overwrite && (current?.recordId ?? null) !== expectedParentRecordId) {
      return freeze({ status: 'conflict', current });
    }
    const record = freeze({
      schemaVersion: SOURCE_SCHEMA_VERSION,
      writerId: resolvedWriterId,
      recordId: makeId('source'),
      parentRecordId: current?.recordId ?? null,
      draftRevision,
      sourceHash,
      source,
      updatedAt: new Date(now()).toISOString(),
    });
    write(SOURCE_STORAGE_KEY, record, 'source');
    const readBack = readSource();
    if (readBack.status !== 'available' || readBack.record.recordId !== record.recordId) {
      return freeze({
        status: 'unconfirmed',
        record,
        current: readBack.status === 'available' ? readBack.record : readBack,
      });
    }
    return freeze({ status: 'confirmed', record });
  }

  function saveSettings(settings) {
    if (!settings || typeof settings !== 'object' || Array.isArray(settings)) {
      throw new SourceStoreError('invalid_settings');
    }
    const record = freeze({
      schemaVersion: SETTINGS_SCHEMA_VERSION,
      writerId: resolvedWriterId,
      recordId: makeId('settings'),
      settings: JSON.parse(JSON.stringify(settings)),
      updatedAt: new Date(now()).toISOString(),
    });
    write(SETTINGS_STORAGE_KEY, record, 'settings');
    return freeze({ status: 'confirmed', record });
  }

  function clearSource({ allowFutureSchema = false } = {}) {
    const current = readSource();
    if (current.status === 'future_schema' && !allowFutureSchema) {
      throw new SourceStoreError('future_schema', 'newer source data requires explicit migration');
    }
    try {
      storage.removeItem(SOURCE_STORAGE_KEY);
    } catch (error) {
      throw new SourceStoreError('storage_write_failed', 'source could not be cleared', error);
    }
    return freeze({ status: 'cleared' });
  }

  return freeze({ readSource, readSettings, saveSource, saveSettings, clearSource });
}

export function createAutosaveScheduler({
  save,
  delay = AUTOSAVE_DELAY_MS,
  setTimer = globalThis.setTimeout.bind(globalThis),
  clearTimer = globalThis.clearTimeout.bind(globalThis),
}) {
  let timer = null;
  let pendingSnapshot = null;
  let disposed = false;

  async function flush() {
    if (disposed || pendingSnapshot === null) return null;
    if (timer !== null) clearTimer(timer);
    timer = null;
    const snapshot = pendingSnapshot;
    pendingSnapshot = null;
    return save(snapshot);
  }

  function schedule(snapshot) {
    if (disposed) return false;
    if (timer !== null) clearTimer(timer);
    pendingSnapshot = freeze({ ...snapshot });
    timer = setTimer(() => {
      timer = null;
      void flush();
    }, delay);
    return true;
  }

  function dispose() {
    disposed = true;
    if (timer !== null) clearTimer(timer);
    timer = null;
    pendingSnapshot = null;
  }

  function cancel() {
    if (timer !== null) clearTimer(timer);
    timer = null;
    pendingSnapshot = null;
  }

  return freeze({
    schedule,
    flush,
    cancel,
    dispose,
    pending: () => pendingSnapshot !== null,
  });
}

export function decodePythonFile(name, bytes) {
  if (typeof name !== 'string' || name.includes('/') || name.includes('\\') || !/\.py$/i.test(name)) {
    throw new SourceStoreError('file_type', 'Import requires one .py file');
  }
  let source;
  try {
    source = new TextDecoder('utf-8', { fatal: true }).decode(bytes);
  } catch (error) {
    throw new SourceStoreError('invalid_utf8', 'Import must be strict UTF-8', error);
  }
  return validateCanonicalSource(canonicalizeSource(source));
}

'use strict';

import {
  encodedByteLength,
  validateCommandBatch,
} from './strategy-protocol.mjs';

export const NATIVE_TRACE_CONTRACT = 'native_trace_v1';

export const NATIVE_TRACE_LIMITS = Object.freeze({
  sourceBytes: 262_144,
  decisions: 10_000,
  commandsPerDecision: 32,
  commandsPerRun: 10_000,
  artifactBytes: 4_194_304,
  stringBytes: 2_048,
  nestingDepth: 12,
});

const HASH_PATTERN = /^[0-9a-f]{64}$/;
const JOB_ID_PATTERN = /^[0-9a-f]{32}$/;
const DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;
const JOB_STATES = new Set([
  'queued', 'running', 'validating', 'ready', 'failed', 'cancelled', 'timed_out',
]);
const FAILURE_STATES = new Set(['failed', 'timed_out']);

export class NativeTraceProtocolError extends Error {
  constructor(code, message, path = '$') {
    super(`${message} (${path})`);
    this.name = 'NativeTraceProtocolError';
    this.code = code;
    this.path = path;
  }
}

function fail(code, message, path) {
  throw new NativeTraceProtocolError(code, message, path);
}

function plain(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function clonePlain(value, path = '$', seen = new Set(), depth = 0) {
  if (depth > NATIVE_TRACE_LIMITS.nestingDepth) fail('nesting_limit', 'value is nested too deeply', path);
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return value;
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) fail('non_finite', 'number must be finite', path);
    return value;
  }
  if (typeof value !== 'object' || (!Array.isArray(value) && !plain(value))) {
    fail('non_plain_value', 'expected plain data', path);
  }
  if (seen.has(value)) fail('cyclic_value', 'cyclic value is not allowed', path);
  seen.add(value);
  try {
    if (Array.isArray(value)) {
      return value.map((child, index) => clonePlain(child, `${path}[${index}]`, seen, depth + 1));
    }
    const result = {};
    for (const key of Object.keys(value)) {
      if (value[key] === undefined) fail('non_plain_value', 'undefined is not allowed', `${path}.${key}`);
      result[key] = clonePlain(value[key], `${path}.${key}`, seen, depth + 1);
    }
    return result;
  } finally {
    seen.delete(value);
  }
}

function freeze(value) {
  if (value && typeof value === 'object' && !Object.isFrozen(value)) {
    for (const child of Object.values(value)) freeze(child);
    Object.freeze(value);
  }
  return value;
}

function exact(value, required, optional, path) {
  if (!plain(value)) fail('non_plain_value', 'expected a plain object', path);
  const allowed = new Set([...required, ...optional]);
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) fail('unknown_field', `unknown field ${key}`, `${path}.${key}`);
  }
  for (const key of required) {
    if (!Object.hasOwn(value, key)) fail('missing_field', `missing field ${key}`, path);
  }
}

function string(value, path, { pattern = null, maximumBytes = NATIVE_TRACE_LIMITS.stringBytes } = {}) {
  if (typeof value !== 'string' || value.length === 0) fail('invalid_string', 'expected non-empty string', path);
  if (encodedByteLength(value) > maximumBytes) fail('string_limit', 'string is too long', path);
  if (pattern && !pattern.test(value)) fail('invalid_string', 'string has invalid format', path);
}

function integer(value, path, minimum = 0) {
  if (!Number.isSafeInteger(value) || value < minimum) {
    fail('invalid_integer', `expected a safe integer >= ${minimum}`, path);
  }
}

function hash(value, path) {
  string(value, path, { pattern: HASH_PATTERN, maximumBytes: 64 });
}

function validateApiSession(value, path) {
  exact(value, ['stem', 'code', 'date', 'dataHash'], [], path);
  string(value.stem, `${path}.stem`);
  string(value.code, `${path}.code`);
  string(value.date, `${path}.date`, { pattern: DATE_PATTERN });
  hash(value.dataHash, `${path}.dataHash`);
}

export function validateNativeTraceCapability(value, path = '$') {
  const copy = clonePlain(value, path);
  exact(copy, ['available', 'contractVersion', 'trustedLocalOnly', 'limits'], [], path);
  if (typeof copy.available !== 'boolean') fail('invalid_boolean', 'available must be boolean', `${path}.available`);
  if (copy.contractVersion !== NATIVE_TRACE_CONTRACT) fail('version_mismatch', 'unsupported contract version', `${path}.contractVersion`);
  if (copy.trustedLocalOnly !== true) fail('trust_boundary', 'native execution must be trusted/local only', `${path}.trustedLocalOnly`);
  exact(copy.limits, ['sourceBytes', 'decisions', 'commandsPerDecision', 'commandsPerRun', 'artifactBytes'], [], `${path}.limits`);
  for (const [key, expected] of Object.entries({
    sourceBytes: NATIVE_TRACE_LIMITS.sourceBytes,
    decisions: NATIVE_TRACE_LIMITS.decisions,
    commandsPerDecision: NATIVE_TRACE_LIMITS.commandsPerDecision,
    commandsPerRun: NATIVE_TRACE_LIMITS.commandsPerRun,
    artifactBytes: NATIVE_TRACE_LIMITS.artifactBytes,
  })) {
    if (copy.limits[key] !== expected) fail('limit_mismatch', `unexpected ${key} limit`, `${path}.limits.${key}`);
  }
  return freeze(copy);
}

export function createNativeTraceSubmitRequest({ source, sourceHash, session }) {
  if (typeof source !== 'string' || encodedByteLength(source) === 0) {
    fail('invalid_source', 'source must be non-empty', '$.source');
  }
  if (encodedByteLength(source) > NATIVE_TRACE_LIMITS.sourceBytes) {
    fail('source_limit', 'source is too large', '$.source');
  }
  if (source.startsWith('\ufeff') || source.includes('\r')) {
    fail('source_canonicalization', 'source must use LF without a BOM', '$.source');
  }
  hash(sourceHash, '$.sourceHash');
  const sessionCopy = clonePlain(session, '$.session');
  validateApiSession(sessionCopy, '$.session');
  return freeze({ contractVersion: NATIVE_TRACE_CONTRACT, source, sourceHash, session: sessionCopy });
}

export function validateNativeTraceJob(value, path = '$') {
  const copy = clonePlain(value, path);
  exact(copy, ['jobId', 'inputId', 'state', 'revision', 'traceId', 'error'], [], path);
  string(copy.jobId, `${path}.jobId`, { pattern: JOB_ID_PATTERN, maximumBytes: 32 });
  hash(copy.inputId, `${path}.inputId`);
  if (!JOB_STATES.has(copy.state)) fail('invalid_state', 'unknown job state', `${path}.state`);
  integer(copy.revision, `${path}.revision`);
  if (copy.traceId !== null) hash(copy.traceId, `${path}.traceId`);
  if (copy.error !== null) {
    exact(copy.error, ['code', 'message'], [], `${path}.error`);
    string(copy.error.code, `${path}.error.code`);
    string(copy.error.message, `${path}.error.message`);
  }
  if (copy.state === 'ready' && (copy.traceId === null || copy.error !== null)) {
    fail('invalid_job', 'ready job requires only a trace ID', path);
  }
  if (FAILURE_STATES.has(copy.state) && (copy.traceId !== null || copy.error === null)) {
    fail('invalid_job', 'failed job requires only a public error', path);
  }
  if (!FAILURE_STATES.has(copy.state) && copy.state !== 'ready' && copy.traceId !== null) {
    fail('invalid_job', 'non-ready job cannot expose a trace ID', path);
  }
  if (!FAILURE_STATES.has(copy.state) && copy.error !== null) {
    fail('invalid_job', 'non-failed job cannot expose an error', path);
  }
  return freeze(copy);
}

function validateTraceSession(value, path) {
  exact(value, ['stem', 'code', 'date', 'data_hash', 'tick_count'], [], path);
  string(value.stem, `${path}.stem`);
  string(value.code, `${path}.code`);
  string(value.date, `${path}.date`, { pattern: DATE_PATTERN });
  hash(value.data_hash, `${path}.data_hash`);
  integer(value.tick_count, `${path}.tick_count`, 1);
}

function validateDecision(value, index, tickCount, path) {
  exact(value, ['sequence', 'observed_session_tick_index', 'eligible_from_session_tick', 'commands'], [], path);
  integer(value.sequence, `${path}.sequence`);
  integer(value.observed_session_tick_index, `${path}.observed_session_tick_index`);
  integer(value.eligible_from_session_tick, `${path}.eligible_from_session_tick`);
  if (value.sequence !== index) fail('invalid_sequence', 'decision sequence must be consecutive', `${path}.sequence`);
  if (value.observed_session_tick_index >= tickCount) {
    fail('tick_range', 'observed tick is outside the session', `${path}.observed_session_tick_index`);
  }
  if (value.eligible_from_session_tick !== value.observed_session_tick_index + 1) {
    fail('causality', 'decision must become eligible on the next tick', `${path}.eligible_from_session_tick`);
  }
  const commands = validateCommandBatch(value.commands, `${path}.commands`);
  if (commands.length === 0) fail('sparse_trace', 'sparse decisions must contain commands', `${path}.commands`);
  if (commands.length > NATIVE_TRACE_LIMITS.commandsPerDecision) {
    fail('command_limit', 'too many commands in one decision', `${path}.commands`);
  }
  value.commands = commands;
  return commands.length;
}

export function validateNativeTraceArtifact(value, path = '$') {
  let bytes;
  try { bytes = encodedByteLength(value); } catch {
    fail('non_plain_value', 'artifact is not JSON data', path);
  }
  if (bytes > NATIVE_TRACE_LIMITS.artifactBytes) fail('artifact_limit', 'artifact is too large', path);
  const copy = clonePlain(value, path);
  exact(copy, [
    'contract_version', 'input_id', 'source_hash', 'session', 'executor', 'run_config', 'decisions',
  ], [], path);
  if (copy.contract_version !== NATIVE_TRACE_CONTRACT) fail('version_mismatch', 'unsupported contract version', `${path}.contract_version`);
  hash(copy.input_id, `${path}.input_id`);
  hash(copy.source_hash, `${path}.source_hash`);
  validateTraceSession(copy.session, `${path}.session`);
  exact(copy.executor, ['protocol_version', 'runtime_fingerprint'], [], `${path}.executor`);
  if (copy.executor.protocol_version !== 1) fail('version_mismatch', 'unsupported executor protocol', `${path}.executor.protocol_version`);
  string(copy.executor.runtime_fingerprint, `${path}.executor.runtime_fingerprint`);
  exact(copy.run_config, ['execution_contract', 'cursor_policy'], [], `${path}.run_config`);
  if (copy.run_config.execution_contract !== 'backcast_paper_v1') {
    fail('execution_contract', 'unsupported execution contract', `${path}.run_config.execution_contract`);
  }
  if (copy.run_config.cursor_policy !== 'zero_only') {
    fail('cursor_policy', 'unsupported cursor policy', `${path}.run_config.cursor_policy`);
  }
  if (!Array.isArray(copy.decisions)) fail('invalid_array', 'decisions must be an array', `${path}.decisions`);
  if (copy.decisions.length > NATIVE_TRACE_LIMITS.decisions) {
    fail('decision_limit', 'too many decisions', `${path}.decisions`);
  }
  let previousTick = -1;
  let commandCount = 0;
  for (let index = 0; index < copy.decisions.length; index += 1) {
    const decision = copy.decisions[index];
    commandCount += validateDecision(decision, index, copy.session.tick_count, `${path}.decisions[${index}]`);
    if (decision.observed_session_tick_index <= previousTick) {
      fail('decision_order', 'observed ticks must be strictly increasing', `${path}.decisions[${index}].observed_session_tick_index`);
    }
    previousTick = decision.observed_session_tick_index;
  }
  if (commandCount > NATIVE_TRACE_LIMITS.commandsPerRun) {
    fail('command_limit', 'too many commands in trace', `${path}.decisions`);
  }
  return freeze(copy);
}

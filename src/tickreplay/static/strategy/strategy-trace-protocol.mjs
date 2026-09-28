'use strict';

import { encodedByteLength } from './strategy-protocol.mjs';

export const BUNDLED_RL_TRACE_CONTRACT = 'bundled_rl_trace_v1';
export const BUNDLED_RL_PRESET = Object.freeze({
  id: 'bundled_rl',
  revision: 'rl_scalp_evolution_v1',
  digest: '528b9dc44928984c471731bb64ef82fa4a299a2ffd5707d1ce2a482223d28ef1',
});
export const BUNDLED_RL_TRACE_LIMITS = Object.freeze({ decisions: 10_000, artifactBytes: 4_194_304, stringBytes: 2_048, nestingDepth: 12 });
const HASH_PATTERN = /^[0-9a-f]{64}$/;
const JOB_ID_PATTERN = /^[0-9a-f]{32,64}$/;
const DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;
const JOB_STATES = new Set(['queued', 'running', 'validating', 'ready', 'failed', 'cancelled', 'timed_out']);
const FAILURE_STATES = new Set(['failed', 'cancelled', 'timed_out']);

export class BundledRlTraceProtocolError extends Error {
  constructor(code, message, path = '$') { super(`${message} (${path})`); this.name = 'BundledRlTraceProtocolError'; this.code = code; this.path = path; }
}
function fail(code, message, path) { throw new BundledRlTraceProtocolError(code, message, path); }
function isPlain(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}
function clonePlain(value, path = '$', seen = new Set(), depth = 0) {
  if (depth > BUNDLED_RL_TRACE_LIMITS.nestingDepth) fail('nesting_limit', 'value is nested too deeply', path);
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return value;
  if (typeof value === 'number') { if (!Number.isFinite(value)) fail('non_finite', 'number must be finite', path); return value; }
  if (typeof value !== 'object' || (!Array.isArray(value) && !isPlain(value))) fail('non_plain_value', 'expected plain JSON data', path);
  if (seen.has(value)) fail('cyclic_value', 'cyclic value is not allowed', path);
  seen.add(value);
  try {
    if (Array.isArray(value)) return value.map((child, index) => clonePlain(child, `${path}[${index}]`, seen, depth + 1));
    const result = {};
    for (const key of Object.keys(value)) {
      if (value[key] === undefined) fail('non_plain_value', 'undefined is not allowed', `${path}.${key}`);
      result[key] = clonePlain(value[key], `${path}.${key}`, seen, depth + 1);
    }
    return result;
  } finally { seen.delete(value); }
}
function deepFreeze(value) {
  if (value && typeof value === 'object' && !Object.isFrozen(value)) { for (const child of Object.values(value)) deepFreeze(child); Object.freeze(value); }
  return value;
}
function exact(value, required, optional, path) {
  if (!isPlain(value)) fail('non_plain_value', 'expected a plain object', path);
  const allowed = new Set([...required, ...optional]);
  for (const key of Object.keys(value)) if (!allowed.has(key)) fail('unknown_field', `unknown field ${key}`, `${path}.${key}`);
  for (const key of required) if (!Object.hasOwn(value, key)) fail('missing_field', `missing field ${key}`, path);
}
function string(value, path, { pattern = null, maximumBytes = BUNDLED_RL_TRACE_LIMITS.stringBytes } = {}) {
  if (typeof value !== 'string' || value.length === 0) fail('invalid_string', 'expected a non-empty string', path);
  if (encodedByteLength(value) > maximumBytes) fail('string_limit', 'string is too long', path);
  if (pattern && !pattern.test(value)) fail('invalid_string', 'string has invalid format', path);
}
function integer(value, path, minimum = 0) { if (!Number.isSafeInteger(value) || value < minimum) fail('invalid_integer', `expected a safe integer >= ${minimum}`, path); }
function hash(value, path) { string(value, path, { pattern: HASH_PATTERN, maximumBytes: 64 }); }

export function validateBundledRlPreset(value, path = '$.preset') {
  const copy = clonePlain(value, path);
  exact(copy, ['id', 'revision', 'digest'], [], path);
  for (const key of ['id', 'revision', 'digest']) if (copy[key] !== BUNDLED_RL_PRESET[key]) fail('preset_mismatch', `unexpected preset ${key}`, `${path}.${key}`);
  return deepFreeze(copy);
}
function validateApiSession(value, path) {
  exact(value, ['stem', 'code', 'date', 'dataHash'], [], path);
  string(value.stem, `${path}.stem`); string(value.code, `${path}.code`); string(value.date, `${path}.date`, { pattern: DATE_PATTERN }); hash(value.dataHash, `${path}.dataHash`);
}
export function validateBundledRlCapability(value, path = '$') {
  const copy = clonePlain(value, path);
  exact(copy, ['available', 'contractVersion', 'preset'], ['unavailableReason'], path);
  if (typeof copy.available !== 'boolean') fail('invalid_boolean', 'available must be boolean', `${path}.available`);
  if (copy.contractVersion !== BUNDLED_RL_TRACE_CONTRACT) fail('version_mismatch', 'unsupported contract version', `${path}.contractVersion`);
  copy.preset = validateBundledRlPreset(copy.preset, `${path}.preset`);
  const hasUnavailableReason = Object.hasOwn(copy, 'unavailableReason');
  if (copy.available && hasUnavailableReason) fail('invalid_capability', 'available capability must not have an unavailable reason', path);
  if (!copy.available && !hasUnavailableReason) fail('invalid_capability', 'unavailable capability requires a reason', path);
  if (hasUnavailableReason) string(copy.unavailableReason, `${path}.unavailableReason`);
  return deepFreeze(copy);
}
export function createBundledRlSubmitRequest({ session }) {
  const sessionCopy = clonePlain(session, '$.session'); validateApiSession(sessionCopy, '$.session');
  return deepFreeze({ contractVersion: BUNDLED_RL_TRACE_CONTRACT, preset: { ...BUNDLED_RL_PRESET }, session: sessionCopy });
}
export function validateBundledRlJob(value, path = '$') {
  const copy = clonePlain(value, path);
  exact(copy, ['jobId', 'inputId', 'state', 'revision', 'traceId', 'error'], [], path);
  string(copy.jobId, `${path}.jobId`, { pattern: JOB_ID_PATTERN, maximumBytes: 64 }); hash(copy.inputId, `${path}.inputId`);
  if (!JOB_STATES.has(copy.state)) fail('invalid_state', 'unknown job state', `${path}.state`); integer(copy.revision, `${path}.revision`);
  if (copy.traceId !== null) hash(copy.traceId, `${path}.traceId`);
  if (copy.error !== null) { exact(copy.error, ['code', 'message'], [], `${path}.error`); string(copy.error.code, `${path}.error.code`); string(copy.error.message, `${path}.error.message`); }
  if (copy.state === 'ready' && (copy.traceId === null || copy.error !== null)) fail('invalid_job', 'ready job requires a trace ID only', path);
  if (FAILURE_STATES.has(copy.state) && (copy.traceId !== null || copy.error === null)) fail('invalid_job', 'failed job requires a public error only', path);
  if (!FAILURE_STATES.has(copy.state) && copy.state !== 'ready' && (copy.traceId !== null || copy.error !== null)) fail('invalid_job', 'non-terminal job exposes terminal data', path);
  return deepFreeze(copy);
}
export function validateBundledRlJobSubmission(value, path = '$') {
  const copy = clonePlain(value, path);
  exact(copy, ['job'], [], path);
  return validateBundledRlJob(copy.job, `${path}.job`);
}
function validateTraceSession(value, path) {
  exact(value, ['stem', 'code', 'date', 'dataHash', 'tickCount'], [], path);
  string(value.stem, `${path}.stem`); string(value.code, `${path}.code`); string(value.date, `${path}.date`, { pattern: DATE_PATTERN }); hash(value.dataHash, `${path}.dataHash`); integer(value.tickCount, `${path}.tickCount`, 1);
}
function validatePositionBranch(value, expectedPosition, path) {
  exact(value, ['positionQuantity', 'targetQuantity'], [], path);
  if (value.positionQuantity !== expectedPosition) fail('branch_order', `expected position ${expectedPosition}`, `${path}.positionQuantity`);
  if (![0, 100].includes(value.targetQuantity)) fail('invalid_target', 'target must be 0 or 100', `${path}.targetQuantity`);
}
function validateDecision(value, tickCount, path) {
  exact(value, ['observedSessionTickIndex', 'eligibleFromSessionTick', 'decisionTimeUs', 'positionBranches'], [], path);
  integer(value.observedSessionTickIndex, `${path}.observedSessionTickIndex`); integer(value.eligibleFromSessionTick, `${path}.eligibleFromSessionTick`); integer(value.decisionTimeUs, `${path}.decisionTimeUs`);
  if (value.observedSessionTickIndex >= tickCount) fail('tick_range', 'observed tick is outside the session', `${path}.observedSessionTickIndex`);
  if (value.eligibleFromSessionTick !== value.observedSessionTickIndex + 1) fail('causality', 'decision must become eligible on the next tick', `${path}.eligibleFromSessionTick`);
  if (value.eligibleFromSessionTick >= tickCount) fail('tick_range', 'decision has no eligible replay tick', `${path}.eligibleFromSessionTick`);
  if (!Array.isArray(value.positionBranches) || value.positionBranches.length !== 2) fail('invalid_branches', 'exactly two position branches are required', `${path}.positionBranches`);
  validatePositionBranch(value.positionBranches[0], 0, `${path}.positionBranches[0]`); validatePositionBranch(value.positionBranches[1], 100, `${path}.positionBranches[1]`);
}
export function validateBundledRlTraceArtifact(value, path = '$') {
  let bytes; try { bytes = encodedByteLength(value); } catch { fail('non_plain_value', 'artifact is not JSON data', path); }
  if (bytes > BUNDLED_RL_TRACE_LIMITS.artifactBytes) fail('artifact_limit', 'artifact is too large', path);
  const copy = clonePlain(value, path);
  exact(copy, ['contractVersion', 'preset', 'session', 'inputId', 'runtimeFingerprint', 'proposalContract', 'decisions'], [], path);
  if (copy.contractVersion !== BUNDLED_RL_TRACE_CONTRACT) fail('version_mismatch', 'unsupported contract version', `${path}.contractVersion`);
  copy.preset = validateBundledRlPreset(copy.preset, `${path}.preset`); validateTraceSession(copy.session, `${path}.session`); hash(copy.inputId, `${path}.inputId`); hash(copy.runtimeFingerprint, `${path}.runtimeFingerprint`);
  if (copy.proposalContract !== 'backcast_position_target_v1') fail('proposal_contract', 'unsupported proposal contract', `${path}.proposalContract`);
  if (!Array.isArray(copy.decisions)) fail('invalid_array', 'decisions must be an array', `${path}.decisions`);
  if (copy.decisions.length > BUNDLED_RL_TRACE_LIMITS.decisions) fail('decision_limit', 'too many decisions', `${path}.decisions`);
  let previousObserved = -1;
  copy.decisions.forEach((decision, index) => { validateDecision(decision, copy.session.tickCount, `${path}.decisions[${index}]`); if (decision.observedSessionTickIndex <= previousObserved) fail('decision_order', 'observed ticks must be strictly increasing', `${path}.decisions[${index}].observedSessionTickIndex`); previousObserved = decision.observedSessionTickIndex; });
  return deepFreeze(copy);
}

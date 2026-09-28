'use strict';

export const PROTOCOL_VERSION = 1;

export const LIMITS = Object.freeze({
  sourceBytes: 256 * 1024,
  sourceScalars: 256 * 1024,
  requestBytes: 256 * 1024,
  responseBytes: 64 * 1024,
  stringBytes: 128,
  openOrders: 256,
  fillsPerEvent: 256,
  commandsPerEvent: 32,
  commandsPerRun: 10_000,
  diagnosticEntries: 200,
  diagnosticBytes: 256 * 1024,
  stdoutBytes: 256 * 1024,
  stderrBytes: 256 * 1024,
  sourceLines: 8192,
  longestLineScalars: 4096,
  nestingDepth: 12,
});

export const ABI_SCHEMA = deepFreeze({
  protocolVersion: PROTOCOL_VERSION,
  lifecycle: ['load', 'start', 'event', 'end', 'interrupt', 'dispose'],
  callbacks: ['on_start', 'on_event', 'on_end'],
  commandTypes: ['submit_market', 'submit_limit', 'cancel_strategy_orders'],
  priceRepresentation: 'integer_logical_tick_level',
});

const PHASES = new Set(ABI_SCHEMA.lifecycle);
const SIDES = new Set(['buy', 'sell']);
const SEVERITIES = new Set(['info', 'warning', 'error']);
const HASH_PATTERN = /^[0-9a-f]{64}$/;
const DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;

export class StrategyProtocolError extends Error {
  constructor(code, message, path = '$') {
    super(`${message} (${path})`);
    this.name = 'StrategyProtocolError';
    this.code = code;
    this.path = path;
  }
}

function fail(code, message, path) {
  throw new StrategyProtocolError(code, message, path);
}

function isPlainObject(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function assertPlainObject(value, path) {
  if (!isPlainObject(value)) fail('non_plain_value', 'expected a plain object', path);
}

function assertExactKeys(value, required, optional, path) {
  assertPlainObject(value, path);
  const allowed = new Set([...required, ...optional]);
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) fail('unknown_field', `unknown field ${key}`, `${path}.${key}`);
  }
  for (const key of required) {
    if (!Object.hasOwn(value, key)) fail('missing_field', `missing field ${key}`, path);
  }
}

function assertString(
  value,
  path,
  { nullable = false, pattern = null, maximumBytes = LIMITS.stringBytes } = {},
) {
  if (nullable && value === null) return;
  if (typeof value !== 'string' || value.length === 0) {
    fail('invalid_string', 'expected a non-empty string', path);
  }
  if (encodedByteLength(value) > maximumBytes) {
    fail('string_limit', `string exceeds ${maximumBytes} UTF-8 bytes`, path);
  }
  if (pattern && !pattern.test(value)) fail('invalid_string', 'string has invalid format', path);
}

function assertInteger(value, path, { minimum = Number.MIN_SAFE_INTEGER, nullable = false } = {}) {
  if (nullable && value === null) return;
  if (!Number.isSafeInteger(value) || value < minimum) {
    fail('invalid_integer', `expected a safe integer >= ${minimum}`, path);
  }
}

function assertBoolean(value, path) {
  if (typeof value !== 'boolean') fail('invalid_boolean', 'expected a boolean', path);
}

function clonePlain(value, path = '$', seen = new Set(), depth = 0) {
  if (depth > LIMITS.nestingDepth) fail('nesting_limit', 'value is nested too deeply', path);
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return value;
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) fail('non_finite', 'number must be finite', path);
    return value;
  }
  if (typeof value !== 'object') fail('non_plain_value', 'value is not structured-clone plain data', path);
  if (seen.has(value)) fail('cyclic_value', 'cyclic value is not allowed', path);
  seen.add(value);
  try {
    if (Array.isArray(value)) {
      return value.map((item, index) => clonePlain(item, `${path}[${index}]`, seen, depth + 1));
    }
    assertPlainObject(value, path);
    const result = {};
    for (const key of Object.keys(value)) {
      if (typeof value[key] === 'undefined') fail('non_plain_value', 'undefined is not allowed', `${path}.${key}`);
      result[key] = clonePlain(value[key], `${path}.${key}`, seen, depth + 1);
    }
    return result;
  } catch (error) {
    if (error instanceof StrategyProtocolError) throw error;
    fail('non_plain_value', 'proxy-like value could not be inspected', path);
  } finally {
    seen.delete(value);
  }
}

function deepFreeze(value) {
  if (value && typeof value === 'object' && !Object.isFrozen(value)) {
    for (const child of Object.values(value)) deepFreeze(child);
    Object.freeze(value);
  }
  return value;
}

export function encodedByteLength(value) {
  const text = typeof value === 'string' ? value : JSON.stringify(value);
  return new TextEncoder().encode(text).byteLength;
}

export function truncateUtf8(text, maximumBytes) {
  const parts = [];
  let used = 0;
  for (const scalar of text) {
    const bytes = new TextEncoder().encode(scalar).byteLength;
    if (used + bytes > maximumBytes) break;
    parts.push(scalar);
    used += bytes;
  }
  return parts.join('');
}

export function canonicalSessionKey(session) {
  const normalized = validateSession(session);
  return `${normalized.generation}|${normalized.stem}|${normalized.code}|${normalized.actual_date}`;
}

export function validateSession(value, path = '$.session') {
  const copy = clonePlain(value, path);
  assertExactKeys(copy, ['stem', 'code', 'actual_date', 'generation'], [], path);
  assertString(copy.stem, `${path}.stem`);
  assertString(copy.code, `${path}.code`);
  assertString(copy.actual_date, `${path}.actual_date`, { pattern: DATE_PATTERN });
  assertInteger(copy.generation, `${path}.generation`, { minimum: 0 });
  return deepFreeze(copy);
}

export function validateSourceIdentity(value, path = '$.source') {
  const copy = clonePlain(value, path);
  assertExactKeys(
    copy,
    ['source_hash', 'strategy_revision', 'load_attempt_id'],
    [],
    path,
  );
  assertString(copy.source_hash, `${path}.source_hash`, { pattern: HASH_PATTERN });
  assertInteger(copy.strategy_revision, `${path}.strategy_revision`, { minimum: 0 });
  assertString(copy.load_attempt_id, `${path}.load_attempt_id`);
  return deepFreeze(copy);
}

function validatePortfolio(value, path) {
  const copy = clonePlain(value, path);
  assertExactKeys(copy, [
    'cash_minor', 'position_quantity', 'average_price_level', 'realized_pnl_minor',
    'unrealized_pnl_minor', 'equity_minor',
  ], [], path);
  assertInteger(copy.cash_minor, `${path}.cash_minor`);
  assertInteger(copy.position_quantity, `${path}.position_quantity`);
  assertInteger(copy.average_price_level, `${path}.average_price_level`, { minimum: 1, nullable: true });
  assertInteger(copy.realized_pnl_minor, `${path}.realized_pnl_minor`);
  assertInteger(copy.unrealized_pnl_minor, `${path}.unrealized_pnl_minor`);
  assertInteger(copy.equity_minor, `${path}.equity_minor`);
  if (copy.position_quantity === 0 && copy.average_price_level !== null) {
    fail('invalid_portfolio', 'flat portfolio must have a null average price', path);
  }
  if (copy.position_quantity !== 0 && copy.average_price_level === null) {
    fail('invalid_portfolio', 'open position requires an average price', path);
  }
  return deepFreeze(copy);
}

function validateOpenOrder(value, path) {
  const copy = clonePlain(value, path);
  assertExactKeys(copy, [
    'order_id', 'owner', 'side', 'quantity_remaining', 'limit_price_level',
    'eligible_from_session_tick',
  ], [], path);
  assertInteger(copy.order_id, `${path}.order_id`, { minimum: 1 });
  if (!['manual', 'strategy'].includes(copy.owner)) fail('invalid_owner', 'invalid order owner', `${path}.owner`);
  if (!SIDES.has(copy.side)) fail('invalid_side', 'invalid order side', `${path}.side`);
  assertInteger(copy.quantity_remaining, `${path}.quantity_remaining`, { minimum: 1 });
  assertInteger(copy.limit_price_level, `${path}.limit_price_level`, { minimum: 1 });
  assertInteger(copy.eligible_from_session_tick, `${path}.eligible_from_session_tick`, { minimum: 0 });
  return deepFreeze(copy);
}

function validateOpenOrderSnapshot(payload, path) {
  if (!Array.isArray(payload.open_orders)) fail('invalid_array', 'open_orders must be an array', `${path}.open_orders`);
  if (payload.open_orders.length > LIMITS.openOrders) {
    fail('open_order_limit', `open_orders must have at most ${LIMITS.openOrders} entries`, `${path}.open_orders`);
  }
  const orders = payload.open_orders.map((order, index) => validateOpenOrder(order, `${path}.open_orders[${index}]`));
  for (let index = 1; index < orders.length; index += 1) {
    if (orders[index - 1].order_id >= orders[index].order_id) {
      fail('order_sort', 'open_orders must be ordered by ascending order_id', `${path}.open_orders`);
    }
  }
  assertInteger(payload.open_orders_total, `${path}.open_orders_total`, { minimum: orders.length });
  assertBoolean(payload.open_orders_truncated, `${path}.open_orders_truncated`);
  if (payload.open_orders_truncated !== (payload.open_orders_total > orders.length)) {
    fail('invalid_truncation', 'open_orders_truncated does not match total', path);
  }
  return orders;
}

function validateFill(value, path) {
  const copy = clonePlain(value, path);
  assertExactKeys(copy, [
    'fill_delivery_sequence', 'order_id', 'side', 'quantity', 'price_level',
    'session_tick_index', 'replay_time_us',
  ], [], path);
  assertInteger(copy.fill_delivery_sequence, `${path}.fill_delivery_sequence`, { minimum: 1 });
  assertInteger(copy.order_id, `${path}.order_id`, { minimum: 1 });
  if (!SIDES.has(copy.side)) fail('invalid_side', 'invalid fill side', `${path}.side`);
  assertInteger(copy.quantity, `${path}.quantity`, { minimum: 1 });
  assertInteger(copy.price_level, `${path}.price_level`, { minimum: 1 });
  assertInteger(copy.session_tick_index, `${path}.session_tick_index`, { minimum: 0 });
  assertInteger(copy.replay_time_us, `${path}.replay_time_us`, { minimum: 0 });
  return deepFreeze(copy);
}

export function validateStartPayload(value, path = '$.payload') {
  const copy = clonePlain(value, path);
  assertExactKeys(copy, [
    'next_session_tick_index', 'last_committed_session_tick_index', 'last_replay_time_us',
    'last_price_level', 'portfolio', 'open_orders', 'open_orders_total',
    'open_orders_truncated',
  ], [], path);
  assertInteger(copy.next_session_tick_index, `${path}.next_session_tick_index`, { minimum: 0 });
  for (const field of ['last_committed_session_tick_index', 'last_replay_time_us']) {
    assertInteger(copy[field], `${path}.${field}`, { minimum: 0, nullable: true });
  }
  assertInteger(copy.last_price_level, `${path}.last_price_level`, { minimum: 1, nullable: true });
  const cursorZero = copy.next_session_tick_index === 0;
  const nullableFields = [
    copy.last_committed_session_tick_index, copy.last_replay_time_us, copy.last_price_level,
  ];
  if (cursorZero && nullableFields.some((field) => field !== null)) {
    fail('future_exposure', 'cursor zero cannot expose committed market fields', path);
  }
  if (!cursorZero && nullableFields.some((field) => field === null)) {
    fail('missing_history', 'mid-session start requires prior committed market fields', path);
  }
  if (!cursorZero && copy.last_committed_session_tick_index !== copy.next_session_tick_index - 1) {
    fail('invalid_cursor', 'last committed tick must immediately precede the cursor', path);
  }
  copy.portfolio = validatePortfolio(copy.portfolio, `${path}.portfolio`);
  copy.open_orders = validateOpenOrderSnapshot(copy, path);
  return deepFreeze(copy);
}

export function validateEventPayload(value, path = '$.payload') {
  const copy = clonePlain(value, path);
  assertExactKeys(copy, [
    'tick', 'fills', 'fill_cursor', 'portfolio', 'open_orders', 'open_orders_total',
    'open_orders_truncated',
  ], [], path);
  assertExactKeys(copy.tick, [
    'session_tick_index', 'replay_time_us', 'price_level', 'quantity', 'trade_type',
  ], [], `${path}.tick`);
  assertInteger(copy.tick.session_tick_index, `${path}.tick.session_tick_index`, { minimum: 0 });
  assertInteger(copy.tick.replay_time_us, `${path}.tick.replay_time_us`, { minimum: 0 });
  assertInteger(copy.tick.price_level, `${path}.tick.price_level`, { minimum: 1 });
  assertInteger(copy.tick.quantity, `${path}.tick.quantity`, { minimum: 1 });
  assertString(copy.tick.trade_type, `${path}.tick.trade_type`);
  if (!Array.isArray(copy.fills)) fail('invalid_array', 'fills must be an array', `${path}.fills`);
  if (copy.fills.length > LIMITS.fillsPerEvent) {
    fail('fill_limit', `fills must have at most ${LIMITS.fillsPerEvent} entries`, `${path}.fills`);
  }
  copy.fills = copy.fills.map((fill, index) => validateFill(fill, `${path}.fills[${index}]`));
  assertInteger(copy.fill_cursor, `${path}.fill_cursor`, { minimum: 0 });
  if (copy.fills.length > 0) {
    const first = copy.fills[0].fill_delivery_sequence;
    for (let index = 0; index < copy.fills.length; index += 1) {
      if (copy.fills[index].fill_delivery_sequence !== first + index) {
        fail('fill_sequence', 'fill delivery sequences must be contiguous', `${path}.fills`);
      }
    }
    if (copy.fills.at(-1).fill_delivery_sequence !== copy.fill_cursor) {
      fail('fill_cursor', 'fill_cursor must equal the last delivered fill sequence', path);
    }
  }
  copy.portfolio = validatePortfolio(copy.portfolio, `${path}.portfolio`);
  copy.open_orders = validateOpenOrderSnapshot(copy, path);
  if (encodedByteLength(copy) > LIMITS.requestBytes) {
    fail('request_limit', `event exceeds ${LIMITS.requestBytes} encoded bytes`, path);
  }
  return deepFreeze(copy);
}

function validateLoadPayload(value, path) {
  const copy = clonePlain(value, path);
  assertExactKeys(copy, ['source', 'filename', 'assets'], [], path);
  if (typeof copy.source !== 'string' || encodedByteLength(copy.source) === 0) {
    fail('invalid_source', 'source must be non-empty', `${path}.source`);
  }
  if (encodedByteLength(copy.source) > LIMITS.sourceBytes) {
    fail('source_limit', `source exceeds ${LIMITS.sourceBytes} UTF-8 bytes`, `${path}.source`);
  }
  if (copy.source.startsWith('\ufeff') || copy.source.includes('\r')) {
    fail('source_canonicalization', 'source must use canonical LF text without a BOM', `${path}.source`);
  }
  if (Array.from(copy.source).length > LIMITS.sourceScalars) {
    fail('source_limit', 'source has too many Unicode scalars', `${path}.source`);
  }
  const lines = copy.source.split('\n');
  const lineCount = lines.length - (lines.at(-1) === '' ? 1 : 0);
  if (lineCount > LIMITS.sourceLines) fail('source_limit', 'source has too many lines', `${path}.source`);
  if (lines.some((line) => Array.from(line).length > LIMITS.longestLineScalars)) {
    fail('source_limit', 'source has a line that is too long', `${path}.source`);
  }
  assertString(copy.filename, `${path}.filename`);
  assertExactKeys(copy.assets, ['loader_url', 'index_url', 'runner_url'], [], `${path}.assets`);
  for (const key of ['loader_url', 'index_url', 'runner_url']) {
    assertString(copy.assets[key], `${path}.assets.${key}`, { maximumBytes: 2048 });
  }
  return deepFreeze(copy);
}

function validateGenericPayload(value, path) {
  return deepFreeze(clonePlain(value, path));
}

function validatePayload(phase, value, path) {
  if (phase === 'load') return validateLoadPayload(value, path);
  if (phase === 'start') return validateStartPayload(value, path);
  if (phase === 'event') return validateEventPayload(value, path);
  return validateGenericPayload(value, path);
}

export function createRequestEnvelope({
  phase, requestId, session, runId, source, sequence, payload,
}) {
  if (!PHASES.has(phase)) fail('invalid_phase', 'unsupported request phase', '$.phase');
  assertString(requestId, '$.request_id');
  if (runId !== null) assertString(runId, '$.run_id');
  assertInteger(sequence, '$.sequence', { minimum: 0 });
  const envelope = {
    kind: 'strategy_request',
    protocol_version: PROTOCOL_VERSION,
    phase,
    request_id: requestId,
    session: validateSession(session),
    run_id: runId,
    source: validateSourceIdentity(source),
    sequence,
    payload: validatePayload(phase, payload, '$.payload'),
  };
  if (phase !== 'load' && encodedByteLength(envelope) > LIMITS.requestBytes) {
    fail('request_limit', `request exceeds ${LIMITS.requestBytes} encoded bytes`, '$');
  }
  return deepFreeze(envelope);
}

function validateCommand(command, path) {
  const copy = clonePlain(command, path);
  assertPlainObject(copy, path);
  assertString(copy.type, `${path}.type`);
  const common = ['type', 'client_intent_id'];
  if (copy.type === 'submit_market') {
    assertExactKeys(copy, [...common, 'side', 'quantity'], [], path);
  } else if (copy.type === 'submit_limit') {
    assertExactKeys(copy, [...common, 'side', 'quantity', 'limit_price_level'], [], path);
  } else if (copy.type === 'cancel_strategy_orders') {
    assertExactKeys(copy, common, ['side'], path);
  } else {
    fail('unsupported_command', 'unsupported command type', `${path}.type`);
  }
  assertString(copy.client_intent_id, `${path}.client_intent_id`);
  if (Object.hasOwn(copy, 'side') && !SIDES.has(copy.side)) {
    fail('invalid_side', 'side must be buy or sell', `${path}.side`);
  }
  if (copy.type.startsWith('submit_')) {
    assertInteger(copy.quantity, `${path}.quantity`, { minimum: 1 });
  }
  if (copy.type === 'submit_limit') {
    assertInteger(copy.limit_price_level, `${path}.limit_price_level`, { minimum: 1 });
  }
  return deepFreeze(copy);
}

export function validateCommandBatch(value, path = '$.commands') {
  let copy;
  try {
    copy = clonePlain(value, path);
  } catch (error) {
    if (error instanceof StrategyProtocolError) throw error;
    fail('non_plain_value', 'command batch could not be inspected', path);
  }
  if (!Array.isArray(copy)) fail('invalid_array', 'commands must be an array', path);
  if (copy.length > LIMITS.commandsPerEvent) {
    fail('command_limit', `commands must have at most ${LIMITS.commandsPerEvent} entries`, path);
  }
  const seen = new Set();
  const commands = copy.map((command, index) => {
    const normalized = validateCommand(command, `${path}[${index}]`);
    if (seen.has(normalized.client_intent_id)) {
      fail('duplicate_intent', 'duplicate client_intent_id', `${path}[${index}].client_intent_id`);
    }
    seen.add(normalized.client_intent_id);
    return normalized;
  });
  if (encodedByteLength(commands) > LIMITS.responseBytes) {
    fail('response_limit', `commands exceed ${LIMITS.responseBytes} encoded bytes`, path);
  }
  return deepFreeze(commands);
}

function validatePosition(value, path) {
  assertExactKeys(value, ['line', 'column'], [], path);
  assertInteger(value.line, `${path}.line`, { minimum: 1 });
  assertInteger(value.column, `${path}.column`, { minimum: 0 });
  if (value.line > LIMITS.sourceLines) fail('invalid_range', 'diagnostic line is out of range', path);
  if (value.column > LIMITS.longestLineScalars) {
    fail('invalid_range', 'diagnostic column is out of range', path);
  }
}

export function validateDiagnostic(value, path = '$.diagnostic') {
  const copy = clonePlain(value, path);
  assertExactKeys(copy, [
    'protocol_version', 'phase', 'severity', 'category', 'message', 'source_hash',
    'draft_revision', 'loaded_revision', 'running_revision', 'load_attempt_id',
    'run_id', 'sequence', 'range',
  ], [], path);
  if (copy.protocol_version !== PROTOCOL_VERSION) fail('version_mismatch', 'unsupported protocol version', path);
  if (!PHASES.has(copy.phase)) fail('invalid_phase', 'invalid diagnostic phase', `${path}.phase`);
  if (!SEVERITIES.has(copy.severity)) fail('invalid_severity', 'invalid diagnostic severity', `${path}.severity`);
  assertString(copy.category, `${path}.category`);
  if (typeof copy.message !== 'string') fail('invalid_string', 'message must be a string', `${path}.message`);
  if (encodedByteLength(copy.message) > LIMITS.stringBytes * 16) {
    fail('diagnostic_limit', 'diagnostic message is too large', `${path}.message`);
  }
  assertString(copy.source_hash, `${path}.source_hash`, { pattern: HASH_PATTERN });
  for (const field of ['draft_revision', 'loaded_revision', 'running_revision']) {
    assertInteger(copy[field], `${path}.${field}`, { minimum: 0, nullable: true });
  }
  if (copy.load_attempt_id !== null) assertString(copy.load_attempt_id, `${path}.load_attempt_id`);
  if (copy.run_id !== null) assertString(copy.run_id, `${path}.run_id`);
  assertInteger(copy.sequence, `${path}.sequence`, { minimum: 0, nullable: true });
  if (copy.range !== null) {
    assertExactKeys(copy.range, ['start', 'end'], [], `${path}.range`);
    validatePosition(copy.range.start, `${path}.range.start`);
    validatePosition(copy.range.end, `${path}.range.end`);
    const start = copy.range.start;
    const end = copy.range.end;
    if (end.line < start.line || (end.line === start.line && end.column < start.column)) {
      fail('invalid_range', 'diagnostic end must not precede start', `${path}.range`);
    }
  }
  return deepFreeze(copy);
}

function compareIdentity(response, expected) {
  if (response.phase !== expected.phase) fail('stale_phase', 'stale phase', '$.phase');
  if (response.request_id !== expected.requestId) fail('stale_request', 'stale request identity', '$.request_id');
  if (canonicalSessionKey(response.session) !== canonicalSessionKey(expected.session)) {
    fail('stale_session', 'stale session identity', '$.session');
  }
  if (response.run_id !== expected.runId) fail('stale_run', 'stale run identity', '$.run_id');
  const responseSource = validateSourceIdentity(response.source);
  const expectedSource = validateSourceIdentity(expected.source);
  if (
    responseSource.source_hash !== expectedSource.source_hash
    || responseSource.strategy_revision !== expectedSource.strategy_revision
    || responseSource.load_attempt_id !== expectedSource.load_attempt_id
  ) fail('stale_source', 'stale source identity', '$.source');
  if (response.sequence !== expected.sequence) fail('stale_sequence', 'stale sequence', '$.sequence');
}

export function validateWorkerResponse(value, expected) {
  const copy = clonePlain(value);
  assertExactKeys(copy, [
    'kind', 'protocol_version', 'phase', 'request_id', 'session', 'run_id',
    'source', 'sequence', 'ok',
  ], ['result', 'diagnostic'], '$');
  if (copy.kind !== 'strategy_response') fail('invalid_kind', 'invalid worker response kind', '$.kind');
  if (copy.protocol_version !== PROTOCOL_VERSION) fail('version_mismatch', 'unsupported protocol version', '$.protocol_version');
  assertBoolean(copy.ok, '$.ok');
  compareIdentity(copy, expected);
  if (copy.ok) {
    if (!Object.hasOwn(copy, 'result')) fail('missing_field', 'successful response requires result', '$');
    copy.result = clonePlain(copy.result, '$.result');
    assertPlainObject(copy.result, '$.result');
    if (copy.phase === 'event') {
      assertExactKeys(copy.result, ['commands'], [], '$.result');
      copy.result.commands = validateCommandBatch(copy.result.commands);
    } else if (copy.phase === 'load') {
      assertExactKeys(copy.result, ['callbacks', 'capabilities'], [], '$.result');
      if (!Array.isArray(copy.result.callbacks)) {
        fail('invalid_array', 'callbacks must be an array', '$.result.callbacks');
      }
      const callbacks = copy.result.callbacks;
      if (
        new Set(callbacks).size !== callbacks.length
        || !callbacks.includes('on_event')
        || callbacks.some((name) => !ABI_SCHEMA.callbacks.includes(name))
      ) fail('invalid_abi', 'callbacks do not implement ABI v1', '$.result.callbacks');
      assertExactKeys(copy.result.capabilities, ['soft_interrupt'], [], '$.result.capabilities');
      assertBoolean(copy.result.capabilities.soft_interrupt, '$.result.capabilities.soft_interrupt');
    } else if (['start', 'end'].includes(copy.phase)) {
      assertExactKeys(copy.result, [], [], '$.result');
    }
  } else {
    if (!Object.hasOwn(copy, 'diagnostic')) fail('missing_field', 'failed response requires diagnostic', '$');
    copy.diagnostic = validateDiagnostic(copy.diagnostic);
  }
  if (encodedByteLength(copy) > LIMITS.responseBytes) {
    fail('response_limit', `response exceeds ${LIMITS.responseBytes} encoded bytes`, '$');
  }
  return deepFreeze(copy);
}

export function boundedOpenOrders(orders, maximum = LIMITS.openOrders) {
  if (!Array.isArray(orders)) fail('invalid_array', 'orders must be an array', '$.orders');
  assertInteger(maximum, '$.maximum', { minimum: 0 });
  const validated = orders.map((order, index) => validateOpenOrder(order, `$.orders[${index}]`));
  const sorted = [...validated].sort((left, right) => left.order_id - right.order_id);
  const projected = sorted.slice(0, Math.min(maximum, LIMITS.openOrders));
  return deepFreeze({
    open_orders: projected,
    open_orders_total: sorted.length,
    open_orders_truncated: projected.length < sorted.length,
  });
}

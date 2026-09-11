import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import {
  ABI_SCHEMA,
  LIMITS,
  PROTOCOL_VERSION,
  StrategyProtocolError,
  boundedOpenOrders,
  canonicalSessionKey,
  createRequestEnvelope,
  validateCommandBatch,
  validateDiagnostic,
  validateWorkerResponse,
} from './strategy-protocol.mjs';

const session = Object.freeze({
  stem: '7203', code: '7203', actual_date: '2026-09-10', generation: 4,
});
const source = Object.freeze({
  source_hash: 'a'.repeat(64), strategy_revision: 3, load_attempt_id: 'load-8',
});

function identity(overrides = {}) {
  return {
    phase: 'event', requestId: 'request-9', session, runId: 'run-2', source,
    sequence: 9, ...overrides,
  };
}

function portfolio() {
  return {
    cash_minor: 1_000_000,
    position_quantity: 100,
    average_price_level: 101,
    realized_pnl_minor: 20,
    unrealized_pnl_minor: -5,
    equity_minor: 1_000_015,
  };
}

function eventPayload(overrides = {}) {
  return {
    tick: {
      session_tick_index: 12,
      replay_time_us: 1_000_000,
      price_level: 101,
      quantity: 100,
      trade_type: 'trade',
    },
    fills: [], fill_cursor: 0, portfolio: portfolio(), open_orders: [],
    open_orders_total: 0, open_orders_truncated: false, ...overrides,
  };
}

function response(overrides = {}) {
  return {
    kind: 'strategy_response', protocol_version: PROTOCOL_VERSION,
    phase: 'event', request_id: 'request-9', session, run_id: 'run-2',
    source, sequence: 9, ok: true,
    result: { commands: [{
      type: 'submit_limit', client_intent_id: 'limit-1', side: 'buy',
      quantity: 100, limit_price_level: 100,
    }] },
    ...overrides,
  };
}

test('ABI constants are immutable and mechanically match the Python runner', () => {
  assert.equal(PROTOCOL_VERSION, 1);
  assert.equal(Object.isFrozen(ABI_SCHEMA), true);
  assert.equal(LIMITS.commandsPerEvent, 32);
  assert.equal(LIMITS.commandsPerRun, 10_000);
  assert.equal(LIMITS.requestBytes, 256 * 1024);
  assert.equal(LIMITS.responseBytes, 64 * 1024);

  const python = readFileSync(new URL('./strategy-runner.py', import.meta.url), 'utf8');
  assert.match(python, /PROTOCOL_VERSION: Final\[int\] = 1/);
  assert.match(python, /MAX_COMMANDS_PER_EVENT: Final\[int\] = 32/);
  assert.match(python, /MAX_COMMANDS_PER_RUN: Final\[int\] = 10_000/);
  assert.match(python, /MAX_RESPONSE_BYTES: Final\[int\] = 64 \* 1024/);
  for (const command of ABI_SCHEMA.commandTypes) assert.ok(python.includes(`"${command}"`));
});

test('request constructor validates and freezes a complete event snapshot', () => {
  const request = createRequestEnvelope({ ...identity(), payload: eventPayload() });

  assert.equal(request.payload.tick.session_tick_index, 12);
  assert.equal(Object.isFrozen(request), true);
  assert.equal(Object.isFrozen(request.payload.portfolio), true);
  assert.equal(canonicalSessionKey(session), '4|7203|7203|2026-09-10');
  assert.throws(() => { request.payload.tick.quantity = 1; }, TypeError);
});

test('cursor zero start carries no unseen market fields', () => {
  const request = createRequestEnvelope({
    ...identity({ phase: 'start', sequence: 0 }),
    payload: {
      next_session_tick_index: 0,
      last_committed_session_tick_index: null,
      last_replay_time_us: null,
      last_price_level: null,
      portfolio: portfolio(), open_orders: [], open_orders_total: 0,
      open_orders_truncated: false,
    },
  });

  assert.equal(request.payload.last_price_level, null);
  assert.equal(JSON.stringify(request).includes('trade_type'), false);
});

test('mid-session start must describe exactly the prior committed tick', () => {
  assert.throws(() => createRequestEnvelope({
    ...identity({ phase: 'start', sequence: 0 }),
    payload: {
      next_session_tick_index: 8,
      last_committed_session_tick_index: 5,
      last_replay_time_us: 100, last_price_level: 7,
      portfolio: portfolio(), open_orders: [], open_orders_total: 0,
      open_orders_truncated: false,
    },
  }), /immediately precede/);
});

test('Load rejects non-canonical source before a Worker receives it', () => {
  assert.throws(() => createRequestEnvelope({
    ...identity({ phase: 'load', runId: null, sequence: 0 }),
    payload: {
      source: '\ufeffdef on_event(event):\r\n    return []\r\n',
      filename: 'strategy.py',
      assets: { loader_url: '/loader.mjs', index_url: '/runtime/', runner_url: '/runner.py' },
    },
  }), /canonical LF/);
});

test('command batches are all-or-nothing, exact, and deeply frozen', () => {
  const commands = validateCommandBatch([
    { type: 'cancel_strategy_orders', client_intent_id: 'cancel-1', side: 'sell' },
    { type: 'submit_market', client_intent_id: 'market-1', side: 'buy', quantity: 1 },
  ]);
  assert.equal(Object.isFrozen(commands[0]), true);
  assert.throws(() => validateCommandBatch([
    { type: 'submit_market', client_intent_id: 'same', side: 'buy', quantity: 1 },
    { type: 'submit_market', client_intent_id: 'same', side: 'sell', quantity: 1 },
  ]), /duplicate/);
  assert.throws(() => validateCommandBatch([
    { type: 'submit_market', client_intent_id: 'x', side: 'buy', quantity: 1, extra: true },
  ]), /unknown field/);
  assert.throws(() => validateCommandBatch([
    { type: 'submit_limit', client_intent_id: 'x', side: 'buy', quantity: 1, limit_price_level: 1.5 },
  ]), /integer/);
});

test('command batch rejects proxies, cycles, non-finite values, and overrun', () => {
  const cyclic = [];
  cyclic.push(cyclic);
  assert.throws(() => validateCommandBatch(cyclic), /cyclic/);
  assert.throws(() => validateCommandBatch(new Proxy([], {
    get(target, property, receiver) {
      if (property === 'length') throw new Error('proxy trap');
      return Reflect.get(target, property, receiver);
    },
  })), StrategyProtocolError);
  assert.throws(() => validateCommandBatch([
    { type: 'submit_market', client_intent_id: 'x', side: 'buy', quantity: Number.NaN },
  ]), /finite|integer/);
  assert.throws(
    () => validateCommandBatch(Array.from({ length: LIMITS.commandsPerEvent + 1 }, (_, index) => ({
      type: 'submit_market', client_intent_id: `x-${index}`, side: 'buy', quantity: 1,
    }))),
    /at most 32/,
  );
});

test('worker response requires the full exact identity before exposing commands', () => {
  const parsed = validateWorkerResponse(response(), identity());
  assert.equal(parsed.result.commands[0].limit_price_level, 100);
  assert.throws(
    () => validateWorkerResponse(response({ run_id: 'stale-run' }), identity()),
    /stale run identity/,
  );
  assert.throws(
    () => validateWorkerResponse(response({ sequence: 10 }), identity()),
    /stale sequence/,
  );
  assert.throws(
    () => validateWorkerResponse(response({ protocol_version: 2 }), identity()),
    /protocol version/,
  );
});

test('diagnostics use bounded typed source coordinates', () => {
  const diagnostic = validateDiagnostic({
    protocol_version: 1, phase: 'event', severity: 'error', category: 'python_exception',
    message: 'bad input', source_hash: source.source_hash, draft_revision: null,
    loaded_revision: 3, running_revision: 3, load_attempt_id: 'load-8',
    run_id: 'run-2', sequence: 9,
    range: { start: { line: 2, column: 0 }, end: { line: 2, column: 4 } },
  });
  assert.equal(Object.isFrozen(diagnostic.range), true);
  assert.throws(() => validateDiagnostic({ ...diagnostic, severity: 'fatal' }), /severity/);
  assert.throws(() => validateDiagnostic({
    ...diagnostic,
    range: { start: { line: 2, column: 4 }, end: { line: 2, column: 3 } },
  }), /end must not precede/);
});

test('open-order snapshots are oldest-first and disclose truncation', () => {
  const projected = boundedOpenOrders([
    { order_id: 3, owner: 'manual', side: 'sell', quantity_remaining: 1,
      limit_price_level: 103, eligible_from_session_tick: 0 },
    { order_id: 1, owner: 'strategy', side: 'buy', quantity_remaining: 2,
      limit_price_level: 99, eligible_from_session_tick: 2 },
    { order_id: 2, owner: 'strategy', side: 'buy', quantity_remaining: 2,
      limit_price_level: 98, eligible_from_session_tick: 3 },
  ], 2);

  assert.deepEqual(projected.open_orders.map(({ order_id }) => order_id), [1, 2]);
  assert.equal(projected.open_orders_total, 3);
  assert.equal(projected.open_orders_truncated, true);
});

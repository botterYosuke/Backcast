import assert from 'node:assert/strict';
import test from 'node:test';

import { StrategyController } from './strategy-controller.mjs';
import { createRequestEnvelope } from './strategy-protocol.mjs';
import { StrategyWorkerRuntime } from './strategy-worker.mjs';

class FakeWorker {
  constructor() {
    this.listeners = new Map();
    this.messages = [];
    this.terminated = 0;
  }

  addEventListener(type, listener) {
    const listeners = this.listeners.get(type) ?? [];
    this.listeners.set(type, [...listeners, listener]);
  }

  postMessage(message) {
    this.messages.push(message);
  }

  terminate() {
    this.terminated += 1;
  }

  emit(type, data = undefined) {
    for (const listener of this.listeners.get(type) ?? []) listener({ data, message: 'worker failed' });
  }

  respond(overrides = {}) {
    const request = this.messages.findLast(({ kind }) => kind === 'strategy_request');
    const response = {
      kind: 'strategy_response', protocol_version: request.protocol_version,
      phase: request.phase, request_id: request.request_id, session: request.session,
      run_id: request.run_id, source: request.source, sequence: request.sequence,
      ok: true, result: request.phase === 'event' ? { commands: [] } : {}, ...overrides,
    };
    for (const key of Object.keys(response)) {
      if (response[key] === undefined) delete response[key];
    }
    this.emit('message', response);
  }
}

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

const session = Object.freeze({
  stem: '7203', code: '7203', actual_date: '2026-09-10', generation: 1,
});

function portfolio() {
  return {
    cash_minor: 1_000_000, position_quantity: 0, average_price_level: null,
    realized_pnl_minor: 0, unrealized_pnl_minor: 0, equity_minor: 1_000_000,
  };
}

function startInput(overrides = {}) {
  return {
    session, cursor: 0, tickCount: 10, lastCommitted: null,
    portfolio: portfolio(), openOrders: [], ...overrides,
  };
}

function eventInput(index = 0) {
  return {
    tick: {
      session_tick_index: index, replay_time_us: 1_000_000 + index,
      price_level: 100 + index, quantity: 10, trade_type: 'trade',
    },
    fills: [], fillCursor: 0, portfolio: portfolio(), openOrders: [],
    openOrdersTotal: 0, openOrdersTruncated: false,
  };
}

async function loadedController(options = {}) {
  const workers = [];
  const controller = new StrategyController({
    createWorker: () => {
      const worker = new FakeWorker();
      workers.push(worker);
      return worker;
    },
    assets: { loaderUrl: '/pyodide.mjs', indexUrl: '/pyodide/', runnerUrl: '/strategy-runner.py' },
    makeId: (() => { let id = 0; return (prefix) => `${prefix}-${++id}`; })(),
    ...options,
  });
  const loading = controller.load({
    source: 'def on_event(event):\n    return []\n', sourceHash: 'a'.repeat(64),
    draftRevision: 2, loadAttemptId: 'attempt-1', session,
  });
  workers[0].respond({ result: { callbacks: ['on_event'], capabilities: { soft_interrupt: false } } });
  await loading;
  return { controller, workers };
}

async function startedController(options = {}) {
  const fixture = await loadedController(options);
  const starting = fixture.controller.start(startInput());
  fixture.workers[0].respond();
  await starting;
  return fixture;
}

test('listeners exist before Load is posted and a successful load allocates a revision', async () => {
  let worker;
  const states = [];
  const controller = new StrategyController({
    createWorker: () => {
      worker = new FakeWorker();
      return worker;
    },
    assets: { loaderUrl: '/pyodide.mjs', indexUrl: '/pyodide/', runnerUrl: '/strategy-runner.py' },
    onStateChange: (state) => states.push(state.tag),
  });

  const loading = controller.load({
    source: 'def on_event(event):\n    return []\n', sourceHash: 'b'.repeat(64),
    draftRevision: 1, loadAttemptId: 'load-1', session,
  });
  assert.equal(worker.listeners.has('message'), true);
  assert.equal(worker.listeners.has('messageerror'), true);
  assert.equal(worker.listeners.has('error'), true);
  worker.respond({ result: { callbacks: ['on_event'], capabilities: { soft_interrupt: false } } });

  assert.deepEqual(await loading, { ok: true, loadedRevision: 1, sourceHash: 'b'.repeat(64) });
  assert.equal(controller.getState().tag, 'ready');
  assert.equal(states[0], 'loading');
  assert.equal(states.at(-1), 'ready');
});

test('Start at cursor zero exposes no future tick and settles paused', async () => {
  const { controller, workers } = await loadedController();
  const starting = controller.start(startInput());
  const request = workers[0].messages.at(-1);

  assert.equal(controller.getState().tag, 'starting');
  assert.equal(request.payload.next_session_tick_index, 0);
  assert.equal(request.payload.last_price_level, null);
  assert.equal(JSON.stringify(request.payload).includes('trade_type'), false);
  workers[0].respond();
  const result = await starting;
  assert.equal(result.ok, true);
  assert.equal(controller.getState().tag, 'paused');
  assert.equal(controller.manualEntryBlocked(), true);
});

test('Start refuses playing, exhausted, and inconsistent snapshots without posting', async () => {
  const { controller, workers } = await loadedController();
  const before = workers[0].messages.length;

  await assert.rejects(controller.start(startInput({ replayPaused: false })), /paused/);
  await assert.rejects(controller.start(startInput({ cursor: 10 })), /session_complete/);
  await assert.rejects(controller.start(startInput({
    cursor: 2,
    lastCommitted: { sessionTickIndex: 0, replayTimeUs: 1, priceLevel: 1 },
  })), /immediately precede/);
  assert.equal(workers[0].messages.length, before);
});

test('dispatch is single-flight and returns i+1 eligibility with full identity', async () => {
  const { controller, workers } = await startedController();
  controller.resume();
  const first = controller.dispatchOneEvent(eventInput(4));

  await assert.rejects(controller.dispatchOneEvent(eventInput(5)), /single-flight/);
  const request = workers[0].messages.at(-1);
  workers[0].respond({ result: { commands: [{
    type: 'submit_market', client_intent_id: 'buy-1', side: 'buy', quantity: 1,
  }] } });
  const result = await first;

  assert.equal(result.observedSessionTickIndex, 4);
  assert.equal(result.eligibleFromSessionTick, 5);
  assert.equal(result.runId, controller.getState().runId);
  assert.equal(Object.isFrozen(result.commands[0]), true);
});

test('pause during a callback accepts matching result but remains paused', async () => {
  const { controller, workers } = await startedController();
  controller.resume();
  const pending = controller.dispatchOneEvent(eventInput(2));
  controller.pause();
  workers[0].respond();

  await pending;
  assert.equal(controller.getState().tag, 'paused');
});

test('stale session/run/revision/sequence responses never expose commands', async () => {
  for (const override of [
    { run_id: 'old-run' },
    { source: { source_hash: 'a'.repeat(64), strategy_revision: 99, load_attempt_id: 'attempt-1' } },
    { sequence: 99 },
    { session: { ...session, generation: 99 } },
  ]) {
    const cleanups = [];
    const { controller, workers } = await startedController({ onCleanup: (entry) => cleanups.push(entry) });
    controller.resume();
    const pending = controller.dispatchOneEvent(eventInput());
    workers[0].respond(override);
    await assert.rejects(pending, /stale/);
    assert.equal(controller.getState().tag, 'errored');
    assert.equal(cleanups.length, 1);
  }
});

test('active worker errors pause replay and clean exact run once', async () => {
  const pauses = [];
  const cleanups = [];
  const gates = [];
  const { controller, workers } = await startedController({
    onPauseReplay: (entry) => pauses.push(entry),
    onCleanup: (entry) => cleanups.push(entry),
    onManualGateChange: (blocked) => gates.push(blocked),
  });
  const runId = controller.getState().runId;

  workers[0].emit('error');
  workers[0].emit('messageerror');
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(controller.getState().tag, 'errored');
  assert.equal(cleanups.length, 1);
  assert.equal(cleanups[0].runId, runId);
  assert.equal(pauses.length, 1);
  assert.deepEqual(gates, [true, false]);
});

test('timeout invalidates before cleanup and terminates the Worker', async () => {
  const timers = [];
  const cleanups = [];
  const { controller, workers } = await startedController({
    setTimer: (callback) => { timers.push(callback); return callback; },
    clearTimer: () => {},
    onCleanup: (entry) => cleanups.push(entry),
  });
  controller.resume();
  const pending = controller.dispatchOneEvent(eventInput());
  timers.at(-1)();

  await assert.rejects(pending, /timeout/);
  assert.equal(controller.getState().tag, 'errored');
  assert.equal(cleanups.length, 1);
  assert.equal(workers[0].terminated, 1);
});

test('explicit invalidation rejects a late response and cleanup is idempotent', async () => {
  const cleanups = [];
  const { controller, workers } = await startedController({ onCleanup: (entry) => cleanups.push(entry) });
  controller.resume();
  const pending = controller.dispatchOneEvent(eventInput());
  const invalidating = controller.invalidate('seek');
  workers[0].respond({ result: { commands: [{
    type: 'submit_market', client_intent_id: 'late', side: 'buy', quantity: 1,
  }] } });

  await assert.rejects(pending, /invalidated/);
  await invalidating;
  await controller.invalidate('explicit_stop');
  assert.equal(cleanups.length, 1);
  assert.equal(workers[0].terminated, 1);
  assert.equal(controller.manualEntryBlocked(), false);
});

test('same-turn invalidation applies the highest precedence cause once', async () => {
  const cleanups = [];
  const { controller } = await startedController({ onCleanup: (entry) => cleanups.push(entry) });

  const stopped = controller.stop();
  const changed = controller.invalidate('session_change');
  const [first, second] = await Promise.all([stopped, changed]);

  assert.equal(first.cause, 'session_change');
  assert.equal(second.cause, 'session_change');
  assert.equal(cleanups.length, 1);
  assert.equal(cleanups[0].cause, 'session_change');
});

test('cooperative interrupt is signaled before grace-bounded hard termination', async () => {
  const timers = [];
  const interruptView = new Int32Array(new SharedArrayBuffer(Int32Array.BYTES_PER_ELEMENT));
  const { controller, workers } = await startedController({
    createInterruptBuffer: () => interruptView,
    setTimer: (callback, milliseconds) => {
      const timer = { callback, milliseconds };
      timers.push(timer);
      return timer;
    },
    clearTimer: () => {},
  });

  const stopping = controller.stop();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(Atomics.load(interruptView, 0), 2);
  assert.equal(workers[0].terminated, 0);
  const grace = timers.at(-1);
  assert.equal(grace.milliseconds, 250);
  grace.callback();
  await stopping;
  assert.equal(workers[0].terminated, 1);
});

test('replay end ignores commands, cleans once, and on_end failure remains completed', async () => {
  const cleanups = [];
  const diagnostics = [];
  const { controller, workers } = await startedController({
    onCleanup: (entry) => cleanups.push(entry),
    onDiagnostic: (entry) => diagnostics.push(entry),
  });
  const ending = controller.end({ reason: 'replay_end', final_session_tick_index: 9 });
  await new Promise((resolve) => setImmediate(resolve));
  workers[0].respond({ ok: false, result: undefined, diagnostic: {
    protocol_version: 1, phase: 'end', severity: 'error', category: 'python_exception',
    message: 'on_end failed', source_hash: 'a'.repeat(64), draft_revision: null,
    loaded_revision: 1, running_revision: 1, load_attempt_id: 'attempt-1',
    run_id: controller.getState().runId, sequence: 1, range: null,
  } });

  const outcome = await ending;
  assert.equal(outcome.kind, 'completed');
  assert.equal(controller.getState().tag, 'completed');
  assert.equal(cleanups.length, 1);
  assert.equal(diagnostics.some(({ phase }) => phase === 'end'), true);
  assert.equal(workers[0].terminated, 1);
});

test('diagnostics are bounded by entry count and encoded bytes', async () => {
  const { controller, workers } = await startedController();
  for (let index = 0; index < 220; index += 1) {
    workers[0].emit('message', {
      kind: 'strategy_diagnostic', protocol_version: 1,
      diagnostic: {
        protocol_version: 1, phase: 'event', severity: 'warning', category: 'log_truncated',
        message: `warning ${index}`, source_hash: 'a'.repeat(64), draft_revision: null,
        loaded_revision: 1, running_revision: 1, load_attempt_id: 'attempt-1',
        run_id: controller.getState().runId, sequence: 0, range: null,
      },
    });
  }
  const state = controller.getState();
  assert.equal(state.diagnosticCount <= 200, true);
  assert.equal(state.diagnosticsTruncated > 0, true);
});

test('load failure has no run cleanup and terminates only the candidate', async () => {
  const worker = new FakeWorker();
  const cleanups = [];
  const controller = new StrategyController({
    createWorker: () => worker,
    assets: { loaderUrl: '/pyodide.mjs', indexUrl: '/pyodide/', runnerUrl: '/strategy-runner.py' },
    onCleanup: (entry) => cleanups.push(entry),
  });
  const loading = controller.load({
    source: 'bad syntax', sourceHash: 'c'.repeat(64), draftRevision: 1,
    loadAttemptId: 'bad-load', session,
  });
  worker.respond({ ok: false, result: undefined, diagnostic: {
    protocol_version: 1, phase: 'load', severity: 'error', category: 'syntax_error',
    message: 'invalid syntax', source_hash: 'c'.repeat(64), draft_revision: 1,
    loaded_revision: null, running_revision: null, load_attempt_id: 'bad-load',
    run_id: null, sequence: 0, range: null,
  } });

  await assert.rejects(loading, /invalid syntax/);
  assert.equal(cleanups.length, 0);
  assert.equal(worker.terminated, 1);
});

test('Worker seam owns Pyodide and destroys every temporary proxy exactly once', async () => {
  const listeners = new Map();
  const posted = [];
  const scope = {
    addEventListener: (type, listener) => listeners.set(type, listener),
    postMessage: (message) => posted.push(message),
  };
  const destroyCounts = [];
  const outcomes = [
    { ok: true, callbacks: ['on_event'], stdout: 'loaded', stderr: '',
      stdout_truncated: false, stderr_truncated: false },
    { ok: true, commands: [{
      type: 'submit_market', client_intent_id: 'worker-buy', side: 'buy', quantity: 1,
    }], stdout: '', stderr: '', stdout_truncated: false, stderr_truncated: false },
  ];
  const pyodide = {
    FS: { writeFile: () => {} },
    toPy: (value) => ({
      value,
      destroy: (() => { let count = 0; return () => { count += 1; destroyCounts.push(count); }; })(),
    }),
    runPythonAsync: async (_code, { globals }) => ({
      toJs: () => JSON.stringify(outcomes.shift()),
      destroy: (() => { let count = 0; return () => { count += 1; destroyCounts.push(count); }; })(),
      globals,
    }),
    setInterruptBuffer: () => {},
  };
  const runtime = new StrategyWorkerRuntime(scope, {
    importModule: async () => ({ loadPyodide: async () => pyodide }),
    fetchResource: async () => ({ ok: true, text: async () => '# runner' }),
  }).install();
  assert.equal(listeners.has('message'), true);
  assert.equal(listeners.has('messageerror'), true);

  const sourceIdentity = {
    source_hash: 'a'.repeat(64), strategy_revision: 1, load_attempt_id: 'worker-load',
  };
  const loadRequest = createRequestEnvelope({
    phase: 'load', requestId: 'worker-request-1', session, runId: null,
    source: sourceIdentity, sequence: 0,
    payload: {
      source: 'def on_event(event): return []', filename: 'strategy.py',
      assets: { loader_url: '/loader.mjs', index_url: '/pyodide/', runner_url: '/runner.py' },
    },
  });
  await runtime.onMessage(loadRequest);
  const event = eventInput();
  const eventRequest = createRequestEnvelope({
    phase: 'event', requestId: 'worker-request-2', session, runId: 'worker-run',
    source: sourceIdentity, sequence: 1, payload: {
      tick: event.tick, fills: event.fills, fill_cursor: event.fillCursor,
      portfolio: event.portfolio, open_orders: event.openOrders,
      open_orders_total: event.openOrdersTotal,
      open_orders_truncated: event.openOrdersTruncated,
    },
  });
  await runtime.onMessage(eventRequest);

  assert.equal(posted.some(({ kind, stage }) => kind === 'strategy_progress' && stage === 'runtime_ready'), true);
  assert.equal(posted.some(({ kind, stream, text }) => kind === 'strategy_log' && stream === 'stdout' && text === 'loaded'), true);
  const response = posted.findLast(({ kind }) => kind === 'strategy_response');
  assert.equal(response.result.commands[0].client_intent_id, 'worker-buy');
  assert.deepEqual(destroyCounts, [1, 1, 1, 1]);
});

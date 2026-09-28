import assert from 'node:assert/strict';
import test from 'node:test';

import {
  BundledRlTraceController,
  createBundledRlTraceHttpTransport,
} from './strategy-trace-controller.mjs';
import { BUNDLED_RL_PRESET, BUNDLED_RL_TRACE_CONTRACT } from './strategy-trace-protocol.mjs';

const DATA_HASH = 'd'.repeat(64);
const INPUT_ID = 'e'.repeat(64);
const TRACE_ID = 'f'.repeat(64);
const RUNTIME_ID = 'a'.repeat(64);
const SESSION = { stem: '7203', code: '7203', actual_date: '2026-09-28', generation: 7, dataHash: DATA_HASH };

function proposal(observedSessionTickIndex, flatTarget, longTarget) {
  return {
    observedSessionTickIndex,
    eligibleFromSessionTick: observedSessionTickIndex + 1,
    decisionTimeUs: 1_789_000_200_000_000 + observedSessionTickIndex * 10_000_000,
    positionBranches: [
      { positionQuantity: 0, targetQuantity: flatTarget },
      { positionQuantity: 100, targetQuantity: longTarget },
    ],
  };
}

function trace(decisions = [proposal(0, 100, 0)]) {
  return {
    contractVersion: BUNDLED_RL_TRACE_CONTRACT,
    preset: {
      digest: BUNDLED_RL_PRESET.digest,
      id: BUNDLED_RL_PRESET.id,
      revision: BUNDLED_RL_PRESET.revision,
    },
    session: { stem: '7203', code: '7203', date: '2026-09-28', dataHash: DATA_HASH, tickCount: 4 },
    inputId: INPUT_ID,
    runtimeFingerprint: RUNTIME_ID,
    proposalContract: 'backcast_position_target_v1',
    decisions,
  };
}

function job(state, revision, extras = {}) {
  return { jobId: '1'.repeat(32), inputId: INPUT_ID, state, revision, traceId: state === 'ready' ? TRACE_ID : null, error: null, ...extras };
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((onResolve, onReject) => { resolve = onResolve; reject = onReject; });
  return { promise, resolve, reject };
}

function fakeTransport({ submit = [{ job: job('ready', 0) }], statuses = [], artifact = trace() } = {}) {
  const calls = { submit: [], status: [], cancel: [], fetchTrace: [] };
  return {
    calls,
    async submit(payload) { calls.submit.push(payload); return submit.shift(); },
    async status(jobId) { calls.status.push(jobId); return statuses.shift(); },
    async cancel(jobId) { calls.cancel.push(jobId); return { cancelled: true }; },
    async fetchTrace(traceId) { calls.fetchTrace.push(traceId); return JSON.stringify(artifact); },
  };
}

function controller(transport, callbacks = {}) {
  let id = 0;
  return new BundledRlTraceController({ transport, poll: async () => {}, hashText: async () => TRACE_ID, makeId: (prefix) => `${prefix}-${++id}`, ...callbacks });
}

async function load(target) { return target.load({ session: SESSION, tickCount: 4 }); }

function safeSnapshot(positionQuantity = 0, overrides = {}) {
  return {
    portfolio: { position_quantity: positionQuantity },
    openOrders: [],
    openOrdersTotal: 0,
    openOrdersTruncated: false,
    ...overrides,
  };
}

function startInput(overrides = {}) {
  return { session: SESSION, cursor: 0, tickCount: 4, lastCommitted: null, replayPaused: true, ...safeSnapshot(0), ...overrides };
}

test('Load sends no source and publishes a verified immutable trace', async () => {
  const transport = fakeTransport();
  const target = controller(transport);
  const result = await load(target);
  assert.equal(result.traceId, TRACE_ID);
  assert.deepEqual(Object.keys(transport.calls.submit[0]), ['contractVersion', 'preset', 'session']);
  assert.equal(JSON.stringify(transport.calls.submit[0]).includes('source'), false);
  assert.equal(target.getState().mode, BUNDLED_RL_TRACE_CONTRACT);
});

test('polling is monotonic and fetches one immutable trace', async () => {
  const transport = fakeTransport({ submit: [{ job: job('queued', 0) }], statuses: [job('running', 1), job('running', 1), job('validating', 2), job('ready', 3)] });
  await load(controller(transport));
  assert.equal(transport.calls.status.length, 4);
  assert.deepEqual(transport.calls.fetchTrace, [TRACE_ID]);
});

test('same revision cannot change the job snapshot', async () => {
  const transport = fakeTransport({
    submit: [{ job: job('queued', 0) }],
    statuses: [job('running', 1), job('validating', 1)],
  });
  await assert.rejects(load(controller(transport)), (error) => error.code === 'stale_status');
  assert.deepEqual(transport.calls.fetchTrace, []);
});

test('polling rejects lower revisions and stage regression', async () => {
  for (const statuses of [
    [job('running', 2), job('running', 1)],
    [job('validating', 2), job('running', 3)],
  ]) {
    const transport = fakeTransport({ submit: [{ job: job('queued', 0) }], statuses });
    await assert.rejects(load(controller(transport)), (error) => error.code === 'stale_status');
    assert.deepEqual(transport.calls.fetchTrace, []);
  }
});

for (const failure of ['poll', 'transport', 'malformed status', 'same revision', 'trace fetch', 'trace hash', 'trace identity']) {
  test(`failed Load cancels exactly its acquired job after ${failure}`, async () => {
    const originalError = Object.assign(new Error(`injected ${failure}`), { code: 'injected_load_failure' });
    const transport = fakeTransport({ submit: [{ job: job('queued', 0) }], statuses: [job('ready', 1)] });
    const callbacks = {};
    let expectedCode = originalError.code;
    if (failure === 'poll') callbacks.poll = async () => { throw originalError; };
    if (failure === 'transport') transport.status = async () => { throw originalError; };
    if (failure === 'malformed status') { transport.status = async () => ({}); expectedCode = 'missing_field'; }
    if (failure === 'same revision') { transport.status = async () => job('running', 0); expectedCode = 'stale_status'; }
    if (failure === 'trace fetch') transport.fetchTrace = async () => { throw originalError; };
    if (failure === 'trace hash') { callbacks.hashText = async () => '0'.repeat(64); expectedCode = 'trace_hash_mismatch'; }
    if (failure === 'trace identity') { transport.fetchTrace = async () => JSON.stringify({ ...trace(), inputId: '0'.repeat(64) }); expectedCode = 'trace_identity_mismatch'; }
    const target = controller(transport, callbacks);
    await assert.rejects(load(target), (error) => {
      assert.equal(error.code, expectedCode);
      if (expectedCode === originalError.code) assert.equal(error, originalError);
      return true;
    });
    assert.deepEqual(transport.calls.cancel, ['1'.repeat(32)]);
    assert.equal(target.getState().tag, 'errored');
    assert.equal(target.getState().inFlight, false);
    assert.equal(target.isActive(), false);
    assert.equal(target.manualEntryBlocked(), false);
    assert.deepEqual(target.terminalOutcome(), { kind: 'load_failed', code: expectedCode });
    await target.stop();
    assert.deepEqual(transport.calls.cancel, ['1'.repeat(32)], 'Stop never cancels a failed job twice');
  });
}

test('failed Load preserves its original error when best-effort cancellation fails', async () => {
  const originalError = new Error('status transport failed');
  const cancelError = Object.assign(new Error('cancel transport failed'), { code: 'cancel_transport_failed' });
  const diagnostics = [];
  const transport = fakeTransport({ submit: [{ job: job('queued', 0) }] });
  transport.status = async () => { throw originalError; };
  transport.cancel = async (jobId) => { transport.calls.cancel.push(jobId); throw cancelError; };
  const target = controller(transport, { onDiagnostic: (diagnostic) => diagnostics.push(diagnostic) });
  await assert.rejects(load(target), (error) => error === originalError);
  assert.deepEqual(transport.calls.cancel, ['1'.repeat(32)]);
  assert.deepEqual(diagnostics, [{ phase: 'cancel', category: cancelError.code, message: cancelError.message }]);
  assert.deepEqual(target.diagnosticHistory(), diagnostics);
  assert.deepEqual(target.terminalOutcome(), { kind: 'load_failed', code: 'load_failed' });
  assert.equal(target.isActive(), false);
});

test('failed Load settles its original error without waiting for cancellation transport', async () => {
  const originalError = new Error('status failed with a stalled DELETE');
  const cancellation = deferred(), cancelling = deferred();
  const transport = fakeTransport({ submit: [{ job: job('queued', 0) }] });
  transport.status = async () => { throw originalError; };
  transport.cancel = (jobId) => { transport.calls.cancel.push(jobId); cancelling.resolve(); return cancellation.promise; };
  const target = controller(transport);
  let settled = false;
  const failed = assert.rejects(load(target), (error) => error === originalError);
  failed.then(() => { settled = true; });
  await cancelling.promise;
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(settled, true, 'best-effort DELETE must not delay the original Load rejection');
  assert.equal(target.getState().tag, 'errored');
  assert.equal(target.isActive(), false);
  assert.deepEqual(transport.calls.cancel, ['1'.repeat(32)]);
  cancellation.resolve({ cancelled: true });
  await failed;
});

test('stale failing Load cancellation cannot cancel or overwrite a newer loaded job', async () => {
  const oldJobId = '1'.repeat(32), newJobId = '2'.repeat(32);
  const cancellation = deferred(), cancelling = deferred();
  const originalError = new Error('old status failed');
  const cancelError = new Error('old cancellation failed late');
  const transport = fakeTransport({ submit: [{ job: job('queued', 0) }, { job: job('ready', 0, { jobId: newJobId }) }] });
  transport.status = async () => { throw originalError; };
  transport.cancel = (jobId) => { transport.calls.cancel.push(jobId); cancelling.resolve(); return cancellation.promise; };
  const target = controller(transport);
  const failed = assert.rejects(load(target), (error) => error === originalError);
  await Promise.race([cancelling.promise, failed]);
  assert.deepEqual(transport.calls.cancel, [oldJobId], 'failed attempt starts its own cancellation before settlement');
  await load(target);
  const newState = target.getState();
  cancellation.reject(cancelError);
  await failed;
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(transport.calls.cancel, [oldJobId]);
  assert.equal(target.loaded.jobId, newJobId);
  assert.deepEqual(target.getState(), { ...newState, diagnosticCount: 1 });
  assert.equal(target.terminalOutcome(), null);
  assert.deepEqual(target.diagnosticHistory(), [{ phase: 'cancel', category: 'cancel_failed', message: cancelError.message }]);
});

test('invalidated failing Load cancels only its own job once while a newer attempt succeeds', async () => {
  const oldJobId = '1'.repeat(32), newJobId = '2'.repeat(32);
  const status = deferred(), polling = deferred();
  const transport = fakeTransport({ submit: [{ job: job('queued', 0) }, { job: job('ready', 0, { jobId: newJobId }) }] });
  transport.status = () => { polling.resolve(); return status.promise; };
  const target = controller(transport);
  const failed = assert.rejects(load(target), { code: 'invalidated' });
  await polling.promise;
  await load(target);
  const newState = target.getState();
  status.reject(new Error('stale status rejected late'));
  await failed;
  await Promise.resolve();
  assert.deepEqual(transport.calls.cancel, [oldJobId]);
  assert.equal(target.loaded.jobId, newJobId);
  assert.deepEqual(target.getState(), newState);
  assert.equal(target.terminalOutcome(), null);
});

test('Start requires actual position 0/100 and a complete empty open-order snapshot', async () => {
  for (const overrides of [
    safeSnapshot(1),
    safeSnapshot(0, { openOrdersTotal: 1, openOrdersTruncated: true }),
    safeSnapshot(0, { openOrders: [{}], openOrdersTotal: 1 }),
    safeSnapshot(0, { openOrdersTruncated: undefined }),
  ]) {
    const target = controller(fakeTransport());
    await load(target);
    await assert.rejects(() => target.start(startInput(overrides)));
  }
});

test('dispatch chooses the branch from actual post-commit position and emits at most one 100-share delta', async () => {
  const transport = fakeTransport({ artifact: trace([proposal(0, 100, 0), proposal(2, 100, 0)]) });
  const target = controller(transport);
  await load(target);
  await target.start(startInput());
  target.resume();
  const entry = await target.dispatchOneEvent({ tick: { session_tick_index: 0 }, ...safeSnapshot(0) });
  const noProposal = await target.dispatchOneEvent({ tick: { session_tick_index: 1 }, ...safeSnapshot(100) });
  const exit = await target.dispatchOneEvent({ tick: { session_tick_index: 2 }, ...safeSnapshot(100) });
  assert.deepEqual(entry.commands.map(({ side, quantity }) => ({ side, quantity })), [{ side: 'buy', quantity: 100 }]);
  assert.deepEqual(noProposal.commands, []);
  assert.deepEqual(exit.commands.map(({ side, quantity }) => ({ side, quantity })), [{ side: 'sell', quantity: 100 }]);
  assert.equal(entry.eligibleFromSessionTick, 1);
  assert.equal(exit.eligibleFromSessionTick, 3);
  assert.equal(transport.calls.status.length, 0, 'replay dispatch performs no HTTP');
});

test('dispatch fails closed when post-commit broker state is unsafe', async () => {
  const target = controller(fakeTransport());
  await load(target); await target.start(startInput()); target.resume();
  await assert.rejects(() => target.dispatchOneEvent({ tick: { session_tick_index: 0 }, ...safeSnapshot(200) }), (error) => error.code === 'unsafe_position');
});

test('Stop cancels by job ID without a lease and is idempotent', async () => {
  const transport = fakeTransport();
  const target = controller(transport);
  await load(target);
  const first = target.stop();
  const second = target.stop();
  assert.equal(first, second);
  await first;
  assert.deepEqual(transport.calls.cancel, ['1'.repeat(32)]);
});

test('HTTP transport uses only fixed bundled endpoints and immutable trace headers', async () => {
  const calls = [];
  const fetchImpl = async (url, options = {}) => {
    calls.push([url, options.method ?? 'GET']);
    if (url.endsWith('/capability')) return new Response(JSON.stringify({ available: true, contractVersion: BUNDLED_RL_TRACE_CONTRACT, preset: BUNDLED_RL_PRESET }), { status: 200, headers: { 'content-type': 'application/json' } });
    if (url.includes('/traces/')) return new Response(JSON.stringify(trace()), { status: 200, headers: { 'content-type': 'application/json', 'cache-control': 'private, immutable, max-age=31536000', etag: `"${TRACE_ID}"` } });
    if ((options.method ?? 'GET') === 'POST') return new Response(JSON.stringify({ job: job('ready', 0) }), { status: 200, headers: { 'content-type': 'application/json' } });
    return new Response(JSON.stringify(job('ready', 0)), { status: 200, headers: { 'content-type': 'application/json' } });
  };
  const transport = createBundledRlTraceHttpTransport({ fetchImpl });
  await transport.capability(); await transport.submit({}); await transport.status('1'.repeat(32)); await transport.cancel('1'.repeat(32)); await transport.fetchTrace(TRACE_ID);
  assert.deepEqual(calls.map(([url, method]) => [url.replace('/api/strategies/bundled-rl', ''), method]), [
    ['/capability', 'GET'], ['/jobs', 'POST'], [`/jobs/${'1'.repeat(32)}`, 'GET'], [`/jobs/${'1'.repeat(32)}`, 'DELETE'], [`/traces/${TRACE_ID}`, 'GET'],
  ]);
});

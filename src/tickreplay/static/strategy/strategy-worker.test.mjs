import assert from 'node:assert/strict';
import test from 'node:test';
import { StrategyWorkerRuntime } from './strategy-worker.mjs';
import { createRequestEnvelope, validateWorkerResponse } from './strategy-protocol.mjs';

function request() {
  return createRequestEnvelope({ phase: 'load', requestId: 'worker-load',
    session: { stem: '7203', code: '7203', actual_date: '2026-09-10', generation: 1 },
    runId: null, source: { source_hash: 'a'.repeat(64), strategy_revision: 1, load_attempt_id: 'load' },
    sequence: 0, payload: { source: 'def on_event(event): return []', filename: 'strategy.py',
      assets: { loader_url: '/loader.mjs', index_url: '/pyodide/', runner_url: '/runner.py' } } });
}

function harness({ outcome = { ok: true, callbacks: ['on_event'] }, failure = null } = {}) {
  const posted = [];
  const destroyed = [];
  const pyodide = {
    FS: { writeFile() {} },
    toPy() {
      if (failure === 'toPy') throw new Error('conversion failed');
      return { destroy() { destroyed.push('globals'); } };
    },
    async runPythonAsync() {
      if (failure === 'invoke') throw new Error('invoke failed');
      return {
        toJs() {
          if (failure === 'toJs') throw new Error('conversion failed');
          return failure === 'json' ? '{' : JSON.stringify(outcome);
        },
        destroy() {
          destroyed.push('result');
          if (failure === 'destroy') throw new Error('destructor failed');
        },
      };
    },
  };
  const runtime = new StrategyWorkerRuntime({
    postMessage(message) {
      if (failure === 'post' && message.ok === true) throw new Error('uncloneable');
      posted.push(message);
    },
  }, {
    importModule: async () => {
      if (failure === 'loader') throw new Error('loader failed');
      return { loadPyodide: async () => pyodide };
    },
    fetchResource: async () => ({ ok: failure !== 'fetch', status: 404, text: async () => '# runner' }),
  });
  return { runtime, pyodide, posted, destroyed };
}

test('production progress carries complete identity for deadline ownership', async () => {
  const fixture = harness();
  const input = request();
  await fixture.runtime.onMessage(input);
  const progress = fixture.posted.find(({ kind }) => kind === 'strategy_progress');
  for (const key of ['phase', 'request_id', 'session', 'run_id', 'source', 'sequence']) {
    assert.deepEqual(progress[key], input[key]);
  }
});

test('runner failure and loader/fetch/conversion/post errors retain structured request identity', async () => {
  for (const failure of [null, 'loader', 'fetch', 'toPy', 'invoke', 'toJs', 'json', 'post']) {
    const fixture = harness({ failure, ...(failure === null ? {
      outcome: { ok: false, diagnostic: { category: 'python_exception', message: 'fixture failed', range: null } },
    } : {}) });
    const input = request();
    await fixture.runtime.onMessage(input);
    const response = fixture.posted.findLast(({ kind }) => kind === 'strategy_response');
    assert.equal(validateWorkerResponse(response, { phase: input.phase, requestId: input.request_id,
      session: input.session, runId: input.run_id, source: input.source, sequence: input.sequence }).ok, false);
    assert.equal(response.request_id, input.request_id);
    assert.deepEqual(response.source, input.source);
    assert.equal(fixture.runtime.currentRequest, null);
    const expected = ['loader', 'fetch', 'toPy'].includes(failure) ? []
      : failure === 'invoke' ? ['globals'] : ['result', 'globals'];
    assert.deepEqual(fixture.destroyed, expected);
  }
});

test('result destructor failure still destroys the separate globals proxy', async () => {
  const fixture = harness({ failure: 'destroy' });
  await fixture.runtime.onMessage(request());
  assert.deepEqual(fixture.destroyed, ['result', 'globals']);
  assert.equal(fixture.posted.at(-1).ok, false);
});

test('messageerror settles once and discards an in-flight Python completion', async () => {
  const fixture = harness();
  let finish;
  fixture.pyodide.runPythonAsync = () => new Promise((resolve) => { finish = resolve; });
  const loading = fixture.runtime.onMessage(request());
  await new Promise((resolve) => setImmediate(resolve));
  fixture.runtime.onMessageError();
  finish(JSON.stringify({ ok: true, callbacks: ['on_event'], stdout: 'late' }));
  await loading;
  const responses = fixture.posted.filter(({ kind }) => kind === 'strategy_response');
  assert.equal(responses.length, 1);
  assert.equal(responses[0].diagnostic.category, 'messageerror');
  assert.equal(fixture.posted.some(({ kind }) => kind === 'strategy_log'), false);
});

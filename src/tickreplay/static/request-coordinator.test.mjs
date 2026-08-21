import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { runInNewContext } from 'node:vm';

import {
  OperationStatusGuard,
  RequestCoordinator,
  isCancellationError,
} from './request-coordinator.mjs';

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

const noWait = async () => {};

function extractFunction(source, name) {
  const start = source.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `${name} must exist in app.js`);
  const bodyStart = source.indexOf('{', start);
  let depth = 0;
  for (let index = bodyStart; index < source.length; index += 1) {
    if (source[index] === '{') depth += 1;
    if (source[index] !== '}') continue;
    depth -= 1;
    if (depth === 0) return source.slice(start, index + 1);
  }
  throw new Error(`Could not extract ${name} from app.js`);
}

test('overlapping requests apply only the latest out-of-order response', async () => {
  const first = deferred();
  const second = deferred();
  const responses = [first, second];
  const coordinator = new RequestCoordinator({
    fetchJson: () => responses.shift().promise,
    wait: noWait,
  });
  const applied = [];

  const requestA = coordinator.fetchLatest('symbols', '/api/symbols?q=A')
    .then((value) => applied.push(value.name))
    .catch((error) => assert.ok(isCancellationError(error)));
  const requestB = coordinator.fetchLatest('symbols', '/api/symbols?q=B')
    .then((value) => applied.push(value.name));

  second.resolve({ name: 'B' });
  await requestB;
  first.resolve({ name: 'A' });
  await requestA;

  assert.deepEqual(applied, ['B']);
});

test('session handshake ignores a previous terminal operation and waits for its baseline', async () => {
  const calls = [];
  const responses = [
    { pending: true, serverEpoch: 'epoch-a', operationId: 2 },
    { serverEpoch: 'epoch-a', operationId: 1, revision: 99, state: 'fresh' },
    { serverEpoch: 'epoch-a', operationId: 2, revision: 0, state: 'downloading' },
    { serverEpoch: 'epoch-a', operationId: 2, revision: 1, state: 'fresh' },
    { pending: false, stem: '7203', count: 1 },
  ];
  const statuses = [];
  const coordinator = new RequestCoordinator({
    fetchJson: async (url) => {
      calls.push(url);
      return responses.shift();
    },
    wait: noWait,
  });

  const result = await coordinator.fetchUntilReady('session', '/api/session?stem=7203', {
    stem: '7203',
    onStatus: (status) => statuses.push([status.operationId, status.revision]),
  });

  assert.equal(result.data.count, 1);
  assert.deepEqual(statuses, [[2, 0], [2, 1]]);
  assert.equal(calls.filter((url) => url.startsWith('/api/status')).length, 3);
});

test('polling stops only on the matching operation terminal state', async () => {
  const responses = [
    { pending: true, serverEpoch: 'epoch-a', operationId: 7 },
    { serverEpoch: 'epoch-a', operationId: 8, revision: 50, state: 'corrupt' },
    { serverEpoch: 'epoch-a', operationId: 7, revision: 0, state: 'downloading' },
    { serverEpoch: 'epoch-a', operationId: 7, revision: 1, state: 'fresh' },
    { pending: false, count: 4 },
  ];
  let callCount = 0;
  const coordinator = new RequestCoordinator({
    fetchJson: async () => {
      callCount += 1;
      return responses.shift();
    },
    wait: noWait,
  });

  const result = await coordinator.fetchUntilReady('session', '/api/session', {
    stem: '7203',
  });

  assert.equal(result.data.count, 4);
  assert.equal(callCount, 5);
  assert.equal(responses.length, 0);
});

test('a second operation accepts revision zero after a prior higher revision', async () => {
  const responses = [
    { pending: true, serverEpoch: 'epoch-a', operationId: 10 },
    { serverEpoch: 'epoch-a', operationId: 10, revision: 4, state: 'downloading' },
    { serverEpoch: 'epoch-a', operationId: 10, revision: 5, state: 'fresh' },
    { pending: false, count: 1 },
    { pending: true, serverEpoch: 'epoch-a', operationId: 11 },
    { serverEpoch: 'epoch-a', operationId: 11, revision: 0, state: 'downloading' },
    { serverEpoch: 'epoch-a', operationId: 11, revision: 1, state: 'fresh' },
    { pending: false, count: 2 },
  ];
  const revisions = [];
  const coordinator = new RequestCoordinator({
    fetchJson: async () => responses.shift(),
    wait: noWait,
  });

  await coordinator.fetchUntilReady('session', '/api/session', {
    stem: '7203',
    onStatus: (status) => revisions.push([status.operationId, status.revision]),
  });
  const second = await coordinator.fetchUntilReady('session', '/api/session', {
    stem: '7203',
    onStatus: (status) => revisions.push([status.operationId, status.revision]),
  });

  assert.equal(second.data.count, 2);
  assert.deepEqual(revisions, [[10, 4], [10, 5], [11, 0], [11, 1]]);
});

test('a cache-hit handshake never starts status polling', async () => {
  const calls = [];
  const coordinator = new RequestCoordinator({
    fetchJson: async (url) => {
      calls.push(url);
      return { pending: false, count: 3, operationId: null };
    },
    wait: noWait,
  });

  const result = await coordinator.fetchUntilReady('session', '/api/session', {
    stem: '7203',
  });

  assert.equal(result.data.count, 3);
  assert.deepEqual(calls, ['/api/session']);
});

test('changing the selected stem cancels an old status response', async () => {
  const oldStatus = deferred();
  const coordinator = new RequestCoordinator({
    fetchJson: async (url) => {
      if (url === '/session-a') {
        return { pending: true, serverEpoch: 'epoch-a', operationId: 1 };
      }
      if (url.startsWith('/api/status')) return oldStatus.promise;
      return { pending: false, stem: 'B' };
    },
    wait: noWait,
  });
  const applied = [];

  const requestA = coordinator.fetchUntilReady('session', '/session-a', {
    stem: 'A',
    onStatus: (status) => applied.push(status.state),
  }).catch((error) => assert.ok(isCancellationError(error)));
  await Promise.resolve();
  const requestB = coordinator.fetchUntilReady('symbol-info', '/symbol-b', { stem: 'B' });
  await requestB;
  oldStatus.resolve({ serverEpoch: 'epoch-a', operationId: 1, revision: 1, state: 'fresh' });
  await requestA;

  assert.deepEqual(applied, []);
});

test('an old server epoch cannot roll back the active handshake epoch', () => {
  const guard = new OperationStatusGuard();
  guard.begin('7203', { serverEpoch: 'epoch-new', operationId: 2 });

  const decision = guard.accept('7203', {
    serverEpoch: 'epoch-old',
    operationId: 99,
    revision: 99,
    state: 'fresh',
  });

  assert.equal(decision.restart, true);
  assert.equal(guard.serverEpoch, 'epoch-new');
});

test('a timed-out start retry observes one server operation handshake', async () => {
  let calls = 0;
  const statuses = [];
  const coordinator = new RequestCoordinator({
    fetchJson: async () => {
      calls += 1;
      if (calls === 1) throw new Error('response timed out');
      if (calls === 2) return { pending: true, serverEpoch: 'epoch-a', operationId: 12 };
      if (calls === 3) {
        return { serverEpoch: 'epoch-a', operationId: 12, revision: 0, state: 'fresh' };
      }
      return { pending: false, count: 8 };
    },
    wait: noWait,
    startRetries: 1,
  });

  const result = await coordinator.fetchUntilReady('session', '/api/session', {
    stem: '7203',
    onStatus: (status) => statuses.push(status.operationId),
  });

  assert.equal(result.data.count, 8);
  assert.deepEqual(statuses, [12]);
  assert.equal(calls, 4);
});

test('stale-served is terminal and preserves a degraded result for cached playback', async () => {
  const responses = [
    { pending: true, serverEpoch: 'epoch-a', operationId: 13 },
    { serverEpoch: 'epoch-a', operationId: 13, revision: 0, state: 'stale-served' },
    { pending: false, count: 9 },
  ];
  const coordinator = new RequestCoordinator({
    fetchJson: async () => responses.shift(),
    wait: noWait,
  });

  const result = await coordinator.fetchUntilReady('session', '/api/session', {
    stem: '7203',
  });

  assert.equal(result.data.count, 9);
  assert.equal(result.status.state, 'stale-served');
});

test('tape row renders a malicious remote Type as text without parsing HTML', () => {
  const maliciousType = '<img src=x onerror="globalThis.exploited=true">';
  const appSource = readFileSync(new URL('./app.js', import.meta.url), 'utf8');
  const tapeRowSource = extractFunction(appSource, 'tapeRow');
  let innerHtmlWrites = 0;

  class FakeElement {
    constructor(tagName) {
      this.tagName = tagName;
      this.className = '';
      this.textContent = '';
      this.children = [];
    }

    set innerHTML(value) {
      innerHtmlWrites += 1;
      throw new Error(`Unsafe innerHTML assignment: ${value}`);
    }

    appendChild(child) {
      this.children.push(child);
      return child;
    }
  }

  const row = runInNewContext(`${tapeRowSource}\ntapeRow(0)`, {
    document: { createElement: (tagName) => new FakeElement(tagName) },
    formatClock: () => '09:00:00.000',
    intFormat: { format: String },
    priceFormat: { format: String },
    state: { t: [0], price: [100], qty: [200], type: [maliciousType] },
    tapeDirection: () => 'flat',
  });

  assert.equal(innerHtmlWrites, 0);
  assert.equal(row.children.length, 4);
  assert.equal(row.children[3].textContent, maliciousType);
  assert.equal(row.children[3].children.length, 0);
});

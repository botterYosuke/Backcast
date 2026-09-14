import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import { createOrderBroker } from './order-broker.mjs';
import {
  advanceStrategyClock,
  createReplayCommitter,
  strategyPortfolioSnapshot,
} from './replay-commit.mjs';
import { validateEventPayload } from './strategy/strategy-protocol.mjs';
import { StrategyController } from './strategy/strategy-controller.mjs';

const SESSION = Object.freeze({
  stem: '285A', code: '285A', actual_date: '2026-09-10', generation: 1,
});
const RUN = Object.freeze({ session: SESSION, runId: 'run-1', strategyRevision: 1 });
const APP_SOURCE = readFileSync(new URL('./app.js', import.meta.url), 'utf8');

function hostLifecycle(controller, committer = null) {
  const invalidation = APP_SOURCE.match(/function invalidateStrategyRuntime\(cause\) \{[\s\S]*?\n\}/)[0];
  const stop = APP_SOURCE.match(/function stopStrategy\(\) \{[\s\S]*?\n\}/)[0];
  return new Function('strategyController', 'strategyCommitter', `
    ${invalidation}
    ${stop}
    return { stop: stopStrategy, invalidate: invalidateStrategyRuntime };
  `)(controller, committer);
}

function hostControllerFixture() {
  const listeners = new Map();
  const messages = [];
  const worker = {
    terminated: 0,
    addEventListener(type, listener) { listeners.set(type, listener); },
    removeEventListener(type) { listeners.delete(type); },
    postMessage(message) { messages.push(message); },
    terminate() { this.terminated += 1; },
    respond() {
      const request = messages.findLast((message) => message.kind === 'strategy_request');
      listeners.get('message')({ data: {
        kind: 'strategy_response', protocol_version: request.protocol_version,
        phase: request.phase, request_id: request.request_id, session: request.session,
        run_id: request.run_id, source: request.source, sequence: request.sequence, ok: true,
        result: { callbacks: ['on_event'], capabilities: { soft_interrupt: false } },
      } });
    },
  };
  const controller = new StrategyController({
    createWorker: () => worker,
    createInterruptBuffer: () => null,
    setTimer: () => 1,
    clearTimer() {},
    assets: { loaderUrl: '/pyodide.mjs', indexUrl: '/pyodide/', runnerUrl: '/strategy-runner.py' },
  });
  const loading = controller.load({
    source: 'def on_event(event):\n    return []\n', sourceHash: 'a'.repeat(64),
    draftRevision: 1, loadAttemptId: 'host-load', session: SESSION,
  });
  return { worker, controller, loading };
}

for (const operation of ['stop', 'workspace dispose']) {
  test(`host ${operation} terminates a ready loaded Worker exactly once without an active run`, async () => {
    const { worker, controller, loading } = hostControllerFixture();
    worker.respond();
    await loading;
    assert.equal(controller.getState().tag, 'ready');
    assert.equal(controller.isActive(), false);
    const host = hostLifecycle(controller);
    try {
      if (operation === 'stop') {
        await host.stop();
        await host.stop();
      } else {
        const workspaceSource = readFileSync(new URL('./strategy/strategy-workspace.mjs', import.meta.url), 'utf8');
        const disposeSource = workspaceSource.match(/  function dispose\(\) \{[\s\S]*?\n  \}/)[0];
        const dispose = new Function('host', `
          let disposed = false, beforeUnloadInstalled = false;
          const cancelPendingLoad = () => {};
          const sourceAutosave = { dispose() {} }, settingsAutosave = { dispose() {} };
          const cleanups = [], editor = null, windowObject = null, beforeUnload = null;
          ${disposeSource}
          return dispose;
        `)(host);
        dispose();
        dispose();
        // Workspace teardown intentionally returns void; observe its controller job.
        await controller.invalidationJob?.promise;
      }
      assert.equal(worker.terminated, 1);
      assert.equal(controller.worker, null);
      assert.equal(controller.getState().tag, 'idle');
    } finally {
      await controller.stop();
    }
  });
}

test('host Stop settles a pending Load and finalizes its Worker without an active run', async () => {
  const { worker, controller, loading } = hostControllerFixture();
  assert.equal(controller.getState().tag, 'loading');
  assert.equal(controller.isActive(), true, 'pending requests already participate in isActive');
  const rejection = assert.rejects(loading, { code: 'invalidated' });
  try {
    await hostLifecycle(controller).stop();
    assert.equal(worker.terminated, 1);
    assert.equal(controller.getState().tag, 'idle');
  } finally {
    await controller.stop();
    await rejection;
  }
});

test('host invalidation waits for retirement resources even when the controller reports idle', async () => {
  const controller = new StrategyController({ createWorker() { assert.fail('no new Worker needed'); }, assets: {} });
  let releaseRetirement;
  const retirement = new Promise((resolve) => { releaseRetirement = resolve; });
  controller.retiringWorkers.add(retirement);
  const stopped = hostLifecycle(controller).stop();
  try {
    assert.ok(stopped instanceof Promise, 'the host must delegate idle-owned resources');
    let settled = false;
    stopped.then(() => { settled = true; });
    await Promise.resolve();
    assert.equal(settled, false);
    releaseRetirement();
    await stopped;
    assert.equal(controller.getState().tag, 'idle');
  } finally {
    releaseRetirement();
    await controller.stop();
  }
});

test('host invalidation retains the active committer cleanup path and safely handles no controller', async () => {
  const calls = [];
  const host = hostLifecycle({ invalidate() { assert.fail('committer owns this run'); } }, {
    status: () => ({ active: true }),
    invalidate(cause) { calls.push(cause); return 'committer-cleanup'; },
  });
  assert.equal(await host.stop(), 'committer-cleanup');
  assert.deepEqual(calls, ['explicit_stop']);
  assert.equal(hostLifecycle(null).stop(), null);
});

function commandResult(index, sequence, commands) {
  return {
    kind: 'commands',
    protocolVersion: 1,
    session: SESSION,
    runId: RUN.runId,
    strategyRevision: RUN.strategyRevision,
    sequence,
    observedSessionTickIndex: index,
    eligibleFromSessionTick: index + 1,
    commands,
  };
}

function market(clientIntentId, side, quantity) {
  return { type: 'submit_market', client_intent_id: clientIntentId, side, quantity };
}

function limit(clientIntentId, side, quantity, limitPriceLevel) {
  return {
    type: 'submit_limit',
    client_intent_id: clientIntentId,
    side,
    quantity,
    limit_price_level: limitPriceLevel,
  };
}

function cancel(clientIntentId, side) {
  return { type: 'cancel_strategy_orders', client_intent_id: clientIntentId, side };
}

function createFakeStrategyPort(responder) {
  const events = [];
  let paused = false;
  let invalidated = false;
  return {
    events,
    dispatchOneEvent(event) {
      events.push(structuredClone(event));
      return responder(event, events.length);
    },
    pause() { paused = true; },
    resume() { paused = false; },
    invalidate() { invalidated = true; },
    get paused() { return paused; },
    get invalidated() { return invalidated; },
  };
}

function tick(index, replayTimeUs, price) {
  return {
    session_tick_index: index,
    replay_time_us: replayTimeUs,
    price_level: price,
    quantity: 100,
    trade_type: 'uptick',
  };
}

test('numeric rejection rolls back staged commands, intent ledger, broker state, and replay cursor together', async () => {
  const broker = createOrderBroker();
  const strategyPort = createFakeStrategyPort((event, sequence) => commandResult(
    event.tick.session_tick_index, sequence, event.tick.session_tick_index === 0 ? [
      market('first-valid', 'buy', 1),
      market('second-overflow', 'buy', Number.MAX_SAFE_INTEGER),
    ] : [],
  ));
  const committer = createReplayCommitter({ broker, strategyPort });
  committer.beginRun(RUN);
  committer.resume();
  let cursor = 0;
  const applyTick = () => { cursor += 1; };
  await committer.commitTick({ sessionTickIndex: 0, tick: tick(0, 1, 100), applyTick });
  const before = broker.exportState();
  const statusBefore = committer.status();
  await assert.rejects(committer.commitTick({ sessionTickIndex: 1, tick: tick(1, 2, 100), applyTick }), {
    code: 'strategy_representation_error',
  });
  assert.equal(cursor, 1, 'rejected tick is not consumed');
  assert.equal(broker.exportState(), before, 'batch IDs, intents and first fill are not accepted');
  assert.deepEqual(committer.status(), statusBefore, 'the staged batch is retained');
  assert.equal(strategyPort.events.length, 1, 'no invalid event is dispatched');
});

test('exact outgoing event preflight happens before broker and replay publication', async () => {
  const broker = createOrderBroker();
  const strategyPort = createFakeStrategyPort((event, sequence) => commandResult(
    event.tick.session_tick_index, sequence, [],
  ));
  let rejectSnapshot = true;
  strategyPort.validateEventSnapshot = (event) => {
    assert.equal(event.portfolio.cash_minor, -10000);
    if (rejectSnapshot) throw new Error('injected_snapshot_failure');
  };
  broker.submitManualLimit({ side: 'buy', level: 100, price: 100, quantity: 1 });
  const committer = createReplayCommitter({ broker, strategyPort });
  committer.beginRun(RUN);
  committer.resume();
  const before = broker.exportState();
  let cursor = 0;
  const request = { sessionTickIndex: 0, tick: tick(0, 1, 100), applyTick() { cursor += 1; } };
  await assert.rejects(committer.commitTick(request), /injected_snapshot_failure/);
  assert.equal(broker.exportState(), before);
  assert.equal(cursor, 0);
  assert.equal(strategyPort.events.length, 0);
  rejectSnapshot = false;
  await committer.commitTick(request);
  assert.equal(cursor, 1);
  assert.equal(strategyPort.events.length, 1);
  assert.equal(broker.state.nextFillId, 2);
});

for (const field of ['session_tick_index', 'replay_time_us', 'price_level', 'quantity']) {
  test(`unsafe outgoing tick ${field} is rejected without consuming a replay tick`, async () => {
    const broker = createOrderBroker();
    const strategyPort = createFakeStrategyPort((event, sequence) => commandResult(0, sequence, []));
    const committer = createReplayCommitter({ broker, strategyPort });
    committer.beginRun(RUN);
    committer.resume();
    const before = broker.exportState();
    let applied = false;
    await assert.rejects(committer.commitTick({
      sessionTickIndex: 0,
      tick: { ...tick(0, 1, 100), [field]: Number.MAX_SAFE_INTEGER + 1 },
      tradePrice: 100,
      applyTick() { applied = true; },
    }), { code: 'strategy_representation_error' });
    assert.equal(applied, false);
    assert.equal(broker.exportState(), before);
    assert.equal(strategyPort.events.length, 0);
  });
}

test('last safe tick index cannot consume an unrepresentable next replay cursor', async () => {
  const broker = createOrderBroker();
  const strategyPort = createFakeStrategyPort((event, sequence) => commandResult(
    event.tick.session_tick_index, sequence, [],
  ));
  const committer = createReplayCommitter({ broker, strategyPort });
  committer.beginRun(RUN);
  committer.resume();
  const before = broker.exportState();
  let applied = false;
  await assert.rejects(committer.commitTick({
    sessionTickIndex: Number.MAX_SAFE_INTEGER,
    tick: tick(Number.MAX_SAFE_INTEGER, 1, 100),
    applyTick() { applied = true; },
  }), { code: 'strategy_representation_error' });
  assert.equal(applied, false);
  assert.equal(broker.exportState(), before);
  assert.equal(strategyPort.events.length, 0);
});

test('app rejects incompatible manual state before calling controller Start or beginning a run', async () => {
  const broker = createOrderBroker();
  broker.submitManualMarket({ side: 'buy', quantity: Number.MAX_SAFE_INTEGER, price: 100, time: 1 });
  const before = broker.exportState();
  let starts = 0;
  let begins = 0;
  const startSource = APP_SOURCE.match(/async function startStrategy\(\) \{[\s\S]*?\n\}/)[0];
  const invoke = new Function(
    'strategyController', 'strategyCommitter', 'strategySessionIdentity', 'orderBroker',
    'state', 'strategyPortfolio', 'levelOf',
    `return (${startSource})();`,
  );
  await assert.rejects(invoke(
    { start() { starts += 1; } },
    { beginRun() { begins += 1; } },
    () => SESSION,
    broker,
    { cursor: 1, last: 100, t: [1, 2], price: [100, 101], playing: false },
    () => strategyPortfolioSnapshot(broker.portfolioSnapshot(), 100),
    Math.round,
  ), { code: 'strategy_representation_error' });
  assert.equal(starts, 0);
  assert.equal(begins, 0);
  assert.equal(broker.exportState(), before);
});

test('failed run admission does not publish an active replay identity', () => {
  const broker = createOrderBroker();
  broker.submitManualMarket({ side: 'buy', quantity: Number.MAX_SAFE_INTEGER, price: 100, time: 1 });
  const committer = createReplayCommitter({ broker, strategyPort: createFakeStrategyPort(() => {}) });
  const before = committer.status();
  assert.throws(() => committer.beginRun(RUN), { code: 'strategy_representation_error' });
  assert.deepEqual(committer.status(), before);
});

test('tick i commands cannot affect i and first become effective before i+1', async () => {
  const broker = createOrderBroker();
  const strategyPort = createFakeStrategyPort((event, sequence) => {
    if (event.tick.session_tick_index === 0) {
      return commandResult(0, sequence, [limit('buy-at-100', 'buy', 2, 100)]);
    }
    return commandResult(event.tick.session_tick_index, sequence, []);
  });
  const committer = createReplayCommitter({ broker, strategyPort });
  committer.beginRun(RUN);
  committer.resume();

  const first = await committer.commitTick({
    sessionTickIndex: 0,
    tick: tick(0, 1_000_000, 99),
    applyTick: () => {},
  });
  assert.deepEqual(first.fills, []);
  assert.equal(broker.state.orders.length, 0, 'tick 0 response is staged, not retroactive');

  const second = await committer.commitTick({
    sessionTickIndex: 1,
    tick: tick(1, 2_000_000, 99),
    applyTick: () => {},
  });
  assert.equal(second.fills.length, 1);
  assert.equal(second.fills[0].price, 100, 'limit fills at limit, not crossed trade price');
  assert.equal(second.fills[0].eligibleFromSessionTick, 1);
});

test('strategy market uses the next real tick and fill feedback is delivered in that tick event', async () => {
  const broker = createOrderBroker();
  const strategyPort = createFakeStrategyPort((event, sequence) => {
    const index = event.tick.session_tick_index;
    return commandResult(index, sequence, index === 0 ? [market('sell-next', 'sell', 3)] : []);
  });
  const committer = createReplayCommitter({ broker, strategyPort });
  committer.beginRun(RUN);
  committer.resume();

  await committer.commitTick({
    sessionTickIndex: 0,
    tick: tick(0, 1_000_000, 100),
    applyTick: () => {},
  });
  await committer.commitTick({
    sessionTickIndex: 1,
    tick: tick(1, 1_000_000, 107),
    applyTick: () => {},
  });

  assert.equal(strategyPort.events[0].fills.length, 0);
  assert.deepEqual(strategyPort.events[1].fills.map((fill) => ({
    side: fill.side,
    priceLevel: fill.price_level,
    fill_delivery_sequence: fill.fill_delivery_sequence,
  })), [{ side: 'sell', priceLevel: 107, fill_delivery_sequence: 1 }]);
  assert.equal(strategyPort.events[1].fillCursor, 1);
  const delivered = strategyPort.events[1];
  assert.doesNotThrow(() => validateEventPayload({
    tick: delivered.tick,
    fills: delivered.fills,
    fill_cursor: delivered.fillCursor,
    portfolio: delivered.portfolio,
    open_orders: delivered.openOrders,
    open_orders_total: delivered.openOrdersTotal,
    open_orders_truncated: delivered.openOrdersTruncated,
  }));
});

test('strategy portfolio derives cash and equity from the authoritative net position', () => {
  assert.deepEqual(strategyPortfolioSnapshot({
    qty: 60,
    avgPrice: 10,
    realized: 80,
    entryCount: 1,
    exitCount: 1,
  }, 12), {
    cash_minor: -52_000,
    position_quantity: 60,
    average_price_level: 10,
    realized_pnl_minor: 8_000,
    unrealized_pnl_minor: 12_000,
    equity_minor: 20_000,
  });
});

for (const sign of [1, -1]) {
  test(`signed ${sign} minor-unit exact boundary is accepted and boundary+1 is rejected`, () => {
    const portfolio = { qty: 0, avgPrice: 0, realized: sign * Number.MAX_SAFE_INTEGER / 100, entryCount: 0, exitCount: 0 };
    const projected = strategyPortfolioSnapshot(portfolio, null);
    assert.equal(projected.cash_minor, sign * Number.MAX_SAFE_INTEGER);
    assert.equal(projected.realized_pnl_minor, sign * Number.MAX_SAFE_INTEGER);
    assert.equal(projected.equity_minor, sign * Number.MAX_SAFE_INTEGER);
    assert.throws(() => strategyPortfolioSnapshot({ ...portfolio, realized: sign * (Number.MAX_SAFE_INTEGER + 1) / 100 }, null), {
      code: 'strategy_representation_error',
    });
  });
}

test('equity alone may overflow even when realized and unrealized P&L each fit', () => {
  assert.throws(() => strategyPortfolioSnapshot({
    qty: 1, avgPrice: 1, realized: Number.MAX_SAFE_INTEGER / 100, entryCount: 1, exitCount: 0,
  }, 2), { code: 'strategy_representation_error' });
});

test('nonfinite derived money and unrepresentable average conversion are rejected', () => {
  const portfolio = { qty: 2, avgPrice: 1, realized: 0, entryCount: 1, exitCount: 0 };
  for (const lastPrice of [Infinity, NaN, Number.MAX_VALUE]) {
    assert.throws(() => strategyPortfolioSnapshot(portfolio, lastPrice), { code: 'strategy_representation_error' });
  }
  for (const conversion of [() => 0, () => 1.5, () => Infinity, () => Number.MAX_SAFE_INTEGER + 1]) {
    assert.throws(() => strategyPortfolioSnapshot(portfolio, 1, conversion), { code: 'strategy_representation_error' });
  }
});

test('cancel from tick i cannot erase an i fill and applies before matching i+1', async () => {
  const broker = createOrderBroker();
  const strategyPort = createFakeStrategyPort((event, sequence) => {
    const index = event.tick.session_tick_index;
    if (index === 0) return commandResult(index, sequence, [limit('resting', 'sell', 1, 110)]);
    if (index === 1) return commandResult(index, sequence, [cancel('cancel-resting', 'sell')]);
    return commandResult(index, sequence, []);
  });
  const committer = createReplayCommitter({ broker, strategyPort });
  committer.beginRun(RUN);
  committer.resume();

  await committer.commitTick({ sessionTickIndex: 0, tick: tick(0, 1, 100), applyTick() {} });
  await committer.commitTick({ sessionTickIndex: 1, tick: tick(1, 2, 109), applyTick() {} });
  assert.equal(broker.state.orders.length, 1);
  await committer.commitTick({ sessionTickIndex: 2, tick: tick(2, 3, 111), applyTick() {} });
  assert.equal(broker.state.orders.length, 0);
  assert.equal(broker.state.fills.length, 0, 'cancel is committed before tick 2 matching');
});

test('manual orders keep canonical priority and fill delivery is contiguous', async () => {
  const broker = createOrderBroker();
  broker.submitManualLimit({
    side: 'buy', level: 100, price: 100, quantity: 1, eligibleFromSessionTick: 0,
  });
  const strategyPort = createFakeStrategyPort((event, sequence) => commandResult(
    event.tick.session_tick_index,
    sequence,
    event.tick.session_tick_index === 0 ? [limit('strategy', 'buy', 1, 100)] : [],
  ));
  const committer = createReplayCommitter({ broker, strategyPort });
  committer.beginRun(RUN);
  committer.resume();

  await committer.commitTick({ sessionTickIndex: 0, tick: tick(0, 1, 101), applyTick() {} });
  await committer.commitTick({ sessionTickIndex: 1, tick: tick(1, 2, 99), applyTick() {} });

  assert.deepEqual(strategyPort.events.flatMap((event) => event.fills).map((fill) => ({
    orderId: fill.order_id,
    delivery: fill.fill_delivery_sequence,
  })), [
    { orderId: 1, delivery: 1 },
    { orderId: 2, delivery: 2 },
  ]);
  assert.equal(strategyPort.events[1].fillCursor, 2);
});

test('pause during callback stages the matching result and resume commits before next tick', async () => {
  let resolveDispatch;
  let dispatchCount = 0;
  const broker = createOrderBroker();
  const strategyPort = createFakeStrategyPort((event, sequence) => {
    dispatchCount += 1;
    if (dispatchCount > 1) return commandResult(event.tick.session_tick_index, sequence, []);
    return new Promise((resolve) => { resolveDispatch = resolve; });
  });
  const committer = createReplayCommitter({ broker, strategyPort });
  committer.beginRun(RUN);
  committer.resume();

  const pending = committer.commitTick({
    sessionTickIndex: 0,
    tick: tick(0, 1, 100),
    applyTick() {},
  });
  await Promise.resolve();
  committer.pause();
  resolveDispatch(commandResult(0, 1, [market('after-pause', 'buy', 1)]));
  await pending;
  assert.equal(committer.status().hasStagedBatch, true);
  assert.equal(broker.state.orders.length, 0);

  committer.resume();
  await committer.commitTick({ sessionTickIndex: 1, tick: tick(1, 2, 105), applyTick() {} });
  assert.equal(broker.state.fills[0].price, 105);
});

test('one unresolved event is enforced and invalidation rejects a delayed result', async () => {
  let resolveDispatch;
  const broker = createOrderBroker();
  const strategyPort = createFakeStrategyPort(() => new Promise((resolve) => {
    resolveDispatch = resolve;
  }));
  const committer = createReplayCommitter({ broker, strategyPort });
  committer.beginRun(RUN);
  committer.resume();
  const pending = committer.commitTick({
    sessionTickIndex: 0,
    tick: tick(0, 1, 100),
    applyTick() {},
  });

  await assert.rejects(
    committer.commitTick({ sessionTickIndex: 1, tick: tick(1, 2, 101), applyTick() {} }),
    /event_in_flight/,
  );
  committer.invalidate('seek');
  resolveDispatch(commandResult(0, 1, [market('stale', 'buy', 1)]));
  const result = await pending;
  assert.equal(result.kind, 'stale_result');
  assert.equal(committer.status().hasStagedBatch, false);
});

test('invalidation during an asynchronous host commit prevents event dispatch', async () => {
  let releaseHostCommit;
  const broker = createOrderBroker();
  const strategyPort = createFakeStrategyPort(() => {
    assert.fail('an invalidated tick must not reach the strategy');
  });
  const committer = createReplayCommitter({ broker, strategyPort });
  committer.beginRun(RUN);
  committer.resume();
  const pending = committer.commitTick({
    sessionTickIndex: 0,
    tick: tick(0, 1, 100),
    applyTick() {},
    afterTick: () => new Promise((resolve) => { releaseHostCommit = resolve; }),
  });

  await Promise.resolve();
  committer.invalidate('seek');
  releaseHostCommit();
  assert.equal((await pending).kind, 'stale_result');
  assert.equal(strategyPort.events.length, 0);
});

test('final tick response is validated and discarded without broker side effects', async () => {
  const broker = createOrderBroker();
  const strategyPort = createFakeStrategyPort((event, sequence) => commandResult(
    event.tick.session_tick_index,
    sequence,
    [market('post-session', 'buy', 1)],
  ));
  const committer = createReplayCommitter({ broker, strategyPort });
  committer.beginRun(RUN);
  committer.resume();
  const before = broker.exportState();

  const result = await committer.commitTick({
    sessionTickIndex: 0,
    tick: tick(0, 1, 100),
    applyTick() {},
    isFinalTick: true,
  });
  assert.equal(result.strategyResult.kind, 'final_tick_discarded');
  const after = broker.snapshot();
  const beforeSnapshot = JSON.parse(before);
  assert.equal(after.orders.length, beforeSnapshot.orders.length);
  assert.equal(after.nextOrderId, beforeSnapshot.nextOrderId);
  assert.equal(after.nextSubmissionSequence, beforeSnapshot.nextSubmissionSequence);
});

test('same-timestamp ticks remain separate events with speed/delay-independent traces', async () => {
  async function runFixture(delayMs) {
    const broker = createOrderBroker();
    const strategyPort = createFakeStrategyPort(async (event, sequence) => {
      if (delayMs) await new Promise((resolve) => setTimeout(resolve, delayMs));
      const index = event.tick.session_tick_index;
      return commandResult(index, sequence, index < 2 ? [market(`m-${index}`, 'buy', 1)] : []);
    });
    const committer = createReplayCommitter({ broker, strategyPort });
    committer.beginRun(RUN);
    committer.resume();
    const ticks = [tick(0, 1_000_000, 100), tick(1, 1_000_000, 101), tick(2, 2_000_000, 102)];
    for (let index = 0; index < ticks.length; index += 1) {
      await committer.commitTick({
        sessionTickIndex: index,
        tick: ticks[index],
        applyTick() {},
        isFinalTick: index === ticks.length - 1,
      });
    }
    return {
      eventTicks: strategyPort.events.map((event) => event.tick.session_tick_index),
      fills: broker.state.fills.map((fill) => ({ side: fill.side, price: fill.price, time: fill.time })),
      portfolio: broker.portfolioSnapshot(),
    };
  }

  assert.deepEqual(await runFixture(0), await runFixture(3));
  assert.deepEqual((await runFixture(0)).eventTicks, [0, 1, 2]);
});

test('x1, x10, and x500 scheduler budgets produce identical causal traces', async () => {
  async function runAtSpeed(speed) {
    const broker = createOrderBroker();
    const strategyPort = createFakeStrategyPort((event, sequence) => {
      const index = event.tick.session_tick_index;
      return commandResult(index, sequence, index < 2 ? [market(`speed-${index}`, 'buy', 1)] : []);
    });
    const committer = createReplayCommitter({ broker, strategyPort });
    committer.beginRun(RUN);
    committer.resume();
    const ticks = [tick(0, 1_000_000, 100), tick(1, 2_000_000, 101), tick(2, 3_000_000, 102)];
    let cursor = 0;
    let timestamp = 0;
    let clock = {
      committedVirtualTime: 0,
      candidateVirtualTime: 0,
      lastFrame: 0,
    };
    while (cursor < ticks.length) {
      timestamp += 16;
      clock = advanceStrategyClock(clock, {
        timestamp,
        playing: true,
        blocked: false,
        speed,
        nextTickTime: ticks[cursor].replay_time_us / 1_000_000,
        skipGaps: false,
      });
      if (!clock.shouldCommitTick) continue;
      await committer.commitTick({
        sessionTickIndex: cursor,
        tick: ticks[cursor],
        applyTick() {},
        isFinalTick: cursor === ticks.length - 1,
      });
      cursor += 1;
    }
    return {
      fills: broker.state.fills.map((fill) => ({
        orderId: fill.orderId,
        price: fill.price,
        time: fill.time,
      })),
      portfolio: broker.portfolioSnapshot(),
      eventTicks: strategyPort.events.map((event) => event.tick.session_tick_index),
    };
  }

  const atOne = await runAtSpeed(1);
  assert.deepEqual(await runAtSpeed(10), atOne);
  assert.deepEqual(await runAtSpeed(500), atOne);
});

test('strategy clock commits only the earliest due tick and discards blocked wall time', () => {
  let clock = {
    committedVirtualTime: 0,
    candidateVirtualTime: 0,
    lastFrame: 0,
  };
  clock = advanceStrategyClock(clock, {
    timestamp: 1_000,
    playing: true,
    blocked: false,
    speed: 10,
    nextTickTime: 2,
    skipGaps: false,
  });
  assert.equal(clock.shouldCommitTick, false);

  clock = advanceStrategyClock(clock, {
    timestamp: 1_200,
    playing: true,
    blocked: false,
    speed: 10,
    nextTickTime: 2,
    skipGaps: false,
  });
  assert.equal(clock.shouldCommitTick, true);
  assert.equal(clock.committedVirtualTime, 2);
  assert.equal(clock.candidateVirtualTime, 2);

  clock = advanceStrategyClock(clock, {
    timestamp: 5_000,
    playing: true,
    blocked: true,
    speed: 500,
    nextTickTime: 3,
    skipGaps: false,
  });
  assert.equal(clock.committedVirtualTime, 2);
  assert.equal(clock.lastFrame, 5_000);

  clock = advanceStrategyClock(clock, {
    timestamp: 5_010,
    playing: true,
    blocked: false,
    speed: 1,
    nextTickTime: 3,
    skipGaps: false,
  });
  assert.equal(clock.shouldCommitTick, false, 'blocked duration is not caught up');
  assert.ok(clock.candidateVirtualTime < 3);
});

test('gap skip is deterministic and never advances beyond the due tick', () => {
  const result = advanceStrategyClock({
    committedVirtualTime: 10,
    candidateVirtualTime: 10,
    lastFrame: 1_000,
  }, {
    timestamp: 1_016,
    playing: true,
    blocked: false,
    speed: 1,
    nextTickTime: 30,
    skipGaps: true,
    gapThreshold: 5,
  });
  assert.equal(result.shouldCommitTick, true);
  assert.equal(result.committedVirtualTime, 30);
  assert.equal(result.candidateVirtualTime, 30);
});

test('app owns one controller port, lifecycle invalidation, and the manual gate', () => {
  assert.match(APP_SOURCE, /createReplayCommitter\(\{/);
  assert.match(APP_SOURCE, /window\.tickReplayStrategyHost = Object\.freeze\(\{/);
  assert.match(APP_SOURCE, /load: loadStrategy/);
  assert.match(APP_SOURCE, /start: startStrategy/);
  assert.match(APP_SOURCE, /stop: stopStrategy/);
  assert.match(APP_SOURCE, /async function loadStrategy[\s\S]*strategyController\.load\(\{ \.\.\.request, session \}\)/);
  assert.match(APP_SOURCE, /async function startStrategy[\s\S]*strategyController\.start\(\{/);
  assert.match(APP_SOURCE, /function placeOrder[\s\S]*manualEntryBlocked\(\)/);
  assert.match(APP_SOURCE, /function placeMobileMarketOrder[\s\S]*manualEntryBlocked\(\)/);
  assert.match(APP_SOURCE, /function cancelOrders[\s\S]*manualEntryBlocked\(\)/);
  assert.match(APP_SOURCE, /async function loadSession[\s\S]*invalidateStrategyRuntime\('session_change'\)/);
  assert.match(APP_SOURCE, /function seekTo[\s\S]*invalidateStrategyRuntime\('seek'\)/);
  assert.match(APP_SOURCE, /resetButton[\s\S]*invalidateStrategyRuntime\('reset'\)/);
  assert.match(APP_SOURCE, /addEventListener\('pagehide'[\s\S]*invalidateStrategyRuntime\('explicit_stop'\)/);
  assert.match(APP_SOURCE, /addEventListener\('pageshow'[\s\S]*event\.persisted/);
});

test('app bootstraps the immutable strategy release and isolates raw recovery failures', () => {
  const releaseUrl = '/static/strategy/releases/' +
    'fde82a53f12e81c6fbac68d116c2afc3fd8e0254907dd7b6e93cb9750a63f14d/' +
    'strategy-release.mjs';
  const capturedUrl = `const STRATEGY_RELEASE_URL = '${releaseUrl}';`;
  assert.equal(APP_SOURCE.includes(capturedUrl), true);
  assert.ok(APP_SOURCE.indexOf(capturedUrl) < APP_SOURCE.indexOf('const els = {'));
  assert.match(APP_SOURCE, /function bootstrapStrategyWorkspace\(\)[\s\S]*import\(STRATEGY_RELEASE_URL\)/);
  assert.ok(
    APP_SOURCE.indexOf('window.tickReplayStrategyHost = Object.freeze({') <
      APP_SOURCE.indexOf('void bootstrapStrategyWorkspace();'),
    'the host boundary must exist before the release import starts',
  );
  assert.match(
    APP_SOURCE,
    /function safeCall\(label, operation, onError\)[\s\S]*Promise\.resolve\(operation\(\)\)\.catch/,
  );
  assert.match(
    APP_SOURCE,
    /releaseModule\.mount\(\{\s*root,\s*host: window\.tickReplayStrategyHost,\s*safeCall,\s*\}\)/,
  );
  assert.match(APP_SOURCE, /root\.dataset\.strategyMode = 'raw-recovery'/);
  assert.match(APP_SOURCE, /source\.hidden = false[\s\S]*source\.readOnly = true[\s\S]*source\.tabIndex = 0/);
  assert.match(APP_SOURCE, /querySelectorAll\('\[data-strategy-action\]'\)[\s\S]*control\.disabled = true/);
  assert.match(APP_SOURCE, /querySelector\('\[data-strategy-import\]'\)[\s\S]*importInput\.disabled = true/);
  assert.match(APP_SOURCE, /status\.textContent =/);
  assert.match(APP_SOURCE, /if \(!event\.persisted\) disposeStrategyWorkspace\(\)/);
});

test('app safeCall contains synchronous throws and asynchronous rejections', async () => {
  const start = APP_SOURCE.indexOf('function safeCall(');
  const end = APP_SOURCE.indexOf('\nasync function getJson', start);
  const safeCall = Function(`${APP_SOURCE.slice(start, end)}; return safeCall;`)();
  const handled = [];
  let invoked = false;

  const synchronous = safeCall('sync', () => {
    invoked = true;
    throw new Error('sync failure');
  }, (error) => {
    handled.push(error.message);
    return 'sync recovered';
  });
  assert.equal(invoked, true, 'operation is invoked before safeCall returns');
  assert.equal(await synchronous, 'sync recovered');
  assert.equal(await safeCall(
    'async',
    () => Promise.reject(new Error('async failure')),
    (error) => {
      handled.push(error.message);
      return 'async recovered';
    },
  ), 'async recovered');
  assert.deepEqual(handled, ['sync failure', 'async failure']);
});

test('app raw recovery exposes only a read-only source surface', () => {
  const start = APP_SOURCE.indexOf('function enterStrategyRawRecovery(');
  const end = APP_SOURCE.indexOf('\nfunction bootstrapStrategyWorkspace', start);
  const enterRawRecovery = Function(
    `${APP_SOURCE.slice(start, end)}; return enterStrategyRawRecovery;`,
  )();
  const panel = { hidden: true };
  const toggle = { setAttribute(name, value) { this[name] = value; } };
  const editor = { hidden: false };
  const source = {
    hidden: true,
    readOnly: false,
    tabIndex: -1,
    setAttribute(name, value) { this[name] = value; },
    removeAttribute(name) { delete this[name]; },
  };
  const importInput = { disabled: false };
  const status = { textContent: '', dataset: {} };
  const controls = [{ disabled: false }, { disabled: false }];
  const elements = new Map([
    ['[data-strategy-panel]', panel],
    ['[data-strategy-action="toggle"]', toggle],
    ['[data-strategy-source]', source],
    ['[data-strategy-editor]', editor],
    ['[data-strategy-import]', importInput],
    ['[data-strategy-status]', status],
  ]);
  const root = {
    dataset: {},
    querySelector: (selector) => elements.get(selector) ?? null,
    querySelectorAll: () => controls,
  };

  enterRawRecovery(root);
  assert.equal(root.dataset.strategyMode, 'raw-recovery');
  assert.equal(panel.hidden, false);
  assert.equal(toggle['aria-expanded'], 'true');
  assert.equal(editor.hidden, true);
  assert.deepEqual(
    { hidden: source.hidden, readOnly: source.readOnly, tabIndex: source.tabIndex },
    { hidden: false, readOnly: true, tabIndex: 0 },
  );
  assert.equal(importInput.disabled, true);
  assert.equal(controls.every((control) => control.disabled), true);
  assert.match(status.textContent, /ソースの選択とコピーのみ/);
});

test('app keeps the legacy fast path and uses one-tick commits only while strategy is active', () => {
  assert.match(APP_SOURCE, /if \(strategyCommitter\?\.status\(\)\.active\) \{\s*runStrategyReplayFrame\(timestamp\)/);
  assert.match(APP_SOURCE, /while \(state\.cursor < state\.t\.length && state\.t\[state\.cursor\] <= state\.vt\)/);
  assert.match(APP_SOURCE, /matchOrders,/);
  assert.match(APP_SOURCE, /commitStrategyTick\(state\.cursor\)/);
  assert.match(APP_SOURCE, /isFinalTick: index === state\.t\.length - 1/);
});

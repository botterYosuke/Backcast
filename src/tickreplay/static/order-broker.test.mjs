import assert from 'node:assert/strict';
import test from 'node:test';

import {
  BROKER_TRANSACTION_STAGES,
  BrokerValidationError,
  createOrderBroker,
  legacyObservableProjection,
} from './order-broker.mjs';

const SESSION = Object.freeze({
  stem: '285A', code: '285A', actualDate: '2026-09-10', generation: 1,
});
const RUN = Object.freeze({ session: SESSION, runId: 'run-1', strategyRevision: 1 });

function strategyBatch({
  sequence = 1,
  observedSessionTickIndex = 0,
  commands = [],
} = {}) {
  return {
    protocolVersion: 1,
    session: SESSION,
    runId: RUN.runId,
    strategyRevision: RUN.strategyRevision,
    sequence,
    observedSessionTickIndex,
    eligibleFromSessionTick: observedSessionTickIndex + 1,
    commands,
  };
}

function submitLimit(clientIntentId, side, quantity, limitPriceLevel) {
  return {
    type: 'submit_limit',
    client_intent_id: clientIntentId,
    side,
    quantity,
    limit_price_level: limitPriceLevel,
  };
}

function submitMarket(clientIntentId, side, quantity) {
  return { type: 'submit_market', client_intent_id: clientIntentId, side, quantity };
}

function cancelStrategyOrders(clientIntentId, side) {
  const command = { type: 'cancel_strategy_orders', client_intent_id: clientIntentId };
  if (side) command.side = side;
  return command;
}

test('manual normalization preserves immediate market, aggregation, cancellation, and golden projection', () => {
  const broker = createOrderBroker();

  broker.submitManualOrder({
    side: 'sell', level: 103, price: 103, quantity: 2,
    lastLevel: 100, lastPrice: 100, time: 100, eligibleFromSessionTick: 1,
  });
  broker.submitManualOrder({
    side: 'sell', level: 103, price: 103, quantity: 3,
    lastLevel: 100, lastPrice: 100, time: 100, eligibleFromSessionTick: 1,
  });
  broker.submitManualOrder({
    side: 'buy', level: 100, price: 100, quantity: 2,
    lastLevel: 100, lastPrice: 100, time: 100, eligibleFromSessionTick: 1,
  });

  assert.deepEqual(legacyObservableProjection(broker.snapshot()), {
    portfolio: { qty: 2, avgPrice: 100, realized: 0, entryCount: 1, exitCount: 0 },
    orders: [{ id: 1, side: 'sell', level: 103, price: 103, qty: 5 }],
    fills: [{ side: 'buy', qty: 2, price: 100, time: 100 }],
    events: [{ kind: 'entry', side: 'buy', qty: 2, price: 100, time: 100 }],
    nextOrderId: 2,
  });

  assert.equal(broker.cancelManualOrders('sell').canceledOrderIds.length, 1);
  assert.deepEqual(broker.state.orders, []);
});

test('broker projection is byte-for-byte equal to the full legacy characterization golden', () => {
  const broker = createOrderBroker();
  broker.submitManualLimit({
    side: 'sell', level: 103, price: 103, quantity: 2, eligibleFromSessionTick: 0,
  });
  broker.submitManualLimit({
    side: 'sell', level: 103, price: 103, quantity: 3, eligibleFromSessionTick: 0,
  });
  broker.submitManualLimit({
    side: 'buy', level: 99, price: 99, quantity: 4, eligibleFromSessionTick: 0,
  });
  broker.matchRange({
    fromIndex: 0,
    toIndex: 4,
    times: [100, 101, 102, 103],
    prices: [100, 103, 99, 104],
  });
  broker.submitManualMarket({ side: 'buy', quantity: 2, price: 104, time: 103 });
  broker.submitManualMarket({ side: 'sell', quantity: 1, price: 104, time: 103 });
  broker.submitManualLimit({
    side: 'sell', level: 105, price: 105, quantity: 7, eligibleFromSessionTick: 4,
  });
  broker.submitManualLimit({
    side: 'buy', level: 98, price: 98, quantity: 8, eligibleFromSessionTick: 4,
  });
  broker.cancelManualOrders('sell');

  assert.deepEqual(legacyObservableProjection(broker.snapshot()), {
    portfolio: {
      qty: 0, avgPrice: 0, realized: 15, entryCount: 2, exitCount: 3,
    },
    orders: [{ id: 4, side: 'buy', level: 98, price: 98, qty: 8 }],
    fills: [
      { side: 'sell', qty: 5, price: 103, time: 101 },
      { side: 'buy', qty: 4, price: 99, time: 102 },
      { side: 'buy', qty: 2, price: 104, time: 103 },
      { side: 'sell', qty: 1, price: 104, time: 103 },
    ],
    events: [
      { kind: 'entry', side: 'sell', qty: 5, price: 103, time: 101 },
      { kind: 'exit', side: 'buy', qty: 4, price: 99, pnl: 16, time: 102 },
      { kind: 'exit', side: 'buy', qty: 1, price: 104, pnl: -1, time: 103 },
      { kind: 'entry', side: 'buy', qty: 1, price: 104, time: 103 },
      { kind: 'exit', side: 'sell', qty: 1, price: 104, pnl: 0, time: 103 },
    ],
    nextOrderId: 5,
  });
});

test('manual frame matching keeps first crossing and fills limits at the limit price', () => {
  const broker = createOrderBroker();
  broker.submitManualLimit({
    side: 'sell', level: 104, price: 104, quantity: 10, eligibleFromSessionTick: 0,
  });

  const result = broker.matchRange({
    fromIndex: 0,
    toIndex: 4,
    times: [10, 11, 12, 13],
    prices: [100, 105, 103, 106],
  });

  assert.equal(result.fills.length, 1);
  assert.deepEqual(legacyObservableProjection(broker.snapshot()).fills, [
    { side: 'sell', qty: 10, price: 104, time: 11 },
  ]);
});

test('strategy batch is eligible only at i+1 and market fills at that real tick price', () => {
  const broker = createOrderBroker();
  broker.beginStrategyRun(RUN);
  const batch = strategyBatch({
    observedSessionTickIndex: 4,
    commands: [
      submitLimit('limit-1', 'buy', 3, 99),
      submitMarket('market-1', 'sell', 2),
    ],
  });

  assert.throws(
    () => broker.commitStrategyBatch(batch, { sessionTickIndex: 4 }),
    (error) => error instanceof BrokerValidationError && error.code === 'ineligible_batch',
  );
  assert.equal(broker.state.orders.length, 0);

  broker.commitStrategyBatch(batch, { sessionTickIndex: 5 });
  const result = broker.matchTick({
    sessionTickIndex: 5,
    tradePrice: 98,
    time: 500,
    deliveryRun: RUN,
  });

  assert.deepEqual(result.fills.map((fill) => ({
    orderId: fill.orderId,
    side: fill.side,
    price: fill.price,
    fillDeliverySequence: fill.fillDeliverySequence,
  })), [
    { orderId: 1, side: 'buy', price: 99, fillDeliverySequence: 1 },
    { orderId: 2, side: 'sell', price: 98, fillDeliverySequence: 2 },
  ]);
  assert.equal(result.fillCursor, 2);
});

test('cancellations run first while new submissions retain returned-list order', () => {
  const broker = createOrderBroker();
  broker.beginStrategyRun(RUN);
  broker.commitStrategyBatch(strategyBatch({
    commands: [submitLimit('old-buy', 'buy', 1, 90)],
  }), { sessionTickIndex: 1 });

  const result = broker.commitStrategyBatch(strategyBatch({
    sequence: 2,
    observedSessionTickIndex: 1,
    commands: [
      submitLimit('new-buy', 'buy', 2, 91),
      cancelStrategyOrders('cancel-buy', 'buy'),
      submitLimit('new-sell', 'sell', 3, 110),
    ],
  }), { sessionTickIndex: 2 });

  assert.deepEqual(result.canceledOrderIds, [1]);
  assert.deepEqual(broker.state.orders.map((order) => ({
    id: order.id,
    side: order.side,
    clientIntentId: order.clientIntentId,
  })), [
    { id: 2, side: 'buy', clientIntentId: 'new-buy' },
    { id: 3, side: 'sell', clientIntentId: 'new-sell' },
  ]);
});

test('manual and strategy orders coexist without cross-aggregation or cross-cancellation', () => {
  const broker = createOrderBroker();
  broker.submitManualLimit({
    side: 'buy', level: 99, price: 99, quantity: 5, eligibleFromSessionTick: 0,
  });
  broker.beginStrategyRun(RUN);
  broker.commitStrategyBatch(strategyBatch({
    commands: [submitLimit('strategy-buy', 'buy', 7, 99)],
  }), { sessionTickIndex: 1 });

  assert.equal(broker.state.orders.length, 2);
  assert.deepEqual(broker.state.orders.map((order) => order.source), ['manual', 'strategy']);
  broker.cancelStrategyRunOrders(RUN);
  assert.deepEqual(broker.state.orders.map((order) => order.source), ['manual']);
});

test('strategy cancellation is scoped to the exact session, run, and revision', () => {
  const otherRun = { session: SESSION, runId: 'run-2', strategyRevision: 1 };
  const broker = createOrderBroker();
  broker.beginStrategyRun(RUN);
  broker.commitStrategyBatch(strategyBatch({
    commands: [submitLimit('run-one-order', 'buy', 1, 90)],
  }), { sessionTickIndex: 1 });
  broker.beginStrategyRun(otherRun);
  broker.commitStrategyBatch({
    ...strategyBatch({ commands: [submitLimit('run-two-order', 'buy', 1, 89)] }),
    runId: otherRun.runId,
  }, { sessionTickIndex: 1 });

  broker.commitStrategyBatch(strategyBatch({
    sequence: 2,
    observedSessionTickIndex: 1,
    commands: [cancelStrategyOrders('cancel-run-one')],
  }), { sessionTickIndex: 2 });

  assert.deepEqual(broker.state.orders.map((order) => order.runId), ['run-2']);
});

test('intent IDs are run-wide unique and command/live-order limits reject atomically', () => {
  const broker = createOrderBroker({ maxCommandsPerRun: 3, maxLiveOrders: 2 });
  broker.beginStrategyRun(RUN);
  broker.commitStrategyBatch(strategyBatch({
    commands: [submitLimit('one', 'buy', 1, 90)],
  }), { sessionTickIndex: 1 });
  const beforeDuplicate = broker.exportState();

  assert.throws(
    () => broker.commitStrategyBatch(strategyBatch({
      sequence: 2,
      observedSessionTickIndex: 1,
      commands: [submitMarket('one', 'sell', 1)],
    }), { sessionTickIndex: 2 }),
    (error) => error.code === 'duplicate_client_intent_id',
  );
  assert.equal(broker.exportState(), beforeDuplicate);

  assert.throws(
    () => broker.commitStrategyBatch(strategyBatch({
      sequence: 2,
      observedSessionTickIndex: 1,
      commands: [
        submitLimit('two', 'buy', 1, 89),
        submitLimit('three', 'buy', 1, 88),
      ],
    }), { sessionTickIndex: 2 }),
    (error) => error.code === 'live_order_limit',
  );
  assert.equal(broker.exportState(), beforeDuplicate);
});

test('manual-only trading may exceed the strategy cap but Start refuses that retained state', () => {
  const broker = createOrderBroker({ maxLiveOrders: 2 });
  for (let index = 0; index < 3; index += 1) {
    broker.submitManualLimit({
      side: 'buy', level: 90 - index, price: 90 - index, quantity: 1,
      eligibleFromSessionTick: 0,
    });
  }
  assert.equal(broker.state.orders.length, 3, 'strategy-disabled manual behavior is not capped');
  assert.throws(
    () => broker.beginStrategyRun(RUN),
    (error) => error.code === 'live_order_limit',
  );
});

test('run-wide command budget includes cancellations and rejects without mutation', () => {
  const broker = createOrderBroker({ maxCommandsPerRun: 2 });
  broker.beginStrategyRun(RUN);
  broker.commitStrategyBatch(strategyBatch({
    commands: [submitLimit('one', 'buy', 1, 90), cancelStrategyOrders('two')],
  }), { sessionTickIndex: 1 });
  const before = broker.exportState();
  assert.throws(
    () => broker.commitStrategyBatch(strategyBatch({
      sequence: 2,
      observedSessionTickIndex: 1,
      commands: [submitMarket('three', 'sell', 1)],
    }), { sessionTickIndex: 2 }),
    (error) => error.code === 'run_command_limit',
  );
  assert.equal(broker.exportState(), before);
});

test('defensive batch validation rejects per-event excess, duplicate IDs, and unknown fields', () => {
  const broker = createOrderBroker();
  broker.beginStrategyRun(RUN);
  const invalidBatches = [
    {
      expected: 'event_command_limit',
      commands: Array.from({ length: 33 }, (_, index) => submitMarket(`m-${index}`, 'buy', 1)),
    },
    {
      expected: 'duplicate_client_intent_id',
      commands: [submitMarket('duplicate', 'buy', 1), submitMarket('duplicate', 'sell', 1)],
    },
    {
      expected: 'unknown_command_field',
      commands: [{ ...submitMarket('extra', 'buy', 1), price: 100 }],
    },
  ];
  for (const fixture of invalidBatches) {
    const before = broker.exportState();
    assert.throws(
      () => broker.commitStrategyBatch(strategyBatch({ commands: fixture.commands }), {
        sessionTickIndex: 1,
      }),
      (error) => error.code === fixture.expected,
    );
    assert.equal(broker.exportState(), before);
  }
  assert.throws(
    () => broker.commitStrategyBatch({ ...strategyBatch(), unexpected: true }, {
      sessionTickIndex: 1,
    }),
    (error) => error.code === 'unknown_batch_field',
  );
  assert.throws(
    () => broker.commitStrategyBatch({ ...strategyBatch(), kind: 'diagnostic' }, {
      sessionTickIndex: 1,
    }),
    (error) => error.code === 'invalid_batch_kind',
  );
});

test('duplicate applied response is idempotent while out-of-order response fails closed', () => {
  const broker = createOrderBroker();
  broker.beginStrategyRun(RUN);
  const batch = strategyBatch({ commands: [submitLimit('one', 'buy', 1, 90)] });
  const first = broker.commitStrategyBatch(batch, { sessionTickIndex: 1 });
  const afterFirst = broker.exportState();
  const duplicate = broker.commitStrategyBatch(batch, { sessionTickIndex: 1 });

  assert.equal(first.kind, 'committed');
  assert.equal(duplicate.kind, 'duplicate');
  assert.equal(broker.exportState(), afterFirst);
  assert.throws(
    () => broker.commitStrategyBatch(strategyBatch({
      sequence: 3,
      observedSessionTickIndex: 2,
      commands: [],
    }), { sessionTickIndex: 3 }),
    (error) => error.code === 'out_of_order_sequence',
  );
});

test('copy-on-write preparation faults leave every authoritative component unchanged', () => {
  for (const faultStage of BROKER_TRANSACTION_STAGES) {
    const broker = createOrderBroker({
      faultInjector(stage) {
        if (stage === faultStage) throw new Error(`fault:${stage}`);
      },
    });
    broker.beginStrategyRun(RUN);
    const before = broker.exportState();
    assert.throws(
      () => broker.commitStrategyBatch(strategyBatch({
        commands: [submitLimit(`intent-${faultStage}`, 'buy', 1, 90)],
      }), { sessionTickIndex: 1 }),
      new RegExp(`fault:${faultStage}`),
    );
    assert.equal(broker.exportState(), before, faultStage);
  }
});

test('final-tick commands validate but reserve no broker identity or state', () => {
  const broker = createOrderBroker();
  broker.beginStrategyRun(RUN);
  const before = broker.exportState();
  const result = broker.discardFinalStrategyBatch(strategyBatch({
    observedSessionTickIndex: 9,
    commands: [submitMarket('too-late', 'buy', 1)],
  }));

  assert.equal(result.kind, 'final_tick_discarded');
  assert.equal(result.commandCount, 1);
  assert.equal(broker.exportState(), before);
});

test('open-order snapshots are oldest-first, bounded, and report hidden totals', () => {
  const broker = createOrderBroker({ maxLiveOrders: 4, maxVisibleOpenOrders: 2 });
  for (let index = 0; index < 3; index += 1) {
    broker.submitManualLimit({
      side: 'buy',
      level: 90 - index,
      price: 90 - index,
      quantity: 1,
      eligibleFromSessionTick: 0,
    });
  }

  assert.deepEqual(broker.strategyOpenOrders(), {
    openOrders: [
      {
        order_id: 1,
        owner: 'manual',
        side: 'buy',
        quantity_remaining: 1,
        limit_price_level: 90,
        eligible_from_session_tick: 0,
      },
      {
        order_id: 2,
        owner: 'manual',
        side: 'buy',
        quantity_remaining: 1,
        limit_price_level: 89,
        eligible_from_session_tick: 0,
      },
    ],
    openOrdersTotal: 3,
    openOrdersTruncated: true,
  });
});

test('capacity violation commits authoritative fills but delivers none partially', () => {
  const broker = createOrderBroker({ maxLiveOrders: 3, maxFillsPerEvent: 2 });
  broker.beginStrategyRun(RUN);
  broker.commitStrategyBatch(strategyBatch({
    commands: [
      submitLimit('one', 'buy', 1, 100),
      submitLimit('two', 'buy', 1, 100),
      submitLimit('three', 'buy', 1, 100),
    ],
  }), { sessionTickIndex: 1 });

  const result = broker.matchTick({
    sessionTickIndex: 1,
    tradePrice: 99,
    time: 10,
    deliveryRun: RUN,
  });
  assert.equal(result.kind, 'host_capacity_violation');
  assert.deepEqual(result.fills, []);
  assert.equal(result.authoritativeFillCount, 3);
  assert.equal(result.fillCursor, 0);
  assert.equal(broker.state.fills.length, 3);
  assert.equal(broker.state.orders.length, 0);
});

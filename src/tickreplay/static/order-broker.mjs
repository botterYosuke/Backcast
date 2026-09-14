import {
  applyFill,
  classifyClick,
  createPortfolio,
  isFilled,
  strategyPortfolioSnapshot,
  assertStrategyInteger,
  StrategyRepresentationError,
} from './paper-trading.mjs';

'use strict';

export const DEFAULT_BROKER_LIMITS = Object.freeze({
  maxCommandsPerEvent: 32,
  maxCommandsPerRun: 10_000,
  maxLiveOrders: 256,
  maxVisibleOpenOrders: 256,
  maxFillsPerEvent: 256,
  maxQuantity: Number.MAX_SAFE_INTEGER,
});

export const BROKER_TRANSACTION_STAGES = Object.freeze([
  'prepare_orders',
  'prepare_broker_events',
  'prepare_seen_intents',
  'prepare_counters',
  'prepare_ledger',
  'before_swap',
]);

const COMMAND_KEYS = Object.freeze({
  submit_market: Object.freeze(['client_intent_id', 'quantity', 'side', 'type']),
  submit_limit: Object.freeze([
    'client_intent_id', 'limit_price_level', 'quantity', 'side', 'type',
  ]),
  cancel_strategy_orders: Object.freeze(['client_intent_id', 'side', 'type']),
});
const BATCH_KEYS = Object.freeze([
  'commands', 'eligibleFromSessionTick', 'observedSessionTickIndex', 'protocolVersion',
  'runId', 'sequence', 'session', 'strategyRevision',
]);

export class BrokerValidationError extends Error {
  constructor(code, message = code) {
    super(message);
    this.name = 'BrokerValidationError';
    this.code = code;
  }
}

function validationError(code, message) {
  throw new BrokerValidationError(code, message);
}

function isPlainObject(value) {
  if (value === null || typeof value !== 'object') return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function assertPlainObject(value, code) {
  if (!isPlainObject(value)) validationError(code);
}

function assertSide(side) {
  if (side !== 'buy' && side !== 'sell') validationError('invalid_side');
}

function assertPositiveSafeInteger(value, code) {
  if (!Number.isSafeInteger(value) || value <= 0) validationError(code);
}

function canonicalSession(session) {
  assertPlainObject(session, 'invalid_session');
  const stem = session.stem;
  const code = session.code;
  const actualDate = session.actualDate ?? session.actual_date ?? session.date;
  if (![stem, code, actualDate].every((value) => typeof value === 'string' && value)) {
    validationError('invalid_session');
  }
  if (!Number.isSafeInteger(session.generation) || session.generation < 0) {
    validationError('invalid_session');
  }
  return { stem, code, actualDate, generation: session.generation };
}

function sessionKey(session) {
  const normalized = canonicalSession(session);
  return `${normalized.stem}\u0000${normalized.code}\u0000${normalized.actualDate}\u0000${normalized.generation}`;
}

function runKey(identity) {
  assertPlainObject(identity, 'invalid_run_identity');
  if (typeof identity.runId !== 'string' || !identity.runId) {
    validationError('invalid_run_identity');
  }
  assertPositiveSafeInteger(identity.strategyRevision, 'invalid_run_identity');
  return `${sessionKey(identity.session)}\u0000${identity.runId}\u0000${identity.strategyRevision}`;
}

function cloneLedger(ledger) {
  return {
    identity: {
      session: { ...ledger.identity.session },
      runId: ledger.identity.runId,
      strategyRevision: ledger.identity.strategyRevision,
    },
    seenIntentIds: new Set(ledger.seenIntentIds),
    acceptedCommandCount: ledger.acceptedCommandCount,
    lastAppliedSequence: ledger.lastAppliedSequence,
    lastAppliedSignature: ledger.lastAppliedSignature,
    fillCursor: ledger.fillCursor,
  };
}

function cloneRunLedgers(runLedgers) {
  return new Map([...runLedgers].map(([key, ledger]) => [key, cloneLedger(ledger)]));
}

function clonePortfolio(portfolio) {
  return { ...portfolio };
}

function commandSignature(batch) {
  return JSON.stringify({
    protocolVersion: batch.protocolVersion,
    session: canonicalSession(batch.session),
    runId: batch.runId,
    strategyRevision: batch.strategyRevision,
    sequence: batch.sequence,
    observedSessionTickIndex: batch.observedSessionTickIndex,
    eligibleFromSessionTick: batch.eligibleFromSessionTick,
    commands: batch.commands,
  });
}

function validateExactKeys(value, allowed, code) {
  const actual = Object.keys(value).sort();
  const expected = [...allowed].sort();
  if (actual.length !== expected.length || actual.some((key, index) => key !== expected[index])) {
    validationError(code);
  }
}

function validateClientIntentId(value) {
  if (typeof value !== 'string' || !value || new TextEncoder().encode(value).byteLength > 128) {
    validationError('invalid_client_intent_id');
  }
}

function normalizeStrategyCommand(command, limits) {
  assertPlainObject(command, 'invalid_command');
  const allowedKeys = COMMAND_KEYS[command.type];
  if (!allowedKeys) validationError('unsupported_command');
  if (command.type === 'cancel_strategy_orders' && !Object.hasOwn(command, 'side')) {
    validateExactKeys(command, ['client_intent_id', 'type'], 'unknown_command_field');
  } else {
    validateExactKeys(command, allowedKeys, 'unknown_command_field');
  }
  validateClientIntentId(command.client_intent_id);

  if (command.type === 'cancel_strategy_orders') {
    if (Object.hasOwn(command, 'side')) assertSide(command.side);
    return { ...command };
  }

  assertSide(command.side);
  assertPositiveSafeInteger(command.quantity, 'invalid_quantity');
  if (command.quantity > limits.maxQuantity) validationError('invalid_quantity');
  if (command.type === 'submit_limit') {
    assertPositiveSafeInteger(command.limit_price_level, 'invalid_limit_price_level');
  }
  return { ...command };
}

function withoutBrokerMetadata(value, { removeKind = false } = {}) {
  const projected = {};
  for (const [key, item] of Object.entries(value)) {
    if ([
      'fillId',
      'orderId',
      'source',
      'submissionSequence',
      'eligibleFromSessionTick',
      'session',
      'runId',
      'strategyRevision',
      'clientIntentId',
      'fillDeliverySequence',
    ].includes(key)) continue;
    if (removeKind && key === 'kind') continue;
    projected[key] = item;
  }
  return projected;
}

/** Remove only broker metadata so extraction can be compared with legacy bytes. */
export function legacyObservableProjection(snapshot) {
  return {
    portfolio: { ...snapshot.portfolio },
    orders: snapshot.orders.map((order) => withoutBrokerMetadata(order, { removeKind: true })),
    fills: snapshot.fills.map(withoutBrokerMetadata),
    events: snapshot.events.map(withoutBrokerMetadata),
    nextOrderId: snapshot.nextOrderId,
  };
}

export function createOrderBroker(options = {}) {
  const limits = Object.fromEntries(Object.keys(DEFAULT_BROKER_LIMITS).map((key) => [
    key,
    options[key] ?? DEFAULT_BROKER_LIMITS[key],
  ]));
  const priceForLevel = options.priceForLevel || ((level) => level);
  const priceLevelForPrice = options.priceLevelForPrice || ((price) => Math.round(price));
  const faultInjector = options.faultInjector || (() => {});
  const state = {
    portfolio: createPortfolio(),
    orders: [],
    fills: [],
    events: [],
    brokerEvents: [],
    nextOrderId: 1,
    nextFillId: 1,
    nextSubmissionSequence: 1,
    runLedgers: new Map(),
  };

  function snapshot() {
    return {
      portfolio: clonePortfolio(state.portfolio),
      orders: state.orders.map((order) => ({ ...order, session: order.session && { ...order.session } })),
      fills: state.fills.map((fill) => ({ ...fill, session: fill.session && { ...fill.session } })),
      events: state.events.map((event) => ({ ...event })),
      brokerEvents: state.brokerEvents.map((event) => ({ ...event })),
      nextOrderId: state.nextOrderId,
      nextFillId: state.nextFillId,
      nextSubmissionSequence: state.nextSubmissionSequence,
      runLedgers: [...state.runLedgers.entries()].map(([key, ledger]) => ({
        key,
        identity: {
          ...ledger.identity,
          session: { ...ledger.identity.session },
        },
        seenIntentIds: [...ledger.seenIntentIds].sort(),
        acceptedCommandCount: ledger.acceptedCommandCount,
        lastAppliedSequence: ledger.lastAppliedSequence,
        lastAppliedSignature: ledger.lastAppliedSignature,
        fillCursor: ledger.fillCursor,
      })),
    };
  }

  function exportState() {
    return JSON.stringify(snapshot());
  }

  function validateNumericRecord(record) {
    for (const [field, value] of Object.entries(record)) {
      if (typeof value !== 'number') continue;
      if (!Number.isFinite(value)) throw new StrategyRepresentationError(field);
      if (['price', 'pnl', 'time'].includes(field)) {
        if (field === 'price' && value <= 0) throw new StrategyRepresentationError(field);
        if (field === 'price') assertStrategyInteger(priceLevelForPrice(value), 'price_level', 1);
        if (field === 'pnl') assertStrategyInteger(Math.round(value * 100), 'pnl_minor');
      } else {
        assertStrategyInteger(value, field, ['eligibleFromSessionTick'].includes(field) ? 0 : 1);
      }
    }
  }

  function validateCandidate(candidate, lastPrice, { history = false } = {}) {
    strategyPortfolioSnapshot(candidate.portfolio, lastPrice, priceLevelForPrice);
    for (const field of ['nextOrderId', 'nextFillId', 'nextSubmissionSequence']) {
      assertStrategyInteger(candidate[field], field, 1);
    }
    candidate.orders.forEach(validateNumericRecord);
    for (const ledger of candidate.runLedgers.values()) {
      for (const field of ['acceptedCommandCount', 'lastAppliedSequence', 'fillCursor']) {
        assertStrategyInteger(ledger[field], field, 0);
      }
    }
    if (history) {
      candidate.fills.forEach(validateNumericRecord);
      candidate.events.forEach(validateNumericRecord);
    }
  }

  function validateStrategyState(lastPrice = state.portfolio.avgPrice) {
    validateCandidate(state, lastPrice, { history: true });
  }

  function appendFill(transaction, order, price, time, fillDeliverySequence = null) {
    const fill = {
      fillId: transaction.nextFillId,
      orderId: order.id ?? null,
      source: order.source,
      submissionSequence: order.submissionSequence,
      eligibleFromSessionTick: order.eligibleFromSessionTick,
      side: order.side,
      qty: order.qty,
      price,
      time,
    };
    if (order.session) fill.session = { ...order.session };
    if (order.runId) fill.runId = order.runId;
    if (order.strategyRevision) fill.strategyRevision = order.strategyRevision;
    if (order.clientIntentId) fill.clientIntentId = order.clientIntentId;
    if (fillDeliverySequence !== null) fill.fillDeliverySequence = fillDeliverySequence;

    const portfolio = transaction.portfolio;
    const fillEvents = applyFill(portfolio, fill).map((event) => ({
      ...event,
      fillId: fill.fillId,
      orderId: fill.orderId,
      source: fill.source,
    }));
    transaction.fills.push(fill);
    transaction.events.push(...fillEvents);
    transaction.nextFillId += 1;
    return fill;
  }

  function validateManualLimit({ side, level, price, quantity, eligibleFromSessionTick }) {
    assertSide(side);
    assertPositiveSafeInteger(level, 'invalid_limit_price_level');
    assertPositiveSafeInteger(quantity, 'invalid_quantity');
    if (quantity > limits.maxQuantity) validationError('invalid_quantity');
    if (!Number.isFinite(price) || price <= 0) validationError('invalid_limit_price');
    if (!Number.isSafeInteger(eligibleFromSessionTick) || eligibleFromSessionTick < 0) {
      validationError('invalid_eligibility');
    }
  }

  function submitManualLimit({
    side,
    level,
    price,
    quantity,
    eligibleFromSessionTick = 0,
  }) {
    validateManualLimit({ side, level, price, quantity, eligibleFromSessionTick });
    const existing = state.orders.find(
      (order) => order.source === 'manual' && order.side === side && order.level === level,
    );
    if (existing) {
      const orders = state.orders.map((order) => (
        order === existing ? { ...order, qty: order.qty + quantity } : order
      ));
      if (state.runLedgers.size) orders.forEach(validateNumericRecord);
      state.orders = orders;
      return { kind: 'limit_aggregated', order: state.orders.find((order) => order.id === existing.id) };
    }
    const order = {
      id: state.nextOrderId,
      kind: 'limit',
      source: 'manual',
      submissionSequence: state.nextSubmissionSequence,
      eligibleFromSessionTick,
      side,
      level,
      price,
      qty: quantity,
    };
    if (state.runLedgers.size) {
      validateNumericRecord(order);
      assertStrategyInteger(state.nextOrderId + 1, 'nextOrderId', 1);
      assertStrategyInteger(state.nextSubmissionSequence + 1, 'nextSubmissionSequence', 1);
    }
    state.orders = [...state.orders, order];
    state.nextOrderId += 1;
    state.nextSubmissionSequence += 1;
    return { kind: 'limit_submitted', order };
  }

  function submitManualMarket({ side, quantity, price, time, eligibleFromSessionTick = 0 }) {
    assertSide(side);
    assertPositiveSafeInteger(quantity, 'invalid_quantity');
    if (quantity > limits.maxQuantity) validationError('invalid_quantity');
    if (!Number.isFinite(price) || price <= 0) validationError('invalid_market_price');
    if (!Number.isFinite(time)) validationError('invalid_fill_time');
    const transaction = {
      portfolio: clonePortfolio(state.portfolio),
      fills: [...state.fills],
      events: [...state.events],
      nextFillId: state.nextFillId,
    };
    const order = {
      id: null,
      source: 'manual',
      submissionSequence: state.nextSubmissionSequence,
      eligibleFromSessionTick,
      side,
      qty: quantity,
    };
    const fill = appendFill(transaction, order, price, time);
    if (state.runLedgers.size) {
      validateCandidate({ ...state, ...transaction, nextSubmissionSequence: state.nextSubmissionSequence + 1 }, price);
      validateNumericRecord(fill);
      transaction.events.slice(state.events.length).forEach(validateNumericRecord);
    }
    state.portfolio = transaction.portfolio;
    state.fills = transaction.fills;
    state.events = transaction.events;
    state.nextFillId = transaction.nextFillId;
    state.nextSubmissionSequence += 1;
    return { kind: 'market_filled', fill };
  }

  function submitManualOrder({
    side,
    level,
    price,
    quantity,
    lastLevel,
    lastPrice,
    time,
    eligibleFromSessionTick = 0,
  }) {
    assertSide(side);
    assertPositiveSafeInteger(level, 'invalid_limit_price_level');
    if (!Number.isSafeInteger(lastLevel)) validationError('invalid_last_level');
    if (classifyClick(side, level, lastLevel) === 'market') {
      return submitManualMarket({
        side,
        quantity,
        price: lastPrice,
        time,
        eligibleFromSessionTick,
      });
    }
    return submitManualLimit({ side, level, price, quantity, eligibleFromSessionTick });
  }

  function cancelManualOrders(side) {
    assertSide(side);
    const canceledOrderIds = state.orders
      .filter((order) => order.source === 'manual' && order.side === side)
      .map((order) => order.id);
    if (canceledOrderIds.length) {
      const canceled = new Set(canceledOrderIds);
      state.orders = state.orders.filter((order) => !canceled.has(order.id));
    }
    return { kind: 'manual_canceled', canceledOrderIds };
  }

  function beginStrategyRun(identity) {
    const key = runKey(identity);
    if (state.orders.length > limits.maxLiveOrders) validationError('live_order_limit');
    validateStrategyState();
    if (state.runLedgers.has(key)) return cloneLedger(state.runLedgers.get(key));
    const normalizedIdentity = {
      session: canonicalSession(identity.session),
      runId: identity.runId,
      strategyRevision: identity.strategyRevision,
    };
    const ledger = {
      identity: normalizedIdentity,
      seenIntentIds: new Set(),
      acceptedCommandCount: 0,
      lastAppliedSequence: 0,
      lastAppliedSignature: null,
      fillCursor: 0,
    };
    state.runLedgers = new Map(state.runLedgers).set(key, ledger);
    return cloneLedger(ledger);
  }

  function validateStrategyBatch(batch) {
    assertPlainObject(batch, 'invalid_batch');
    const exactKeys = Object.hasOwn(batch, 'kind') ? [...BATCH_KEYS, 'kind'] : BATCH_KEYS;
    validateExactKeys(batch, exactKeys, 'unknown_batch_field');
    if (Object.hasOwn(batch, 'kind') && batch.kind !== 'commands') {
      validationError('invalid_batch_kind');
    }
    if (batch.protocolVersion !== 1) validationError('unsupported_protocol_version');
    assertPositiveSafeInteger(batch.sequence, 'invalid_sequence');
    if (!Number.isSafeInteger(batch.observedSessionTickIndex) || batch.observedSessionTickIndex < 0) {
      validationError('invalid_observed_tick');
    }
    if (batch.eligibleFromSessionTick !== batch.observedSessionTickIndex + 1) {
      validationError('invalid_eligibility');
    }
    assertStrategyInteger(batch.eligibleFromSessionTick, 'eligibleFromSessionTick', 0);
    if (!Array.isArray(batch.commands)) validationError('invalid_commands');
    if (batch.commands.length > limits.maxCommandsPerEvent) validationError('event_command_limit');
    const key = runKey(batch);
    const ledger = state.runLedgers.get(key);
    if (!ledger) validationError('unknown_strategy_run');
    const normalizedCommands = batch.commands.map((command) => normalizeStrategyCommand(command, limits));
    const ids = normalizedCommands.map((command) => command.client_intent_id);
    if (new Set(ids).size !== ids.length) validationError('duplicate_client_intent_id');
    return { key, ledger, normalizedCommands, signature: commandSignature(batch) };
  }

  function commitStrategyBatch(batch, { sessionTickIndex }) {
    const prepared = validateStrategyBatch(batch);
    const { key, ledger, normalizedCommands, signature } = prepared;
    if (batch.sequence === ledger.lastAppliedSequence) {
      if (signature !== ledger.lastAppliedSignature) validationError('duplicate_sequence_conflict');
      return { kind: 'duplicate', canceledOrderIds: [], submittedOrderIds: [] };
    }
    if (batch.sequence !== ledger.lastAppliedSequence + 1) {
      validationError('out_of_order_sequence');
    }
    if (sessionTickIndex !== batch.eligibleFromSessionTick) {
      validationError('ineligible_batch');
    }
    if (ledger.acceptedCommandCount + normalizedCommands.length > limits.maxCommandsPerRun) {
      validationError('run_command_limit');
    }
    for (const command of normalizedCommands) {
      if (ledger.seenIntentIds.has(command.client_intent_id)) {
        validationError('duplicate_client_intent_id');
      }
    }

    const normalizedSession = canonicalSession(batch.session);
    const cancellationCommands = normalizedCommands.filter(
      (command) => command.type === 'cancel_strategy_orders',
    );
    const submissionCommands = normalizedCommands.filter(
      (command) => command.type !== 'cancel_strategy_orders',
    );
    const canceledOrderIds = [];
    let nextOrders = state.orders.filter((order) => {
      if (order.source !== 'strategy' || order.runKey !== key) return true;
      const canceled = cancellationCommands.some(
        (command) => !command.side || command.side === order.side,
      );
      if (canceled) canceledOrderIds.push(order.id);
      return !canceled;
    });
    if (nextOrders.length + submissionCommands.length > limits.maxLiveOrders) {
      validationError('live_order_limit');
    }

    let nextOrderId = state.nextOrderId;
    let nextSubmissionSequence = state.nextSubmissionSequence;
    const submittedOrderIds = [];
    for (const command of submissionCommands) {
      const order = {
        id: nextOrderId,
        kind: command.type === 'submit_market' ? 'market' : 'limit',
        source: 'strategy',
        submissionSequence: nextSubmissionSequence,
        eligibleFromSessionTick: batch.eligibleFromSessionTick,
        session: normalizedSession,
        runId: batch.runId,
        strategyRevision: batch.strategyRevision,
        runKey: key,
        clientIntentId: command.client_intent_id,
        side: command.side,
        qty: command.quantity,
      };
      if (command.type === 'submit_limit') {
        const level = command.limit_price_level;
        const price = priceForLevel(level);
        if (!Number.isFinite(price) || price <= 0) validationError('invalid_limit_price_level');
        order.level = level;
        order.price = price;
      }
      nextOrders = [...nextOrders, order];
      submittedOrderIds.push(nextOrderId);
      nextOrderId += 1;
      nextSubmissionSequence += 1;
    }
    faultInjector('prepare_orders');

    const nextBrokerEvents = [...state.brokerEvents, {
      kind: 'strategy_batch_committed',
      runId: batch.runId,
      strategyRevision: batch.strategyRevision,
      sequence: batch.sequence,
      eligibleFromSessionTick: batch.eligibleFromSessionTick,
      canceledOrderIds: [...canceledOrderIds],
      submittedOrderIds: [...submittedOrderIds],
    }];
    faultInjector('prepare_broker_events');

    const nextSeenIntentIds = new Set(ledger.seenIntentIds);
    normalizedCommands.forEach((command) => nextSeenIntentIds.add(command.client_intent_id));
    faultInjector('prepare_seen_intents');

    const nextAcceptedCommandCount = ledger.acceptedCommandCount + normalizedCommands.length;
    faultInjector('prepare_counters');

    const nextRunLedgers = cloneRunLedgers(state.runLedgers);
    nextRunLedgers.set(key, {
      ...cloneLedger(ledger),
      seenIntentIds: nextSeenIntentIds,
      acceptedCommandCount: nextAcceptedCommandCount,
      lastAppliedSequence: batch.sequence,
      lastAppliedSignature: signature,
    });
    faultInjector('prepare_ledger');
    validateCandidate({
      ...state, orders: nextOrders, nextOrderId, nextSubmissionSequence, runLedgers: nextRunLedgers,
    }, state.portfolio.avgPrice);
    faultInjector('before_swap');

    state.orders = nextOrders;
    state.brokerEvents = nextBrokerEvents;
    state.nextOrderId = nextOrderId;
    state.nextSubmissionSequence = nextSubmissionSequence;
    state.runLedgers = nextRunLedgers;
    return { kind: 'committed', canceledOrderIds, submittedOrderIds };
  }

  function discardFinalStrategyBatch(batch) {
    const { ledger, normalizedCommands, signature } = validateStrategyBatch(batch);
    if (batch.sequence === ledger.lastAppliedSequence) {
      if (signature !== ledger.lastAppliedSignature) validationError('duplicate_sequence_conflict');
      return {
        kind: 'final_tick_discarded',
        commandCount: 0,
        observedSessionTickIndex: batch.observedSessionTickIndex,
        duplicate: true,
      };
    }
    if (batch.sequence !== ledger.lastAppliedSequence + 1) {
      validationError('out_of_order_sequence');
    }
    if (ledger.acceptedCommandCount + normalizedCommands.length > limits.maxCommandsPerRun) {
      validationError('run_command_limit');
    }
    for (const command of normalizedCommands) {
      if (ledger.seenIntentIds.has(command.client_intent_id)) {
        validationError('duplicate_client_intent_id');
      }
    }
    return {
      kind: 'final_tick_discarded',
      commandCount: batch.commands.length,
      observedSessionTickIndex: batch.observedSessionTickIndex,
    };
  }

  function cancelStrategyRunOrders(identity) {
    const key = runKey(identity);
    const canceledOrderIds = state.orders
      .filter((order) => order.source === 'strategy' && order.runKey === key)
      .map((order) => order.id);
    if (canceledOrderIds.length) {
      const canceled = new Set(canceledOrderIds);
      state.orders = state.orders.filter((order) => !canceled.has(order.id));
    }
    if (state.runLedgers.has(key)) {
      const nextRunLedgers = new Map(state.runLedgers);
      nextRunLedgers.delete(key);
      state.runLedgers = nextRunLedgers;
    }
    return { kind: 'strategy_canceled', canceledOrderIds };
  }

  function matchTick({ sessionTickIndex, tradePrice, time, deliveryRun = null }) {
    if (!Number.isSafeInteger(sessionTickIndex) || sessionTickIndex < 0) {
      validationError('invalid_session_tick');
    }
    if (!Number.isFinite(tradePrice) || tradePrice <= 0) validationError('invalid_trade_price');
    if (!Number.isFinite(time)) validationError('invalid_fill_time');
    let deliveryKey = null;
    let deliveryLedger = null;
    if (deliveryRun) {
      deliveryKey = runKey(deliveryRun);
      deliveryLedger = state.runLedgers.get(deliveryKey);
      if (!deliveryLedger) validationError('unknown_strategy_run');
    }
    const strict = Boolean(deliveryLedger || state.runLedgers.size);
    if (strict) validateCandidate(state, tradePrice);

    const filledOrders = state.orders
      .filter((order) => (
        order.eligibleFromSessionTick <= sessionTickIndex &&
        (order.kind === 'market' || isFilled(order, tradePrice))
      ))
      .sort((left, right) => left.id - right.id);
    if (!filledOrders.length) {
      return {
        kind: 'matched',
        fills: [],
        fillCursor: deliveryLedger?.fillCursor ?? 0,
      };
    }
    const fillIds = new Set(filledOrders.map((order) => order.id));
    const transaction = {
      portfolio: clonePortfolio(state.portfolio),
      orders: state.orders.filter((order) => !fillIds.has(order.id)),
      fills: [...state.fills],
      events: [...state.events],
      nextFillId: state.nextFillId,
      runLedgers: state.runLedgers,
    };
    const capacityViolation = Boolean(
      deliveryLedger && filledOrders.length > limits.maxFillsPerEvent,
    );
    let nextFillCursor = deliveryLedger?.fillCursor ?? 0;
    const committedFills = filledOrders.map((order) => {
      const deliverySequence = capacityViolation || !deliveryLedger ? null : ++nextFillCursor;
      const fillPrice = order.kind === 'market' ? tradePrice : order.price;
      const fill = appendFill(transaction, order, fillPrice, time, deliverySequence);
      if (strict) {
        strategyPortfolioSnapshot(transaction.portfolio, tradePrice, priceLevelForPrice);
        validateNumericRecord(fill);
      }
      return fill;
    });

    if (deliveryLedger && !capacityViolation) {
      transaction.runLedgers = cloneRunLedgers(state.runLedgers);
      transaction.runLedgers.set(deliveryKey, {
        ...cloneLedger(deliveryLedger),
        fillCursor: nextFillCursor,
      });
    }
    if (strict) {
      validateCandidate({ ...state, ...transaction }, tradePrice);
      transaction.events.slice(state.events.length).forEach(validateNumericRecord);
    }
    state.portfolio = transaction.portfolio;
    state.orders = transaction.orders;
    state.fills = transaction.fills;
    state.events = transaction.events;
    state.nextFillId = transaction.nextFillId;
    state.runLedgers = transaction.runLedgers;

    if (capacityViolation) {
      return {
        kind: 'host_capacity_violation',
        fills: [],
        authoritativeFillCount: committedFills.length,
        fillCursor: deliveryLedger.fillCursor,
      };
    }
    return {
      kind: 'matched',
      fills: committedFills,
      fillCursor: nextFillCursor,
    };
  }

  function matchRange({ fromIndex, toIndex, times, prices }) {
    const fills = [];
    for (let index = fromIndex; index < toIndex; index += 1) {
      if (!state.orders.length) break;
      const result = matchTick({
        sessionTickIndex: index,
        tradePrice: prices[index],
        time: times[index],
      });
      fills.push(...result.fills);
    }
    return { kind: 'range_matched', fills };
  }

  /** Prepare a whole tick privately, including the prior event's command batch. */
  function commitStrategyTick({ batch, beforePublish, ...tick }) {
    const candidate = createOrderBroker(options);
    Object.assign(candidate.state, state, { portfolio: clonePortfolio(state.portfolio) });
    if (batch) candidate.commitStrategyBatch(batch, { sessionTickIndex: tick.sessionTickIndex });
    const matched = candidate.matchTick(tick);
    const prepared = beforePublish(candidate, matched);
    Object.assign(state, candidate.state);
    return { matched, prepared };
  }

  function truncateAt(targetTime) {
    const keptFills = state.fills.filter((fill) => fill.time <= targetTime);
    const dropped = keptFills.length !== state.fills.length;
    state.orders = [];
    state.fills = keptFills;
    if (dropped) {
      const portfolio = createPortfolio();
      const events = [];
      keptFills.forEach((fill) => {
        applyFill(portfolio, fill).forEach((event) => events.push({
          ...event,
          fillId: fill.fillId,
          orderId: fill.orderId,
          source: fill.source,
        }));
      });
      state.portfolio = portfolio;
      state.events = events;
    }
    return { kind: 'truncated', dropped };
  }

  function reset() {
    state.portfolio = createPortfolio();
    state.orders = [];
    state.fills = [];
    state.events = [];
    state.brokerEvents = [];
    state.runLedgers = new Map();
  }

  function strategyOpenOrders(maximum = limits.maxVisibleOpenOrders) {
    const sorted = [...state.orders].sort((left, right) => left.id - right.id);
    const openOrders = sorted.slice(0, maximum).map((order) => {
      const visible = {
        order_id: order.id,
        owner: order.source,
        side: order.side,
        quantity_remaining: order.qty,
        limit_price_level: order.level,
        eligible_from_session_tick: order.eligibleFromSessionTick,
      };
      return visible;
    });
    return {
      openOrders,
      openOrdersTotal: sorted.length,
      openOrdersTruncated: sorted.length > openOrders.length,
    };
  }

  function portfolioSnapshot() {
    return clonePortfolio(state.portfolio);
  }

  return Object.freeze({
    state,
    limits: Object.freeze({ ...limits }),
    snapshot,
    exportState,
    submitManualOrder,
    submitManualLimit,
    submitManualMarket,
    cancelManualOrders,
    beginStrategyRun,
    commitStrategyBatch,
    discardFinalStrategyBatch,
    cancelStrategyRunOrders,
    matchTick,
    commitStrategyTick,
    matchRange,
    truncateAt,
    reset,
    strategyOpenOrders,
    portfolioSnapshot,
    validateStrategyState,
  });
}

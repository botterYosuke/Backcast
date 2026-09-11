'use strict';

const DEFAULT_GAP_THRESHOLD = 5;
const MAX_FRAME_ELAPSED_SECONDS = 0.5;

function cloneRunIdentity(identity) {
  return {
    session: { ...identity.session },
    runId: identity.runId,
    strategyRevision: identity.strategyRevision,
  };
}

function defaultFillToStrategy(fill, sessionTickIndex, replayTimeUs, priceLevelForPrice) {
  return {
    fill_delivery_sequence: fill.fillDeliverySequence,
    order_id: fill.orderId,
    side: fill.side,
    quantity: fill.qty,
    price_level: priceLevelForPrice(fill.price),
    session_tick_index: sessionTickIndex,
    replay_time_us: replayTimeUs,
  };
}

export function strategyPortfolioSnapshot(portfolio, lastPrice, priceLevelForPrice = (price) => price) {
  const unrealized = portfolio.qty && Number.isFinite(lastPrice)
    ? (lastPrice - portfolio.avgPrice) * portfolio.qty
    : 0;
  const cash = portfolio.realized - portfolio.qty * portfolio.avgPrice;
  return {
    cash_minor: Math.round(cash * 100),
    position_quantity: portfolio.qty,
    average_price_level: portfolio.qty ? priceLevelForPrice(portfolio.avgPrice) : null,
    realized_pnl_minor: Math.round(portfolio.realized * 100),
    unrealized_pnl_minor: Math.round(unrealized * 100),
    equity_minor: Math.round((portfolio.realized + unrealized) * 100),
  };
}

function defaultOpenOrdersToStrategy(broker) {
  return broker.strategyOpenOrders();
}

/**
 * Advance the strategy-mode scheduler without letting unresolved Worker time
 * become replay time.  `candidateVirtualTime` is private scheduling progress;
 * `committedVirtualTime` is the only value suitable for clock/scrubber UI.
 */
export function advanceStrategyClock(clock, {
  timestamp,
  playing,
  blocked,
  speed,
  nextTickTime,
  skipGaps,
  gapThreshold = DEFAULT_GAP_THRESHOLD,
}) {
  const base = {
    committedVirtualTime: clock.committedVirtualTime,
    candidateVirtualTime: clock.candidateVirtualTime,
    lastFrame: timestamp,
    shouldCommitTick: false,
  };
  if (!playing || blocked || !Number.isFinite(nextTickTime)) {
    return { ...base, candidateVirtualTime: clock.committedVirtualTime };
  }

  const elapsed = clock.lastFrame
    ? Math.min(Math.max((timestamp - clock.lastFrame) / 1000, 0), MAX_FRAME_ELAPSED_SECONDS)
    : 0;
  let candidateVirtualTime = clock.candidateVirtualTime + elapsed * speed;
  if (skipGaps && nextTickTime > candidateVirtualTime + gapThreshold) {
    candidateVirtualTime = nextTickTime;
  }
  if (nextTickTime > candidateVirtualTime) {
    return { ...base, candidateVirtualTime };
  }
  return {
    ...base,
    committedVirtualTime: nextTickTime,
    candidateVirtualTime: nextTickTime,
    shouldCommitTick: true,
  };
}

/**
 * Own the deterministic strategy tick barrier.  UI painting and replay-array
 * mutation remain injected ports so this module is DOM-free and unit-testable.
 */
export function createReplayCommitter({
  broker,
  strategyPort,
  priceLevelForPrice = (price) => price,
  fillToStrategy = defaultFillToStrategy,
  portfolioToStrategy = strategyPortfolioSnapshot,
  openOrdersToStrategy = defaultOpenOrdersToStrategy,
} = {}) {
  if (!broker || !strategyPort) throw new TypeError('broker_and_strategy_port_required');
  let runIdentity = null;
  let stagedBatch = null;
  let inFlight = null;
  let paused = true;
  let generation = 0;

  function beginRun(identity) {
    generation += 1;
    runIdentity = cloneRunIdentity(identity);
    stagedBatch = null;
    inFlight = null;
    paused = true;
    broker.beginStrategyRun(runIdentity);
    return status();
  }

  function pause() {
    paused = true;
    strategyPort.pause();
    return status();
  }

  function resume() {
    if (!runIdentity) throw new Error('strategy_run_not_started');
    paused = false;
    strategyPort.resume();
    return status();
  }

  function invalidate(cause) {
    generation += 1;
    stagedBatch = null;
    runIdentity = null;
    paused = true;
    return strategyPort.invalidate(cause);
  }

  function complete() {
    generation += 1;
    stagedBatch = null;
    runIdentity = null;
    paused = true;
    return status();
  }

  function status() {
    return Object.freeze({
      active: Boolean(runIdentity),
      paused,
      inFlight: Boolean(inFlight),
      hasStagedBatch: Boolean(stagedBatch),
      generation,
    });
  }

  async function commitTick({
    sessionTickIndex,
    tick,
    tradePrice = tick.price ?? tick.price_level,
    time = tick.replay_time_us / 1_000_000,
    applyTick,
    afterTick = () => {},
    isFinalTick = false,
  }) {
    if (!runIdentity) throw new Error('strategy_run_not_started');
    if (paused) throw new Error('strategy_replay_paused');
    if (inFlight) throw new Error('event_in_flight');
    const admittedGeneration = generation;

    const work = (async () => {
      if (stagedBatch) {
        broker.commitStrategyBatch(stagedBatch, { sessionTickIndex });
        stagedBatch = null;
      }
      applyTick(sessionTickIndex, tick);
      const matched = broker.matchTick({
        sessionTickIndex,
        tradePrice,
        time,
        deliveryRun: runIdentity,
      });
      await afterTick({ matched, sessionTickIndex, tick });
      if (generation !== admittedGeneration || !runIdentity) {
        return { kind: 'stale_result', fills: matched.fills, fillCursor: matched.fillCursor };
      }
      if (matched.kind === 'host_capacity_violation') {
        void strategyPort.invalidate('worker_or_protocol_error');
        generation += 1;
        stagedBatch = null;
        runIdentity = null;
        paused = true;
        return { ...matched, strategyResult: { kind: 'host_capacity_violation' } };
      }

      const replayTimeUs = tick.replay_time_us;
      const fills = matched.fills.map((fill) => fillToStrategy(
        fill,
        sessionTickIndex,
        replayTimeUs,
        priceLevelForPrice,
      ));
      const openOrderSnapshot = openOrdersToStrategy(broker, priceLevelForPrice);
      const event = {
        tick: { ...tick },
        fills,
        fillCursor: matched.fillCursor,
        portfolio: portfolioToStrategy(
          broker.portfolioSnapshot(),
          tradePrice,
          priceLevelForPrice,
        ),
        ...openOrderSnapshot,
      };
      const result = await strategyPort.dispatchOneEvent(event);
      if (generation !== admittedGeneration || !runIdentity) {
        return { kind: 'stale_result', fills: matched.fills, fillCursor: matched.fillCursor };
      }
      if (isFinalTick) {
        const discarded = broker.discardFinalStrategyBatch(result);
        return {
          kind: 'tick_committed',
          fills: matched.fills,
          fillCursor: matched.fillCursor,
          strategyResult: discarded,
        };
      }
      stagedBatch = result;
      return {
        kind: 'tick_committed',
        fills: matched.fills,
        fillCursor: matched.fillCursor,
        strategyResult: { kind: 'staged', eligibleFromSessionTick: result.eligibleFromSessionTick },
      };
    })();
    inFlight = work;
    try {
      return await work;
    } finally {
      if (inFlight === work) inFlight = null;
    }
  }

  return Object.freeze({
    beginRun,
    pause,
    resume,
    invalidate,
    complete,
    commitTick,
    status,
  });
}

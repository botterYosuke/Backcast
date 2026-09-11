import assert from 'node:assert/strict';
import test from 'node:test';

import {
  applyFill,
  classifyClick,
  createPortfolio,
  isFilled,
} from './paper-trading.mjs';

/**
 * This deliberately mirrors the pre-broker app.js manual-order path.  Keep it
 * independent from the extracted broker so the frozen outputs remain a useful
 * compatibility oracle after app.js starts routing through that broker.
 */
function createLegacyManualHarness({ times, prices, cursor = 0, virtualTime = 0 }) {
  const state = {
    times,
    prices,
    cursor,
    virtualTime,
    last: cursor > 0 ? prices[cursor - 1] : null,
  };
  const trading = {
    portfolio: createPortfolio(),
    orders: [],
    fills: [],
    events: [],
    nextOrderId: 1,
  };

  function currentTradeTime() {
    return state.cursor > 0 ? state.times[state.cursor - 1] : state.virtualTime;
  }

  function executeFill(side, quantity, price, time) {
    const fill = { side, qty: quantity, price, time };
    trading.fills.push(fill);
    applyFill(trading.portfolio, fill).forEach((event) => trading.events.push(event));
  }

  function placeDesktopOrder(side, level, quantity) {
    if (state.last === null) return;
    if (classifyClick(side, level, state.last) === 'market') {
      executeFill(side, quantity, state.last, currentTradeTime());
      return;
    }
    const existing = trading.orders.find(
      (order) => order.side === side && order.level === level,
    );
    if (existing) existing.qty += quantity;
    else trading.orders.push({
      id: trading.nextOrderId++,
      side,
      level,
      price: level,
      qty: quantity,
    });
  }

  function placeMobileMarketOrder(side, quantity) {
    if (state.last === null) return;
    executeFill(side, quantity, state.last, currentTradeTime());
  }

  function cancelOrders(side) {
    trading.orders = trading.orders.filter((order) => order.side !== side);
  }

  function matchOrders(fromIndex, toIndex) {
    for (let index = fromIndex; index < toIndex && trading.orders.length; index += 1) {
      const tradePrice = state.prices[index];
      const filled = trading.orders.filter((order) => isFilled(order, tradePrice));
      if (!filled.length) continue;
      trading.orders = trading.orders.filter((order) => !isFilled(order, tradePrice));
      filled.forEach((order) => {
        executeFill(order.side, order.qty, order.price, state.times[index]);
      });
    }
  }

  return {
    state,
    trading,
    currentTradeTime,
    placeDesktopOrder,
    placeMobileMarketOrder,
    cancelOrders,
    matchOrders,
  };
}

test('legacy manual desktop/mobile path retains its canonical golden trace', () => {
  const legacy = createLegacyManualHarness({
    times: [100, 101, 102, 103],
    prices: [100, 103, 99, 104],
  });

  legacy.state.cursor = 1;
  legacy.state.last = 100;
  legacy.placeDesktopOrder('sell', 103, 2);
  legacy.placeDesktopOrder('sell', 103, 3);
  legacy.placeDesktopOrder('buy', 99, 4);

  legacy.state.cursor = 4;
  legacy.state.last = 104;
  legacy.matchOrders(0, 4);
  legacy.placeDesktopOrder('buy', 104, 2);
  legacy.placeMobileMarketOrder('sell', 1);
  legacy.placeDesktopOrder('sell', 105, 7);
  legacy.placeDesktopOrder('buy', 98, 8);
  legacy.cancelOrders('sell');

  assert.deepEqual(legacy.trading, {
    portfolio: {
      qty: 0,
      avgPrice: 0,
      realized: 15,
      entryCount: 2,
      exitCount: 3,
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

test('legacy manual market time uses virtual time before the first tick', () => {
  const legacy = createLegacyManualHarness({
    times: [20],
    prices: [500],
    virtualTime: 19.999,
  });
  legacy.state.last = 500;

  legacy.placeDesktopOrder('buy', 500, 100);
  legacy.placeMobileMarketOrder('sell', 100);

  assert.deepEqual(legacy.trading.fills.map((fill) => fill.time), [19.999, 19.999]);
});

test('legacy frame scan fills at the first crossing even when price returns or crosses again', () => {
  const crossingAndReturn = createLegacyManualHarness({
    times: [10, 11, 12],
    prices: [100, 105, 100],
    cursor: 1,
  });
  crossingAndReturn.placeDesktopOrder('sell', 104, 10);
  crossingAndReturn.matchOrders(0, 3);
  assert.deepEqual(crossingAndReturn.trading.fills, [
    { side: 'sell', qty: 10, price: 104, time: 11 },
  ]);

  const repeatedCrossing = createLegacyManualHarness({
    times: [30, 31, 32, 33],
    prices: [100, 105, 103, 106],
    cursor: 1,
  });
  repeatedCrossing.placeDesktopOrder('sell', 104, 10);
  repeatedCrossing.matchOrders(0, 4);
  assert.deepEqual(repeatedCrossing.trading.fills, [
    { side: 'sell', qty: 10, price: 104, time: 31 },
  ]);
});

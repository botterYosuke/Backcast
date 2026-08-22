import assert from 'node:assert/strict';
import test from 'node:test';

import {
  applyFill,
  classifyClick,
  createPortfolio,
  isFilled,
  positionSide,
  totalPnl,
  unrealizedPnl,
} from './paper-trading.mjs';

function fill(side, qty, price, time = 0) {
  return { side, qty, price, time };
}

test('新規買いは建玉と平均単価を作る', () => {
  const portfolio = createPortfolio();
  const events = applyFill(portfolio, fill('buy', 100, 1000));

  assert.equal(portfolio.qty, 100);
  assert.equal(portfolio.avgPrice, 1000);
  assert.equal(portfolio.realized, 0);
  assert.equal(positionSide(portfolio), 'buy');
  assert.deepEqual(events.map((event) => event.kind), ['entry']);
});

test('同方向の買い増しは数量加重の平均単価になる', () => {
  const portfolio = createPortfolio();
  applyFill(portfolio, fill('buy', 100, 1000));
  applyFill(portfolio, fill('buy', 300, 1100));

  assert.equal(portfolio.qty, 400);
  assert.equal(portfolio.avgPrice, (1000 * 100 + 1100 * 300) / 400);
  assert.equal(portfolio.entryCount, 2);
});

test('買い建玉に対する売り約定は返済になり実現損益が乗る', () => {
  const portfolio = createPortfolio();
  applyFill(portfolio, fill('buy', 200, 1000));
  const events = applyFill(portfolio, fill('sell', 200, 1015));

  assert.equal(portfolio.qty, 0);
  assert.equal(portfolio.avgPrice, 0);
  assert.equal(portfolio.realized, 3000);
  assert.equal(positionSide(portfolio), null);
  assert.deepEqual(events.map((event) => event.kind), ['exit']);
  assert.equal(events[0].pnl, 3000);
});

test('売り建玉は値下がりで利益になる', () => {
  const portfolio = createPortfolio();
  applyFill(portfolio, fill('sell', 100, 1000));

  assert.equal(portfolio.qty, -100);
  assert.equal(unrealizedPnl(portfolio, 990), 1000);

  applyFill(portfolio, fill('buy', 100, 990));
  assert.equal(portfolio.realized, 1000);
  assert.equal(portfolio.qty, 0);
});

test('一部返済は建玉を減らすが平均単価は据え置く', () => {
  const portfolio = createPortfolio();
  applyFill(portfolio, fill('buy', 300, 1000));
  applyFill(portfolio, fill('sell', 100, 1020));

  assert.equal(portfolio.qty, 200);
  assert.equal(portfolio.avgPrice, 1000);
  assert.equal(portfolio.realized, 2000);
  assert.equal(portfolio.exitCount, 1);
});

test('返済数量を超える反対売買は両建てにならずドテンする', () => {
  const portfolio = createPortfolio();
  applyFill(portfolio, fill('buy', 100, 1000));
  const events = applyFill(portfolio, fill('sell', 300, 1010));

  assert.equal(portfolio.qty, -200, '両建てではなくネットの売建玉になる');
  assert.equal(portfolio.avgPrice, 1010);
  assert.equal(portfolio.realized, 1000);
  assert.deepEqual(events.map((event) => event.kind), ['exit', 'entry']);
  assert.equal(events[0].qty, 100);
  assert.equal(events[1].qty, 200);
});

test('返済後は何回でも新規建玉を建て直せる（一日信用）', () => {
  const portfolio = createPortfolio();
  for (let round = 0; round < 5; round += 1) {
    applyFill(portfolio, fill('buy', 100, 1000));
    applyFill(portfolio, fill('sell', 100, 1001));
  }
  assert.equal(portfolio.qty, 0);
  assert.equal(portfolio.entryCount, 5);
  assert.equal(portfolio.exitCount, 5);
  assert.equal(portfolio.realized, 500);
});

test('合計損益は実現損益と評価損益の和', () => {
  const portfolio = createPortfolio();
  applyFill(portfolio, fill('buy', 100, 1000));
  applyFill(portfolio, fill('sell', 100, 1010)); // 実現 +1000
  applyFill(portfolio, fill('buy', 100, 1005));  // 建玉を持ったまま

  assert.equal(portfolio.realized, 1000);
  assert.equal(unrealizedPnl(portfolio, 1002), -300);
  assert.equal(totalPnl(portfolio, 1002), 700);
});

test('現在値が未確定なら評価損益は 0 として扱う', () => {
  const portfolio = createPortfolio();
  applyFill(portfolio, fill('buy', 100, 1000));
  assert.equal(unrealizedPnl(portfolio, NaN), 0);
  assert.equal(totalPnl(portfolio, NaN), 0);
});

test('板のクリックは現在値との位置関係で成行 / 指値に分かれる', () => {
  const last = 100;
  assert.equal(classifyClick('sell', last + 1, last), 'limit');
  assert.equal(classifyClick('sell', last, last), 'market');
  assert.equal(classifyClick('sell', last - 1, last), 'market');
  assert.equal(classifyClick('buy', last - 1, last), 'limit');
  assert.equal(classifyClick('buy', last, last), 'market');
  assert.equal(classifyClick('buy', last + 1, last), 'market');
});

test('指値は逆行では約定せず、指値に届いた約定で成立する', () => {
  const sell = { side: 'sell', price: 1010 };
  assert.equal(isFilled(sell, 1009), false);
  assert.equal(isFilled(sell, 1010), true);
  assert.equal(isFilled(sell, 1012), true);

  const buy = { side: 'buy', price: 990 };
  assert.equal(isFilled(buy, 991), false);
  assert.equal(isFilled(buy, 990), true);
  assert.equal(isFilled(buy, 988), true);
});

test('約定列の前半だけを再生し直すと、その時点の建玉が再現される', () => {
  // シークで「そこから先の取引履歴」を捨てたあと、残った約定を頭から
  // 流し直して建玉を作り直す（app.js の rebuildPortfolio と同じ手順）。
  const history = [
    fill('buy', 100, 1000, 10),
    fill('sell', 100, 1020, 20),
    fill('sell', 200, 1015, 30),
    fill('buy', 200, 1005, 40),
  ];

  const full = createPortfolio();
  history.forEach((one) => applyFill(full, one));
  assert.equal(full.realized, 2000 + 2000);

  const rewound = createPortfolio();
  const kept = history.filter((one) => one.time <= 25);
  kept.forEach((one) => applyFill(rewound, one));

  assert.equal(kept.length, 2);
  assert.equal(rewound.qty, 0);
  assert.equal(rewound.realized, 2000, 'シーク先より後の実現損益は消える');
  assert.equal(rewound.entryCount, 1);
  assert.equal(rewound.exitCount, 1);
});

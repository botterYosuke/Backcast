import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import * as minuteHistoryModule from './minute-history.mjs';

import {
  MINUTE_HISTORY_DEFAULTS,
  MinuteHistorySession,
  cutoffFromEpochSeconds,
  cutoffKey,
  mergeOlderBars,
  shiftLogicalRange,
} from './minute-history.mjs';

const APP_SOURCE = readFileSync(new URL('./app.js', import.meta.url), 'utf8');

/** request-coordinator.test.mjs と同じ手口で app.js から関数本体を切り出す。 */
function extractFunction(source, name) {
  const start = source.search(new RegExp(`(async )?function ${name}\\(`));
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

/**
 * コメント（本文にも "await" と書いてある）を空白で潰す。文字位置を変えないので
 * 「A は B より前に書いてある」という順序の検証にそのまま使える。
 */
function withoutComments(source) {
  const NEWLINE = '\n';
  const BACKSLASH = '\\';
  let out = '';
  let mode = 'code';
  let quote = '';
  for (let index = 0; index < source.length; index += 1) {
    const char = source[index];
    const next = source[index + 1];
    if (mode === 'code') {
      if (char === '/' && next === '/') { mode = 'line'; out += '  '; index += 1; continue; }
      if (char === '/' && next === '*') { mode = 'block'; out += '  '; index += 1; continue; }
      if (char === "'" || char === '"' || char === '`') { mode = 'string'; quote = char; }
      out += char;
      continue;
    }
    if (mode === 'string') {
      out += char;
      if (char === BACKSLASH) { out += source[index + 1] ?? ''; index += 1; continue; }
      if (char === quote) mode = 'code';
      continue;
    }
    if (mode === 'line') {
      if (char === NEWLINE) { mode = 'code'; out += char; continue; }
      out += ' ';
      continue;
    }
    if (char === '*' && next === '/') { mode = 'code'; out += '  '; index += 1; continue; }
    out += char === NEWLINE ? char : ' ';
  }
  return out;
}

function bar(time, close = 100) {
  return { time, open: close, high: close, low: close, close, volume: 1 };
}

function minuteApplyHarness(failAt = null) {
  const contextBars = [bar(300), bar(360)];
  const state = {
    meta: { stem: '7203', code: '7203', date: '2026-08-20', count: 4 },
    t: new Float64Array([420, 480]),
    price: new Float64Array([101, 102]),
    qty: new Float64Array([100, 200]),
    type: ['1', '2'],
    cursor: 2,
    vt: 480,
    playing: true,
    speed: 500,
    contextBars,
    bars: contextBars.concat([bar(420, 101), bar(480, 102)]),
    tickPoints: [{ time: 420, value: 101 }],
    last: 102,
    lastFrame: 1234,
    scrubbing: false,
  };
  const trading = {
    portfolio: { qty: 100, avgPrice: 101, realized: 20, entryCount: 1, exitCount: 0 },
    orders: [{ id: 3, side: 'sell', level: 103, price: 103, qty: 100 }],
    fills: [{ side: 'buy', qty: 100, price: 101, time: 420 }],
    events: [{ kind: 'entry', side: 'buy', qty: 100, price: 101, time: 420 }],
    nextOrderId: 4,
  };
  const renderCandle = (item) => ({ time: item.time, value: item.close });
  const renderVolume = (item) => ({ time: item.time, value: item.volume });
  const failures = new Set();
  const failOnce = (stage) => {
    if (failAt !== stage || failures.has(stage)) return;
    failures.add(stage);
    throw new Error(`${stage} failed`);
  };
  const candleSeries = {
    data: state.bars.map(renderCandle),
    setData(data) {
      this.data = structuredClone(data);
      failOnce('candle');
    },
  };
  const volumeSeries = {
    data: state.bars.map(renderVolume),
    setData(data) {
      this.data = structuredClone(data);
      failOnce('volume');
    },
  };
  const timeScale = {
    range: { from: 0, to: 4 },
    getVisibleLogicalRange() { return { ...this.range }; },
    setVisibleLogicalRange(range) {
      this.range = { ...range };
      failOnce('range');
    },
  };
  let markerCalls = 0;
  const dependencies = {
    state,
    candleSeries,
    volumeSeries,
    timeScale,
    paintCandle(item) {
      if (item.time === 180) failOnce('paint');
      return renderCandle(item);
    },
    paintVolume: renderVolume,
    runProgrammatic: (apply) => apply(),
    refreshMarkers() {
      markerCalls += 1;
      assert.ok(state.bars[0].time < 300, 'markers run only after the page commits');
      failOnce('marker');
    },
  };
  const replaySnapshot = structuredClone({
    meta: state.meta,
    t: state.t,
    price: state.price,
    qty: state.qty,
    type: state.type,
    cursor: state.cursor,
    vt: state.vt,
    playing: state.playing,
    speed: state.speed,
    tickPoints: state.tickPoints,
    last: state.last,
    lastFrame: state.lastFrame,
    scrubbing: state.scrubbing,
  });
  return {
    dependencies,
    state,
    trading,
    candleSeries,
    volumeSeries,
    timeScale,
    replaySnapshot,
    tradingSnapshot: structuredClone(trading),
    markerCalls: () => markerCalls,
  };
}

function session(overrides = {}) {
  let clock = 0;
  const controller = new MinuteHistorySession({
    now: () => clock,
    ...overrides,
  });
  const generation = controller.resetSession('test-session');
  assert.equal(controller.markSessionReady(generation, 'test-session'), true);
  controller.advanceClock = (milliseconds) => { clock += milliseconds; };
  return controller;
}

function resetReady(controller, identity) {
  const generation = controller.resetSession(identity);
  assert.equal(controller.markSessionReady(generation, identity), true);
  return generation;
}

// ------------------------------------------------------------ mergeOlderBars

test('mergeOlderBars prepends older bars in chronological order', () => {
  const existing = [bar(300), bar(360)];
  const result = mergeOlderBars(existing, [bar(240), bar(120), bar(180)]);

  assert.equal(result.added, 3);
  assert.deepEqual(result.bars.map((item) => item.time), [120, 180, 240, 300, 360]);
  // 既存部分は 1 バイトも変わらない（追加ぶんだけ後ろへずれる）。
  assert.deepEqual(result.bars.slice(result.added), existing);
});

test('mergeOlderBars keeps existing bars when a timestamp collides', () => {
  const current = bar(300, 999);
  const result = mergeOlderBars([current], [bar(300, 111), bar(240, 222)]);

  assert.equal(result.added, 1);
  assert.deepEqual(result.bars.map((item) => item.time), [240, 300]);
  assert.equal(result.bars[1], current, '形成中の足を古いスナップショットで潰さない');
});

test('mergeOlderBars removes duplicates inside the incoming page', () => {
  const result = mergeOlderBars([bar(300)], [bar(180), bar(180), bar(120)]);
  assert.equal(result.added, 2);
  assert.deepEqual(result.bars.map((item) => item.time), [120, 180, 300]);
});

test('mergeOlderBars drops bars at or after the boundary', () => {
  const existing = [bar(300), bar(360)];
  // boundary は contextBars と bars の両方へ同じ集合を足すために外から渡す。
  const result = mergeOlderBars(existing, [bar(240), bar(300), bar(420)], { boundary: 300 });
  assert.equal(result.added, 1);
  assert.deepEqual(result.bars.map((item) => item.time), [240, 300, 360]);
});

test('mergeOlderBars returns the same array reference when nothing is added', () => {
  const existing = [bar(300)];
  for (const older of [[], [bar(300)], [bar(360)], null, undefined]) {
    const result = mergeOlderBars(existing, older);
    assert.equal(result.added, 0);
    assert.equal(result.bars, existing);
    assert.deepEqual(result.addedBars, []);
  }
});

test('mergeOlderBars ignores malformed bars', () => {
  const result = mergeOlderBars([bar(300)], [null, { time: 'x' }, bar(120)]);
  assert.equal(result.added, 1);
  assert.deepEqual(result.bars.map((item) => item.time), [120, 300]);
});

test('two consecutive pages stay ordered and unique in both canonical arrays', () => {
  // app.js の prependMinuteHistory と同じ手順を再現する。
  let contextBars = [bar(600), bar(660)];
  let bars = contextBars.concat([bar(720), bar(780)]);

  const apply = (page) => {
    const boundary = contextBars.length ? contextBars[0].time : bars[0].time;
    const context = mergeOlderBars(contextBars, page, { boundary });
    const full = mergeOlderBars(bars, context.addedBars, { boundary });
    assert.equal(full.added, context.added, '両方の配列へ同じ本数だけ足す');
    contextBars = context.bars;
    bars = full.bars;
    return full.added;
  };

  assert.equal(apply([bar(480), bar(540)]), 2);
  assert.equal(apply([bar(360), bar(420), bar(480)]), 2); // 480 は重複
  assert.deepEqual(contextBars.map((item) => item.time), [360, 420, 480, 540, 600, 660]);
  assert.deepEqual(bars.map((item) => item.time), [360, 420, 480, 540, 600, 660, 720, 780]);
  assert.deepEqual(bars.slice(0, contextBars.length), contextBars);
});

// -------------------------------------------------------------- cutoff/range

test('cutoffFromEpochSeconds formats the cutoff in UTC and truncates seconds', () => {
  // 2026-08-19T09:00:37Z — 秒は落として HH:MM にする（API は strict-before）。
  assert.deepEqual(cutoffFromEpochSeconds(1787130037), { date: '2026-08-19', time: '09:00' });
  assert.deepEqual(cutoffFromEpochSeconds(0), { date: '1970-01-01', time: '00:00' });
  assert.equal(cutoffFromEpochSeconds(Number.NaN), null);
  assert.equal(cutoffFromEpochSeconds(undefined), null);
});

test('cutoffKey is stable and distinguishes cutoffs', () => {
  assert.equal(cutoffKey({ date: '2026-08-19', time: '09:00' }), '2026-08-19T09:00');
  assert.notEqual(
    cutoffKey({ date: '2026-08-19', time: '09:00' }),
    cutoffKey({ date: '2026-08-19', time: '08:00' }),
  );
  assert.equal(cutoffKey(null), '');
});

test('shiftLogicalRange offsets both edges by the unique prepend count', () => {
  assert.deepEqual(shiftLogicalRange({ from: -3.5, to: 86.5 }, 200), { from: 196.5, to: 286.5 });
  assert.equal(shiftLogicalRange({ from: 0, to: 10 }, 0), null);
  assert.equal(shiftLogicalRange(null, 5), null);
  assert.equal(shiftLogicalRange({ from: 0 }, 5), null);
});

test('normalizeMinuteBars keeps only valid plain OHLCV bars from a mixed page', () => {
  assert.equal(typeof minuteHistoryModule.normalizeMinuteBars, 'function');
  const valid = bar(60, 101);
  const inherited = Object.assign(Object.create({ inherited: true }), bar(60));
  const result = minuteHistoryModule.normalizeMinuteBars([
    valid,
    { ...bar(120), volume: -1 },
    { ...bar(180), high: 99, close: 100 },
    { ...bar(240), open: Number.NaN },
    inherited,
  ], 10);

  assert.equal(result.sourceIsArray, true);
  assert.equal(result.receivedCount, 5);
  assert.equal(result.invalidCount, 4);
  assert.deepEqual(result.bars, [bar(60, 101)]);
  assert.notEqual(result.bars[0], valid, 'normalization returns a fresh plain object');
});

test('normalizeMinuteBars rejects invalid dates, fields, and OHLC relationships', () => {
  assert.equal(typeof minuteHistoryModule.normalizeMinuteBars, 'function');
  const invalid = [
    bar(-60),
    bar(1.5),
    bar(8_640_000_000_001),
    { ...bar(60), low: Number.NEGATIVE_INFINITY },
    { ...bar(120), low: 101, open: 100 },
    { ...bar(180), high: 99, open: 100 },
    null,
  ];
  const result = minuteHistoryModule.normalizeMinuteBars(invalid, 20);

  assert.equal(result.receivedCount, invalid.length);
  assert.equal(result.invalidCount, invalid.length);
  assert.deepEqual(result.bars, []);
  assert.equal(minuteHistoryModule.normalizeMinuteBars({ bars: [] }, 20).sourceIsArray, false);
});

test('normalizeMinuteBars bounds oversized responses to the controller page size', () => {
  assert.equal(typeof minuteHistoryModule.normalizeMinuteBars, 'function');
  const oversized = Array.from({ length: 250 }, (_, index) => bar(index * 60));
  const result = minuteHistoryModule.normalizeMinuteBars(
    oversized,
    MINUTE_HISTORY_DEFAULTS.pageSize,
  );

  assert.equal(result.receivedCount, 250);
  assert.equal(result.consideredCount, MINUTE_HISTORY_DEFAULTS.pageSize);
  assert.equal(result.bars.length, MINUTE_HISTORY_DEFAULTS.pageSize);
  assert.equal(result.bars[0].time, 50 * 60, 'the closest bounded suffix is retained');
  assert.equal(result.bars.at(-1).time, 249 * 60);
});

for (const stage of ['paint', 'candle', 'volume', 'range']) {
  test(`applyMinuteHistoryPage rolls back every invariant when ${stage} fails`, () => {
    assert.equal(typeof minuteHistoryModule.applyMinuteHistoryPage, 'function');
    const harness = minuteApplyHarness(stage);
    const stateBefore = structuredClone(harness.state);
    const candleBefore = structuredClone(harness.candleSeries.data);
    const volumeBefore = structuredClone(harness.volumeSeries.data);
    const rangeBefore = { ...harness.timeScale.range };

    assert.throws(
      () => minuteHistoryModule.applyMinuteHistoryPage({
        ...harness.dependencies,
        olderBars: [bar(180), bar(240)],
      }),
      new RegExp(`${stage} failed`),
    );

    assert.deepEqual(harness.state, stateBefore);
    assert.equal(harness.state.contextBars[0].time, 300, 'the retry cutoff stays unchanged');
    assert.deepEqual(harness.candleSeries.data, candleBefore);
    assert.deepEqual(harness.volumeSeries.data, volumeBefore);
    assert.deepEqual(harness.timeScale.range, rangeBefore);
    assert.deepEqual(harness.trading, harness.tradingSnapshot);
    assert.equal(harness.markerCalls(), 0);
  });
}

test('applyMinuteHistoryPage commits two pages and contains marker-only failure', () => {
  assert.equal(typeof minuteHistoryModule.applyMinuteHistoryPage, 'function');
  const harness = minuteApplyHarness('marker');
  const first = minuteHistoryModule.applyMinuteHistoryPage({
    ...harness.dependencies,
    olderBars: [bar(180), bar(240)],
  });

  assert.equal(first.added, 2);
  assert.equal(first.committed, true);
  assert.match(first.markerError?.message || '', /marker failed/);
  assert.deepEqual(harness.state.contextBars.map((item) => item.time), [180, 240, 300, 360]);
  assert.deepEqual(harness.state.bars.map((item) => item.time), [180, 240, 300, 360, 420, 480]);
  assert.deepEqual(harness.timeScale.range, { from: 2, to: 6 });

  const second = minuteHistoryModule.applyMinuteHistoryPage({
    ...harness.dependencies,
    olderBars: [bar(60), bar(120), bar(180)],
  });
  assert.equal(second.added, 2);
  assert.equal(second.markerError, null);
  assert.deepEqual(harness.state.contextBars.map((item) => item.time), [60, 120, 180, 240, 300, 360]);
  assert.deepEqual(harness.state.bars.map((item) => item.time), [60, 120, 180, 240, 300, 360, 420, 480]);
  assert.deepEqual(harness.timeScale.range, { from: 4, to: 8 });
  assert.deepEqual({
    meta: harness.state.meta,
    t: harness.state.t,
    price: harness.state.price,
    qty: harness.state.qty,
    type: harness.state.type,
    cursor: harness.state.cursor,
    vt: harness.state.vt,
    playing: harness.state.playing,
    speed: harness.state.speed,
    tickPoints: harness.state.tickPoints,
    last: harness.state.last,
    lastFrame: harness.state.lastFrame,
    scrubbing: harness.state.scrubbing,
  }, harness.replaySnapshot);
  assert.deepEqual(harness.trading, harness.tradingSnapshot);
  assert.equal(harness.markerCalls(), 2);
});

// --------------------------------------------------------- session controller

test('paging stays disarmed until the user touches the chart', () => {
  const controller = session();
  assert.equal(controller.isNearLeftEdge(0), false);
  assert.equal(controller.admit('k'), null);

  controller.arm();
  assert.equal(controller.isNearLeftEdge(0), true);
  assert.ok(controller.admit('k'));
});

test('the left-edge threshold is the fixed policy constant', () => {
  const controller = session();
  controller.arm();
  assert.equal(controller.edgeThresholdBars, MINUTE_HISTORY_DEFAULTS.edgeThresholdBars);
  assert.equal(controller.isNearLeftEdge(10), true);
  assert.equal(controller.isNearLeftEdge(-40), true, '左に空白があるときも左端扱い');
  assert.equal(controller.isNearLeftEdge(11), false);
  assert.equal(controller.isNearLeftEdge(Number.NaN), false);
});

test('only one history request is admitted at a time', () => {
  const controller = session();
  controller.arm();
  const first = controller.admit('a');
  assert.ok(first);
  assert.equal(controller.admit('b'), null, '飛んでいる間は次を通さない');
  assert.equal(controller.isNearLeftEdge(0), false);

  controller.completeSuccess(first, 200);
  assert.ok(controller.admit('b'));
});

test('programmatic range updates cannot trigger a request', () => {
  const controller = session();
  controller.arm();
  controller.beginProgrammatic();
  controller.beginProgrammatic(); // 入れ子
  assert.equal(controller.isProgrammatic(), true);
  assert.equal(controller.isNearLeftEdge(0), false);
  assert.equal(controller.admit('a'), null);

  controller.endProgrammatic();
  assert.equal(controller.isProgrammatic(), true, '入れ子ぶん残る');
  controller.endProgrammatic();
  controller.endProgrammatic(); // 余分な解除で負にならない
  assert.equal(controller.isProgrammatic(), false);
  assert.ok(controller.admit('a'));
});

test('a stale programmatic completion cannot release the new session guard', () => {
  const controller = session();
  controller.beginProgrammatic(); // 旧セッションで予約された遅延解除

  const generation = controller.resetSession('next-session');
  controller.beginProgrammatic(); // 新セッション側の setData / range 更新
  controller.endProgrammatic(); // 旧セッションの遅延解除が先に到着

  assert.equal(controller.isProgrammatic(), true, '新セッション側のガードは残る');
  controller.endProgrammatic();
  assert.equal(controller.isProgrammatic(), false);
  assert.equal(controller.markSessionReady(generation, 'next-session'), true);
});

test('an empty page marks the session exhausted instead of looping', () => {
  const controller = session();
  controller.arm();
  const token = controller.admit('a');
  controller.completeSuccess(token, 0);

  assert.equal(controller.exhausted, true);
  assert.equal(controller.isNearLeftEdge(0), false);
  assert.equal(controller.admit('b'), null);

  resetReady(controller, 'next-session');
  controller.arm();
  assert.ok(controller.admit('b'), 'セッションが変われば打ち切りは解除される');
});

test('a failed cutoff waits out the cooldown, then retries, then gives up', () => {
  const controller = session();
  controller.arm();

  for (let attempt = 1; attempt <= MINUTE_HISTORY_DEFAULTS.maxFailuresPerCutoff; attempt += 1) {
    const token = controller.admit('a');
    assert.ok(token, `attempt ${attempt} should be admitted`);
    controller.completeFailure(token);
    assert.equal(controller.admit('a'), null, 'クールダウン中は同じ cutoff を叩き直さない');
    // 別の cutoff は巻き添えにしない。
    const other = controller.admit('b');
    assert.ok(other);
    controller.completeAbort(other);
    controller.advanceClock(MINUTE_HISTORY_DEFAULTS.failureCooldownMs);
  }

  assert.equal(controller.admit('a'), null, '規定回数を超えたら諦める');
  controller.advanceClock(60_000);
  assert.equal(controller.admit('a'), null);
  resetReady(controller, 'next-session');
  controller.arm();
  assert.ok(controller.admit('a'), 'セッションが変われば失敗履歴も消える');
});

test('a successful page clears the failure record for that cutoff', () => {
  const controller = session();
  controller.arm();
  controller.completeFailure(controller.admit('a'));
  controller.advanceClock(MINUTE_HISTORY_DEFAULTS.failureCooldownMs);
  controller.completeSuccess(controller.admit('a'), 200);
  assert.deepEqual([...controller.failures.keys()], []);
});

test('a response from the previous session cannot mutate the new one', () => {
  const controller = session();
  controller.arm();
  const stale = controller.admit('a');

  const generation = controller.resetSession('new-session');
  assert.equal(controller.isCurrent(stale), false);
  assert.equal(controller.isGeneration(generation), true);

  // 遅れて返ってきた古いレスポンスは、成功も失敗も新しい世代に触れない。
  controller.completeSuccess(stale, 0);
  controller.completeFailure(stale);
  assert.equal(controller.exhausted, false);
  assert.deepEqual([...controller.failures.keys()], []);

  assert.equal(controller.markSessionReady(generation, 'new-session'), true);
  controller.arm();
  const fresh = controller.admit('a');
  assert.ok(fresh);
  assert.equal(controller.isCurrent(fresh), true);
});

test('the initial preload runs without arming but still under the generation guard', () => {
  const controller = session();
  controller.resetSession('preload-session');
  const token = controller.admitInitial('a', 'preload-session');
  assert.ok(token);
  assert.equal(controller.admitInitial('b', 'preload-session'), null, '単一飛行はプリロードにも効く');

  controller.resetSession('new-session');
  assert.equal(controller.isCurrent(token), false);
});

test('an aborted request returns to idle without recording a failure', () => {
  const controller = session();
  controller.arm();
  const token = controller.admit('a');
  controller.completeAbort(token);

  assert.equal(controller.loading, null);
  assert.deepEqual([...controller.failures.keys()], []);
  assert.ok(controller.admit('a'));
});

test('session loading rejects old-chart admission and keeps only the new preload current', () => {
  const controller = session();
  const generationA = controller.resetSession('7203|2026-08-20');
  const preloadA = controller.admitInitial('a-preload', '7203|2026-08-20');
  assert.ok(preloadA);
  controller.completeSuccess(preloadA, 30);
  assert.equal(controller.markSessionReady(generationA, '7203|2026-08-20'), true);
  assert.equal(controller.arm('7203|2026-08-20'), true);
  const delayedA = controller.admit('a-page', { sessionIdentity: '7203|2026-08-20' });
  assert.ok(delayedA);

  const generationB = controller.resetSession('7203|2026-08-21');
  assert.equal(controller.arm('7203|2026-08-20'), false, 'the old chart cannot arm while B loads');
  assert.equal(
    controller.admit('old-chart-page', { sessionIdentity: '7203|2026-08-20' }),
    null,
    'the old chart cannot occupy B generation single-flight',
  );

  const preloadB = controller.admitInitial('b-preload', '7203|2026-08-21');
  assert.ok(preloadB, 'B preload is admitted through the loading-only path');
  assert.equal(controller.isCurrent(preloadB), true);
  controller.completeSuccess(delayedA, 0);
  assert.equal(controller.isCurrent(preloadB), true, 'the delayed A completion cannot affect B');
  controller.completeSuccess(preloadB, 30);
  assert.equal(controller.markSessionReady(generationB, '7203|2026-08-21'), true);
  assert.equal(controller.isReadySession('7203|2026-08-21'), true);
});

// ------------------------------------------------------- app.js source seams

test('loadSession cancels history and advances the generation before its first await', () => {
  const source = withoutComments(extractFunction(APP_SOURCE, 'loadSession'));
  const cancel = source.indexOf("requests.cancel('minute-history')");
  const reset = source.indexOf('minuteHistory.resetSession(requestedIdentity)');
  const firstAwait = source.indexOf('await ');

  assert.ok(cancel !== -1 && reset !== -1 && firstAwait !== -1);
  assert.ok(cancel < firstAwait, 'cancel は最初の await より前');
  assert.ok(reset < firstAwait, 'resetSession は最初の await より前');
  // contextBars の代入前に世代を確認している。
  const guard = source.indexOf('minuteHistory.isLoadingSession(generation, sessionIdentity)');
  const assign = source.indexOf('state.contextBars = contextBars');
  assert.ok(guard !== -1 && assign !== -1 && guard < assign);
});

test('history requests admit before fetching and re-check the token before mutating', () => {
  for (const name of ['loadOlderMinuteBars', 'loadMinuteContextBars']) {
    const source = withoutComments(extractFunction(APP_SOURCE, name));
    const admissionMethod = name === 'loadOlderMinuteBars' ? 'minuteHistory.admit(' : 'minuteHistory.admitInitial(';
    const admit = source.indexOf(admissionMethod);
    const fetch = source.indexOf('requests.fetchLatest(');
    const current = source.indexOf('minuteHistory.isCurrent(token)');
    assert.ok(admit !== -1, `${name} must admit a token`);
    assert.ok(fetch !== -1, `${name} must go through the request coordinator`);
    assert.ok(admit < fetch, `${name}: 単一飛行の関門は fetch より前`);
    assert.ok(current !== -1 && current > fetch, `${name}: await のあとに世代を確認する`);
    assert.ok(source.includes('completeAbort'), `${name} must handle cancellation`);
    assert.ok(source.includes('completeFailure'), `${name} must handle failure`);
  }

  const older = withoutComments(extractFunction(APP_SOURCE, 'loadOlderMinuteBars'));
  assert.ok(
    older.indexOf('minuteHistory.isCurrent(token)') < older.indexOf('prependMinuteHistory('),
    'state を書き換える直前にトークンを確認する',
  );
});

test('older-history application errors are contained and complete as failures', async () => {
  const source = extractFunction(APP_SOURCE, 'loadOlderMinuteBars');
  const token = { id: 1 };
  let failedToken = null;
  let applyAttempts = 0;
  const loadOlderMinuteBars = Function(
    'state',
    'minuteSessionIdentity',
    'earliestMinuteBarTime',
    'cutoffFromEpochSeconds',
    'cutoffKey',
    'minuteHistory',
    'requests',
    'minuteContextUrl',
    'MINUTE_HISTORY_PAGE_BARS',
    'normalizeMinuteBars',
    'isCancellationError',
    'prependMinuteHistory',
    `return (${source});`,
  )(
    { meta: { stem: '7203', code: '7203', date: '2026-08-20' } },
    () => '7203|7203|2026-08-20',
    () => 600,
    () => ({ date: '1970-01-01', time: '00:10' }),
    () => '1970-01-01T00:10',
    {
      admit: () => token,
      isReadySession: () => true,
      isCurrent: () => true,
      completeAbort: () => assert.fail('an application error is not an abort'),
      completeFailure: (completedToken) => { failedToken = completedToken; },
      completeSuccess: () => assert.fail('an application error is not a success'),
    },
    { fetchLatest: async () => ({ bars: [bar(540)] }) },
    () => '/api/minute-context',
    MINUTE_HISTORY_DEFAULTS.pageSize,
    minuteHistoryModule.normalizeMinuteBars,
    () => false,
    () => {
      applyAttempts += 1;
      throw new Error('chart update failed');
    },
  );

  await assert.doesNotReject(loadOlderMinuteBars());
  assert.equal(applyAttempts, 1);
  assert.equal(failedToken, token);
});

test('a non-empty all-invalid history payload becomes a retryable failure', async () => {
  const source = extractFunction(APP_SOURCE, 'loadOlderMinuteBars');
  const identity = '7203|7203|2026-08-20';
  const controller = new MinuteHistorySession({ now: () => 0 });
  const generation = controller.resetSession(identity);
  assert.equal(controller.markSessionReady(generation, identity), true);
  controller.arm(identity);
  let applyAttempts = 0;
  const loadOlderMinuteBars = Function(
    'state',
    'minuteSessionIdentity',
    'earliestMinuteBarTime',
    'cutoffFromEpochSeconds',
    'cutoffKey',
    'minuteHistory',
    'requests',
    'minuteContextUrl',
    'MINUTE_HISTORY_PAGE_BARS',
    'normalizeMinuteBars',
    'isCancellationError',
    'prependMinuteHistory',
    `return (${source});`,
  )(
    { meta: { stem: '7203', code: '7203', date: '2026-08-20' } },
    () => identity,
    () => 600,
    () => ({ date: '1970-01-01', time: '00:10' }),
    () => '1970-01-01T00:10',
    controller,
    { fetchLatest: async () => ({ bars: [{ ...bar(540), volume: -1 }] }) },
    () => '/api/minute-context',
    MINUTE_HISTORY_DEFAULTS.pageSize,
    minuteHistoryModule.normalizeMinuteBars,
    () => false,
    () => { applyAttempts += 1; return 0; },
  );

  await loadOlderMinuteBars();
  assert.equal(applyAttempts, 0);
  assert.equal(controller.exhausted, false);
  assert.equal(controller.failures.get('1970-01-01T00:10')?.count, 1);
});

test('the older-history loader uses controller pageSize for request and normalization', async () => {
  const source = extractFunction(APP_SOURCE, 'loadOlderMinuteBars');
  const identity = '7203|7203|2026-08-20';
  const controller = new MinuteHistorySession({ pageSize: 2, now: () => 0 });
  const generation = controller.resetSession(identity);
  controller.markSessionReady(generation, identity);
  controller.arm(identity);
  let requestedLimit = null;
  let appliedBars = null;
  const loadOlderMinuteBars = Function(
    'state',
    'minuteSessionIdentity',
    'earliestMinuteBarTime',
    'cutoffFromEpochSeconds',
    'cutoffKey',
    'minuteHistory',
    'requests',
    'minuteContextUrl',
    'MINUTE_HISTORY_PAGE_BARS',
    'normalizeMinuteBars',
    'isCancellationError',
    'prependMinuteHistory',
    `return (${source});`,
  )(
    { meta: { stem: '7203', code: '7203', date: '2026-08-20' } },
    () => identity,
    () => 600,
    () => ({ date: '1970-01-01', time: '00:10' }),
    () => '1970-01-01T00:10',
    controller,
    { fetchLatest: async () => ({ bars: [bar(360), bar(420), bar(480)] }) },
    (_session, _cutoff, limit) => { requestedLimit = limit; return '/api/minute-context'; },
    MINUTE_HISTORY_DEFAULTS.pageSize,
    minuteHistoryModule.normalizeMinuteBars,
    () => false,
    (bars) => { appliedBars = bars; return bars.length; },
  );

  await loadOlderMinuteBars();
  assert.equal(requestedLimit, 2);
  assert.deepEqual(appliedBars.map((item) => item.time), [420, 480]);
});

test('the replay frame no longer writes the minute chart viewport', () => {
  const step = withoutComments(extractFunction(APP_SOURCE, 'step'));
  assert.ok(step.includes('followTickView()'), 'ティックチャートの追従は続ける');
  assert.ok(!step.includes('followMinuteView'), '分足の表示位置は毎フレーム書き戻さない');
  assert.ok(!step.includes('setVisibleLogicalRange'));

  const redraw = withoutComments(extractFunction(APP_SOURCE, 'redrawAll'));
  assert.ok(redraw.includes('followMinuteView()'), 'シーク・読み込み時だけ作り直す');
  assert.ok(redraw.includes('withProgrammaticMinuteRange'));
});

test('prepending history does not go through redrawAll', () => {
  const source = withoutComments(extractFunction(APP_SOURCE, 'prependMinuteHistory'));
  assert.ok(!source.includes('redrawAll'), '板・テープ・可視レンジまで作り直さない');
  assert.ok(source.includes('applyMinuteHistoryPage({'));
  assert.ok(!source.includes('state.contextBars ='));
  assert.ok(!source.includes('state.bars ='));
  assert.ok(!source.includes('candleSeries.setData'));
  assert.ok(!source.includes('volumeSeries.setData'));
});

test('app.js uses the fixed policy constants for paging', () => {
  assert.doesNotMatch(APP_SOURCE, /const MINUTE_HISTORY_PAGE_BARS/);
  assert.ok(APP_SOURCE.includes('minuteContextUrl(session, cutoff, minuteHistory.pageSize)'));
  assert.equal(MINUTE_HISTORY_DEFAULTS.pageSize, 200);
  assert.match(APP_SOURCE, /const MINUTE_HISTORY_EDGE_BARS = 10;/);
  assert.match(APP_SOURCE, /const MINUTE_CONTEXT_BARS = 30;/);
  assert.ok(APP_SOURCE.includes('subscribeVisibleLogicalRangeChange(onMinuteLogicalRangeChange)'));
});

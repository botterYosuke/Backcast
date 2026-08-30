import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import { applyMinuteHistoryPage } from './minute-history.mjs';
import * as dailyChartModule from './daily-chart.mjs';

import {
  applyDailyHistoryPage,
  ChartViewportState,
  DAILY_HISTORY_DEFAULTS,
  DailyChartSession,
  appendPartialDailyBar,
  buildDailySeries,
  buildPartialDailyBar,
  commitMarkerWrites,
  commitReplayFrame,
  dailyContextUrl,
  dailyInitialLogicalRange,
  dailySessionIdentity,
  loadDailyRequest,
  mergeOlderDailyBars,
  normalizeDailyPayload,
  paintedLinePoint,
  renderDailySnapshot,
  simpleMovingAverage,
  shiftDailyLogicalRange,
  updateChartViewport,
} from './daily-chart.mjs';

const {
  ReplayLivenessPresenter,
  buildReplayLivenessView,
  commitDailySession,
  flushDeferredMinuteChart,
  runReplayFrame,
  selectMinuteHistoryTarget,
} = dailyChartModule;

const APP_SOURCE = readFileSync(new URL('./app.js', import.meta.url), 'utf8');
const HTML_SOURCE = readFileSync(new URL('./index.html', import.meta.url), 'utf8');
const STYLE_SOURCE = readFileSync(new URL('./styles.css', import.meta.url), 'utf8');

function bar(day, close, overrides = {}) {
  return {
    time: `2025-01-${String(day).padStart(2, '0')}`,
    open: close,
    high: close + 1,
    low: close - 1,
    close,
    volume: 100,
    ...overrides,
  };
}

function datedBar(index, close = index + 1) {
  const time = new Date(Date.UTC(2024, 0, index + 1)).toISOString().slice(0, 10);
  return { time, open: close, high: close + 1, low: close - 1, close, volume: 100 };
}

function extractFunction(source, name) {
  const start = source.search(new RegExp(`(async )?function ${name}\\(`));
  assert.notEqual(start, -1, `${name} must exist in app.js`);
  const parametersStart = source.indexOf('(', start);
  let parameterDepth = 0;
  let parametersEnd = -1;
  for (let index = parametersStart; index < source.length; index += 1) {
    if (source[index] === '(') parameterDepth += 1;
    if (source[index] !== ')') continue;
    parameterDepth -= 1;
    if (parameterDepth === 0) {
      parametersEnd = index;
      break;
    }
  }
  const bodyStart = source.indexOf('{', parametersEnd);
  let depth = 0;
  for (let index = bodyStart; index < source.length; index += 1) {
    if (source[index] === '{') depth += 1;
    if (source[index] !== '}') continue;
    depth -= 1;
    if (depth === 0) return source.slice(start, index + 1);
  }
  throw new Error(`Could not extract ${name}`);
}

function compileExtractedFunction(source, name, dependencies = {}) {
  const names = Object.keys(dependencies);
  const values = Object.values(dependencies);
  return Function(...names, `return (${extractFunction(source, name)});`)(...values);
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function readyDailySession(options = {}) {
  const session = new DailyChartSession(options);
  const generation = session.resetSession();
  const meta = { stem: '7203', code: '7203', date: '2025-08-01' };
  const identity = session.setActualSession(generation, meta);
  const token = session.admitRequest();
  const history = Array.from({ length: 250 }, (_, index) => datedBar(index, 100 + index));
  assert.equal(session.complete(token, normalizeDailyPayload({ available: true, bars: history })), true);
  assert.equal(session.phase, 'ready');
  return { session, generation, meta, identity };
}

function prepareOlderPlan(session, count = 3) {
  assert.equal(session.armHistory(), true);
  const cutoff = session.historyBars[0].time;
  const token = session.admitOlderPage(cutoff);
  assert.ok(token);
  const older = Array.from(
    { length: count },
    (_, index) => datedBar(-index - 1, 90 - index),
  );
  const normalized = normalizeDailyPayload({ available: true, bars: older }, session.historyPageSize);
  const plan = session.prepareOlderPage(token, normalized);
  assert.equal(plan.kind, 'page');
  return { cutoff, token, plan };
}

test('Daily history defaults and initial logical range enforce newest 90 plus five padding', () => {
  assert.equal(Object.isFrozen(DAILY_HISTORY_DEFAULTS), true);
  assert.deepEqual(DAILY_HISTORY_DEFAULTS, {
    visibleBars: 90,
    rightPaddingBars: 5,
    pageSize: 200,
    edgeThresholdBars: 10,
    failureCooldownMs: 5000,
    maxFailuresPerCutoff: 3,
  });
  assert.equal(dailyInitialLogicalRange(0), null);
  assert.equal(dailyInitialLogicalRange(-1), null);
  assert.equal(dailyInitialLogicalRange(1.5), null);
  assert.deepEqual(dailyInitialLogicalRange(1), { from: 0, to: 5 });
  assert.deepEqual(dailyInitialLogicalRange(89), { from: 0, to: 93 });
  assert.deepEqual(dailyInitialLogicalRange(90), { from: 0, to: 94 });
  assert.deepEqual(dailyInitialLogicalRange(500), { from: 410, to: 504 });
  assert.deepEqual(
    dailyInitialLogicalRange(501),
    { from: 411, to: 505 },
    'the replay-derived partial day counts in the total',
  );
  assert.deepEqual(
    dailyInitialLogicalRange(20, { visibleBars: 10, rightPaddingBars: 2 }),
    { from: 10, to: 21 },
  );
});

test('Daily context URL keeps initial compatibility and validates older-page options', () => {
  const identity = '285A|285A0|2025-08-01';
  assert.equal(
    dailyContextUrl(identity),
    '/api/daily-context?stem=285A&date=2025-08-01&limit=500',
  );
  assert.equal(
    dailyContextUrl(identity, { beforeDate: '2024-01-02', limit: 200 }),
    '/api/daily-context?stem=285A&date=2024-01-02&limit=200',
  );
  for (const options of [
    null,
    [],
    { beforeDate: '2024-02-30', limit: 200 },
    { beforeDate: '2024-01-02', limit: 0 },
    { beforeDate: '2024-01-02', limit: 501 },
    { beforeDate: '2024-01-02', limit: 1.5 },
  ]) {
    assert.equal(dailyContextUrl(identity, options), null);
  }
  for (const malformed of [null, '', '285A||2025-08-01', '285A|285A0|bad-date', 'a|b|2025-01-01|x']) {
    assert.equal(dailyContextUrl(malformed), null);
  }
});

test('older Daily merge is chronological, strict-before, unique, and preserves existing dates', () => {
  const existingFirst = bar(10, 100);
  const existingSecond = bar(11, 110);
  const result = mergeOlderDailyBars(
    [existingSecond, existingFirst],
    [
      bar(9, 90),
      bar(8, 80),
      { ...bar(8, 80) },
      bar(10, 999),
      bar(12, 120),
      bar(7, 70, { volume: -1 }),
    ],
    { boundary: '2025-01-10' },
  );
  assert.equal(result.added, 2);
  assert.deepEqual(result.addedBars.map(({ time }) => time), ['2025-01-08', '2025-01-09']);
  assert.deepEqual(result.bars.map(({ time }) => time), [
    '2025-01-08', '2025-01-09', '2025-01-10', '2025-01-11',
  ]);
  assert.equal(result.bars.find(({ time }) => time === '2025-01-10').close, 100);

  const duplicateOnly = mergeOlderDailyBars(result.bars, [bar(8, 80), bar(10, 100)], {
    boundary: '2025-01-08',
  });
  assert.equal(duplicateOnly.added, 0);
  const secondPage = mergeOlderDailyBars(result.bars, [bar(6, 60), bar(7, 70)], {
    boundary: '2025-01-08',
  });
  assert.equal(secondPage.added, 2);
  assert.deepEqual(secondPage.bars.slice(0, 4).map(({ time }) => time), [
    '2025-01-06', '2025-01-07', '2025-01-08', '2025-01-09',
  ]);
  assert.equal(mergeOlderDailyBars(result.bars, [bar(6, 60)], { boundary: 'bad' }).added, 0);

  assert.deepEqual(shiftDailyLogicalRange({ from: -2.5, to: 10 }, 7), { from: 4.5, to: 17 });
  assert.equal(shiftDailyLogicalRange(null, 1), null);
  assert.equal(shiftDailyLogicalRange({ from: 0, to: Number.NaN }, 1), null);
  assert.equal(shiftDailyLogicalRange({ from: 0, to: 1 }, -1), null);
});

test('Daily older-page admission requires a user arm, threshold, current identity, and single flight', () => {
  const { session, identity } = readyDailySession();
  const cutoff = session.historyBars[0].time;
  assert.equal(session.canLoadOlder({ identity, barsBefore: 10 }), false);
  assert.equal(session.armHistory('other|code|2025-08-01'), false);
  assert.equal(session.armHistory(identity), true);
  assert.equal(session.canLoadOlder({ identity, barsBefore: 11 }), false);
  assert.equal(session.canLoadOlder({ identity, barsBefore: 10, programmatic: true }), false);
  assert.equal(session.canLoadOlder({ identity, barsBefore: 10 }), true);
  const token = session.admitOlderPage(cutoff, { identity });
  assert.ok(token);
  assert.equal(Object.isFrozen(token), true);
  assert.equal(session.historyArmed, false);
  assert.equal(session.canLoadOlder({ identity, barsBefore: 0 }), false);
  assert.equal(session.admitOlderPage(cutoff, { identity }), null);
  assert.equal(session.isCurrentOlderPage({ ...token }), false, 'token object identity is required');
  assert.equal(session.isCurrentOlderPage(token), true);
  assert.equal(session.abortOlderPage(token), true);
  assert.equal(session.historyFailures.size, 0, 'cancellation consumes no retry');
  assert.equal(session.armHistory(identity), true);
  const nextToken = session.admitOlderPage(cutoff, { identity });
  assert.ok(nextToken);
  session.resetSession();
  assert.equal(session.isCurrentOlderPage(nextToken), false, 'generation changes reject old pages');
  assert.equal(session.abortOlderPage(nextToken), false);
  assert.equal(session.armHistory(identity), false, 'loading and stale identities cannot arm paging');
});

test('Daily older-page cooldown, three-failure stop, exhaustion, and reset are session-local', () => {
  let now = 1_000;
  const { session, identity } = readyDailySession({ now: () => now });
  const cutoff = session.historyBars[0].time;

  for (let failure = 1; failure <= 3; failure += 1) {
    assert.equal(session.armHistory(identity), true);
    const token = session.admitOlderPage(cutoff, { identity });
    assert.ok(token);
    assert.equal(session.failOlderPage(token), true);
    assert.equal(session.historyFailures.get(cutoff).count, failure);
    assert.equal(session.armHistory(identity), true);
    assert.equal(session.canLoadOlder({ identity, barsBefore: 10 }), false);
    now += 5_000;
  }
  assert.equal(session.canLoadOlder({ identity, barsBefore: 10 }), false);
  assert.equal(session.admitOlderPage(cutoff, { identity }), null);

  const generation = session.resetSession();
  assert.equal(session.historyFailures.size, 0);
  assert.equal(session.historyExhausted, false);
  assert.equal(session.historyArmed, false);
  assert.equal(session.hasInitialViewport, false);
  session.setActualSession(generation, { stem: '7203', code: '7203', date: '2025-08-02' });
  const initial = session.admitRequest();
  session.complete(initial, normalizeDailyPayload({ available: true, bars: [bar(1, 10)] }));
  session.armHistory();
  const exhaustedToken = session.admitOlderPage(session.historyBars[0].time);
  assert.equal(session.completeOlderExhausted(exhaustedToken), true);
  assert.equal(session.historyExhausted, true);
  assert.equal(session.armHistory(), true);
  assert.equal(session.canLoadOlder({ barsBefore: 0 }), false);
});

test('prepared Daily pages are tagged, side-effect-free, and classify every payload outcome', () => {
  const { session } = readyDailySession();
  session.appendTick('2025-08-01', 400, 10);
  session.deriveTerminal();
  session.armHistory();
  const cutoff = session.historyBars[0].time;
  const token = session.admitOlderPage(cutoff);
  const before = {
    bars: session.historyBars,
    cache: session.cache.get(session.identity),
    request: session.historyPageRequest,
    precomputations: session.metrics.historyPrecomputations,
  };

  assert.deepEqual(session.prepareOlderPage({ ...token }, null), { kind: 'stale' });
  assert.equal(
    session.prepareOlderPage(token, normalizeDailyPayload({ available: false, bars: [] })).kind,
    'failure',
  );
  assert.equal(session.prepareOlderPage(token, null).kind, 'failure');
  assert.equal(
    session.prepareOlderPage(
      token,
      normalizeDailyPayload({ available: true, bars: [bar(7, 70, { volume: -1 })] }),
    ).kind,
    'failure',
  );
  assert.equal(
    session.prepareOlderPage(token, normalizeDailyPayload({ available: true, bars: [] })).kind,
    'exhausted',
  );
  assert.equal(
    session.prepareOlderPage(
      token,
      normalizeDailyPayload({ available: true, bars: [session.historyBars[0]] }),
    ).kind,
    'exhausted',
  );

  const page = session.prepareOlderPage(
    token,
    normalizeDailyPayload({ available: true, bars: [datedBar(-1, 90), datedBar(-2, 89)] }),
  );
  assert.equal(page.kind, 'page');
  assert.equal(page.token, token);
  assert.equal(page.added, 2);
  assert.equal(page.previousSnapshot.bars.at(-1).time, '2025-08-01');
  assert.equal(page.nextSnapshot.bars.at(-1).time, '2025-08-01');
  assert.equal(page.nextCached.history.bars.length, before.bars.length + 2);
  assert.equal(Object.isFrozen(page), true);
  assert.equal(session.historyBars, before.bars);
  assert.equal(session.cache.get(session.identity), before.cache);
  assert.equal(session.historyPageRequest, before.request);
  assert.equal(session.metrics.historyPrecomputations, before.precomputations);
});

test('committing one Daily page replaces cache and SMA state exactly once without changing existing values', () => {
  const { session, meta } = readyDailySession();
  session.appendTick(meta.date, 400, 10);
  session.deriveTerminal();
  const previousBars = session.historyBars;
  const previousSma25 = new Map(session.historicalSma25.map((point) => [point.time, point.value]));
  const previousSma200 = new Map(session.historicalSma200.map((point) => [point.time, point.value]));
  const previousTerminal25 = session.terminalSma25;
  const previousTerminal200 = session.terminalSma200;
  const beforePrecomputations = session.metrics.historyPrecomputations;
  const { cutoff, plan } = prepareOlderPlan(session, 12);
  assert.equal(session.metrics.historyPrecomputations, beforePrecomputations);

  const committed = session.commitOlderPage(plan);
  assert.deepEqual(committed, { accepted: true, added: 12 });
  assert.equal(session.phase, 'ready');
  assert.equal(session.historyBars.length, previousBars.length + 12);
  assert.equal(session.snapshot().bars.at(-1).time, meta.date);
  assert.equal(session.historyPageRequest, null);
  assert.equal(session.historyFailures.has(cutoff), false);
  assert.equal(session.metrics.historyPrecomputations, beforePrecomputations + 1);
  assert.equal(session.cache.get(session.identity).history.bars, session.historyBars);
  for (const point of session.historicalSma25) {
    if (previousSma25.has(point.time)) assert.equal(point.value, previousSma25.get(point.time));
  }
  for (const point of session.historicalSma200) {
    if (previousSma200.has(point.time)) assert.equal(point.value, previousSma200.get(point.time));
  }
  assert.deepEqual(session.terminalSma25, previousTerminal25);
  assert.deepEqual(session.terminalSma200, previousTerminal200);
  session.appendTick(meta.date, 401, 1);
  session.deriveTerminal();
  assert.equal(session.metrics.historyPrecomputations, beforePrecomputations + 1);

  const generation = session.resetSession();
  session.setActualSession(generation, meta);
  assert.equal(session.phase, 'ready');
  assert.equal(session.historyBars.length, previousBars.length + 12, 'same-identity cache retains pages');
  assert.equal(session.historyBars[0].time, plan.nextCached.history.bars[0].time);
});

test('successful Daily page commit clears a cutoff failure while stale plans cannot mutate state', () => {
  let now = 0;
  const { session } = readyDailySession({ now: () => now });
  const cutoff = session.historyBars[0].time;
  session.armHistory();
  const failedToken = session.admitOlderPage(cutoff);
  session.failOlderPage(failedToken);
  assert.equal(session.historyFailures.get(cutoff).count, 1);
  now = 5_000;
  const { token, plan } = prepareOlderPlan(session, 1);
  assert.equal(session.commitOlderPage(plan).accepted, true);
  assert.equal(session.historyFailures.has(cutoff), false);

  session.armHistory();
  const nextToken = session.admitOlderPage(session.historyBars[0].time);
  const nextPlan = session.prepareOlderPage(
    nextToken,
    normalizeDailyPayload({ available: true, bars: [datedBar(-3, 87)] }),
  );
  assert.equal(nextPlan.kind, 'page');
  assert.equal(session.abortOlderPage(nextToken), true);
  const before = session.historyBars;
  assert.deepEqual(session.commitOlderPage(nextPlan), { accepted: false, added: 0 });
  assert.equal(session.historyBars, before);
  assert.equal(session.isCurrentOlderPage(token), false);
});

test('Daily page transaction writes four series, shifts the latest live range, and commits last', () => {
  const { session } = readyDailySession();
  const { plan } = prepareOlderPlan(session, 3);
  const events = [];
  const datasets = {};
  const ports = {};
  for (const name of ['candle', 'volume', 'sma25', 'sma200']) {
    ports[name] = {
      setData(data) {
        events.push(name);
        datasets[name] = structuredClone(data);
      },
    };
  }
  ports.paintCandle = (value) => ({ ...value, painted: 'candle' });
  ports.paintVolume = (value) => ({ time: value.time, value: value.volume });
  const viewport = {
    savedRange: Object.freeze({ from: 1, to: 5 }),
    runProgrammatic(apply) { return apply(); },
    replace(range) {
      events.push(`saved:${range.from}:${range.to}`);
      this.savedRange = range ? Object.freeze({ ...range }) : null;
    },
  };
  const timeScale = {
    liveRange: { from: 4, to: 14 },
    getVisibleLogicalRange() { return { ...this.liveRange }; },
    setVisibleLogicalRange(range) {
      events.push(`live:${range.from}:${range.to}`);
      this.liveRange = { ...range };
    },
  };
  const originalCommit = session.commitOlderPage.bind(session);
  session.commitOlderPage = (candidate) => {
    events.push('commit');
    return originalCommit(candidate);
  };

  const result = applyDailyHistoryPage({ session, plan, viewport, ports, timeScale });
  assert.deepEqual(result, { committed: true, added: 3, shiftedRange: { from: 7, to: 17 } });
  assert.deepEqual(events, [
    'candle', 'volume', 'sma25', 'sma200', 'live:7:17', 'saved:7:17', 'commit',
  ]);
  assert.deepEqual(timeScale.liveRange, { from: 7, to: 17 });
  assert.deepEqual(viewport.savedRange, { from: 7, to: 17 });
  assert.equal(datasets.candle.length, plan.nextSnapshot.bars.length);
  assert.equal(datasets.sma25.every((point) => Object.isFrozen(point) === false), true);
});

test('Daily page transaction fully rolls back every write, range, saved-range, and rejected commit failure', () => {
  for (const failStage of ['candle', 'volume', 'sma25', 'sma200', 'live', 'saved', 'commit']) {
    const { session } = readyDailySession();
    const { plan } = prepareOlderPlan(session, 2);
    const previous = {
      bars: session.historyBars,
      cache: session.cache.get(session.identity),
      token: session.historyPageRequest,
      precomputations: session.metrics.historyPrecomputations,
      saved: { from: 2, to: 8 },
      live: { from: 5, to: 15 },
    };
    const current = {
      candle: structuredClone(plan.previousSnapshot.bars),
      volume: plan.previousSnapshot.bars.map((item) => ({ time: item.time, value: item.volume })),
      sma25: structuredClone(plan.previousSnapshot.sma25),
      sma200: structuredClone(plan.previousSnapshot.sma200),
    };
    const expectedDatasets = structuredClone(current);
    let failed = false;
    const ports = {};
    for (const name of ['candle', 'volume', 'sma25', 'sma200']) {
      ports[name] = {
        setData(data) {
          if (failStage === name && !failed) {
            failed = true;
            throw new Error(`fail ${name}`);
          }
          current[name] = structuredClone(data);
        },
      };
    }
    ports.paintCandle = (value) => ({ ...value });
    ports.paintVolume = (value) => ({ time: value.time, value: value.volume });
    const viewport = {
      savedRange: Object.freeze({ ...previous.saved }),
      runProgrammatic(apply) { return apply(); },
      replace(range) {
        if (failStage === 'saved' && !failed) {
          failed = true;
          throw new Error('fail saved');
        }
        this.savedRange = range ? Object.freeze({ ...range }) : null;
      },
    };
    const timeScale = {
      liveRange: { ...previous.live },
      getVisibleLogicalRange() { return { ...this.liveRange }; },
      setVisibleLogicalRange(range) {
        if (failStage === 'live' && !failed) {
          failed = true;
          throw new Error('fail live');
        }
        this.liveRange = { ...range };
      },
    };
    if (failStage === 'commit') {
      session.commitOlderPage = () => ({ accepted: false, added: 0 });
    }

    assert.throws(
      () => applyDailyHistoryPage({ session, plan, viewport, ports, timeScale }),
      /fail|rejected/,
      failStage,
    );
    assert.deepEqual(current, expectedDatasets, `${failStage}: four datasets restored`);
    assert.deepEqual(timeScale.liveRange, previous.live, `${failStage}: live range restored`);
    assert.deepEqual(viewport.savedRange, previous.saved, `${failStage}: saved range restored`);
    assert.equal(session.historyBars, previous.bars, `${failStage}: canonical bars unchanged`);
    assert.equal(session.cache.get(session.identity), previous.cache, `${failStage}: cache unchanged`);
    assert.equal(session.historyPageRequest, previous.token, `${failStage}: request remains retryable`);
    assert.equal(session.metrics.historyPrecomputations, previous.precomputations);
  }
});

test('Daily page transaction leaves state untouched on painter failure and aggregates rollback failures', () => {
  {
    const { session } = readyDailySession();
    const { plan } = prepareOlderPlan(session, 1);
    const bars = session.historyBars;
    const writes = [];
    const ports = Object.fromEntries(
      ['candle', 'volume', 'sma25', 'sma200'].map((name) => [name, { setData: () => writes.push(name) }]),
    );
    ports.paintCandle = () => { throw new Error('paint failed'); };
    const viewport = { savedRange: null, runProgrammatic: (apply) => apply(), replace() {} };
    const timeScale = { getVisibleLogicalRange: () => ({ from: 0, to: 10 }), setVisibleLogicalRange() {} };
    assert.throws(() => applyDailyHistoryPage({ session, plan, viewport, ports, timeScale }), /paint failed/);
    assert.deepEqual(writes, []);
    assert.equal(session.historyBars, bars);
  }

  {
    const { session } = readyDailySession();
    const { plan } = prepareOlderPlan(session, 1);
    const ports = {
      candle: { setData() { throw new Error('candle always fails'); } },
      volume: { setData() {} },
      sma25: { setData() {} },
      sma200: { setData() {} },
      paintCandle: (value) => ({ ...value }),
      paintVolume: (value) => ({ time: value.time, value: value.volume }),
    };
    const viewport = { savedRange: null, runProgrammatic: (apply) => apply(), replace() {} };
    const timeScale = { getVisibleLogicalRange: () => ({ from: 0, to: 10 }), setVisibleLogicalRange() {} };
    assert.throws(
      () => applyDailyHistoryPage({ session, plan, viewport, ports, timeScale }),
      (error) => error instanceof AggregateError && error.errors.length >= 2,
    );
  }
});

test('payload normalization distinguishes unavailable, valid empty, and malformed data', () => {
  assert.deepEqual(normalizeDailyPayload({ available: false, bars: [] }), {
    ok: false, cacheable: false, phase: 'unavailable', bars: [], receivedCount: 0,
  });
  assert.deepEqual(normalizeDailyPayload({ available: true, bars: [] }), {
    ok: true, cacheable: true, phase: 'empty', bars: [], receivedCount: 0,
  });
  for (const payload of [null, {}, { available: true }, { available: 'yes', bars: [] }]) {
    assert.equal(normalizeDailyPayload(payload).phase, 'error');
  }
  assert.equal(normalizeDailyPayload({ available: false, bars: [bar(1, 10)] }).phase, 'error');
});

test('normalization enforces OHLCV invariants and treats invalid-all as retryable error', () => {
  const invalid = [
    bar(1, 10, { open: 0 }),
    bar(2, 10, { high: Number.POSITIVE_INFINITY }),
    bar(3, 10, { volume: -1 }),
    bar(4, 10, { low: 11 }),
    { ...bar(5, 10), time: '2025-02-30' },
  ];
  const result = normalizeDailyPayload({ available: true, bars: invalid });
  assert.equal(result.ok, false);
  assert.equal(result.cacheable, false);
  assert.equal(result.phase, 'error');
  assert.deepEqual(result.bars, []);
});

test('normalization sorts, bounds, collapses exact duplicates, and omits conflicted dates', () => {
  const exact = bar(2, 20);
  const conflicted = bar(3, 30);
  const payload = {
    available: true,
    bars: [conflicted, bar(1, 10), exact, { ...exact }, { ...conflicted, close: 31 }],
  };
  const result = normalizeDailyPayload(payload, 2);
  assert.equal(result.ok, true);
  assert.deepEqual(result.bars.map((item) => item.time), ['2025-01-01', '2025-01-02']);
  assert.notEqual(result.bars[1], exact, 'normalization returns immutable copies');

  const invalidSibling = normalizeDailyPayload({
    available: true,
    bars: [bar(4, 40), bar(4, 40, { volume: -1 }), bar(5, 50)],
  });
  assert.deepEqual(invalidSibling.bars.map((item) => item.time), ['2025-01-05']);

  const oversized = normalizeDailyPayload({
    available: true,
    bars: Array.from({ length: 501 }, (_, index) => datedBar(index)),
  });
  assert.equal(oversized.bars.length, 500);
  assert.equal(oversized.bars[0].time, datedBar(1).time);
});

test('partial candle folds raw prices and quantities and zero ticks omit the day', () => {
  assert.equal(buildPartialDailyBar('2025-01-31', [], [], 0), null);
  const partial = buildPartialDailyBar('2025-01-31', [101, 99, 103, 102], [2, 3, 5, 7], 4);
  assert.deepEqual(partial, {
    time: '2025-01-31', open: 101, high: 103, low: 99, close: 102, volume: 17,
  });
  assert.deepEqual(appendPartialDailyBar(null, '2025-01-31', 101, 2), {
    time: '2025-01-31', open: 101, high: 101, low: 101, close: 101, volume: 2,
  });
  assert.deepEqual(appendPartialDailyBar(partial, '2025-01-31', 98, 11), {
    time: '2025-01-31', open: 101, high: 103, low: 98, close: 98, volume: 28,
  });
  assert.deepEqual(appendPartialDailyBar(partial, '2025-01-31', 104, 0), {
    time: '2025-01-31', open: 101, high: 104, low: 99, close: 104, volume: 17,
  });
});

test('history SMA is precomputed once and 10,000 ticks derive no historical arrays', () => {
  const session = new DailyChartSession();
  const generation = session.resetSession();
  session.setActualSession(generation, { stem: '7203', code: '7203', date: '2025-08-01' });
  const token = session.admitRequest();
  const history = Array.from({ length: 500 }, (_, index) => datedBar(index, index + 2));
  assert.equal(session.complete(token, normalizeDailyPayload({ available: true, bars: history })), true);
  const historical25 = session.historicalSma25;
  const historical200 = session.historicalSma200;

  for (let index = 0; index < 10_000; index += 1) {
    session.appendTick('2025-08-01', 100 + (index % 7), index % 2);
  }

  assert.equal(session.metrics.historyPrecomputations, 1);
  assert.equal(session.metrics.terminalDerivations, 0);
  assert.equal(session.historicalSma25, historical25);
  assert.equal(session.historicalSma200, historical200);
  session.deriveTerminal();
  assert.equal(session.metrics.historyPrecomputations, 1);
  assert.equal(session.metrics.terminalDerivations, 1);
});

test('partial SMA terminal points have the selected date and exact hand-calculated values', () => {
  const session = new DailyChartSession();
  const generation = session.resetSession();
  session.setActualSession(generation, { stem: '7203', code: '7203', date: '2025-08-01' });
  const token = session.admitRequest();
  const history = Array.from({ length: 199 }, (_, index) => datedBar(index, index + 2));
  session.complete(token, normalizeDailyPayload({ available: true, bars: history }));
  session.appendTick('2025-08-01', 201, 0);
  session.deriveTerminal();

  assert.deepEqual(session.terminalSma25, { time: '2025-08-01', value: 189 });
  assert.deepEqual(session.terminalSma200, { time: '2025-08-01', value: 101.5 });
});

test('backward rebuild and reset are deterministic and remove old tails', () => {
  const prices = [10, 12, 9, 14];
  const quantities = [1, 2, 3, 4];
  const forward = prices.reduce(
    (partial, price, index) => appendPartialDailyBar(partial, '2025-01-31', price, quantities[index]),
    null,
  );
  assert.deepEqual(forward, buildPartialDailyBar('2025-01-31', prices, quantities, 4));
  assert.deepEqual(buildPartialDailyBar('2025-01-31', prices, quantities, 2), {
    time: '2025-01-31', open: 10, high: 12, low: 10, close: 12, volume: 3,
  });
  assert.equal(buildPartialDailyBar('2025-01-31', prices, quantities, 0), null);
});

test('SMA25 and SMA200 start exactly at their observation boundaries', () => {
  for (const [period, below] of [[25, 24], [200, 199]]) {
    assert.equal(simpleMovingAverage(Array.from({ length: below }, (_, i) => datedBar(i)), period).length, 0);
    const exact = simpleMovingAverage(Array.from({ length: period }, (_, i) => datedBar(i)), period);
    assert.equal(exact.length, 1);
    assert.equal(exact[0].value, (period + 1) / 2);
  }
});

test('a non-empty partial counts as one SMA observation and zero ticks do not', () => {
  const history23 = Array.from({ length: 23 }, (_, i) => datedBar(i));
  const history24 = Array.from({ length: 24 }, (_, i) => datedBar(i));
  const partial25 = datedBar(24, 25);
  assert.equal(buildDailySeries(history23, datedBar(23, 24)).sma25.length, 0);
  assert.equal(buildDailySeries(history24, null).sma25.length, 0);
  assert.equal(buildDailySeries(history24, partial25).sma25.length, 1);

  const history198 = Array.from({ length: 198 }, (_, i) => datedBar(i));
  const history199 = Array.from({ length: 199 }, (_, i) => datedBar(i));
  const partial200 = datedBar(199, 200);
  assert.equal(buildDailySeries(history198, datedBar(198, 199)).sma200.length, 0);
  assert.equal(buildDailySeries(history199, null).sma200.length, 0);
  assert.equal(buildDailySeries(history199, partial200).sma200.length, 1);
});

test('session identity is full actual metadata and never the requested date', () => {
  assert.equal(
    dailySessionIdentity({ stem: '285A', code: '285A0', date: '2025-01-31' }),
    '285A|285A0|2025-01-31',
  );
  assert.equal(dailySessionIdentity({ stem: '285A', date: '2025-01-31' }), null);
});

test('actual metadata may stage a request but cannot commit chart or old ticks before atomic session commit', () => {
  const session = new DailyChartSession();
  const generation = session.resetSession();
  const actual = { stem: '285A', code: '285A0', date: '2025-01-31' };
  const identity = session.stageActualSession(generation, actual);
  const token = session.admitRequest();
  assert.equal(identity, '285A|285A0|2025-01-31');
  assert.equal(session.identity, null);
  assert.equal(session.canAppendTick('7203|7203|2025-01-30'), false);
  assert.equal(session.complete(token, normalizeDailyPayload({ available: true, bars: [bar(1, 10)] })), true);
  assert.equal(session.cache.has(identity), true, 'staged response may cache');
  assert.equal(session.canCommitChart('daily', identity), false, 'staged response cannot paint');
  assert.equal(session.partialBar, null);

  assert.equal(session.commitSession(generation, actual), identity);
  assert.equal(session.canAppendTick(identity), true);
  assert.equal(session.canCommitChart('daily', identity), true);
});

test('daily terminal status publishes exactly once immediately after atomic commit', () => {
  const cases = [
    ['ready', { available: true, bars: [bar(1, 10)] }],
    ['empty', { available: true, bars: [] }],
    ['unavailable', { available: false, bars: [] }],
    ['error', { available: true, bars: [bar(1, 10, { volume: -1 })] }],
  ];
  for (const [expectedPhase, payload] of cases) {
    const session = new DailyChartSession();
    const generation = session.resetSession();
    const meta = { stem: '7203', code: '7203', date: '2025-01-31' };
    session.stageActualSession(generation, meta);
    const token = session.admitRequest();
    session.complete(token, normalizeDailyPayload(payload));
    const events = [];

    const identity = commitDailySession({
      session,
      generation,
      meta,
      commit: () => {
        events.push(['commit-start', session.identity]);
        const committed = session.commitSession(generation, meta);
        events.push(['commit-end', session.identity]);
        return committed;
      },
      publishStatus: (phase) => events.push(['status', phase, session.identity]),
    });

    assert.equal(identity, '7203|7203|2025-01-31');
    assert.deepEqual(events, [
      ['commit-start', null],
      ['commit-end', identity],
      ['status', expectedPhase, identity],
    ]);
  }
});

test('daily URL uses actual metadata and request waits for staging', async () => {
  const session = new DailyChartSession();
  const generation = session.resetSession();
  const calls = [];
  assert.equal(await loadDailyRequest({
    session,
    fetchPayload: async (request) => { calls.push(request); return { available: true, bars: [] }; },
  }), null);
  assert.deepEqual(calls, []);

  const actual = { stem: '285A', code: '285A0', date: '2025-01-30' };
  const identity = session.stageActualSession(generation, actual);
  session.commitSession(generation, actual);
  await loadDailyRequest({
    session,
    getMode: () => 'minute',
    fetchPayload: async (request) => { calls.push(request); return { available: true, bars: [] }; },
  });
  assert.equal(calls[0].identity, identity);
  assert.equal(calls[0].url, dailyContextUrl(identity));
  assert.match(calls[0].url, /date=2025-01-30/);
  assert.doesNotMatch(calls[0].url, /2025-01-31/);
});

test('out-of-order requests cannot change status, cache, or chart across every stale dimension', async () => {
  const variants = [
    { stem: '7203', code: '7203', date: '2025-01-30' },
    { stem: '7203', code: '7203', date: '2025-01-31' },
    { stem: '7203', code: '7203', date: '2025-02-01' },
    { stem: '7203', code: '72030', date: '2025-01-31' },
    { stem: '285A', code: '285A0', date: '2025-01-31' },
  ];
  for (const replacement of variants) {
    const session = new DailyChartSession();
    let generation = session.resetSession();
    const oldMeta = { stem: '7203', code: '7203', date: '2025-01-30' };
    const oldIdentity = session.stageActualSession(generation, oldMeta);
    session.commitSession(generation, oldMeta);
    const oldDeferred = deferred();
    const statuses = [];
    const charts = [];
    const oldRun = loadDailyRequest({
      session,
      getMode: () => 'daily',
      fetchPayload: () => oldDeferred.promise,
      onStatus: (phase) => statuses.push(phase),
      commitChart: (identity) => charts.push(identity),
    });

    generation = session.resetSession();
    const newIdentity = session.stageActualSession(generation, replacement);
    session.commitSession(generation, replacement);
    statuses.length = 0;
    oldDeferred.resolve({ available: true, bars: [bar(1, 10)] });
    const outcome = await oldRun;
    assert.equal(outcome.stale, true);
    assert.deepEqual(statuses, []);
    assert.deepEqual(charts, []);
    assert.equal(session.cache.has(oldIdentity), false);
    assert.equal(session.identity, newIdentity);
  }
});

test('mode switch caches a valid response without chart commit and failure retries successfully', async () => {
  const session = new DailyChartSession();
  const generation = session.resetSession();
  const meta = { stem: '7203', code: '7203', date: '2025-01-31' };
  const identity = session.stageActualSession(generation, meta);
  session.commitSession(generation, meta);
  let mode = 'daily';
  const first = deferred();
  const charts = [];
  const pending = loadDailyRequest({
    session,
    getMode: () => mode,
    fetchPayload: () => first.promise,
    commitChart: (value) => charts.push(value),
  });
  mode = 'minute';
  first.resolve({ available: true, bars: [bar(1, 10)] });
  assert.equal((await pending).accepted, true);
  assert.equal(session.cache.has(identity), true);
  assert.deepEqual(charts, []);

  const retry = new DailyChartSession();
  const retryGeneration = retry.resetSession();
  retry.stageActualSession(retryGeneration, meta);
  retry.commitSession(retryGeneration, meta);
  const statuses = [];
  const failed = await loadDailyRequest({
    session: retry,
    getMode: () => 'daily',
    fetchPayload: async () => { throw new Error('network'); },
    onStatus: (phase) => statuses.push(phase),
  });
  assert.equal(failed.accepted, false);
  assert.equal(retry.phase, 'error');
  assert.equal(retry.cache.size, 0);
  const succeeded = await loadDailyRequest({
    session: retry,
    getMode: () => 'daily',
    fetchPayload: async () => ({ available: true, bars: [bar(1, 10)] }),
    commitChart: (value) => charts.push(value),
  });
  assert.equal(succeeded.committed, true);
  assert.equal(charts.at(-1), identity);
  assert.deepEqual(statuses, ['loading', 'error']);
});

test('recognized cancellation releases the request without cache or terminal error and permits retry', async () => {
  const session = new DailyChartSession();
  const generation = session.resetSession();
  const meta = { stem: '7203', code: '7203', date: '2025-01-31' };
  session.stageActualSession(generation, meta);
  session.commitSession(generation, meta);
  const cancellation = new Error('cancelled');
  const statuses = [];

  const result = await loadDailyRequest({
    session,
    fetchPayload: async () => { throw cancellation; },
    onStatus: (phase) => statuses.push(phase),
    isCancellation: (error) => error === cancellation,
  });

  assert.equal(result.stale, true);
  assert.equal(session.request, null);
  assert.equal(session.cache.size, 0);
  assert.deepEqual(statuses, ['loading']);
  assert.ok(session.admitRequest(), 'the cancelled identity remains retryable');
});

test('failSession removes both identities, request data, partial candle, and SMA tails', () => {
  const session = new DailyChartSession();
  const generation = session.resetSession();
  const meta = { stem: '7203', code: '7203', date: '2025-08-01' };
  session.setActualSession(generation, meta);
  const token = session.admitRequest();
  session.complete(token, normalizeDailyPayload({
    available: true,
    bars: Array.from({ length: 199 }, (_, index) => datedBar(index, index + 2)),
  }));
  session.appendTick(meta.date, 201, 1);
  session.deriveTerminal();
  assert.ok(session.historyBars.length);
  assert.ok(session.partialBar);
  assert.ok(session.terminalSma200);

  assert.equal(session.failSession(generation), true);
  assert.equal(session.stagedIdentity, null);
  assert.equal(session.identity, null);
  assert.equal(session.request, null);
  assert.deepEqual(session.historyBars, []);
  assert.equal(session.partialBar, null);
  assert.deepEqual(session.historicalSma25, []);
  assert.deepEqual(session.historicalSma200, []);
  assert.equal(session.terminalSma25, null);
  assert.equal(session.terminalSma200, null);
});

test('controller rejects every stale identity/generation and admits only one request', () => {
  const session = new DailyChartSession();
  const generation = session.resetSession();
  const identity = session.setActualSession(generation, {
    stem: '285A', code: '285A0', date: '2025-01-31',
  });
  const token = session.admitRequest();
  assert.equal(identity, '285A|285A0|2025-01-31');
  assert.ok(token);
  assert.equal(session.admitRequest(), null);

  session.resetSession();
  assert.equal(session.isCurrent(token), false);
  assert.equal(session.complete(token, normalizeDailyPayload({ available: true, bars: [bar(1, 10)] })), false);
  assert.equal(session.cache.has(identity), false);
});

test('same-stem date changes, code changes, and stem changes all reject late responses', () => {
  const variants = [
    { stem: '7203', code: '7203', date: '2025-01-30' },
    { stem: '7203', code: '72030', date: '2025-01-31' },
    { stem: '285A', code: '285A', date: '2025-01-31' },
  ];
  for (const replacement of variants) {
    const session = new DailyChartSession();
    let generation = session.resetSession();
    const oldIdentity = session.setActualSession(generation, {
      stem: '7203', code: '7203', date: '2025-01-31',
    });
    const token = session.admitRequest();
    generation = session.resetSession();
    session.setActualSession(generation, replacement);
    assert.equal(session.complete(token, normalizeDailyPayload({ available: true, bars: [bar(1, 10)] })), false);
    assert.equal(session.cache.has(oldIdentity), false);
  }
});

test('valid responses cache after a mode switch but chart commits require active current daily', () => {
  const session = new DailyChartSession();
  const generation = session.resetSession();
  const identity = session.setActualSession(generation, { stem: '7203', code: '7203', date: '2025-01-31' });
  const token = session.admitRequest();
  const normalized = normalizeDailyPayload({ available: true, bars: [bar(1, 10)] });
  assert.equal(session.complete(token, normalized), true);
  assert.equal(session.cache.has(identity), true);
  assert.equal(session.canCommitChart('minute', identity), false);
  assert.equal(session.canCommitChart('daily', '7203|7203|2025-01-30'), false);
  assert.equal(session.canCommitChart('daily', identity), true);

  session.resetSession();
  session.setActualSession(session.generation, { stem: '7203', code: '7203', date: '2025-01-31' });
  assert.equal(session.useCached(), true);
  assert.equal(session.admitRequest(), null);
});

test('valid empty history is cached distinctly and remains chart-committable', () => {
  const session = new DailyChartSession();
  const generation = session.resetSession();
  const identity = session.setActualSession(generation, { stem: '7203', code: '7203', date: '2025-01-31' });
  const token = session.admitRequest();
  assert.equal(session.complete(token, normalizeDailyPayload({ available: true, bars: [] })), true);
  assert.equal(session.phase, 'empty');
  assert.equal(session.cache.has(identity), true);
  assert.equal(session.canCommitChart('daily', identity), true);
});

test('unavailable, malformed, and invalid-all responses are not cached and retry', () => {
  for (const payload of [
    { available: false, bars: [] },
    { available: true, bars: [bar(1, 10, { volume: -1 })] },
    { available: true },
  ]) {
    const session = new DailyChartSession();
    const generation = session.resetSession();
    const identity = session.setActualSession(generation, { stem: '7203', code: '7203', date: '2025-01-31' });
    const token = session.admitRequest();
    assert.equal(session.complete(token, normalizeDailyPayload(payload)), false);
    assert.equal(session.cache.has(identity), false);
    assert.ok(session.admitRequest(), 'next explicit daily selection can retry');
  }
});

test('programmatic setData callbacks cannot overwrite either saved viewport', () => {
  for (const shouldFit of [false, true]) {
    const viewport = new ChartViewportState();
    viewport.capture({ from: 10, to: 20 });
    const calls = [];
    const timeScale = {
      setVisibleLogicalRange: (range) => {
        calls.push(['restore', range]);
        viewport.capture({ from: -2, to: -1 });
      },
      fitContent: () => {
        calls.push(['fit']);
        viewport.capture({ from: 0, to: 500 });
      },
    };
    updateChartViewport({
      viewport,
      writeData: () => {
        calls.push(['setData']);
        viewport.capture({ from: 100, to: 200 });
      },
      timeScale,
      restore: !shouldFit,
      fit: shouldFit,
    });
    assert.deepEqual(viewport.savedRange, { from: 10, to: 20 });
    assert.deepEqual(calls, shouldFit
      ? [['setData'], ['fit']]
      : [['setData'], ['restore', { from: 10, to: 20 }]]);
  }
});

test('viewport suppression survives asynchronous callbacks and nested releases drain exactly', async () => {
  const releases = [];
  const viewport = new ChartViewportState((release) => releases.push(release));
  viewport.capture({ from: 10, to: 20 });

  viewport.runProgrammatic(() => {
    viewport.runProgrammatic(() => {
      assert.equal(viewport.capture({ from: 30, to: 40 }), false);
    });
  });
  viewport.runProgrammatic(() => {});
  assert.equal(viewport.programmaticDepth, 3);
  assert.equal(releases.length, 3);

  await Promise.resolve();
  assert.equal(viewport.capture({ from: 100, to: 200 }), false, 'async chart callback stays suppressed');
  releases.shift()();
  assert.equal(viewport.programmaticDepth, 2);
  assert.equal(viewport.capture({ from: 300, to: 400 }), false);
  releases.shift()();
  assert.equal(viewport.programmaticDepth, 1);
  assert.equal(viewport.capture({ from: 500, to: 600 }), false);
  releases.shift()();
  assert.equal(viewport.programmaticDepth, 0);
  assert.equal(viewport.capture({ from: 700, to: 800 }), true);
  assert.deepEqual(viewport.savedRange, { from: 700, to: 800 });
});

test('backward seek and reset replace all four daily series with a fresh exact snapshot', () => {
  function loadedSession() {
    const session = new DailyChartSession();
    const generation = session.resetSession();
    const meta = { stem: '7203', code: '7203', date: '2025-08-01' };
    session.setActualSession(generation, meta);
    const token = session.admitRequest();
    session.complete(token, normalizeDailyPayload({
      available: true,
      bars: Array.from({ length: 199 }, (_, index) => datedBar(index, index + 2)),
    }));
    return session;
  }

  const session = loadedSession();
  session.appendTick('2025-08-01', 500, 8);
  session.deriveTerminal();
  const oldSnapshot = session.snapshot();
  session.clearPartial();
  session.appendTick('2025-08-01', 200, 0);
  session.appendTick('2025-08-01', 180, 3);
  session.deriveTerminal();

  const fresh = loadedSession();
  fresh.appendTick('2025-08-01', 200, 0);
  fresh.appendTick('2025-08-01', 180, 3);
  fresh.deriveTerminal();
  assert.deepEqual(session.snapshot(), fresh.snapshot());
  assert.notDeepEqual(session.snapshot(), oldSnapshot);

  const calls = [];
  renderDailySnapshot(session.snapshot(), {
    candle: { setData: (data) => calls.push(['candle', data]) },
    volume: { setData: (data) => calls.push(['volume', data]) },
    sma25: { setData: (data) => calls.push(['sma25', data]) },
    sma200: { setData: (data) => calls.push(['sma200', data]) },
  });
  assert.deepEqual(calls.map(([owner]) => owner), ['candle', 'volume', 'sma25', 'sma200']);
  assert.equal(calls[0][1].at(-1).high, 200);
  assert.equal(calls[1][1].at(-1).value, 3);
  assert.equal(calls[3][1].at(-1).time, '2025-08-01');
});

test('SMA chart boundaries receive mutable copies while canonical points remain frozen', () => {
  const session = new DailyChartSession();
  const generation = session.resetSession();
  session.setActualSession(generation, { stem: '7203', code: '7203', date: '2025-08-01' });
  const token = session.admitRequest();
  session.complete(token, normalizeDailyPayload({
    available: true,
    bars: Array.from({ length: 199 }, (_, index) => datedBar(index, index + 2)),
  }));
  session.appendTick('2025-08-01', 201, 1);
  session.deriveTerminal();

  const snapshot = session.snapshot();
  const canonical = {
    sma25: snapshot.sma25,
    sma200: snapshot.sma200,
  };
  const before = structuredClone(canonical);
  const received = {};
  const mutatingSeries = (owner) => ({
    setData: (points) => {
      received[owner] = points;
      for (const point of points) point.zb = owner;
    },
  });

  renderDailySnapshot(snapshot, {
    candle: { setData: () => {} },
    volume: { setData: () => {} },
    sma25: mutatingSeries('sma25'),
    sma200: mutatingSeries('sma200'),
  });

  for (const owner of ['sma25', 'sma200']) {
    assert.ok(canonical[owner].length, `${owner} must exercise the chart boundary`);
    assert.equal(Object.isFrozen(canonical[owner]), true);
    canonical[owner].forEach((point, index) => {
      assert.equal(Object.isFrozen(point), true);
      assert.equal(Object.isExtensible(point), false);
      assert.equal(Object.hasOwn(point, 'zb'), false);
      assert.notEqual(received[owner][index], point);
      assert.equal(Object.isExtensible(received[owner][index]), true);
      assert.equal(received[owner][index].zb, owner);
    });
  }
  assert.deepEqual(canonical, before);
  assert.equal(canonical.sma25.at(-1), session.terminalSma25);
  assert.equal(canonical.sma200.at(-1), session.terminalSma200);

  const directCopy = paintedLinePoint(session.terminalSma25);
  assert.notEqual(directCopy, session.terminalSma25);
  assert.equal(Object.isExtensible(directCopy), true);
  directCopy.zb = 'metadata';
  assert.equal(Object.hasOwn(session.terminalSma25, 'zb'), false);
});

test('mutating daily SMA updates cannot interrupt ordered replay consumers', () => {
  const canonical = Object.freeze({
    sma25: Object.freeze({ time: '2025-08-01', value: 189 }),
    sma200: Object.freeze({ time: '2025-08-01', value: 101.5 }),
  });
  const calls = [];

  runReplayFrame({
    mode: 'daily',
    touchedBars: ['bar'],
    from: 4,
    to: 6,
    ports: {
      updateMinute: () => calls.push('minute'),
      deferMinute: () => calls.push('defer-minute'),
      deriveDaily: () => calls.push('derive-daily'),
      updateDaily: () => {
        for (const owner of ['sma25', 'sma200']) {
          const chartPoint = paintedLinePoint(canonical[owner]);
          chartPoint.zb = owner;
          calls.push(owner);
        }
      },
      pushTicks: () => calls.push('ticks'),
      pushTape: () => calls.push('tape'),
      matchOrders: () => calls.push('orders'),
      updateBoard: () => calls.push('board'),
      updatePosition: () => calls.push('position'),
      followTick: () => calls.push('follow'),
      updateClock: () => calls.push('clock'),
      syncScrubber: () => calls.push('scrubber'),
      updateLiveness: () => calls.push('liveness'),
    },
  });

  assert.deepEqual(calls, [
    'defer-minute', 'derive-daily', 'sma25', 'sma200',
    'ticks', 'tape', 'orders', 'board', 'position',
    'follow', 'clock', 'scrubber', 'liveness',
  ]);
  assert.deepEqual(canonical, {
    sma25: { time: '2025-08-01', value: 189 },
    sma200: { time: '2025-08-01', value: 101.5 },
  });
  assert.equal(Object.isFrozen(canonical.sma25), true);
  assert.equal(Object.isFrozen(canonical.sma200), true);
});

test('minute and daily replay frames preserve identical side effects and exact chart owners', () => {
  function run(mode) {
    const calls = [];
    commitReplayFrame({
      mode,
      touchedBars: ['a', 'b'],
      from: 4,
      to: 9,
      ports: {
        updateMinute: (barValue) => calls.push(['minute', barValue]),
        deferMinute: () => calls.push(['defer-minute']),
        deriveDaily: () => calls.push(['derive-daily']),
        updateDaily: () => calls.push(['daily']),
        pushTicks: (from, to) => calls.push(['ticks', from, to]),
        pushTape: (from, to) => calls.push(['tape', from, to]),
        matchOrders: (from, to) => calls.push(['orders', from, to]),
        updateBoard: (from, to) => calls.push(['board', from, to]),
        updatePosition: () => calls.push(['position']),
      },
    });
    return calls;
  }
  const minute = run('minute');
  const daily = run('daily');
  assert.deepEqual(minute.filter(([owner]) => ['ticks', 'tape', 'orders', 'board', 'position'].includes(owner)),
    daily.filter(([owner]) => ['ticks', 'tape', 'orders', 'board', 'position'].includes(owner)));
  assert.deepEqual(minute.slice(0, 3), [['minute', 'a'], ['minute', 'b'], ['derive-daily']]);
  assert.deepEqual(daily.slice(0, 3), [['defer-minute'], ['derive-daily'], ['daily']]);
});

test('runReplayFrame owns the complete no-tick and tick frame order in both chart modes', () => {
  function run(mode, hasTick) {
    const calls = [];
    runReplayFrame({
      mode,
      touchedBars: hasTick ? ['a', 'b'] : [],
      from: 4,
      to: hasTick ? 6 : 4,
      ports: {
        updateMinute: (value) => calls.push(['minute', value]),
        deferMinute: () => calls.push(['defer-minute']),
        deriveDaily: () => calls.push(['derive-daily']),
        updateDaily: () => calls.push(['daily']),
        pushTicks: (from, to) => calls.push(['ticks', from, to]),
        pushTape: (from, to) => calls.push(['tape', from, to]),
        matchOrders: (from, to) => calls.push(['orders', from, to]),
        updateBoard: (from, to) => calls.push(['board', from, to]),
        updatePosition: () => calls.push(['position']),
        followTick: () => calls.push(['follow']),
        updateClock: () => calls.push(['clock']),
        syncScrubber: () => calls.push(['scrubber']),
        updateLiveness: () => calls.push(['liveness']),
      },
    });
    return calls;
  }

  const always = [['follow'], ['clock'], ['scrubber'], ['liveness']];
  assert.deepEqual(run('minute', false), always);
  assert.deepEqual(run('daily', false), always);
  const minute = run('minute', true);
  const daily = run('daily', true);
  assert.deepEqual(minute, [
    ['minute', 'a'], ['minute', 'b'], ['derive-daily'],
    ['ticks', 4, 6], ['tape', 4, 6], ['orders', 4, 6], ['board', 4, 6], ['position'],
    ...always,
  ]);
  assert.deepEqual(daily, [
    ['defer-minute'], ['derive-daily'], ['daily'],
    ['ticks', 4, 6], ['tape', 4, 6], ['orders', 4, 6], ['board', 4, 6], ['position'],
    ...always,
  ]);
  assert.deepEqual(minute.slice(3, 8), daily.slice(3, 8), 'tick side effects are mode-independent');
});

test('daily replay liveness reports state, virtual time, and bounded progress', () => {
  const startTime = Date.UTC(2025, 0, 1, 9, 0, 0) / 1000;
  const endTime = startTime + 100;

  assert.deepEqual(buildReplayLivenessView({
    mode: 'daily', playing: true, virtualTime: startTime + 50, startTime, endTime,
  }), {
    hidden: false,
    stateLabel: '再生中',
    timeLabel: '09:00:50',
    progressPercent: 50,
  });
  assert.deepEqual(buildReplayLivenessView({
    mode: 'daily', playing: false, virtualTime: startTime - 10, startTime, endTime,
  }), {
    hidden: false,
    stateLabel: '一時停止',
    timeLabel: '08:59:50',
    progressPercent: 0,
  });
  assert.equal(buildReplayLivenessView({
    mode: 'daily', playing: true, virtualTime: endTime + 10, startTime, endTime,
  }).progressPercent, 100);
  assert.equal(buildReplayLivenessView({
    mode: 'daily', playing: true, virtualTime: Number.NaN, startTime, endTime,
  }).timeLabel, '--:--:--');
  assert.deepEqual(buildReplayLivenessView({
    mode: 'minute', playing: true, virtualTime: startTime, startTime, endTime,
  }), {
    hidden: true,
    stateLabel: '',
    timeLabel: '',
    progressPercent: 0,
  });
});

test('daily replay progress is zero for invalid or non-positive session spans', () => {
  const startTime = Date.UTC(2025, 0, 1, 9, 0, 0) / 1000;
  for (const endTime of [startTime, startTime - 1, Number.POSITIVE_INFINITY]) {
    assert.equal(buildReplayLivenessView({
      mode: 'daily', playing: true, virtualTime: startTime, startTime, endTime,
    }).progressPercent, 0);
  }
});

test('liveness presenter records only visible whole-second or state changes', () => {
  const writes = [];
  const presenter = new ReplayLivenessPresenter((view) => writes.push(view));
  const base = {
    mode: 'daily',
    playing: true,
    startTime: 1_735_722_000,
    endTime: 1_735_732_000,
  };

  assert.equal(presenter.present({ ...base, virtualTime: base.startTime + 10.1 }), true);
  assert.equal(presenter.present({ ...base, virtualTime: base.startTime + 10.9 }), false);
  assert.equal(writes.length, 1, 'sub-second frames must not rewrite the DOM');
  assert.equal(presenter.present({ ...base, virtualTime: base.startTime + 11.0 }), true);
  assert.equal(presenter.present({ ...base, playing: false, virtualTime: base.startTime + 11.1 }), true);
  assert.equal(presenter.present({ ...base, mode: 'minute', virtualTime: base.startTime + 11.2 }), true);
  assert.equal(writes.at(-1).hidden, true);
  assert.equal(presenter.present({ ...base, mode: 'minute', virtualTime: base.startTime + 20 }), false);
  assert.equal(writes.length, 4, 'hidden minute mode must not record clock-only changes');
});

test('daily minute-history target commits canonical arrays without real hidden-chart writes', () => {
  const minuteBar = (time, close) => ({
    time, open: close, high: close + 1, low: close - 1, close, volume: 10,
  });
  const state = {
    contextBars: [minuteBar(200, 20)],
    bars: [minuteBar(200, 20), minuteBar(260, 21)],
  };
  const realCalls = [];
  const deferredCalls = [];
  const viewport = new ChartViewportState((release) => release());
  viewport.capture({ from: 5, to: 10 });
  const target = selectMinuteHistoryTarget({
    mode: 'daily',
    viewport,
    realTarget: {
      candleSeries: { setData: () => realCalls.push('candle') },
      volumeSeries: { setData: () => realCalls.push('volume') },
      timeScale: {
        getVisibleLogicalRange: () => {
          realCalls.push('get-range');
          return { from: 0, to: 0 };
        },
        setVisibleLogicalRange: () => realCalls.push('set-range'),
      },
      refreshMarkers: () => realCalls.push('markers'),
    },
    deferMinute: () => deferredCalls.push('defer'),
    refreshMarkers: () => deferredCalls.push('markers'),
  });

  const result = applyMinuteHistoryPage({
    state,
    olderBars: [minuteBar(140, 19)],
    ...target,
    paintCandle: (value) => value,
    paintVolume: (value) => ({ time: value.time, value: value.volume }),
    runProgrammatic: (apply) => viewport.runProgrammatic(apply),
  });

  assert.deepEqual(result, { added: 1, committed: true, markerError: null });
  assert.deepEqual(state.contextBars.map(({ time }) => time), [140, 200]);
  assert.deepEqual(state.bars.map(({ time }) => time), [140, 200, 260]);
  assert.deepEqual(realCalls, []);
  assert.deepEqual(deferredCalls, ['defer', 'markers']);
  assert.deepEqual(viewport.savedRange, { from: 6, to: 11 });
});

test('minute return flushes one deferred hidden-chart update exactly once', () => {
  const calls = [];
  let result = flushDeferredMinuteChart({
    mode: 'minute',
    deferred: true,
    flush: () => calls.push('flush'),
  });
  assert.deepEqual(result, { deferred: false, flushed: true });
  result = flushDeferredMinuteChart({
    mode: 'minute',
    deferred: result.deferred,
    flush: () => calls.push('flush'),
  });
  assert.deepEqual(result, { deferred: false, flushed: false });
  assert.deepEqual(calls, ['flush']);
  assert.deepEqual(flushDeferredMinuteChart({ mode: 'daily', deferred: true, flush: () => calls.push('flush') }), {
    deferred: true,
    flushed: false,
  });
});

test('marker writes always reach tickSeries and never reach daily series', () => {
  for (const mode of ['minute', 'daily']) {
    const calls = [];
    commitMarkerWrites({
      mode,
      minuteMarkers: ['minute-marker'],
      tickMarkers: ['tick-marker'],
      ports: {
        minute: (markers) => calls.push(['minute', markers]),
        deferMinute: () => calls.push(['defer-minute']),
        tick: (markers) => calls.push(['tick', markers]),
      },
    });
    assert.deepEqual(calls.at(-1), ['tick', ['tick-marker']]);
    assert.equal(calls.some(([owner]) => owner === 'daily'), false);
    assert.equal(calls[0][0], mode === 'minute' ? 'minute' : 'defer-minute');
  }
});

test('steady-state Daily partial updates are O(1), while an unset initial view retries from only a bar count', () => {
  const steadyEvents = [];
  const steadyDaily = {
    identity: '7203|7203|2025-08-01',
    partialBar: bar(31, 400),
    terminalSma25: { time: '2025-01-31', value: 390 },
    terminalSma200: { time: '2025-01-31', value: 300 },
    hasInitialViewport: true,
    snapshot() { throw new Error('steady-state update must not snapshot history'); },
    get historyBars() { throw new Error('steady-state update must not read historyBars'); },
  };
  const steadyUpdate = compileExtractedFunction(APP_SOURCE, 'updateDailyPartialChart', {
    chartMode: 'daily',
    dailyChart: {},
    state: { daily: steadyDaily },
    dailyCandleSeries: { update: (value) => steadyEvents.push(['candle', value]) },
    dailyVolumeSeries: { update: (value) => steadyEvents.push(['volume', value]) },
    dailySma25Series: { update: (value) => steadyEvents.push(['sma25', value]) },
    dailySma200Series: { update: (value) => steadyEvents.push(['sma200', value]) },
    paintedBar: (value) => ({ ...value }),
    paintedVolume: (value) => ({ time: value.time, value: value.volume }),
    paintedLinePoint,
    applyDailyViewport() { throw new Error('initialized viewport must not be revisited'); },
  });
  steadyUpdate();
  assert.deepEqual(steadyEvents.map(([name]) => name), ['candle', 'volume', 'sma25', 'sma200']);

  const historyReads = [];
  const historyBars = new Proxy({ length: 500 }, {
    get(target, property) {
      historyReads.push(property);
      if (property === 'length') return target.length;
      throw new Error(`history iteration/indexing is not O(1): ${String(property)}`);
    },
  });
  const initialDaily = {
    identity: '7203|7203|2025-08-01',
    phase: 'ready',
    partialBar: bar(31, 400),
    terminalSma25: null,
    terminalSma200: null,
    hasInitialViewport: false,
    historyBars,
    snapshot() { throw new Error('initial partial update must pass only an observation count'); },
    markInitialViewport() {
      this.hasInitialViewport = true;
      return true;
    },
  };
  const ranges = [];
  let rangeAttempts = 0;
  const dailyViewport = {
    savedRange: null,
    runProgrammatic(apply) { return apply(); },
  };
  const dailyChart = {
    timeScale: () => ({
      setVisibleLogicalRange(range) {
        rangeAttempts += 1;
        ranges.push({ ...range });
        if (rangeAttempts === 1) throw new Error('retry initial range');
      },
    }),
  };
  const applyDailyViewport = compileExtractedFunction(APP_SOURCE, 'applyDailyViewport', {
    dailyChart,
    dailyViewport,
    state: { daily: initialDaily },
    dailyInitialLogicalRange,
    console: { warn() {} },
  });
  const initialUpdate = compileExtractedFunction(APP_SOURCE, 'updateDailyPartialChart', {
    chartMode: 'daily',
    dailyChart,
    state: { daily: initialDaily },
    dailyCandleSeries: { update() {} },
    dailyVolumeSeries: { update() {} },
    dailySma25Series: { update() {} },
    dailySma200Series: { update() {} },
    paintedBar: (value) => value,
    paintedVolume: (value) => value,
    paintedLinePoint,
    applyDailyViewport,
  });
  initialUpdate();
  assert.equal(initialDaily.hasInitialViewport, false);
  initialUpdate();
  assert.equal(initialDaily.hasInitialViewport, true);
  assert.deepEqual(ranges, [{ from: 411, to: 505 }, { from: 411, to: 505 }]);
  assert.deepEqual(historyReads, ['length', 'length']);
});

test('Daily initial viewport marks success only and retries history or first-partial range failures', () => {
  function exercise(session, snapshot, expectedRange) {
    const ranges = [];
    let failOnce = true;
    const dailyViewport = {
      savedRange: null,
      programmaticDepth: 0,
      runProgrammatic(apply) {
        this.programmaticDepth += 1;
        try { return apply(); } finally { this.programmaticDepth -= 1; }
      },
    };
    const dailyChart = {
      timeScale: () => ({
        setVisibleLogicalRange(range) {
          assert.equal(dailyViewport.programmaticDepth, 1);
          ranges.push({ ...range });
          if (failOnce) {
            failOnce = false;
            throw new Error('range failed once');
          }
        },
      }),
    };
    const applyDailyViewport = compileExtractedFunction(APP_SOURCE, 'applyDailyViewport', {
      dailyChart,
      dailyViewport,
      state: { daily: session },
      dailyInitialLogicalRange,
      console: { warn() {} },
    });

    assert.equal(applyDailyViewport(snapshot), false);
    assert.equal(session.hasInitialViewport, false);
    assert.equal(applyDailyViewport(snapshot), true);
    assert.equal(session.hasInitialViewport, true);
    assert.deepEqual(ranges, [expectedRange, expectedRange]);
  }

  const history = readyDailySession().session;
  exercise(history, history.snapshot(), { from: 160, to: 254 });

  const partial = new DailyChartSession();
  const generation = partial.resetSession();
  partial.setActualSession(generation, { stem: '7203', code: '7203', date: '2025-08-01' });
  const token = partial.admitRequest();
  partial.complete(token, normalizeDailyPayload({ available: true, bars: [] }));
  partial.appendTick('2025-08-01', 100, 1);
  exercise(partial, partial.snapshot(), { from: 0, to: 5 });
});

test('Daily logical-range callback captures users but suppresses inactive and programmatic events', () => {
  function run({ mode, depth, barsBefore }) {
    const events = [];
    const dailyViewport = {
      programmaticDepth: depth,
      capture(range) { events.push(['capture', range]); },
    };
    const state = {
      meta: { stem: '7203', code: '7203', date: '2025-08-01' },
      daily: {
        identity: '7203|7203|2025-08-01',
        canLoadOlder(input) {
          events.push(['can-load', input]);
          return true;
        },
      },
    };
    const callback = compileExtractedFunction(APP_SOURCE, 'onDailyLogicalRangeChange', {
      chartMode: mode,
      dailyViewport,
      dailyCandleSeries: {
        barsInLogicalRange(range) {
          events.push(['bars-before', range]);
          return { barsBefore };
        },
      },
      state,
      dailySessionIdentity,
      loadOlderDailyBars() { events.push(['load']); },
    });
    callback({ from: 1, to: 20 });
    return events;
  }

  assert.deepEqual(run({ mode: 'minute', depth: 0, barsBefore: 0 }), []);
  assert.deepEqual(run({ mode: 'daily', depth: 1, barsBefore: 0 }), [
    ['capture', { from: 1, to: 20 }],
  ]);
  assert.deepEqual(run({ mode: 'daily', depth: 0, barsBefore: 10 }), [
    ['capture', { from: 1, to: 20 }],
    ['bars-before', { from: 1, to: 20 }],
    ['can-load', {
      identity: '7203|7203|2025-08-01',
      barsBefore: 10,
      programmatic: false,
    }],
    ['load'],
  ]);
});

test('older Daily completion rejects every stale identity, generation, cutoff, token, and post-await meta dimension', async () => {
  const normalizedOlder = normalizeDailyPayload({ available: true, bars: [datedBar(-1, 90)] });
  const controllerCases = [
    ['stem', (session, token) => { session.identity = '9984|7203|2025-08-01'; return token; }],
    ['code', (session, token) => { session.identity = '7203|72030|2025-08-01'; return token; }],
    ['actualDate', (session, token) => { session.identity = '7203|7203|2025-08-02'; return token; }],
    ['generation', (session, token) => { session.generation += 1; return token; }],
    ['cutoff', (session, token) => {
      const stale = Object.freeze({ ...token, cutoff: '2023-12-01' });
      session.historyPageRequest = stale;
      return stale;
    }],
    ['token', (_session, token) => Object.freeze({ ...token })],
  ];

  for (const [label, makeStale] of controllerCases) {
    const { session } = readyDailySession();
    session.armHistory();
    const token = session.admitOlderPage(session.historyBars[0].time);
    const candidate = makeStale(session, token);
    const before = {
      phase: session.phase,
      bars: session.historyBars,
      request: session.historyPageRequest,
      exhausted: session.historyExhausted,
      failures: [...session.historyFailures.entries()],
      cache: [...session.cache.entries()],
      metrics: { ...session.metrics },
    };
    assert.equal(session.isCurrentOlderPage(candidate), false, label);
    assert.deepEqual(session.prepareOlderPage(candidate, normalizedOlder), { kind: 'stale' }, label);
    assert.equal(session.phase, before.phase, label);
    assert.equal(session.historyBars, before.bars, label);
    assert.equal(session.historyPageRequest, before.request, label);
    assert.equal(session.historyExhausted, before.exhausted, label);
    assert.deepEqual([...session.historyFailures.entries()], before.failures, label);
    assert.deepEqual([...session.cache.entries()], before.cache, label);
    assert.deepEqual(session.metrics, before.metrics, label);
  }

  const loaderCases = [
    ['stem', ({ session }) => { session.identity = '9984|7203|2025-08-01'; }],
    ['code', ({ session }) => { session.identity = '7203|72030|2025-08-01'; }],
    ['actualDate', ({ session }) => { session.identity = '7203|7203|2025-08-02'; }],
    ['generation', ({ session }) => { session.generation += 1; }],
    ['cutoff', ({ session }) => {
      session.historyBars = Object.freeze([datedBar(-2, 89), ...session.historyBars]);
    }],
    ['token', ({ session }) => {
      session.historyPageRequest = Object.freeze({ ...session.historyPageRequest });
    }],
    ['post-await state.meta', ({ state }) => {
      state.meta = { ...state.meta, date: '2025-08-02' };
    }],
  ];

  for (const [label, makeStale] of loaderCases) {
    const { session, meta, identity } = readyDailySession();
    session.armHistory(identity);
    const state = { daily: session, meta: { ...meta } };
    const gate = deferred();
    const calls = {
      normalize: 0,
      prepare: 0,
      chart: 0,
      hidden: 0,
      failure: 0,
      exhaustion: 0,
    };
    const originalPrepare = session.prepareOlderPage.bind(session);
    const originalFailure = session.failOlderPage.bind(session);
    const originalExhaustion = session.completeOlderExhausted.bind(session);
    session.prepareOlderPage = (...args) => {
      calls.prepare += 1;
      return originalPrepare(...args);
    };
    session.failOlderPage = (...args) => {
      calls.failure += 1;
      return originalFailure(...args);
    };
    session.completeOlderExhausted = (...args) => {
      calls.exhaustion += 1;
      return originalExhaustion(...args);
    };
    const load = compileExtractedFunction(APP_SOURCE, 'loadOlderDailyBars', {
      state,
      dailySessionIdentity,
      dailyContextUrl,
      requests: { fetchLatest: () => gate.promise },
      normalizeDailyPayload(payload, limit) {
        calls.normalize += 1;
        return normalizeDailyPayload(payload, limit);
      },
      chartMode: 'daily',
      dailyChart: { timeScale: () => ({}) },
      applyDailyHistoryPage() { calls.chart += 1; },
      dailyViewport: {},
      dailyCandleSeries: {},
      dailyVolumeSeries: {},
      dailySma25Series: {},
      dailySma200Series: {},
      paintedBar: (value) => value,
      paintedVolume: (value) => value,
      commitInactiveDailyHistoryPage() { calls.hidden += 1; },
      isCancellationError: () => false,
    });

    const completion = load();
    assert.ok(session.historyPageRequest, `${label}: request admitted`);
    makeStale({ session, state });
    const before = {
      bars: session.historyBars,
      request: session.historyPageRequest,
      exhausted: session.historyExhausted,
      failures: [...session.historyFailures.entries()],
      cache: [...session.cache.entries()],
      metrics: { ...session.metrics },
    };
    gate.resolve({ available: true, bars: [datedBar(-1, 90)] });
    assert.deepEqual(await completion, { kind: 'stale' }, label);
    assert.deepEqual(calls, {
      normalize: 0,
      prepare: 0,
      chart: 0,
      hidden: 0,
      failure: 0,
      exhaustion: 0,
    }, label);
    assert.equal(session.historyBars, before.bars, label);
    assert.equal(session.historyPageRequest, before.request, label);
    assert.equal(session.historyExhausted, before.exhausted, label);
    assert.deepEqual([...session.historyFailures.entries()], before.failures, label);
    assert.deepEqual([...session.cache.entries()], before.cache, label);
    assert.deepEqual(session.metrics, before.metrics, label);
  }
});

test('older Daily request admits before fetch and rechecks token and full identity before normalization', async () => {
  const identity = '7203|7203|2025-08-01';
  const events = [];
  const token = Object.freeze({ id: 1, identity, generation: 1, cutoff: '2024-01-01' });
  const state = {
    meta: { stem: '7203', code: '7203', date: '2025-08-01' },
    daily: {
      identity,
      historyBars: [datedBar(0, 100)],
      historyPageSize: 200,
      admitOlderPage(cutoff, options) {
        events.push(['admit', cutoff, options]);
        return token;
      },
      isCurrentOlderPage(candidate) {
        events.push(['current', candidate]);
        return candidate === token;
      },
      prepareOlderPage(candidate, normalized) {
        events.push(['prepare', candidate, normalized]);
        return { kind: 'exhausted', token: candidate };
      },
      completeOlderExhausted(candidate) { events.push(['exhausted', candidate]); },
      failOlderPage(candidate) { events.push(['fail', candidate]); },
      abortOlderPage(candidate) { events.push(['abort', candidate]); },
    },
  };
  let identityChecks = 0;
  const load = compileExtractedFunction(APP_SOURCE, 'loadOlderDailyBars', {
    state,
    dailySessionIdentity(meta) {
      identityChecks += 1;
      events.push(['identity', identityChecks, meta]);
      return identity;
    },
    dailyContextUrl,
    requests: {
      async fetchLatest(kind, url, options) {
        events.push(['fetch', kind, url, options]);
        return { available: true, bars: [] };
      },
    },
    normalizeDailyPayload(payload, limit) {
      events.push(['normalize', payload, limit]);
      return normalizeDailyPayload(payload, limit);
    },
    chartMode: 'daily',
    dailyChart: null,
    applyDailyHistoryPage() { throw new Error('active apply must not run'); },
    dailyViewport: {},
    dailyCandleSeries: null,
    dailyVolumeSeries: null,
    dailySma25Series: null,
    dailySma200Series: null,
    paintedBar: (value) => value,
    paintedVolume: (value) => value,
    commitInactiveDailyHistoryPage() { throw new Error('empty page must not commit'); },
    isCancellationError: () => false,
  });

  assert.deepEqual(await load(), { kind: 'exhausted', token });
  assert.deepEqual(events.map(([name]) => name), [
    'identity', 'admit', 'fetch', 'current', 'identity', 'normalize', 'prepare', 'exhausted',
  ]);
  assert.deepEqual(events[1], ['admit', '2024-01-01', { identity }]);
  assert.deepEqual(events[2], [
    'fetch',
    'daily-context',
    '/api/daily-context?stem=7203&date=2024-01-01&limit=200',
    { stem: '7203' },
  ]);
  assert.equal(events.find(([name]) => name === 'normalize')[2], 200);
});

test('older Daily request maps stale, cancellation, apply failure, and inactive completion safely', async () => {
  const identity = '7203|7203|2025-08-01';
  const token = Object.freeze({ id: 1, identity, generation: 1, cutoff: '2024-01-01' });

  async function run({ current = true, cancellation = false, active = true, applyFails = false } = {}) {
    const events = [];
    const state = {
      meta: { stem: '7203', code: '7203', date: '2025-08-01' },
      daily: {
        identity,
        historyBars: [datedBar(0, 100)],
        historyPageSize: 200,
        admitOlderPage: () => token,
        isCurrentOlderPage: () => current,
        prepareOlderPage: () => ({ kind: 'page', token, added: 2 }),
        failOlderPage: () => events.push('fail'),
        abortOlderPage: () => events.push('abort'),
        completeOlderExhausted: () => events.push('exhausted'),
      },
    };
    const load = compileExtractedFunction(APP_SOURCE, 'loadOlderDailyBars', {
      state,
      dailySessionIdentity: () => identity,
      dailyContextUrl,
      requests: {
        async fetchLatest() {
          if (cancellation) throw new Error('cancelled');
          return { available: true, bars: [datedBar(-1, 90)] };
        },
      },
      normalizeDailyPayload,
      chartMode: active ? 'daily' : 'minute',
      dailyChart: active ? { timeScale: () => ({}) } : null,
      applyDailyHistoryPage() {
        events.push('apply');
        if (applyFails) throw new Error('apply failed');
      },
      dailyViewport: {},
      dailyCandleSeries: {},
      dailyVolumeSeries: {},
      dailySma25Series: {},
      dailySma200Series: {},
      paintedBar: (value) => value,
      paintedVolume: (value) => value,
      commitInactiveDailyHistoryPage() { events.push('inactive-commit'); },
      isCancellationError: () => cancellation,
    });
    return { result: await load(), events };
  }

  assert.deepEqual(await run({ current: false }), { result: { kind: 'stale' }, events: [] });
  assert.deepEqual(await run({ cancellation: true }), {
    result: { kind: 'cancelled' },
    events: ['abort'],
  });
  const failed = await run({ applyFails: true });
  assert.equal(failed.result.kind, 'failure');
  assert.deepEqual(failed.events, ['apply', 'fail']);
  const inactive = await run({ active: false });
  assert.equal(inactive.result.kind, 'page');
  assert.deepEqual(inactive.events, ['inactive-commit']);
});

test('in-flight older page commits terminal SMA from the latest partial-day close exactly once', async () => {
  const { session, meta, identity } = readyDailySession();
  session.appendTick(meta.date, 400, 10);
  session.deriveTerminal();
  const oldTerminal25 = session.terminalSma25;
  const oldTerminal200 = session.terminalSma200;
  const beforePrecomputations = session.metrics.historyPrecomputations;
  assert.equal(session.armHistory(identity), true);

  const gate = deferred();
  const dailyViewport = {
    savedRange: Object.freeze({ from: 5, to: 15 }),
    replace(range) { this.savedRange = range ? Object.freeze({ ...range }) : null; },
  };
  const state = { daily: session, meta: { ...meta } };
  const commitInactiveDailyHistoryPage = compileExtractedFunction(
    APP_SOURCE,
    'commitInactiveDailyHistoryPage',
    { dailyViewport, shiftDailyLogicalRange, state },
  );
  const hiddenWrite = () => { throw new Error('in-flight inactive completion touched a hidden series'); };
  const load = compileExtractedFunction(APP_SOURCE, 'loadOlderDailyBars', {
    state,
    dailySessionIdentity,
    dailyContextUrl,
    requests: { fetchLatest: () => gate.promise },
    normalizeDailyPayload,
    chartMode: 'minute',
    dailyChart: null,
    applyDailyHistoryPage: hiddenWrite,
    dailyViewport,
    dailyCandleSeries: { setData: hiddenWrite },
    dailyVolumeSeries: { setData: hiddenWrite },
    dailySma25Series: { setData: hiddenWrite },
    dailySma200Series: { setData: hiddenWrite },
    paintedBar: (value) => value,
    paintedVolume: (value) => value,
    commitInactiveDailyHistoryPage,
    isCancellationError: () => false,
  });

  const completion = load();
  assert.ok(session.historyPageRequest);
  session.appendTick(meta.date, 450, 5);
  assert.equal(session.partialBar.close, 450);
  assert.equal(session.terminalSma25, oldTerminal25, 'terminal remains old until the frame/page derives it');
  assert.equal(session.terminalSma200, oldTerminal200);
  gate.resolve({
    available: true,
    bars: [datedBar(-1, 90), datedBar(-2, 89), datedBar(-3, 88)],
  });

  const result = await completion;
  assert.equal(result.kind, 'page');
  assert.equal(session.metrics.historyPrecomputations, beforePrecomputations + 1);
  const expected25 = (
    session.historyBars.slice(-24).reduce((sum, item) => sum + item.close, 0) + 450
  ) / 25;
  const expected200 = (
    session.historyBars.slice(-199).reduce((sum, item) => sum + item.close, 0) + 450
  ) / 200;
  assert.deepEqual(session.terminalSma25, { time: meta.date, value: expected25 });
  assert.deepEqual(session.terminalSma200, { time: meta.date, value: expected200 });
  const snapshot = session.snapshot();
  assert.equal(snapshot.bars.at(-1).close, 450);
  assert.equal(snapshot.bars.filter(({ time }) => time === meta.date).length, 1);
  assert.equal(snapshot.bars.length, session.historyBars.length + 1);
});

test('inactive Daily page commits canonical state with one saved-range shift and restores on rejection', () => {
  {
    const { session } = readyDailySession();
    const { plan } = prepareOlderPlan(session, 4);
    const replacements = [];
    const dailyViewport = {
      savedRange: Object.freeze({ from: 5, to: 15 }),
      replace(range) {
        replacements.push(range ? { ...range } : null);
        this.savedRange = range ? Object.freeze({ ...range }) : null;
      },
    };
    const commitInactive = compileExtractedFunction(APP_SOURCE, 'commitInactiveDailyHistoryPage', {
      dailyViewport,
      shiftDailyLogicalRange,
      state: { daily: session },
    });
    assert.deepEqual(commitInactive(plan), { accepted: true, added: 4 });
    assert.deepEqual(replacements, [{ from: 9, to: 19 }]);
    assert.deepEqual(dailyViewport.savedRange, { from: 9, to: 19 });
    assert.equal(session.historyBars.length, 254);
  }

  {
    const replacements = [];
    const dailyViewport = {
      savedRange: Object.freeze({ from: 1, to: 2 }),
      replace(range) {
        replacements.push(range ? { ...range } : null);
        this.savedRange = range ? Object.freeze({ ...range }) : null;
      },
    };
    const commitInactive = compileExtractedFunction(APP_SOURCE, 'commitInactiveDailyHistoryPage', {
      dailyViewport,
      shiftDailyLogicalRange,
      state: { daily: { commitOlderPage: () => ({ accepted: false, added: 0 }) } },
    });
    assert.throws(() => commitInactive({ added: 3 }), /rejected/);
    assert.deepEqual(replacements, [{ from: 4, to: 5 }, { from: 1, to: 2 }]);
    assert.deepEqual(dailyViewport.savedRange, { from: 1, to: 2 });
  }
});

test('Daily-to-Minute in-flight completion stays hidden, then Daily return renders canonical page at shifted range', async () => {
  const { session, meta, identity } = readyDailySession();
  session.hasInitialViewport = true;
  assert.equal(session.armHistory(identity), true);
  let canonicalCommits = 0;
  const originalCommit = session.commitOlderPage.bind(session);
  session.commitOlderPage = (plan) => {
    canonicalCommits += 1;
    return originalCommit(plan);
  };

  const replacements = [];
  const visibleRanges = [];
  const seriesWrites = [];
  const dailyViewport = {
    savedRange: Object.freeze({ from: 10, to: 20 }),
    runProgrammatic(apply) { return apply(); },
    replace(range) {
      replacements.push(range ? { ...range } : null);
      this.savedRange = range ? Object.freeze({ ...range }) : null;
    },
  };
  const dailyChart = {
    timeScale: () => ({
      setVisibleLogicalRange(range) { visibleRanges.push({ ...range }); },
    }),
  };
  const series = Object.fromEntries(
    ['candle', 'volume', 'sma25', 'sma200'].map((name) => [name, {
      setData(data) { seriesWrites.push([name, structuredClone(data)]); },
    }]),
  );
  const state = { daily: session, meta: { ...meta } };
  const commitInactiveDailyHistoryPage = compileExtractedFunction(
    APP_SOURCE,
    'commitInactiveDailyHistoryPage',
    { dailyViewport, shiftDailyLogicalRange, state },
  );
  const gate = deferred();
  const modeRef = { value: 'daily' };
  const loadDependencies = {
    modeRef,
    state,
    dailySessionIdentity,
    dailyContextUrl,
    requests: { fetchLatest: () => gate.promise },
    normalizeDailyPayload,
    dailyChart,
    applyDailyHistoryPage({ ports }) {
      ports.candle.setData([]);
      ports.volume.setData([]);
      ports.sma25.setData([]);
      ports.sma200.setData([]);
    },
    dailyViewport,
    dailyCandleSeries: series.candle,
    dailyVolumeSeries: series.volume,
    dailySma25Series: series.sma25,
    dailySma200Series: series.sma200,
    paintedBar: (value) => ({ ...value }),
    paintedVolume: (value) => ({ time: value.time, value: value.volume }),
    commitInactiveDailyHistoryPage,
    isCancellationError: () => false,
  };
  const dynamicLoaderSource = extractFunction(APP_SOURCE, 'loadOlderDailyBars')
    .replace(/\bchartMode\b/g, 'modeRef.value');
  const load = Function(
    ...Object.keys(loadDependencies),
    `return (${dynamicLoaderSource});`,
  )(...Object.values(loadDependencies));

  const completion = load();
  assert.equal(modeRef.value, 'daily');
  assert.ok(session.historyPageRequest, 'the page was admitted while Daily was active');
  modeRef.value = 'minute';
  gate.resolve({ available: true, bars: [datedBar(-1, 90), datedBar(-2, 89)] });
  assert.equal((await completion).kind, 'page');
  assert.equal(canonicalCommits, 1);
  assert.deepEqual(seriesWrites, [], 'hidden Daily series were not touched');
  assert.deepEqual(replacements, [{ from: 12, to: 22 }]);
  assert.deepEqual(dailyViewport.savedRange, { from: 12, to: 22 });
  assert.equal(session.historyBars.length, 252);

  modeRef.value = 'daily';
  const applyDailyViewport = compileExtractedFunction(APP_SOURCE, 'applyDailyViewport', {
    dailyChart,
    dailyViewport,
    state,
    dailyInitialLogicalRange,
    console: { warn() {} },
  });
  const renderDailyChart = compileExtractedFunction(APP_SOURCE, 'renderDailyChart', {
    chartMode: modeRef.value,
    state,
    ensureDailyChart: () => dailyChart,
    updateChartViewport,
    dailyViewport,
    renderDailySnapshot,
    dailyCandleSeries: series.candle,
    dailyVolumeSeries: series.volume,
    dailySma25Series: series.sma25,
    dailySma200Series: series.sma200,
    paintedBar: (value) => ({ ...value }),
    paintedVolume: (value) => ({ time: value.time, value: value.volume }),
    dailyChart,
    applyDailyViewport,
  });
  renderDailyChart({ restoreRange: true });

  assert.deepEqual(seriesWrites.map(([name]) => name), ['candle', 'volume', 'sma25', 'sma200']);
  const snapshot = session.snapshot();
  assert.equal(seriesWrites[0][1].length, snapshot.bars.length);
  assert.equal(seriesWrites[1][1].length, snapshot.bars.length);
  assert.equal(seriesWrites[2][1].length, snapshot.sma25.length);
  assert.equal(seriesWrites[3][1].length, snapshot.sma200.length);
  assert.deepEqual(visibleRanges, [{ from: 12, to: 22 }]);
  assert.deepEqual(replacements, [{ from: 12, to: 22 }], 'return does not shift twice');
  assert.equal(canonicalCommits, 1);
});

test('Daily paging gesture and lifecycle wiring is user-only, cancellable, and never redraws replay', () => {
  const gestureBlock = APP_SOURCE.match(/\['wheel', 'pointerdown', 'touchstart'\][\s\S]*?\n\}\);/)?.[0] || '';
  assert.match(gestureBlock, /els\.dailyChart\.addEventListener/);
  assert.match(gestureBlock, /state\.daily\.armHistory\(dailySessionIdentity\(state\.meta\)\)/);
  assert.match(gestureBlock, /passive: true/);
  assert.doesNotMatch(extractFunction(APP_SOURCE, 'setChartMode'), /armHistory/);

  const loader = extractFunction(APP_SOURCE, 'loadOlderDailyBars');
  assert.match(loader, /state\.daily\.admitOlderPage/);
  assert.match(loader, /requests\.fetchLatest\('daily-context'/);
  assert.doesNotMatch(loader, /redrawAll\(/);
  const loadSessionSource = extractFunction(APP_SOURCE, 'loadSession');
  const firstAwait = loadSessionSource.indexOf('await requests.fetchUntilReady(');
  assert.ok(loadSessionSource.indexOf("requests.cancel('daily-context')") < firstAwait);
  assert.ok(loadSessionSource.indexOf('state.daily.resetSession()') < firstAwait);
  assert.ok(loadSessionSource.indexOf('dailyViewport.replace(null)') < firstAwait);
});

test('separate DOM, lazy second chart, four daily series, and no switch destruction are explicit', () => {
  assert.match(HTML_SOURCE, /id="price-chart-stage"/);
  assert.match(HTML_SOURCE, /id="minute-chart"/);
  assert.match(HTML_SOURCE, /id="daily-chart"/);
  assert.match(HTML_SOURCE, /role="tablist"/);
  assert.match(HTML_SOURCE, /aria-live="polite"/);
  assert.match(STYLE_SOURCE, /\.price-chart-stage/);
  assert.match(STYLE_SOURCE, /\.chart-pane\.is-active/);

  const ensure = extractFunction(APP_SOURCE, 'ensureDailyChart');
  assert.match(ensure, /if \(dailyChart\) return dailyChart/);
  assert.match(ensure, /LightweightCharts\.createChart\(els\.dailyChart/);
  assert.equal((ensure.match(/addCandlestickSeries\(/g) || []).length, 1);
  assert.equal((ensure.match(/addHistogramSeries\(/g) || []).length, 1);
  assert.equal((ensure.match(/addLineSeries\(/g) || []).length, 2);
  assert.match(ensure, /subscribeVisibleLogicalRangeChange/);
  const resize = extractFunction(APP_SOURCE, 'applyChartStageSize');
  assert.match(resize, /minuteChart\.resize/);
  assert.match(resize, /dailyChart\.resize/);
  const switcher = extractFunction(APP_SOURCE, 'setChartMode');
  assert.match(switcher, /minuteViewport\.capture/);
  assert.match(switcher, /dailyViewport\.capture/);
  assert.match(switcher, /flushDeferredMinuteChart\(\{/);
  assert.doesNotMatch(switcher, /\.remove\(/);
});

test('chart writes are isolated at leaves while replay and marker side effects remain', () => {
  const step = extractFunction(APP_SOURCE, 'step');
  assert.equal((step.match(/runReplayFrame\(\{/g) || []).length, 1);
  assert.match(step, /commitReplayFrame\(\{/);
  assert.match(step, /updateDaily: updateDailyPartialChart/);
  assert.match(step, /pushTicks: pushTickPoints/);
  assert.match(step, /pushTape: pushTapeRows/);
  assert.match(step, /matchOrders,/);
  assert.match(step, /updateBoard: applyTradesToBoard/);
  assert.match(step, /updatePosition: updatePositionPanel/);
  assert.match(step, /followTick: \(\) => followTickView\(\)/);
  assert.match(step, /updateClock: \(\) => updateClock\(\)/);
  assert.match(step, /syncScrubber: \(\) => syncScrubber\(\)/);

  const dailyUpdate = extractFunction(APP_SOURCE, 'updateDailyPartialChart');
  assert.match(dailyUpdate, /dailySma25Series\.update\(paintedLinePoint\(state\.daily\.terminalSma25\)\)/);
  assert.match(dailyUpdate, /dailySma200Series\.update\(paintedLinePoint\(state\.daily\.terminalSma200\)\)/);

  const redraw = extractFunction(APP_SOURCE, 'redrawAll');
  assert.match(redraw, /renderMinuteChart/);
  assert.match(redraw, /renderDailyChart/);
  assert.match(redraw, /rebuildTape\(\)/);
  assert.match(redraw, /updateBoard\(\)/);

  const markers = extractFunction(APP_SOURCE, 'refreshMarkers');
  assert.match(markers, /commitMarkerWrites\(\{/);
  assert.match(markers, /tick: \(markers\) => tickSeries\.setMarkers\(markers\)/);
  assert.doesNotMatch(markers, /dailyCandleSeries\.setMarkers/);

  const historyTarget = extractFunction(APP_SOURCE, 'minuteHistoryChartTarget');
  assert.match(historyTarget, /if \(chartMode === 'minute'\)/);
  assert.match(historyTarget, /selectMinuteHistoryTarget\(\{/);
  assert.match(historyTarget, /minuteChartDeferred = true/);
});

test('daily liveness is wired to frame housekeeping without changing play state on chart switch', () => {
  assert.match(HTML_SOURCE, /id="daily-replay-liveness"[^>]*class="daily-replay-liveness"[^>]*hidden/);
  assert.match(HTML_SOURCE, /id="daily-replay-state"/);
  assert.match(HTML_SOURCE, /id="daily-replay-time"/);
  assert.match(HTML_SOURCE, /id="daily-replay-progress"[^>]*max="100"[^>]*value="0"/);
  assert.doesNotMatch(
    HTML_SOURCE.match(/id="daily-replay-liveness"[\s\S]*?<\/div>/)?.[0] || '',
    /aria-live/,
  );
  assert.match(STYLE_SOURCE, /\.daily-replay-liveness/);

  const step = extractFunction(APP_SOURCE, 'step');
  assert.match(step, /updateLiveness: \(\) => updateDailyReplayLiveness\(\)/);
  const switcher = extractFunction(APP_SOURCE, 'setChartMode');
  assert.match(switcher, /updateDailyReplayLiveness\(\)/);
  assert.doesNotMatch(switcher, /state\.playing\s*=/);
  assert.doesNotMatch(switcher, /setPlaying\s*\(/);
  const setPlaying = extractFunction(APP_SOURCE, 'setPlaying');
  assert.match(setPlaying, /updateDailyReplayLiveness\(\)/);
});

test('daily session reset precedes awaits and staged metadata commits only after atomic state install', () => {
  const source = extractFunction(APP_SOURCE, 'loadSession');
  const cancel = source.indexOf("requests.cancel('daily-context')");
  const reset = source.indexOf('state.daily.resetSession()');
  const firstAwait = source.indexOf('await requests.fetchUntilReady(');
  assert.ok(cancel !== -1 && cancel < firstAwait);
  assert.ok(reset !== -1 && reset < firstAwait);
  const stage = source.indexOf('state.daily.stageActualSession(dailyGeneration, nextMeta)');
  const minuteAwait = source.indexOf('await loadMinuteContextBars(');
  const contextInstall = source.indexOf('state.contextBars = contextBars');
  const commit = source.indexOf('state.daily.commitSession(dailyGeneration, nextMeta)', contextInstall);
  assert.ok(stage > firstAwait && stage < minuteAwait);
  assert.ok(contextInstall > minuteAwait && commit > contextInstall);
  assert.equal((source.match(/commitDailySession\(\{/g) || []).length, 2);
  assert.equal((source.match(/publishStatus: refreshDailyStatus/g) || []).length, 2);
  assert.doesNotMatch(source, /daily-context[^\n]+date/);
});

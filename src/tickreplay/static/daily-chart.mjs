const DEFAULT_DAILY_LIMIT = 500;
const DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;
const DAILY_PAGE_PLAN_OWNER = Symbol('daily-page-plan-owner');

export const DAILY_HISTORY_DEFAULTS = Object.freeze({
  visibleBars: 90,
  rightPaddingBars: 5,
  pageSize: 200,
  edgeThresholdBars: 10,
  failureCooldownMs: 5000,
  maxFailuresPerCutoff: 3,
});

function formatReplayTime(seconds) {
  if (!Number.isFinite(seconds)) return '--:--:--';
  const date = new Date(seconds * 1000);
  const pad = (value) => String(value).padStart(2, '0');
  return pad(date.getUTCHours()) + ':' + pad(date.getUTCMinutes()) + ':' + pad(date.getUTCSeconds());
}

function replayProgress(virtualTime, startTime, endTime) {
  if (![virtualTime, startTime, endTime].every(Number.isFinite) || endTime <= startTime) return 0;
  const percent = Math.round(((virtualTime - startTime) / (endTime - startTime)) * 100);
  return Math.max(0, Math.min(100, percent));
}

export function buildReplayLivenessView({ mode, playing, virtualTime, startTime, endTime }) {
  if (mode !== 'daily') {
    return Object.freeze({ hidden: true, stateLabel: '', timeLabel: '', progressPercent: 0 });
  }
  return Object.freeze({
    hidden: false,
    stateLabel: playing ? '再生中' : '一時停止',
    timeLabel: formatReplayTime(virtualTime),
    progressPercent: replayProgress(virtualTime, startTime, endTime),
  });
}

export class ReplayLivenessPresenter {
  constructor(write) {
    this.write = write;
    this.lastView = null;
  }

  present(input) {
    const view = buildReplayLivenessView(input);
    if (
      this.lastView &&
      this.lastView.hidden === view.hidden &&
      this.lastView.stateLabel === view.stateLabel &&
      this.lastView.timeLabel === view.timeLabel &&
      this.lastView.progressPercent === view.progressPercent
    ) {
      return false;
    }
    this.lastView = view;
    this.write(view);
    return true;
  }
}

function isPlainObject(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return false;
  try {
    const prototype = Object.getPrototypeOf(value);
    return prototype === Object.prototype || prototype === null;
  } catch {
    return false;
  }
}

function isIsoDate(value) {
  if (typeof value !== 'string' || !DATE_PATTERN.test(value)) return false;
  const parsed = new Date(value + 'T00:00:00Z');
  return Number.isFinite(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value;
}

function validPrice(value) {
  return typeof value === 'number' && Number.isFinite(value) && value > 0;
}

function validVolume(value) {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0;
}

function normalizeDailyBar(value) {
  if (!isPlainObject(value) || !isIsoDate(value.time)) return null;
  const { time, open, high, low, close, volume } = value;
  if (![open, high, low, close].every(validPrice) || !validVolume(volume)) return null;
  if (low > Math.min(open, close) || Math.max(open, close) > high) return null;
  return Object.freeze({ time, open, high, low, close, volume });
}

function positiveInteger(value, fallback) {
  return Number.isInteger(value) && value > 0 ? value : fallback;
}

function nonNegativeInteger(value, fallback) {
  return Number.isInteger(value) && value >= 0 ? value : fallback;
}

export function dailyInitialLogicalRange(barCount, options = {}) {
  if (!Number.isInteger(barCount) || barCount <= 0) return null;
  const visibleBars = positiveInteger(options?.visibleBars, DAILY_HISTORY_DEFAULTS.visibleBars);
  const rightPaddingBars = nonNegativeInteger(
    options?.rightPaddingBars,
    DAILY_HISTORY_DEFAULTS.rightPaddingBars,
  );
  const lastIndex = barCount - 1;
  return Object.freeze({
    from: Math.max(0, lastIndex - (visibleBars - 1)),
    to: lastIndex + rightPaddingBars,
  });
}

export function mergeOlderDailyBars(existingBars, olderBars, { boundary } = {}) {
  const existing = [];
  const existingByTime = new Map();
  for (const candidate of Array.isArray(existingBars) ? existingBars : []) {
    const normalized = normalizeDailyBar(candidate);
    if (!normalized || existingByTime.has(normalized.time)) continue;
    existingByTime.set(normalized.time, normalized);
    existing.push(normalized);
  }
  existing.sort((left, right) => left.time.localeCompare(right.time));

  const cutoff = boundary === undefined ? existing[0]?.time : boundary;
  if (!isIsoDate(cutoff)) {
    return Object.freeze({
      bars: Object.freeze(existing),
      added: 0,
      addedBars: Object.freeze([]),
    });
  }

  const addedByTime = new Map();
  for (const candidate of Array.isArray(olderBars) ? olderBars : []) {
    const normalized = normalizeDailyBar(candidate);
    if (!normalized || normalized.time >= cutoff || existingByTime.has(normalized.time)) continue;
    if (!addedByTime.has(normalized.time)) addedByTime.set(normalized.time, normalized);
  }
  const addedBars = Object.freeze(
    [...addedByTime.values()].sort((left, right) => left.time.localeCompare(right.time)),
  );
  const bars = Object.freeze(addedBars.concat(existing));
  return Object.freeze({ bars, added: addedBars.length, addedBars });
}

export function shiftDailyLogicalRange(range, added) {
  if (!isPlainObject(range) || !Number.isFinite(range.from) || !Number.isFinite(range.to)) return null;
  if (!Number.isInteger(added) || added < 0) return null;
  return Object.freeze({ from: range.from + added, to: range.to + added });
}

function sameOhlcv(left, right) {
  return left.open === right.open && left.high === right.high && left.low === right.low &&
    left.close === right.close && left.volume === right.volume;
}

function errorResult(receivedCount = 0) {
  return { ok: false, cacheable: false, phase: 'error', bars: [], receivedCount };
}

export function normalizeDailyPayload(payload, maxBars = DEFAULT_DAILY_LIMIT) {
  if (!isPlainObject(payload) || typeof payload.available !== 'boolean' || !Array.isArray(payload.bars)) {
    return errorResult();
  }
  if (!payload.available) {
    if (payload.bars.length) return errorResult(payload.bars.length);
    return { ok: false, cacheable: false, phase: 'unavailable', bars: [], receivedCount: 0 };
  }
  if (!payload.bars.length) {
    return { ok: true, cacheable: true, phase: 'empty', bars: [], receivedCount: 0 };
  }

  const groups = new Map();
  for (const candidate of payload.bars) {
    const date = isPlainObject(candidate) && isIsoDate(candidate.time) ? candidate.time : null;
    if (!date) continue;
    const group = groups.get(date) || { invalid: false, bars: [] };
    const normalized = normalizeDailyBar(candidate);
    if (!normalized) group.invalid = true;
    else group.bars.push(normalized);
    groups.set(date, group);
  }

  const bars = [];
  for (const [time, group] of groups) {
    if (group.invalid || !group.bars.length) continue;
    const first = group.bars[0];
    if (!group.bars.every((candidate) => sameOhlcv(first, candidate))) continue;
    bars.push(Object.freeze({ ...first, time }));
  }
  bars.sort((left, right) => left.time.localeCompare(right.time));
  if (!bars.length) return errorResult(payload.bars.length);

  const limit = Number.isInteger(maxBars) && maxBars > 0
    ? Math.min(DEFAULT_DAILY_LIMIT, maxBars)
    : DEFAULT_DAILY_LIMIT;
  const bounded = Object.freeze(bars.slice(-limit));
  return {
    ok: true,
    cacheable: true,
    phase: bounded.length ? 'ready' : 'empty',
    bars: bounded,
    receivedCount: payload.bars.length,
  };
}

export function appendPartialDailyBar(partial, date, price, quantity) {
  if (!isIsoDate(date) || !validPrice(price) || !validVolume(quantity)) return partial || null;
  if (!partial || partial.time !== date) {
    return Object.freeze({ time: date, open: price, high: price, low: price, close: price, volume: quantity });
  }
  return Object.freeze({
    time: date,
    open: partial.open,
    high: Math.max(partial.high, price),
    low: Math.min(partial.low, price),
    close: price,
    volume: partial.volume + quantity,
  });
}

export function buildPartialDailyBar(date, prices, quantities, cursor) {
  if (!isIsoDate(date) || !prices || !quantities) return null;
  const length = Math.min(prices.length || 0, quantities.length || 0);
  const end = Math.max(0, Math.min(length, Number.isInteger(cursor) ? cursor : length));
  let partial = null;
  for (let index = 0; index < end; index += 1) {
    partial = appendPartialDailyBar(partial, date, prices[index], quantities[index]);
  }
  return partial;
}

export function simpleMovingAverage(bars, period) {
  if (!Array.isArray(bars) || !Number.isInteger(period) || period <= 0) return [];
  const points = [];
  let sum = 0;
  const window = [];
  for (const bar of bars) {
    if (!bar || !validPrice(bar.close) || !isIsoDate(bar.time)) continue;
    window.push(bar.close);
    sum += bar.close;
    if (window.length > period) sum -= window.shift();
    if (window.length === period) points.push(Object.freeze({ time: bar.time, value: sum / period }));
  }
  return Object.freeze(points);
}

export function buildDailySeries(historyBars, partialBar = null) {
  const history = Array.isArray(historyBars) ? historyBars : [];
  const observations = partialBar ? history.concat([partialBar]) : history.slice();
  return Object.freeze({
    bars: Object.freeze(observations),
    sma25: simpleMovingAverage(observations, 25),
    sma200: simpleMovingAverage(observations, 200),
  });
}

function rollingWindow(bars, period) {
  const closes = bars.slice(-(period - 1)).map((bar) => bar.close);
  return Object.freeze({
    count: closes.length,
    sum: closes.reduce((total, close) => total + close, 0),
  });
}

function deriveHistory(bars) {
  return Object.freeze({
    bars,
    sma25: simpleMovingAverage(bars, 25),
    sma200: simpleMovingAverage(bars, 200),
    window25: rollingWindow(bars, 25),
    window200: rollingWindow(bars, 200),
  });
}

function precomputeHistory(bars, metrics) {
  const history = deriveHistory(bars);
  metrics.historyPrecomputations += 1;
  return history;
}

function terminalPoint(partial, window, period) {
  if (!partial || window.count !== period - 1) return null;
  return Object.freeze({ time: partial.time, value: (window.sum + partial.close) / period });
}

function appendPoint(points, point) {
  return point ? Object.freeze(points.concat([point])) : points;
}

function defaultVolume(bar) {
  return { time: bar.time, value: bar.volume };
}

function snapshotForHistory(history, partialBar) {
  const terminalSma25 = terminalPoint(partialBar, history.window25, 25);
  const terminalSma200 = terminalPoint(partialBar, history.window200, 200);
  return Object.freeze({
    bars: partialBar ? Object.freeze(history.bars.concat([partialBar])) : history.bars,
    sma25: appendPoint(history.sma25, terminalSma25),
    sma200: appendPoint(history.sma200, terminalSma200),
  });
}

export function paintedLinePoint(point) {
  return { time: point.time, value: point.value };
}

export function renderDailySnapshot(snapshot, ports, painters = {}) {
  const paintCandle = painters.paintCandle || ((bar) => bar);
  const paintVolume = painters.paintVolume || defaultVolume;
  ports.candle.setData(snapshot.bars.map(paintCandle));
  ports.volume.setData(snapshot.bars.map(paintVolume));
  ports.sma25.setData(snapshot.sma25.map(paintedLinePoint));
  ports.sma200.setData(snapshot.sma200.map(paintedLinePoint));
}

function materializeDailySnapshot(snapshot, ports) {
  const paintCandle = ports.paintCandle || ((bar) => bar);
  const paintVolume = ports.paintVolume || defaultVolume;
  return {
    candle: snapshot.bars.map(paintCandle),
    volume: snapshot.bars.map(paintVolume),
    sma25: snapshot.sma25.map(paintedLinePoint),
    sma200: snapshot.sma200.map(paintedLinePoint),
  };
}

function writeDailyDatasets(ports, datasets) {
  ports.candle.setData(datasets.candle);
  ports.volume.setData(datasets.volume);
  ports.sma25.setData(datasets.sma25);
  ports.sma200.setData(datasets.sma200);
}

export class ChartViewportState {
  constructor(scheduleRelease = (release) => setTimeout(release, 0)) {
    this.savedRange = null;
    this.programmaticDepth = 0;
    this.scheduleRelease = scheduleRelease;
  }

  capture(range) {
    if (this.programmaticDepth || !range) return false;
    this.savedRange = Object.freeze({ ...range });
    return true;
  }

  runProgrammatic(apply) {
    this.programmaticDepth += 1;
    try {
      return apply();
    } finally {
      let pending = true;
      const release = () => {
        if (!pending) return;
        pending = false;
        this.programmaticDepth = Math.max(0, this.programmaticDepth - 1);
      };
      try {
        this.scheduleRelease(release);
      } catch (error) {
        release();
        throw error;
      }
    }
  }

  replace(range) {
    this.savedRange = range ? Object.freeze({ ...range }) : null;
  }
}

export function updateChartViewport({ viewport, writeData, timeScale, restore = false, fit = false }) {
  const target = viewport.savedRange ? { ...viewport.savedRange } : null;
  return viewport.runProgrammatic(() => {
    writeData();
    if (restore && target) timeScale.setVisibleLogicalRange(target);
    else if (fit) timeScale.fitContent();
  });
}

function captureDailyPageSessionState(session, plan) {
  const identity = plan?.token?.identity;
  return {
    phase: session.phase,
    historyBars: session.historyBars,
    historicalSma25: session.historicalSma25,
    historicalSma200: session.historicalSma200,
    window25: session.window25,
    window200: session.window200,
    terminalSma25: session.terminalSma25,
    terminalSma200: session.terminalSma200,
    historyPageRequest: session.historyPageRequest,
    historyArmed: session.historyArmed,
    historyExhausted: session.historyExhausted,
    historyFailures: new Map(session.historyFailures),
    historyPrecomputations: session.metrics.historyPrecomputations,
    terminalDerivations: session.metrics.terminalDerivations,
    identity,
    cached: identity && session.cache.has(identity) ? session.cache.get(identity) : undefined,
    hadCached: Boolean(identity && session.cache.has(identity)),
  };
}

function restoreDailyPageSessionState(session, previous) {
  session.phase = previous.phase;
  session.historyBars = previous.historyBars;
  session.historicalSma25 = previous.historicalSma25;
  session.historicalSma200 = previous.historicalSma200;
  session.window25 = previous.window25;
  session.window200 = previous.window200;
  session.terminalSma25 = previous.terminalSma25;
  session.terminalSma200 = previous.terminalSma200;
  session.historyPageRequest = previous.historyPageRequest;
  session.historyArmed = previous.historyArmed;
  session.historyExhausted = previous.historyExhausted;
  session.historyFailures = new Map(previous.historyFailures);
  session.metrics.historyPrecomputations = previous.historyPrecomputations;
  session.metrics.terminalDerivations = previous.terminalDerivations;
  if (previous.identity) {
    if (previous.hadCached) session.cache.set(previous.identity, previous.cached);
    else session.cache.delete(previous.identity);
  }
}

function rollbackDailyHistoryPage({
  session,
  sessionState,
  viewport,
  ports,
  timeScale,
  previousDatasets,
  previousLiveRange,
  previousSavedRange,
}) {
  const errors = [];
  const attempt = (operation) => {
    try {
      operation();
    } catch (error) {
      errors.push(error);
    }
  };

  attempt(() => viewport.runProgrammatic(() => {
    attempt(() => ports.candle.setData(previousDatasets.candle));
    attempt(() => ports.volume.setData(previousDatasets.volume));
    attempt(() => ports.sma25.setData(previousDatasets.sma25));
    attempt(() => ports.sma200.setData(previousDatasets.sma200));
    if (previousLiveRange) attempt(() => timeScale.setVisibleLogicalRange({ ...previousLiveRange }));
  }));
  attempt(() => viewport.replace(previousSavedRange));
  attempt(() => restoreDailyPageSessionState(session, sessionState));
  return errors;
}

export function applyDailyHistoryPage({ session, plan, viewport, ports, timeScale }) {
  if (!plan || plan.kind !== 'page' || !Number.isInteger(plan.added) || plan.added <= 0) {
    throw new TypeError('A prepared Daily history page is required');
  }
  const previousLiveRange = timeScale.getVisibleLogicalRange();
  const shiftedRange = shiftDailyLogicalRange(previousLiveRange, plan.added);
  if (!shiftedRange) throw new TypeError('A finite Daily logical range is required');

  const previousSavedRange = viewport.savedRange ? Object.freeze({ ...viewport.savedRange }) : null;
  const previousDatasets = materializeDailySnapshot(plan.previousSnapshot, ports);
  const nextDatasets = materializeDailySnapshot(plan.nextSnapshot, ports);
  const sessionState = captureDailyPageSessionState(session, plan);

  try {
    viewport.runProgrammatic(() => {
      writeDailyDatasets(ports, nextDatasets);
      timeScale.setVisibleLogicalRange({ ...shiftedRange });
    });
    viewport.replace(shiftedRange);
    const committed = session.commitOlderPage(plan);
    if (!committed?.accepted) throw new Error('Daily history page commit was rejected');
    return Object.freeze({ committed: true, added: committed.added, shiftedRange });
  } catch (error) {
    const rollbackErrors = rollbackDailyHistoryPage({
      session,
      sessionState,
      viewport,
      ports,
      timeScale,
      previousDatasets,
      previousLiveRange,
      previousSavedRange,
    });
    if (rollbackErrors.length) {
      throw new AggregateError([error, ...rollbackErrors], 'Daily history page rollback failed');
    }
    throw error;
  }
}

export function commitDailySession({
  session,
  generation,
  meta,
  commit = () => session.commitSession(generation, meta),
  publishStatus = () => {},
}) {
  const identity = commit();
  if (!identity) return null;
  publishStatus(session.phase);
  return identity;
}

export function commitReplayFrame({ mode, touchedBars, from, to, ports }) {
  if (mode === 'minute') touchedBars.forEach(ports.updateMinute);
  else ports.deferMinute();
  ports.deriveDaily();
  if (mode === 'daily') ports.updateDaily();
  ports.pushTicks(from, to);
  ports.pushTape(from, to);
  ports.matchOrders(from, to);
  ports.updateBoard(from, to);
  ports.updatePosition();
}

export function runReplayFrame({ mode, touchedBars, from, to, ports }) {
  if (to > from) {
    const commitFrame = ports.commitFrame || commitReplayFrame;
    commitFrame({ mode, touchedBars, from, to, ports });
  }
  ports.followTick();
  ports.updateClock();
  ports.syncScrubber();
  ports.updateLiveness();
}

export function selectMinuteHistoryTarget({
  mode,
  realTarget,
  viewport,
  deferMinute = () => {},
  refreshMarkers = () => {},
}) {
  if (mode === 'minute') return realTarget;
  deferMinute();
  const deferredSeries = { setData: () => {} };
  return {
    candleSeries: deferredSeries,
    volumeSeries: deferredSeries,
    timeScale: {
      getVisibleLogicalRange: () => viewport.savedRange,
      setVisibleLogicalRange: (range) => viewport.replace(range),
    },
    refreshMarkers,
  };
}

export function flushDeferredMinuteChart({ mode, deferred, flush }) {
  if (mode !== 'minute' || !deferred) {
    return Object.freeze({ deferred: Boolean(deferred), flushed: false });
  }
  flush();
  return Object.freeze({ deferred: false, flushed: true });
}

export function commitMarkerWrites({ mode, minuteMarkers, tickMarkers, ports }) {
  if (mode === 'minute') ports.minute(minuteMarkers);
  else ports.deferMinute();
  ports.tick(tickMarkers);
}

export function dailySessionIdentity(session) {
  if (!session?.stem || !session?.code || !isIsoDate(session?.date)) return null;
  return session.stem + '|' + session.code + '|' + session.date;
}

export function dailyContextUrl(identity, options = {}) {
  if (typeof identity !== 'string') return null;
  const [stem, code, actualDate, extra] = identity.split('|');
  if (!stem || !code || !isIsoDate(actualDate) || extra !== undefined) return null;
  if (!isPlainObject(options)) return null;
  const beforeDate = options.beforeDate === undefined ? actualDate : options.beforeDate;
  const limit = options.limit === undefined ? DEFAULT_DAILY_LIMIT : options.limit;
  if (!isIsoDate(beforeDate) || !Number.isInteger(limit) || limit < 1 || limit > DEFAULT_DAILY_LIMIT) {
    return null;
  }
  return '/api/daily-context?stem=' + encodeURIComponent(stem) +
    '&date=' + encodeURIComponent(beforeDate) + '&limit=' + String(limit);
}

export async function loadDailyRequest({
  session,
  fetchPayload,
  getMode = () => 'minute',
  commitChart = () => {},
  onStatus = () => {},
  isCancellation = () => false,
}) {
  const identity = session.stagedIdentity;
  if (!identity) return null;
  if (session.useRequestCache()) {
    const committed = session.canCommitChart(getMode(), identity);
    if (committed) commitChart(identity);
    onStatus(session.phase);
    return { accepted: true, committed, cached: true, stale: false };
  }
  const token = session.admitRequest();
  if (!token) return null;
  onStatus('loading');
  try {
    const payload = await fetchPayload({ identity, url: dailyContextUrl(identity), token });
    if (!session.isCurrent(token)) return { accepted: false, committed: false, stale: true };
    const accepted = session.complete(token, normalizeDailyPayload(payload));
    const committed = accepted && session.canCommitChart(getMode(), identity);
    if (committed) commitChart(identity);
    onStatus(session.phase);
    return { accepted, committed, cached: false, stale: false };
  } catch (error) {
    if (!session.isCurrent(token)) return { accepted: false, committed: false, stale: true };
    if (isCancellation(error)) {
      session.abort(token);
      return { accepted: false, committed: false, stale: true };
    }
    session.fail(token, 'error');
    onStatus(session.phase);
    return { accepted: false, committed: false, cached: false, stale: false, error };
  }
}

export class DailyChartSession {
  constructor(options = {}) {
    this.generation = 0;
    this.stagedIdentity = null;
    this.identity = null;
    this.phase = 'idle';
    this.cache = new Map();
    this.historyBars = [];
    this.partialBar = null;
    this.historicalSma25 = [];
    this.historicalSma200 = [];
    this.window25 = Object.freeze({ count: 0, sum: 0 });
    this.window200 = Object.freeze({ count: 0, sum: 0 });
    this.terminalSma25 = null;
    this.terminalSma200 = null;
    this.request = null;
    this.nextTokenId = 1;
    this.historyPageRequest = null;
    this.historyArmed = false;
    this.historyExhausted = false;
    this.historyFailures = new Map();
    this.historyPageSize = positiveInteger(options.pageSize, DAILY_HISTORY_DEFAULTS.pageSize);
    this.historyEdgeThresholdBars = nonNegativeInteger(
      options.edgeThresholdBars,
      DAILY_HISTORY_DEFAULTS.edgeThresholdBars,
    );
    this.historyFailureCooldownMs = nonNegativeInteger(
      options.failureCooldownMs,
      DAILY_HISTORY_DEFAULTS.failureCooldownMs,
    );
    this.historyMaxFailuresPerCutoff = positiveInteger(
      options.maxFailuresPerCutoff,
      DAILY_HISTORY_DEFAULTS.maxFailuresPerCutoff,
    );
    this.historyNow = typeof options.now === 'function' ? options.now : () => Date.now();
    this.hasInitialViewport = false;
    this.stagedFailure = null;
    this.metrics = { historyPrecomputations: 0, terminalDerivations: 0 };
  }

  resetSession() {
    this.generation += 1;
    this.stagedIdentity = null;
    this.identity = null;
    this.phase = 'loading';
    this.request = null;
    this.historyBars = [];
    this.partialBar = null;
    this.#clearHistory();
    this.#resetHistoryPaging();
    this.stagedFailure = null;
    return this.generation;
  }

  isGeneration(generation) {
    return this.generation === generation;
  }

  stageActualSession(generation, session) {
    const identity = dailySessionIdentity(session);
    if (this.generation !== generation || !identity) return null;
    this.stagedIdentity = identity;
    this.phase = 'idle';
    return identity;
  }

  commitSession(generation, session) {
    const identity = dailySessionIdentity(session);
    if (this.generation !== generation || !identity || identity !== this.stagedIdentity) return null;
    this.identity = identity;
    if (!this.useRequestCache()) {
      this.phase = this.stagedFailure || (this.request ? 'loading' : 'idle');
    }
    return identity;
  }

  setActualSession(generation, session) {
    const identity = this.stageActualSession(generation, session);
    return identity ? this.commitSession(generation, session) : null;
  }

  useCached() {
    if (!this.identity || !this.cache.has(this.identity)) return false;
    const cached = this.cache.get(this.identity);
    this.#apply(cached);
    return true;
  }

  useRequestCache() {
    if (!this.stagedIdentity || !this.cache.has(this.stagedIdentity)) return false;
    if (this.identity === this.stagedIdentity) this.#apply(this.cache.get(this.stagedIdentity));
    return true;
  }

  admitRequest() {
    if (!this.stagedIdentity || this.request || this.cache.has(this.stagedIdentity)) return null;
    const token = Object.freeze({
      id: this.nextTokenId,
      generation: this.generation,
      identity: this.stagedIdentity,
    });
    this.nextTokenId += 1;
    this.request = token;
    this.phase = 'loading';
    return token;
  }

  isCurrent(token) {
    return Boolean(token) && this.request === token && token.generation === this.generation &&
      token.identity === this.stagedIdentity;
  }

  complete(token, normalized) {
    if (!this.isCurrent(token)) return false;
    this.request = null;
    if (!normalized?.ok || !normalized.cacheable) {
      const nextPhase = normalized?.phase === 'unavailable' ? 'unavailable' : 'error';
      this.stagedFailure = nextPhase;
      if (token.identity === this.identity) this.phase = nextPhase;
      return false;
    }
    const history = precomputeHistory(normalized.bars, this.metrics);
    const cached = Object.freeze({ phase: normalized.phase, history });
    this.cache.set(token.identity, cached);
    if (token.identity === this.identity) this.#apply(cached);
    return true;
  }

  fail(token, phase = 'error') {
    if (!this.isCurrent(token)) return false;
    this.request = null;
    const nextPhase = phase === 'unavailable' ? 'unavailable' : 'error';
    this.stagedFailure = nextPhase;
    if (token.identity === this.identity) this.phase = nextPhase;
    return true;
  }

  abort(token) {
    if (!this.isCurrent(token)) return false;
    this.request = null;
    if (token.identity === this.identity) this.phase = 'idle';
    return true;
  }

  failSession(generation) {
    if (this.generation !== generation) return false;
    this.stagedIdentity = null;
    this.identity = null;
    this.request = null;
    this.phase = 'error';
    this.historyBars = [];
    this.partialBar = null;
    this.#clearHistory();
    this.#resetHistoryPaging();
    return true;
  }

  armHistory(identity = this.identity) {
    if (!identity || identity !== this.identity || this.phase !== 'ready') return false;
    this.historyArmed = true;
    return true;
  }

  canLoadOlder({ identity = this.identity, barsBefore, programmatic = false } = {}) {
    if (!identity || identity !== this.identity || this.phase !== 'ready') return false;
    if (!this.historyArmed || this.historyExhausted || this.historyPageRequest || programmatic) return false;
    if (!Number.isFinite(barsBefore) || barsBefore > this.historyEdgeThresholdBars) return false;
    const cutoff = this.historyBars[0]?.time;
    if (!isIsoDate(cutoff)) return false;
    const failure = this.historyFailures.get(cutoff);
    if (!failure) return true;
    if (failure.count >= this.historyMaxFailuresPerCutoff) return false;
    return this.historyNow() >= failure.retryNotBefore;
  }

  admitOlderPage(cutoff, { identity = this.identity } = {}) {
    if (!identity || identity !== this.identity || this.phase !== 'ready') return null;
    if (!this.historyArmed || this.historyExhausted || this.historyPageRequest) return null;
    if (!isIsoDate(cutoff) || cutoff !== this.historyBars[0]?.time) return null;
    const failure = this.historyFailures.get(cutoff);
    if (failure && (
      failure.count >= this.historyMaxFailuresPerCutoff || this.historyNow() < failure.retryNotBefore
    )) return null;
    const token = Object.freeze({
      id: this.nextTokenId,
      generation: this.generation,
      identity,
      cutoff,
    });
    this.nextTokenId += 1;
    this.historyPageRequest = token;
    this.historyArmed = false;
    return token;
  }

  isCurrentOlderPage(token) {
    return Boolean(token) && this.historyPageRequest === token &&
      token.generation === this.generation && token.identity === this.identity &&
      token.cutoff === this.historyBars[0]?.time;
  }

  prepareOlderPage(token, normalizedPayload) {
    if (!this.isCurrentOlderPage(token)) return Object.freeze({ kind: 'stale' });
    if (!normalizedPayload?.ok || !normalizedPayload.cacheable) {
      return Object.freeze({ kind: 'failure', token });
    }
    if (normalizedPayload.phase === 'empty' && normalizedPayload.bars?.length === 0) {
      return Object.freeze({ kind: 'exhausted', token });
    }
    if (normalizedPayload.phase !== 'ready' || !Array.isArray(normalizedPayload.bars)) {
      return Object.freeze({ kind: 'failure', token });
    }

    const merged = mergeOlderDailyBars(this.historyBars, normalizedPayload.bars, {
      boundary: token.cutoff,
    });
    if (!merged.added) return Object.freeze({ kind: 'exhausted', token });

    const history = deriveHistory(merged.bars);
    const nextCached = Object.freeze({ phase: 'ready', history });
    const plan = {
      kind: 'page',
      token,
      added: merged.added,
      previousSnapshot: this.snapshot(),
      nextSnapshot: snapshotForHistory(history, this.partialBar),
      nextCached,
      [DAILY_PAGE_PLAN_OWNER]: this,
    };
    return Object.freeze(plan);
  }

  commitOlderPage(plan) {
    if (!plan || plan.kind !== 'page' || plan[DAILY_PAGE_PLAN_OWNER] !== this) {
      return Object.freeze({ accepted: false, added: 0 });
    }
    if (!this.isCurrentOlderPage(plan.token) || !Number.isInteger(plan.added) || plan.added <= 0) {
      return Object.freeze({ accepted: false, added: 0 });
    }
    const history = plan.nextCached?.history;
    if (!history || history.bars.length !== this.historyBars.length + plan.added) {
      return Object.freeze({ accepted: false, added: 0 });
    }

    const terminalSma25 = terminalPoint(this.partialBar, history.window25, 25);
    const terminalSma200 = terminalPoint(this.partialBar, history.window200, 200);
    const nextPrecomputations = this.metrics.historyPrecomputations + 1;
    this.cache.set(plan.token.identity, plan.nextCached);
    this.historyBars = history.bars;
    this.historicalSma25 = history.sma25;
    this.historicalSma200 = history.sma200;
    this.window25 = history.window25;
    this.window200 = history.window200;
    this.terminalSma25 = terminalSma25;
    this.terminalSma200 = terminalSma200;
    this.historyPageRequest = null;
    this.historyExhausted = false;
    this.historyFailures.delete(plan.token.cutoff);
    this.metrics.historyPrecomputations = nextPrecomputations;
    return Object.freeze({ accepted: true, added: plan.added });
  }

  completeOlderExhausted(token) {
    if (!this.isCurrentOlderPage(token)) return false;
    this.historyPageRequest = null;
    this.historyExhausted = true;
    this.historyFailures.delete(token.cutoff);
    return true;
  }

  failOlderPage(token) {
    if (!this.isCurrentOlderPage(token)) return false;
    const previous = this.historyFailures.get(token.cutoff);
    const count = (previous?.count || 0) + 1;
    this.historyFailures.set(token.cutoff, Object.freeze({
      count,
      retryNotBefore: this.historyNow() + this.historyFailureCooldownMs,
    }));
    this.historyPageRequest = null;
    return true;
  }

  abortOlderPage(token) {
    if (!this.isCurrentOlderPage(token)) return false;
    this.historyPageRequest = null;
    return true;
  }

  markInitialViewport() {
    if (!this.identity || !['ready', 'empty'].includes(this.phase) || !this.snapshot().bars.length) return false;
    if (this.hasInitialViewport) return false;
    this.hasInitialViewport = true;
    return true;
  }

  clearPartial() {
    this.partialBar = null;
    this.terminalSma25 = null;
    this.terminalSma200 = null;
  }

  appendTick(date, price, quantity) {
    this.partialBar = appendPartialDailyBar(this.partialBar, date, price, quantity);
    return this.partialBar;
  }

  rebuildPartial(date, prices, quantities, cursor) {
    this.partialBar = buildPartialDailyBar(date, prices, quantities, cursor);
    this.deriveTerminal();
    return this.partialBar;
  }

  deriveTerminal() {
    this.metrics.terminalDerivations += 1;
    this.terminalSma25 = terminalPoint(this.partialBar, this.window25, 25);
    this.terminalSma200 = terminalPoint(this.partialBar, this.window200, 200);
    return Object.freeze({ sma25: this.terminalSma25, sma200: this.terminalSma200 });
  }

  series() {
    return this.snapshot();
  }

  snapshot() {
    const bars = this.partialBar
      ? Object.freeze(this.historyBars.concat([this.partialBar]))
      : this.historyBars;
    return Object.freeze({
      bars,
      sma25: appendPoint(this.historicalSma25, this.terminalSma25),
      sma200: appendPoint(this.historicalSma200, this.terminalSma200),
    });
  }

  canAppendTick(identity) {
    return Boolean(identity) && identity === this.identity;
  }

  canCommitChart(mode, identity = this.identity) {
    return mode === 'daily' && Boolean(identity) && identity === this.identity &&
      (this.phase === 'ready' || this.phase === 'empty');
  }

  #apply(cached) {
    this.phase = cached.phase;
    this.historyBars = cached.history.bars;
    this.historicalSma25 = cached.history.sma25;
    this.historicalSma200 = cached.history.sma200;
    this.window25 = cached.history.window25;
    this.window200 = cached.history.window200;
    if (this.partialBar) this.deriveTerminal();
    else {
      this.terminalSma25 = null;
      this.terminalSma200 = null;
    }
  }

  #clearHistory() {
    this.historyBars = [];
    this.historicalSma25 = [];
    this.historicalSma200 = [];
    this.window25 = Object.freeze({ count: 0, sum: 0 });
    this.window200 = Object.freeze({ count: 0, sum: 0 });
    this.terminalSma25 = null;
    this.terminalSma200 = null;
  }

  #resetHistoryPaging() {
    this.historyPageRequest = null;
    this.historyArmed = false;
    this.historyExhausted = false;
    this.historyFailures = new Map();
    this.hasInitialViewport = false;
  }
}

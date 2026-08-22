/*
 * 分足チャートの「古い足の遅延読み込み」— 純粋計算とセッション状態機械。
 *
 * DOM も Lightweight Charts も import しない。app.js 側が
 *   1. 読み込み完了後、ユーザー操作（ホイール / ドラッグ）で arm()
 *   2. 可視論理レンジの変化で isNearLeftEdge() を判定
 *   3. admit() でトークンを取り、/api/minute-context を叩く
 *   4. await のたびに isCurrent() で世代を確認してから state を書き換える
 * という順で駆動する。世代（generation）はセッション読み込みのたびに進み、
 * 飛んで戻ってきた古いレスポンスが新しいセッションを汚すのを防ぐ。
 */

export const MINUTE_HISTORY_DEFAULTS = {
  pageSize: 200,            // 1 ページで取る本数（API の上限は 500）
  edgeThresholdBars: 10,    // 最古の足からこの本数以内に入ったら次を取る
  failureCooldownMs: 5000,  // 同一 cutoff の再試行を抑える時間
  maxFailuresPerCutoff: 3,  // これだけ失敗したらセッションが変わるまで諦める
};

function normalizeMinuteBar(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return null;
  try {
    const prototype = Object.getPrototypeOf(value);
    if (prototype !== Object.prototype && prototype !== null) return null;

    const { time, open, high, low, close, volume } = value;
    const fields = [time, open, high, low, close, volume];
    if (!fields.every((field) => typeof field === 'number' && Number.isFinite(field))) return null;
    if (!Number.isInteger(time) || time < 0 || !Number.isFinite(new Date(time * 1000).getTime())) {
      return null;
    }
    if (volume < 0) return null;
    if (low > high || open < low || open > high || close < low || close > high) return null;
    return { time, open, high, low, close, volume };
  } catch {
    return null;
  }
}

export function normalizeMinuteBars(value, maxBars = MINUTE_HISTORY_DEFAULTS.pageSize) {
  if (!Array.isArray(value)) {
    return {
      bars: [],
      sourceIsArray: false,
      receivedCount: 0,
      consideredCount: 0,
      invalidCount: 0,
      truncatedCount: 0,
    };
  }

  const limit = Number.isInteger(maxBars) && maxBars > 0 ? maxBars : 0;
  const start = Math.max(0, value.length - limit);
  const candidates = value.slice(start);
  const bars = [];
  for (const candidate of candidates) {
    const normalized = normalizeMinuteBar(candidate);
    if (normalized) bars.push(normalized);
  }
  return {
    bars,
    sourceIsArray: true,
    receivedCount: value.length,
    consideredCount: candidates.length,
    invalidCount: candidates.length - bars.length,
    truncatedCount: start,
  };
}

/** epoch 秒を UTC のまま `/api/minute-context` の cutoff（strict-before）に変換する。 */
export function cutoffFromEpochSeconds(seconds) {
  if (!Number.isFinite(seconds)) return null;
  const at = new Date(seconds * 1000);
  const pad = (value) => String(value).padStart(2, '0');
  return {
    date: at.getUTCFullYear() + '-' + pad(at.getUTCMonth() + 1) + '-' + pad(at.getUTCDate()),
    time: pad(at.getUTCHours()) + ':' + pad(at.getUTCMinutes()),
  };
}

/** 失敗記録・単一飛行の同一性判定に使う cutoff のキー。 */
export function cutoffKey(cutoff) {
  return cutoff ? cutoff.date + 'T' + cutoff.time : '';
}

/**
 * 古い足 `older` を `existing` の前に時刻順・重複なしで足す。
 *
 * - `existing` 側が常に優先（形成中の足を古いスナップショットで潰さない）。
 * - `boundary`（既定は existing の先頭時刻）以降の足は捨てる。contextBars と
 *   bars の両方へ「まったく同じ集合」を足すために、呼び出し側が共通の
 *   boundary を渡せるようにしてある。
 * - 追加が 0 本なら `existing` をそのまま返す（参照も変えない）。
 */
export function mergeOlderBars(existing, older, { boundary } = {}) {
  const base = Array.isArray(existing) ? existing : [];
  const empty = { bars: base, added: 0, addedBars: [] };
  if (!Array.isArray(older) || older.length === 0) return empty;

  const limit = Number.isFinite(boundary)
    ? boundary
    : (base.length ? base[0].time : Number.POSITIVE_INFINITY);
  const seen = new Set(base.map((bar) => bar.time));
  const unique = [];
  for (const bar of older) {
    if (!bar || !Number.isFinite(bar.time)) continue;
    if (bar.time >= limit) continue;
    if (seen.has(bar.time)) continue;
    seen.add(bar.time);
    unique.push(bar);
  }
  if (unique.length === 0) return empty;

  unique.sort((left, right) => left.time - right.time);
  return { bars: unique.concat(base), added: unique.length, addedBars: unique };
}

/**
 * 先頭に `count` 本足したぶんだけ可視論理レンジをずらす。
 *
 * Lightweight Charts は可視範囲を「最後の足からの rightOffset」で保持するので
 * prepend 後も見えている足自体は変わらないが、論理インデックスは +count される。
 * 取得前のレンジをこの関数で補正して設定し直すことで、横方向のジャンプが
 * 起きないことを明示的に担保する。
 */
export function shiftLogicalRange(range, count) {
  if (!range || !Number.isFinite(range.from) || !Number.isFinite(range.to)) return null;
  if (!Number.isFinite(count) || count === 0) return null;
  return { from: range.from + count, to: range.to + count };
}

function restoreMinuteChart({
  candleSeries,
  volumeSeries,
  timeScale,
  candleData,
  volumeData,
  logicalRange,
  runProgrammatic,
}) {
  const rollbackErrors = [];
  const attempt = (restore) => {
    try {
      restore();
    } catch (error) {
      rollbackErrors.push(error);
    }
  };
  try {
    runProgrammatic(() => {
      attempt(() => candleSeries.setData(candleData));
      attempt(() => volumeSeries.setData(volumeData));
      if (logicalRange) attempt(() => timeScale.setVisibleLogicalRange(logicalRange));
    });
  } catch (error) {
    rollbackErrors.push(error);
  }
  return rollbackErrors;
}

export function applyMinuteHistoryPage({
  state,
  olderBars,
  candleSeries,
  volumeSeries,
  timeScale,
  paintCandle,
  paintVolume,
  runProgrammatic,
  refreshMarkers = () => {},
}) {
  const boundary = state.contextBars.length
    ? state.contextBars[0].time
    : (state.bars.length ? state.bars[0].time : null);
  if (boundary === null) return { added: 0, committed: false, markerError: null };

  const context = mergeOlderBars(state.contextBars, olderBars, { boundary });
  if (context.added === 0) return { added: 0, committed: false, markerError: null };
  const full = mergeOlderBars(state.bars, context.addedBars, { boundary });
  if (full.added !== context.added) {
    throw new Error('Minute history arrays would receive different prefixes');
  }

  const previousRange = timeScale.getVisibleLogicalRange();
  const previousCandleData = state.bars.map(paintCandle);
  const previousVolumeData = state.bars.map(paintVolume);
  const nextCandleData = full.bars.map(paintCandle);
  const nextVolumeData = full.bars.map(paintVolume);
  const shiftedRange = shiftLogicalRange(previousRange, full.added);

  try {
    runProgrammatic(() => {
      candleSeries.setData(nextCandleData);
      volumeSeries.setData(nextVolumeData);
      if (shiftedRange) timeScale.setVisibleLogicalRange(shiftedRange);
    });
  } catch (error) {
    const rollbackErrors = restoreMinuteChart({
      candleSeries,
      volumeSeries,
      timeScale,
      candleData: previousCandleData,
      volumeData: previousVolumeData,
      logicalRange: previousRange,
      runProgrammatic,
    });
    if (rollbackErrors.length) throw new AggregateError([error, ...rollbackErrors], error.message);
    throw error;
  }

  state.contextBars = context.bars;
  state.bars = full.bars;
  let markerError = null;
  try {
    refreshMarkers();
  } catch (error) {
    markerError = error;
  }
  return { added: full.added, committed: true, markerError };
}

export class MinuteHistorySession {
  constructor(options = {}) {
    const settings = { ...MINUTE_HISTORY_DEFAULTS, ...options };
    this.pageSize = settings.pageSize;
    this.edgeThresholdBars = settings.edgeThresholdBars;
    this.failureCooldownMs = settings.failureCooldownMs;
    this.maxFailuresPerCutoff = settings.maxFailuresPerCutoff;
    this.now = settings.now || (() => Date.now());

    this.generation = 0;
    this.sessionIdentity = null;
    this.sessionPhase = 'idle';
    this.loading = null;
    this.exhausted = false;
    this.armed = false;
    this.programmaticDepth = 0;
    this.nextTokenId = 1;
    this.failures = new Map();
  }

  /**
   * セッションを切り替える。世代を進めるので、これ以降 isCurrent() は
   * 飛んでいる古いリクエストのトークンをすべて拒否する。loadSession() は
   * 最初の await より前にこれを呼ぶ契約。
   */
  resetSession(sessionIdentity) {
    this.generation += 1;
    this.sessionIdentity = sessionIdentity;
    this.sessionPhase = 'loading';
    this.loading = null;
    this.exhausted = false;
    this.armed = false;
    this.failures.clear();
    return this.generation;
  }

  isGeneration(generation) {
    return this.generation === generation;
  }

  setLoadingSession(generation, sessionIdentity) {
    if (this.generation !== generation || this.sessionPhase !== 'loading') return false;
    this.sessionIdentity = sessionIdentity;
    return true;
  }

  isLoadingSession(generation, sessionIdentity) {
    return this.generation === generation &&
      this.sessionPhase === 'loading' &&
      this.sessionIdentity === sessionIdentity;
  }

  markSessionReady(generation, sessionIdentity) {
    if (!this.isLoadingSession(generation, sessionIdentity) || this.loading) return false;
    this.sessionPhase = 'ready';
    return true;
  }

  isReadySession(sessionIdentity = this.sessionIdentity) {
    return this.sessionPhase === 'ready' && this.sessionIdentity === sessionIdentity;
  }

  /** ユーザーがチャートを操作した（＝以降の左端到達は本人の意思とみなす）。 */
  arm(sessionIdentity = this.sessionIdentity) {
    if (!this.isReadySession(sessionIdentity)) return false;
    this.armed = true;
    return true;
  }

  /** setData / setVisibleLogicalRange など、こちらが動かしている間の目印。 */
  beginProgrammatic() {
    this.programmaticDepth += 1;
  }

  endProgrammatic() {
    if (this.programmaticDepth > 0) this.programmaticDepth -= 1;
  }

  isProgrammatic() {
    return this.programmaticDepth > 0;
  }

  isNearLeftEdge(barsBefore) {
    if (!Number.isFinite(barsBefore)) return false;
    if (!this.isReadySession() || !this.armed || this.exhausted || this.loading || this.isProgrammatic()) {
      return false;
    }
    return barsBefore <= this.edgeThresholdBars;
  }

  /**
   * ready セッションの遅延リクエストを 1 本だけ通す。初回プリロードは
   * loading フェーズ専用の admitInitial() を使う。
   */
  admit(key, { sessionIdentity = this.sessionIdentity } = {}) {
    if (!this.isReadySession(sessionIdentity) || !this.armed) return null;
    return this.#admit(key, 'ready', sessionIdentity);
  }

  admitInitial(key, sessionIdentity) {
    if (this.sessionPhase !== 'loading' || this.sessionIdentity !== sessionIdentity) return null;
    return this.#admit(key, 'loading', sessionIdentity);
  }

  #admit(key, phase, sessionIdentity) {
    if (this.exhausted || this.loading || this.isProgrammatic()) return null;

    const failure = this.failures.get(key);
    if (failure) {
      if (failure.count >= this.maxFailuresPerCutoff) return null;
      if (this.now() < failure.retryNotBefore) return null;
    }

    const token = {
      id: this.nextTokenId,
      generation: this.generation,
      key,
      phase,
      sessionIdentity,
    };
    this.nextTokenId += 1;
    this.loading = token;
    return token;
  }

  /** await のあと・state を書き換える直前に必ず通す関門。 */
  isCurrent(token) {
    return Boolean(token) &&
      token.generation === this.generation &&
      token.phase === this.sessionPhase &&
      token.sessionIdentity === this.sessionIdentity &&
      this.loading === token;
  }

  /**
   * 取得成功。`addedCount` は重複を除いて実際に足せた本数。0 本なら
   * これ以上さかのぼれない（＝このセッションでは打ち止め）とみなす。
   */
  completeSuccess(token, addedCount) {
    if (!this.isCurrent(token)) return;
    this.loading = null;
    this.failures.delete(token.key);
    if (!Number.isFinite(addedCount) || addedCount <= 0) this.exhausted = true;
  }

  /** 通信失敗など。同じ cutoff はクールダウンのあいだ叩き直さない。 */
  completeFailure(token) {
    if (!this.isCurrent(token)) return;
    this.loading = null;
    const previous = this.failures.get(token.key);
    this.failures.set(token.key, {
      count: (previous ? previous.count : 0) + 1,
      retryNotBefore: this.now() + this.failureCooldownMs,
    });
  }

  /** キャンセル（セッション切り替え等）。失敗としては数えない。 */
  completeAbort(token) {
    if (this.loading === token) this.loading = null;
  }
}

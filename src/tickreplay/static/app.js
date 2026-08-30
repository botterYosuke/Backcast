import { RequestCoordinator, isCancellationError } from './request-coordinator.mjs';
import {
  applyFill,
  classifyClick,
  createPortfolio,
  isFilled,
  totalPnl,
  unrealizedPnl,
} from './paper-trading.mjs';
import {
  MinuteHistorySession,
  applyMinuteHistoryPage,
  cutoffFromEpochSeconds,
  cutoffKey,
  normalizeMinuteBars,
} from './minute-history.mjs';
import {
  boardRowCount,
  centeredScrollTop,
  centeredTopLevel,
  createBoardPriceFormatter,
  hasProgrammaticScrollDrift,
  levelAtRow,
  pendingQuantityAtRow,
  planBoardRebase,
  rowIndexForLevel,
} from './board-ladder.mjs';
import {
  applyDailyHistoryPage,
  ChartViewportState,
  DailyChartSession,
  ReplayLivenessPresenter,
  commitDailySession,
  commitMarkerWrites,
  commitReplayFrame,
  dailyContextUrl,
  dailyInitialLogicalRange,
  dailySessionIdentity,
  flushDeferredMinuteChart,
  loadDailyRequest,
  normalizeDailyPayload,
  paintedLinePoint,
  renderDailySnapshot,
  runReplayFrame,
  selectMinuteHistoryTarget,
  shiftDailyLogicalRange,
  updateChartViewport,
} from './daily-chart.mjs';

/*
 * 歩み値リプレイ — tick / 分足のプレイバック
 *
 * 設計メモ
 * - サーバは 1 セッション分の約定を列指向 JSON で一括返却する。再生は
 *   すべてブラウザ内で行うので、シークも速度変更も往復なしで即座に効く。
 * - 分足は歩み値から逐次集計する。形成中の足は始値・高値・安値・終値が
 *   歩み値の進行に応じて伸縮し、始値 > 最新値なら青（陰線）、
 *   始値 <= 最新値なら赤（陽線）で塗る（国内証券の慣行）。
 * - 時刻は DB の naive timestamp を UTC とみなして epoch 化している。
 *   Lightweight Charts は UTC で描画するため、保存された壁時計時刻が
 *   そのまま軸に出る。タイムゾーンの読み替えは一切していない。
 * - 仮想発注は板の両脇の列を 1 クリックする方式。建玉と損益の計算そのものは
 *   paper-trading.mjs（DOM 非依存・node:test で検証済み）に置き、この
 *   ファイルは「板のどこを押したか」を呼値レベルに翻訳して渡すだけにする。
 */

'use strict';

const UP = '#c0392b';    // 陽線・上昇
const DOWN = '#1d5fa8';  // 陰線・下降
const FLAT = '#9a9489';
const UP_FILL = 'rgba(192, 57, 43, 0.35)';
const DOWN_FILL = 'rgba(29, 95, 168, 0.35)';
const SMA25 = '#d97706';
const SMA200 = '#7c3aed';

const MINUTE = 60;
const TICK_WINDOW_SECONDS = 300;      // ティックチャートの表示幅
const MINUTE_VISIBLE_BARS = 90;       // 分足チャートに表示するバー本数（論理インデックス基準）
const MINUTE_RIGHT_PADDING_BARS = 5;  // 分足チャートの右側の余白（本数）
const GAP_SKIP_SECONDS = 5;           // これ以上の無約定はスキップ対象
const MAX_TAPE_ROWS = 200;
const MAX_TAPE_INSERTS_PER_FRAME = 40;
const MAX_TICK_UPDATES_PER_FRAME = 600;
const MAX_TICK_POINTS_AFTER_SEEK = 20000;
const MINUTE_CONTEXT_BARS = 30;       // セッション開始前にプリロードする直前の分足の本数
const MINUTE_HISTORY_EDGE_BARS = 10;  // 最古の足からこの本数以内に入ったら次のページを取る

const SYMBOL_SEARCH_DEBOUNCE_MS = 200; // 銘柄候補検索: この間隔だけ入力が止まってからサーバーへ問い合わせる

const BOARD_MIN_ROWS = 41;        // 板の行数（奇数・表示領域に応じて拡張）
const BOARD_ROW_HEIGHT = 20;
const BOARD_OVERSCAN_ROWS = 12;
const BOARD_REBASE_EDGE_ROWS = 4;
const BOARD_REBASE_SHIFT_ROWS = 8;
const BOARD_QTY_SPAN = 10;        // 現在値の上下この行数だけ数量を出す
const BOARD_MAX_FLASH = 6;        // 1 フレームで光らせる行の上限
const BOARD_EMOJI = ['🍌', '🍑', '🍓', '🍒', '🍇', '🍎', '🍊', '🍐', '🥝', '🍡', '🍩', '🌸'];
const BOARD_MAX_UNITS = 4;        // 1 行に並べる記号の最大個数
// 呼値の候補。実データの価格差から一番近いものを選ぶ。
const TICK_CANDIDATES = [0.1, 0.5, 1, 5, 10, 50, 100, 500, 1000];

const DEFAULT_ORDER_QTY = 100;    // 注文数量の既定値（単元株）

const els = {
  symbolInput: document.getElementById('symbol-input'),
  symbolList: document.getElementById('symbol-list'),
  dateInput: document.getElementById('date-input'),
  prevDay: document.getElementById('prev-day'),
  nextDay: document.getElementById('next-day'),
  loadButton: document.getElementById('load-button'),
  statusBar: document.getElementById('status-bar'),
  playButton: document.getElementById('play-button'),
  resetButton: document.getElementById('reset-button'),
  speedGroup: document.getElementById('speed-group'),
  skipGaps: document.getElementById('skip-gaps'),
  scrubber: document.getElementById('scrubber'),
  clock: document.getElementById('clock'),
  tape: document.getElementById('tape'),
  board: document.getElementById('board'),
  boardFix: document.getElementById('board-fix'),
  boardCenter: document.getElementById('board-center'),
  boardQuote: document.getElementById('board-quote'),
  orderQty: document.getElementById('order-qty'),
  cancelSell: document.getElementById('cancel-sell'),
  cancelBuy: document.getElementById('cancel-buy'),
  mobileBuyButton: document.getElementById('mobile-buy-button'),
  mobileSellButton: document.getElementById('mobile-sell-button'),
  posQty: document.getElementById('pos-qty'),
  posAvg: document.getElementById('pos-avg'),
  posPnl: document.getElementById('pos-pnl'),
  posTotal: document.getElementById('pos-total'),
  pnlModal: document.getElementById('pnl-modal'),
  pnlRealized: document.getElementById('pnl-realized'),
  pnlUnrealized: document.getElementById('pnl-unrealized'),
  pnlTotal: document.getElementById('pnl-total'),
  pnlPosition: document.getElementById('pnl-position'),
  pnlFills: document.getElementById('pnl-fills'),
  chartStage: document.getElementById('price-chart-stage'),
  minuteChart: document.getElementById('minute-chart'),
  dailyChart: document.getElementById('daily-chart'),
  chartInterval: document.getElementById('chart-interval'),
  minuteTab: document.getElementById('minute-tab'),
  dailyTab: document.getElementById('daily-tab'),
  dailyStatus: document.getElementById('daily-status'),
  dailyLegend: document.getElementById('daily-legend'),
  dailyReplayLiveness: document.getElementById('daily-replay-liveness'),
  dailyReplayState: document.getElementById('daily-replay-state'),
  dailyReplayTime: document.getElementById('daily-replay-time'),
  dailyReplayProgress: document.getElementById('daily-replay-progress'),
};

// --------------------------------------------------------------- utilities

const intFormat = new Intl.NumberFormat('ja-JP');
const priceFormat = new Intl.NumberFormat('ja-JP', { maximumFractionDigits: 2 });

function pad2(value) {
  return String(value).padStart(2, '0');
}

/** epoch 秒 (UTC 解釈) を HH:MM:SS に整形する。 */
function formatClock(seconds, withMillis) {
  if (!Number.isFinite(seconds)) return '--:--:--';
  const date = new Date(seconds * 1000);
  const base =
    pad2(date.getUTCHours()) + ':' + pad2(date.getUTCMinutes()) + ':' + pad2(date.getUTCSeconds());
  if (!withMillis) return base;
  return base + '.' + String(date.getUTCMilliseconds()).padStart(3, '0');
}

function formatHm(seconds) {
  const date = new Date(seconds * 1000);
  return pad2(date.getUTCHours()) + ':' + pad2(date.getUTCMinutes());
}

/** 損益の表示。円未満は丸め、必ず符号を付ける。 */
function signedYen(value) {
  const rounded = Math.round(value);
  const sign = rounded > 0 ? '+' : rounded < 0 ? '-' : '±';
  return sign + intFormat.format(Math.abs(rounded));
}

function pnlTone(value) {
  if (value > 0) return 'up';
  if (value < 0) return 'down';
  return '';
}

function setStatus(message, tone) {
  if (!message) {
    els.statusBar.hidden = true;
    els.statusBar.textContent = '';
    return;
  }
  els.statusBar.hidden = false;
  els.statusBar.textContent = message;
  els.statusBar.dataset.tone = tone || 'info';
}

async function getJson(url, { signal } = {}) {
  const response = await fetch(url, { signal });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      if (body && body.detail) detail = body.detail;
    } catch (error) {
      /* レスポンスが JSON でない場合はステータス文言のまま */
    }
    throw new Error(detail);
  }
  return response.json();
}

const requests = new RequestCoordinator({
  fetchJson: getJson,
  onActivityChange: (kind, active) => {
    if (kind === 'session') els.loadButton.disabled = active;
  },
});

function showCacheStatus(stem, status) {
  if (!status) {
    setStatus('');
    return;
  }
  if (status.state === 'stale-served') {
    setStatus('⚠ 縮退運転: サーバーに接続できないため ' + stem + ' の既存キャッシュを使用します', 'degraded');
    return;
  }
  if (status.state !== 'downloading') return;

  let progress = intFormat.format(status.bytesReceived || 0) + ' bytes';
  if (status.totalBytes) {
    const percent = Math.min(100, Math.floor((status.bytesReceived || 0) * 100 / status.totalBytes));
    progress = percent + '% (' + progress + ')';
  }
  setStatus('データをダウンロード中… ' + stem + ' ' + progress, 'info');
}

function addDays(isoDate, days) {
  const date = new Date(isoDate + 'T00:00:00Z');
  date.setUTCDate(date.getUTCDate() + days);
  return date.toISOString().slice(0, 10);
}

// ----------------------------------------------------------------- charts

const chartTheme = {
  autoSize: true,
  layout: {
    background: { type: 'solid', color: '#ffffff' },
    textColor: '#56504a',
    fontFamily: '"Yu Gothic UI", "Hiragino Sans", "Noto Sans JP", system-ui, sans-serif',
  },
  grid: {
    vertLines: { color: '#f2eee5' },
    horzLines: { color: '#f2eee5' },
  },
  rightPriceScale: { borderColor: '#e5e0d5' },
  timeScale: { borderColor: '#e5e0d5' },
  crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
};

function chartStageSize() {
  const bounds = els.chartStage.getBoundingClientRect();
  return {
    width: Math.max(1, Math.floor(bounds.width)),
    height: Math.max(1, Math.floor(bounds.height)),
  };
}

const minuteChart = LightweightCharts.createChart(els.minuteChart, {
  ...chartTheme,
  autoSize: false,
  ...chartStageSize(),
  timeScale: {
    ...chartTheme.timeScale,
    timeVisible: true,
    secondsVisible: false,
    tickMarkFormatter: (time) => formatHm(time),
  },
  localization: { timeFormatter: (time) => formatClock(time, false) },
});

const candleSeries = minuteChart.addCandlestickSeries({
  upColor: UP,
  downColor: DOWN,
  borderUpColor: UP,
  borderDownColor: DOWN,
  wickUpColor: UP,
  wickDownColor: DOWN,
  priceLineVisible: false,
});
candleSeries.priceScale().applyOptions({ scaleMargins: { top: 0.08, bottom: 0.28 } });

const volumeSeries = minuteChart.addHistogramSeries({
  priceFormat: { type: 'volume' },
  priceScaleId: 'volume',
  priceLineVisible: false,
  lastValueVisible: false,
});
volumeSeries.priceScale().applyOptions({ scaleMargins: { top: 0.78, bottom: 0 } });

let chartMode = 'minute';
const minuteViewport = new ChartViewportState();
const dailyViewport = new ChartViewportState();
let minuteChartDeferred = false;
let dailyChart = null;
let dailyCandleSeries = null;
let dailyVolumeSeries = null;
let dailySma25Series = null;
let dailySma200Series = null;

const tickChart = LightweightCharts.createChart(document.getElementById('tick-chart'), {
  ...chartTheme,
  timeScale: {
    ...chartTheme.timeScale,
    timeVisible: true,
    secondsVisible: true,
    tickMarkFormatter: (time) => formatClock(time, false),
  },
  localization: { timeFormatter: (time) => formatClock(time, true) },
});

const tickSeries = tickChart.addLineSeries({
  color: '#2e2a24',
  lineWidth: 1,
  lineType: LightweightCharts.LineType.WithSteps,
  priceLineVisible: true,
  priceLineColor: '#b7afa0',
  crosshairMarkerRadius: 3,
});

// ------------------------------------------------------------ replay state

const state = {
  meta: null,       // {stem, code, date, count}
  t: null,          // Float64Array — 約定時刻 (epoch 秒)
  price: null,      // Float64Array
  qty: null,        // Float64Array
  type: null,       // string[]
  cursor: 0,        // 次に流す約定の添字
  vt: 0,            // 仮想時刻 (epoch 秒)
  playing: false,
  speed: 1,
  bars: [],         // 集計済みの分足
  contextBars: [],  // セッション開始前にプリロードした直前の分足（stocks_minute）
  tickPoints: [],   // ティックチャートに投入済みの点
  last: null,
  lastFrame: 0,
  scrubbing: false,
  daily: new DailyChartSession(),
};

const replayLiveness = new ReplayLivenessPresenter((view) => {
  els.dailyReplayLiveness.hidden = view.hidden;
  els.dailyReplayState.textContent = view.stateLabel;
  els.dailyReplayTime.textContent = view.timeLabel;
  els.dailyReplayProgress.value = view.progressPercent;
});

function updateDailyReplayLiveness() {
  const hasTimeline = Boolean(state.t?.length);
  replayLiveness.present({
    mode: chartMode,
    playing: state.playing,
    virtualTime: hasTimeline ? state.vt : Number.NaN,
    startTime: hasTimeline ? state.t[0] : Number.NaN,
    endTime: hasTimeline ? state.t[state.t.length - 1] : Number.NaN,
  });
}

function setDailyStatus(message, tone = 'info') {
  els.dailyStatus.hidden = !message;
  els.dailyStatus.textContent = message || '';
  els.dailyStatus.dataset.tone = tone;
}

function applyChartStageSize() {
  const { width, height } = chartStageSize();
  minuteChart.resize(width, height);
  if (dailyChart) dailyChart.resize(width, height);
}

function ensureDailyChart() {
  if (dailyChart) return dailyChart;
  dailyChart = LightweightCharts.createChart(els.dailyChart, {
    ...chartTheme,
    autoSize: false,
    ...chartStageSize(),
    timeScale: { ...chartTheme.timeScale, timeVisible: false, secondsVisible: false },
  });
  dailyCandleSeries = dailyChart.addCandlestickSeries({
    upColor: UP,
    downColor: DOWN,
    borderUpColor: UP,
    borderDownColor: DOWN,
    wickUpColor: UP,
    wickDownColor: DOWN,
    priceLineVisible: false,
  });
  dailyCandleSeries.priceScale().applyOptions({ scaleMargins: { top: 0.08, bottom: 0.28 } });
  dailyVolumeSeries = dailyChart.addHistogramSeries({
    priceFormat: { type: 'volume' },
    priceScaleId: 'volume',
    priceLineVisible: false,
    lastValueVisible: false,
  });
  dailyVolumeSeries.priceScale().applyOptions({ scaleMargins: { top: 0.78, bottom: 0 } });
  dailySma25Series = dailyChart.addLineSeries({
    color: SMA25,
    lineWidth: 2,
    priceLineVisible: false,
    lastValueVisible: false,
  });
  dailySma200Series = dailyChart.addLineSeries({
    color: SMA200,
    lineWidth: 2,
    priceLineVisible: false,
    lastValueVisible: false,
  });
  dailyChart.timeScale().subscribeVisibleLogicalRangeChange(onDailyLogicalRangeChange);
  return dailyChart;
}

function renderMinuteChart({ restoreRange = false } = {}) {
  withProgrammaticMinuteRange(() => {
    updateChartViewport({
      viewport: minuteViewport,
      writeData: () => {
        candleSeries.setData(state.bars.map(paintedBar));
        volumeSeries.setData(state.bars.map(paintedVolume));
      },
      timeScale: minuteChart.timeScale(),
      restore: restoreRange,
    });
  });
  minuteChartDeferred = false;
}

function renderDailyChart({ restoreRange = false } = {}) {
  if (chartMode !== 'daily' || !state.daily.identity) return;
  ensureDailyChart();
  const snapshot = state.daily.snapshot();
  updateChartViewport({
    viewport: dailyViewport,
    writeData: () => renderDailySnapshot(snapshot, {
      candle: dailyCandleSeries,
      volume: dailyVolumeSeries,
      sma25: dailySma25Series,
      sma200: dailySma200Series,
    }, { paintCandle: paintedBar, paintVolume: paintedVolume }),
    timeScale: dailyChart.timeScale(),
  });
  applyDailyViewport(snapshot.bars.length, { restoreRange });
}

function applyDailyViewport(observationCount, { restoreRange = false } = {}) {
  if (!dailyChart) return false;
  const resolvedObservationCount = Number.isInteger(observationCount)
    ? observationCount
    : observationCount?.bars?.length;
  const savedRange = restoreRange && dailyViewport.savedRange
    ? { ...dailyViewport.savedRange }
    : null;
  const canInitialize = ['ready', 'empty'].includes(state.daily.phase);
  const initialRange = !savedRange && !state.daily.hasInitialViewport && canInitialize
    ? dailyInitialLogicalRange(resolvedObservationCount)
    : null;
  const targetRange = savedRange || initialRange;
  if (!targetRange) return false;

  try {
    dailyViewport.runProgrammatic(() => {
      dailyChart.timeScale().setVisibleLogicalRange(targetRange);
    });
  } catch (error) {
    console.warn('Daily chart viewport update failed', error);
    return false;
  }
  if (initialRange) return state.daily.markInitialViewport();
  return true;
}

function updateDailyPartialChart() {
  if (chartMode !== 'daily' || !dailyChart || !state.daily.identity || !state.daily.partialBar) return;
  const partial = state.daily.partialBar;
  dailyCandleSeries.update(paintedBar(partial));
  dailyVolumeSeries.update(paintedVolume(partial));
  if (state.daily.terminalSma25) dailySma25Series.update(paintedLinePoint(state.daily.terminalSma25));
  if (state.daily.terminalSma200) dailySma200Series.update(paintedLinePoint(state.daily.terminalSma200));
  if (!state.daily.hasInitialViewport) {
    applyDailyViewport(state.daily.historyBars.length + 1);
  }
}

function clearDailyDisplay() {
  setDailyStatus(chartMode === 'daily' ? '日足セッションを読み込み中…' : '');
  if (!dailyChart) return;
  dailyViewport.runProgrammatic(() => {
    dailyCandleSeries.setData([]);
    dailyVolumeSeries.setData([]);
    dailySma25Series.setData([]);
    dailySma200Series.setData([]);
  });
}

function refreshDailyStatus() {
  if (chartMode !== 'daily') {
    setDailyStatus('');
    return;
  }
  const messages = {
    idle: ['日足を準備しています…', 'info'],
    loading: ['日足履歴を読み込み中…', 'info'],
    ready: ['', 'info'],
    empty: ['この日より前の利用可能な日足はありません', 'info'],
    unavailable: ['日足履歴を利用できません。分足の再生は継続します', 'error'],
    error: ['日足履歴を読み込めませんでした。日足を選び直すと再試行します', 'error'],
  };
  const [message, tone] = messages[state.daily.phase] || messages.idle;
  setDailyStatus(message, tone);
}

async function loadDailyContext() {
  return loadDailyRequest({
    session: state.daily,
    fetchPayload: ({ identity, url }) => {
      const [stem] = identity.split('|');
      return requests.fetchLatest('daily-context', url, { stem });
    },
    getMode: () => chartMode,
    commitChart: () => renderDailyChart(),
    onStatus: () => refreshDailyStatus(),
    isCancellation: isCancellationError,
  });
}

function commitInactiveDailyHistoryPage(plan) {
  const previousRange = dailyViewport.savedRange
    ? { ...dailyViewport.savedRange }
    : null;
  const shiftedRange = shiftDailyLogicalRange(previousRange, plan.added);
  dailyViewport.replace(shiftedRange);
  try {
    const committed = state.daily.commitOlderPage(plan);
    if (!committed?.accepted) throw new Error('Daily history page commit was rejected');
    return committed;
  } catch (error) {
    dailyViewport.replace(previousRange);
    throw error;
  }
}

async function loadOlderDailyBars() {
  const identity = dailySessionIdentity(state.meta);
  if (!identity || identity !== state.daily.identity || !state.daily.historyBars.length) {
    return { kind: 'stale' };
  }
  const cutoff = state.daily.historyBars[0].time;
  const token = state.daily.admitOlderPage(cutoff, { identity });
  if (!token) return { kind: 'not-admitted' };
  const url = dailyContextUrl(identity, {
    beforeDate: cutoff,
    limit: state.daily.historyPageSize,
  });
  if (!url) {
    state.daily.failOlderPage(token);
    return { kind: 'failure' };
  }

  try {
    const [stem] = identity.split('|');
    const payload = await requests.fetchLatest('daily-context', url, { stem });
    if (!state.daily.isCurrentOlderPage(token) ||
        state.daily.identity !== identity ||
        dailySessionIdentity(state.meta) !== identity) {
      return { kind: 'stale' };
    }

    const normalized = normalizeDailyPayload(payload, state.daily.historyPageSize);
    const plan = state.daily.prepareOlderPage(token, normalized);
    if (plan.kind === 'stale') return plan;
    if (plan.kind === 'failure') {
      state.daily.failOlderPage(token);
      return plan;
    }
    if (plan.kind === 'exhausted') {
      state.daily.completeOlderExhausted(token);
      return plan;
    }

    if (chartMode === 'daily' && dailyChart) {
      applyDailyHistoryPage({
        session: state.daily,
        plan,
        viewport: dailyViewport,
        ports: {
          candle: dailyCandleSeries,
          volume: dailyVolumeSeries,
          sma25: dailySma25Series,
          sma200: dailySma200Series,
          paintCandle: paintedBar,
          paintVolume: paintedVolume,
        },
        timeScale: dailyChart.timeScale(),
      });
      return plan;
    }

    commitInactiveDailyHistoryPage(plan);
    return plan;
  } catch (error) {
    if (isCancellationError(error)) {
      state.daily.abortOlderPage(token);
      return { kind: 'cancelled' };
    }
    state.daily.failOlderPage(token);
    return { kind: 'failure', error };
  }
}

function onDailyLogicalRangeChange(range) {
  if (chartMode === 'daily') dailyViewport.capture(range);
  if (chartMode !== 'daily' || !range || !dailyCandleSeries) return;
  const identity = dailySessionIdentity(state.meta);
  if (!identity || identity !== state.daily.identity) return;
  const programmatic = dailyViewport.programmaticDepth > 0;
  if (programmatic) return;
  const visible = dailyCandleSeries.barsInLogicalRange(range);
  if (!visible || !state.daily.canLoadOlder({
    identity,
    barsBefore: visible.barsBefore,
    programmatic,
  })) return;
  void loadOlderDailyBars();
}

function setChartMode(mode) {
  if (mode !== 'minute' && mode !== 'daily') return;
  if (chartMode === 'minute') {
    const range = minuteChart.timeScale().getVisibleLogicalRange();
    minuteViewport.capture(range);
  } else if (dailyChart) {
    const range = dailyChart.timeScale().getVisibleLogicalRange();
    dailyViewport.capture(range);
  }

  chartMode = mode;
  const dailyActive = mode === 'daily';
  els.minuteChart.classList.toggle('is-active', !dailyActive);
  els.dailyChart.classList.toggle('is-active', dailyActive);
  els.minuteChart.setAttribute('aria-hidden', String(dailyActive));
  els.dailyChart.setAttribute('aria-hidden', String(!dailyActive));
  els.minuteTab.classList.toggle('is-selected', !dailyActive);
  els.dailyTab.classList.toggle('is-selected', dailyActive);
  els.minuteTab.setAttribute('aria-selected', String(!dailyActive));
  els.dailyTab.setAttribute('aria-selected', String(dailyActive));
  els.minuteTab.tabIndex = dailyActive ? -1 : 0;
  els.dailyTab.tabIndex = dailyActive ? 0 : -1;
  els.dailyLegend.hidden = !dailyActive;
  updateDailyReplayLiveness();

  if (dailyActive) {
    ensureDailyChart();
    applyChartStageSize();
    renderDailyChart({ restoreRange: true });
    void loadDailyContext();
  } else {
    applyChartStageSize();
    const flushResult = flushDeferredMinuteChart({
      mode,
      deferred: minuteChartDeferred,
      flush: () => {
        renderMinuteChart({ restoreRange: true });
        refreshMarkers();
      },
    });
    minuteChartDeferred = flushResult.deferred;
    if (!flushResult.flushed) {
      renderMinuteChart({ restoreRange: true });
      refreshMarkers();
    }
    setDailyStatus('');
  }
}

function candleColor(bar) {
  // 始値 <= 終値(最新値) なら陽線(赤)、始値 > 終値なら陰線(青)。
  return bar.close >= bar.open ? UP : DOWN;
}

function paintedBar(bar) {
  const color = candleColor(bar);
  return {
    time: bar.time,
    open: bar.open,
    high: bar.high,
    low: bar.low,
    close: bar.close,
    color,
    borderColor: color,
    wickColor: color,
  };
}

function paintedVolume(bar) {
  return {
    time: bar.time,
    value: bar.volume,
    color: bar.close >= bar.open ? UP_FILL : DOWN_FILL,
  };
}

/** 約定 1 件を状態に反映する。描画は呼び出し側でまとめて行う。 */
function applyTick(index, touched) {
  const time = state.t[index];
  const price = state.price[index];
  const quantity = state.qty[index];

  state.last = price;

  const minuteStart = Math.floor(time / MINUTE) * MINUTE;
  let bar = state.bars.length ? state.bars[state.bars.length - 1] : null;
  if (bar === null || bar.time !== minuteStart) {
    bar = { time: minuteStart, open: price, high: price, low: price, close: price, volume: 0 };
    state.bars.push(bar);
  }
  if (price > bar.high) bar.high = price;
  if (price < bar.low) bar.low = price;
  bar.close = price;
  bar.volume += quantity;

  if (state.meta && state.daily.canAppendTick(minuteSessionIdentity(state.meta))) {
    state.daily.appendTick(state.meta.date, price, quantity);
  }

  if (touched && touched.indexOf(bar) === -1) touched.push(bar);
  return bar;
}

// ------------------------------------------------------------------- tape

function tapeDirection(index) {
  if (index === 0) return 'flat';
  const delta = state.price[index] - state.price[index - 1];
  if (delta > 0) return 'up';
  if (delta < 0) return 'down';
  return 'flat';
}

function tapeRow(index) {
  const row = document.createElement('li');
  row.className = tapeDirection(index);
  const cells = [
    [formatClock(state.t[index], true), ''],
    [priceFormat.format(state.price[index]), 'tape-price'],
    [intFormat.format(state.qty[index]), ''],
    [state.type[index] || '', ''],
  ];
  cells.forEach(([text, className]) => {
    const cell = document.createElement('span');
    cell.className = className;
    cell.textContent = text;
    row.appendChild(cell);
  });
  return row;
}

function pushTapeRows(fromIndex, toIndex) {
  const total = toIndex - fromIndex;
  if (total <= 0) return;
  // 1 フレームで大量に流れた場合は直近ぶんだけ描く（DOM を溢れさせない）。
  const start = total > MAX_TAPE_INSERTS_PER_FRAME ? toIndex - MAX_TAPE_INSERTS_PER_FRAME : fromIndex;
  const fragment = document.createDocumentFragment();
  for (let index = toIndex - 1; index >= start; index -= 1) {
    fragment.appendChild(tapeRow(index));
  }
  els.tape.insertBefore(fragment, els.tape.firstChild);
  while (els.tape.childElementCount > MAX_TAPE_ROWS) {
    els.tape.removeChild(els.tape.lastChild);
  }
}

function rebuildTape() {
  els.tape.textContent = '';
  const start = Math.max(0, state.cursor - MAX_TAPE_ROWS);
  const fragment = document.createDocumentFragment();
  for (let index = state.cursor - 1; index >= start; index -= 1) {
    fragment.appendChild(tapeRow(index));
  }
  els.tape.appendChild(fragment);
}

// ------------------------------------------------------------------- 板

/*
 * 板固定（呼値固定モード）の板。価格の列は動かさず、現在値の方が板の中を
 * 上下する。歩み値には板情報が無いので、数量は「らしさ」の演出:
 *   - 現在値の上下 BOARD_QTY_SPAN 行だけ記号を並べ、外は空白
 *   - 最良買と最良売の間は 0/1/2 呼値ぶん空ける（0 > 1 > 2 の確率）
 *   - 直近約定が上昇なら現在値＝最良売、下降なら現在値＝最良買とみなす
 *   - 約定した価格の行は一瞬光らせる
 */

const board = {
  tick: 1,          // 呼値（データから推定）
  topLevel: null,   // 最上段（最高値）の呼値レベル。板固定の錨。
  rows: [],         // bounded physical row pool; every repaint derives a logical level
  qty: new Map(),   // level -> 記号文字列
  flashLevels: new Set(),
  spread: 0,        // 最良買と最良売の間に空ける呼値の数 (0/1/2)
  side: 'buy',      // 直近約定が売り板を食った(buy) / 買い板を食った(sell)
  manual: false,    // 板固定中にユーザーが現在値から離れているか
  priceFormatter: createBoardPriceFormatter(1),
  programmaticScroll: false,
  programmaticScrollTarget: null,
  deferredScrollTop: null,
  deferredScrollMarksManual: false,
  scrollFrame: null,
  scrollReleaseFrame: null,
  scrollGeneration: 0,
};

/** 価格差の最小値から呼値を推定する（データに呼値の情報が無いため）。 */
function inferTickSize() {
  const limit = Math.min(state.price.length, 4000);
  const seen = new Set();
  for (let index = 0; index < limit; index += 1) seen.add(state.price[index]);
  const sorted = Array.from(seen).sort((a, b) => a - b);

  let minDiff = Infinity;
  for (let index = 1; index < sorted.length; index += 1) {
    const diff = sorted[index] - sorted[index - 1];
    if (diff > 1e-9 && diff < minDiff) minDiff = diff;
  }
  if (!Number.isFinite(minDiff)) return 1;

  let best = TICK_CANDIDATES[0];
  let bestError = Infinity;
  TICK_CANDIDATES.forEach((candidate) => {
    const error = Math.abs(Math.log(candidate / minDiff));
    if (error < bestError) {
      bestError = error;
      best = candidate;
    }
  });
  return best;
}

function levelOf(price) {
  return Math.round(price / board.tick);
}

function priceOfLevel(level) {
  return Math.round(level * board.tick * 1e4) / 1e4;
}

/** スプレッドを 0 > 1 > 2 の確率で引く。 */
function rollSpread() {
  const roll = Math.random();
  if (roll < 0.6) return 0;
  if (roll < 0.9) return 1;
  return 2;
}

function randomQuantity() {
  const emoji = BOARD_EMOJI[Math.floor(Math.random() * BOARD_EMOJI.length)];
  return emoji.repeat(1 + Math.floor(Math.random() * BOARD_MAX_UNITS));
}

/** 呼値レベルの数量。一度決めた行は約定するまで変えない（チラつき防止）。 */
function quantityAt(level) {
  let quantity = board.qty.get(level);
  if (quantity === undefined) {
    quantity = randomQuantity();
    board.qty.set(level, quantity);
  }
  return quantity;
}

function cancelBoardScrollWork() {
  board.scrollGeneration += 1;
  if (board.scrollFrame !== null) cancelAnimationFrame(board.scrollFrame);
  if (board.scrollReleaseFrame !== null) cancelAnimationFrame(board.scrollReleaseFrame);
  board.scrollFrame = null;
  board.scrollReleaseFrame = null;
  board.programmaticScroll = false;
  board.programmaticScrollTarget = null;
  board.deferredScrollTop = null;
  board.deferredScrollMarksManual = false;
}

function setBoardScrollTop(scrollTop) {
  board.programmaticScroll = true;
  board.programmaticScrollTarget = scrollTop;
  board.deferredScrollTop = null;
  board.deferredScrollMarksManual = false;
  els.board.scrollTop = scrollTop;
  if (board.scrollReleaseFrame !== null) cancelAnimationFrame(board.scrollReleaseFrame);
  const generation = board.scrollGeneration;
  board.scrollReleaseFrame = requestAnimationFrame(() => {
    board.scrollReleaseFrame = null;
    if (generation !== board.scrollGeneration) return;
    const targetScrollTop = board.programmaticScrollTarget;
    const actualScrollTop = els.board.scrollTop;
    board.programmaticScroll = false;
    board.deferredScrollTop = null;
    if (hasProgrammaticScrollDrift(actualScrollTop, targetScrollTop)) {
      board.programmaticScrollTarget = null;
      scheduleBoardScrollReconcile(actualScrollTop, true);
    }
  });
}

function resetBoardNavigation() {
  cancelBoardScrollWork();
  board.manual = false;
  board.topLevel = null;
  setBoardScrollTop(0);
}

function boardRowHeight() {
  if (board.rows.length > 1) {
    const height = board.rows[1].li.offsetTop - board.rows[0].li.offsetTop;
    if (height > 0) return height;
  }
  return board.rows[0]?.li.offsetHeight || BOARD_ROW_HEIGHT;
}

function centerBoardLevelInViewport(level) {
  if (board.topLevel === null) return;
  const rowIndex = rowIndexForLevel(board.topLevel, level, board.rows.length);
  if (rowIndex < 0) return;
  const row = board.rows[rowIndex].li;
  setBoardScrollTop(centeredScrollTop({
    rowOffsetTop: row.offsetTop,
    rowHeight: row.offsetHeight || boardRowHeight(),
    viewportHeight: els.board.clientHeight,
    maxScrollTop: Math.max(0, els.board.scrollHeight - els.board.clientHeight),
  }));
}

function buildBoardRows() {
  const nextRowCount = boardRowCount(els.board.clientHeight, {
    rowHeight: BOARD_ROW_HEIGHT,
    minRows: BOARD_MIN_ROWS,
    overscanRows: BOARD_OVERSCAN_ROWS,
  });
  if (board.rows.length === nextRowCount) return;

  let visibleCenterLevel = null;
  const wasManual = board.manual;
  if (board.topLevel !== null && board.rows.length) {
    const firstOffset = board.rows[0].li.offsetTop;
    const centerOffset = els.board.scrollTop + els.board.clientHeight / 2 - firstOffset;
    const centerIndex = Math.max(
      0,
      Math.min(board.rows.length - 1, Math.floor(centerOffset / boardRowHeight())),
    );
    visibleCenterLevel = levelAtRow(board.topLevel, centerIndex);
  }

  cancelBoardScrollWork();
  els.board.textContent = '';
  board.rows = [];
  const fragment = document.createDocumentFragment();
  for (let index = 0; index < nextRowCount; index += 1) {
    const li = document.createElement('li');
    li.dataset.index = String(index); // クリック位置 → 呼値レベルの逆算に使う
    const sellOrder = document.createElement('span');
    const ask = document.createElement('span');
    const price = document.createElement('span');
    const bid = document.createElement('span');
    const buyOrder = document.createElement('span');
    sellOrder.className = 'board-order order-sell';
    sellOrder.title = '売り注文（現在値より上は指値・以下は成行）';
    ask.className = 'board-ask';
    price.className = 'board-price';
    bid.className = 'board-bid';
    buyOrder.className = 'board-order order-buy';
    buyOrder.title = '買い注文（現在値より下は指値・以上は成行）';
    li.appendChild(sellOrder);
    li.appendChild(ask);
    li.appendChild(price);
    li.appendChild(bid);
    li.appendChild(buyOrder);
    fragment.appendChild(li);
    const row = {
      li, ask, price, bid, sellOrder, buyOrder,
      askText: '', priceText: '', bidText: '', className: '',
      sellOrderText: '', buyOrderText: '',
      flashLevel: null,
    };
    // 光り終わったら論理レベルとクラスを一緒に外す。
    li.addEventListener('animationend', () => {
      if (row.flashLevel !== null) board.flashLevels.delete(row.flashLevel);
      row.flashLevel = null;
      li.classList.remove('flash');
    });
    board.rows.push(row);
  }
  els.board.appendChild(fragment);

  if (state.last === null) {
    resetBoardNavigation();
    return;
  }
  if (wasManual && visibleCenterLevel !== null) {
    board.manual = true;
    board.topLevel = centeredTopLevel(visibleCenterLevel, board.rows.length);
    updateBoard();
    centerBoardLevelInViewport(visibleCenterLevel);
    return;
  }
  resetBoardNavigation();
  updateBoard();
}

/** 板を現在値へ張り直す（中央 = centerLevel）。 */
function anchorBoard(centerLevel) {
  board.manual = false;
  board.topLevel = centeredTopLevel(centerLevel, board.rows.length);
  centerBoardLevelInViewport(centerLevel);
}

function clearBoard() {
  resetBoardNavigation();
  board.qty.clear();
  board.flashLevels.clear();
  board.rows.forEach((row) => {
    row.price.textContent = '';
    row.ask.textContent = '';
    row.bid.textContent = '';
    row.sellOrder.textContent = '';
    row.buyOrder.textContent = '';
    row.sellOrder.className = 'board-order order-sell';
    row.buyOrder.className = 'board-order order-buy';
    row.li.className = '';
    row.askText = '';
    row.priceText = '';
    row.bidText = '';
    row.sellOrderText = '';
    row.buyOrderText = '';
    row.className = '';
    row.flashLevel = null;
  });
  els.boardQuote.textContent = '—';
}

function setRowText(row, ask, bid, className) {
  if (row.askText !== ask) {
    row.ask.textContent = ask;
    row.askText = ask;
  }
  if (row.bidText !== bid) {
    row.bid.textContent = bid;
    row.bidText = bid;
  }
  if (row.className !== className) {
    // flash は再生中に付いたり外れたりするので、行の状態クラスとは別に温存する。
    const flashing = row.li.classList.contains('flash');
    row.li.className = flashing ? (className + ' flash').trim() : className;
    row.className = className;
  }
}

/** 発注列のセル 1 つ。未約定の注文があれば数量を出す。 */
function setOrderCell(row, key, quantity) {
  const text = quantity ? intFormat.format(quantity) : '';
  if (row[key + 'Text'] === text) return;
  row[key + 'Text'] = text;
  const cell = row[key];
  cell.textContent = text;
  cell.classList.toggle('has-order', text !== '');
}

function syncRowFlash(row, level) {
  if (row.flashLevel === level && board.flashLevels.has(level)) return;
  row.li.classList.remove('flash');
  row.flashLevel = null;
  if (!board.flashLevels.has(level)) return;
  row.flashLevel = level;
  row.li.classList.add('flash');
}

function updateBoard() {
  if (state.last === null) {
    clearBoard();
    return;
  }

  const lastLevel = levelOf(state.last);
  const bottomLevel = board.topLevel === null
    ? null
    : levelAtRow(board.topLevel, board.rows.length - 1);
  if (
    board.topLevel === null ||
    !els.boardFix.checked ||
    (!board.manual && (lastLevel > board.topLevel || lastLevel < bottomLevel))
  ) {
    // 手動位置でなければ、固定中も現在値が行プールを出たところで張り直す。
    anchorBoard(lastLevel);
  }

  const askLevel = board.side === 'buy' ? lastLevel : lastLevel + board.spread + 1;
  const bidLevel = board.side === 'buy' ? lastLevel - board.spread - 1 : lastLevel;

  const pendingSell = pendingByLevel('sell');
  const pendingBuy = pendingByLevel('buy');
  board.flashLevels.forEach((level) => {
    if (rowIndexForLevel(board.topLevel, level, board.rows.length) < 0) {
      board.flashLevels.delete(level);
    }
  });

  board.rows.forEach((row, index) => {
    const level = levelAtRow(board.topLevel, index);
    const priceText = board.priceFormatter.format(priceOfLevel(level));
    if (row.priceText !== priceText) {
      row.price.textContent = priceText;
      row.priceText = priceText;
    }
    const near = Math.abs(level - lastLevel) <= BOARD_QTY_SPAN;
    const isAsk = near && level >= askLevel;
    const isBid = near && level <= bidLevel;
    const side = isAsk ? 'ask-side' : isBid ? 'bid-side' : '';
    const className = level === lastLevel ? (side + ' last').trim() : side;
    setRowText(row, isAsk ? quantityAt(level) : '', isBid ? quantityAt(level) : '', className);
    syncRowFlash(row, level);
    setOrderCell(
      row,
      'sellOrder',
      pendingQuantityAtRow(pendingSell, board.topLevel, index, board.rows.length),
    );
    setOrderCell(
      row,
      'buyOrder',
      pendingQuantityAtRow(pendingBuy, board.topLevel, index, board.rows.length),
    );
  });

  els.boardQuote.textContent =
    '売 ' + board.priceFormatter.format(priceOfLevel(askLevel)) +
    ' / 買 ' + board.priceFormatter.format(priceOfLevel(bidLevel)) +
    ' ・ スプレッド ' + board.spread;
}

function centerBoardOnCurrent() {
  resetBoardNavigation();
  updateBoard();
}

function scheduleBoardScrollReconcile(scrollTop, marksManual) {
  board.deferredScrollTop = scrollTop;
  board.deferredScrollMarksManual = board.deferredScrollMarksManual || marksManual;
  if (board.scrollFrame !== null || board.topLevel === null) return;
  const generation = board.scrollGeneration;
  board.scrollFrame = requestAnimationFrame(() => {
    board.scrollFrame = null;
    if (
      generation !== board.scrollGeneration ||
      board.programmaticScroll ||
      board.topLevel === null
    ) return;

    const latestScrollTop = board.deferredScrollTop ?? els.board.scrollTop;
    const shouldMarkManual = board.deferredScrollMarksManual;
    board.deferredScrollTop = null;
    board.deferredScrollMarksManual = false;
    if (!els.boardFix.checked) {
      centerBoardOnCurrent();
      return;
    }
    if (shouldMarkManual) board.manual = true;
    const result = planBoardRebase({
      scrollTop: latestScrollTop,
      viewportHeight: els.board.clientHeight,
      scrollHeight: els.board.scrollHeight,
      rowHeight: boardRowHeight(),
      topLevel: board.topLevel,
      rowCount: board.rows.length,
      edgeRows: BOARD_REBASE_EDGE_ROWS,
      shiftRows: BOARD_REBASE_SHIFT_ROWS,
    });
    if (!result) return;

    board.topLevel = result.topLevel;
    updateBoard();
    setBoardScrollTop(result.scrollTop);
  });
}

function onBoardScroll() {
  if (board.topLevel === null) return;
  if (board.programmaticScroll) {
    board.deferredScrollTop = els.board.scrollTop;
    return;
  }
  if (board.programmaticScrollTarget !== null) {
    const targetScrollTop = board.programmaticScrollTarget;
    board.programmaticScrollTarget = null;
    if (!hasProgrammaticScrollDrift(els.board.scrollTop, targetScrollTop)) return;
  }
  scheduleBoardScrollReconcile(els.board.scrollTop, true);
}

/** 約定した価格の行を光らせる（アニメーションを頭から流し直す）。 */
function flashBoardLevel(level) {
  if (board.topLevel === null) return;
  const index = rowIndexForLevel(board.topLevel, level, board.rows.length);
  if (index < 0) return;
  const row = board.rows[index];
  const li = row.li;
  board.flashLevels.add(level);
  row.flashLevel = level;
  li.classList.remove('flash');
  void li.offsetWidth; // reflow を挟まないと同じ行の連続約定でアニメが再生されない
  li.classList.add('flash');
}

/** 1 フレーム分の約定を板に反映する。 */
function applyTradesToBoard(fromIndex, toIndex) {
  const direction = tapeDirection(toIndex - 1);
  if (direction === 'up') board.side = 'buy';
  else if (direction === 'down') board.side = 'sell';
  board.spread = rollSpread();

  // 高速再生では 1 フレームに数千件届く。板に効かせるのは直近ぶんだけ。
  const start = Math.max(fromIndex, toIndex - BOARD_MAX_FLASH);
  for (let index = start; index < toIndex; index += 1) {
    board.qty.delete(levelOf(state.price[index])); // 約定した板は入れ替わったとみなす
  }
  updateBoard();
  for (let index = start; index < toIndex; index += 1) {
    flashBoardLevel(levelOf(state.price[index]));
  }
}

// ------------------------------------------------------------- 仮想発注

/*
 * 板の両脇の発注列を 1 クリックすると注文が出る。
 *   - 売り列: 現在値より上 = 指値（板がそこまで来たら約定）／現在値以下 = 成行
 *   - 買い列: 現在値より下 = 指値／現在値以上 = 成行
 * 未約定の指値は板の同じ行に数量として残り、最下段の「取消」で消える。
 *
 * 建玉は一日信用・両建て禁止のネット 1 本だけ。反対売買は返済になり、
 * 返済しきった後は同じ日に何度でも建て直せる。
 */

const trading = {
  portfolio: createPortfolio(),
  orders: [],       // 未約定の指値 {id, side, level, price, qty}
  fills: [],        // 約定した注文（時系列）。建玉はここから導出する
  events: [],       // 約定イベント（チャートのマーカーの元ネタ）
  nextOrderId: 1,
};

/** 注文数量。単元株未満や未入力は既定値に丸める。 */
function orderQuantity() {
  const value = Math.floor(Number(els.orderQty.value));
  if (!Number.isFinite(value) || value <= 0) return DEFAULT_ORDER_QTY;
  return value;
}

/** 未約定の指値を呼値レベルごとに合算する（板の描画用）。 */
function pendingByLevel(side) {
  const totals = new Map();
  trading.orders.forEach((order) => {
    if (order.side !== side) return;
    totals.set(order.level, (totals.get(order.level) || 0) + order.qty);
  });
  return totals;
}

/** 約定 1 件を建玉に反映して、パネルとチャートのマーカーを更新する。 */
function executeFill(side, quantity, price, time) {
  const fill = { side, qty: quantity, price, time };
  trading.fills.push(fill);
  applyFill(trading.portfolio, fill).forEach((event) => trading.events.push(event));
  refreshMarkers();
  updatePositionPanel();
}

/** 約定の並びから建玉とイベントを作り直す（シークで履歴を切ったあと）。 */
function rebuildPortfolio() {
  trading.portfolio = createPortfolio();
  trading.events = [];
  trading.fills.forEach((fill) => {
    applyFill(trading.portfolio, fill).forEach((event) => trading.events.push(event));
  });
}

/*
 * シーク先より後の取引をなかったことにする。途中から再生をやり直したとき、
 * その先で出した注文と約定が残っていると、同じ値動きで二重に成績が乗る。
 * 未約定の注文もシーク先には持ち越さない（出した時刻より前へも飛べるため）。
 */
function truncateTradingAt(targetVt) {
  const kept = trading.fills.filter((fill) => fill.time <= targetVt);
  const dropped = kept.length !== trading.fills.length;
  trading.fills = kept;
  trading.orders = [];
  if (dropped) rebuildPortfolio();
}

/** 発注列のクリック 1 回ぶんの処理。 */
function placeOrder(side, level) {
  if (state.last === null) return;
  const quantity = orderQuantity();

  if (classifyClick(side, level, levelOf(state.last)) === 'market') {
    executeFill(side, quantity, state.last, currentTradeTime());
    updateBoard();
    return;
  }

  // 同じ行を続けて押したときは数量を積み増す（注文の本数は増やさない）。
  const price = priceOfLevel(level);
  const existing = trading.orders.find((order) => order.side === side && order.level === level);
  if (existing) existing.qty += quantity;
  else trading.orders.push({ id: trading.nextOrderId++, side, level, price, qty: quantity });
  updateBoard();
}

/*
 * スマホ幅では板を出さず、代わりに買う/売るの大きいボタンを出す。板が無いので
 * 指値は選べず、常に成行注文（placeOrder の成行分岐と同じ処理）にする。
 */
function placeMobileMarketOrder(side) {
  if (state.last === null) return;
  executeFill(side, orderQuantity(), state.last, currentTradeTime());
  updateBoard();
}

function syncMobileTradeButtons() {
  const disabled = state.last === null;
  els.mobileBuyButton.disabled = disabled;
  els.mobileSellButton.disabled = disabled;
}

function cancelOrders(side) {
  const before = trading.orders.length;
  trading.orders = trading.orders.filter((order) => order.side !== side);
  if (trading.orders.length !== before) updateBoard();
}

/**
 * このフレームで流れた約定を未約定の指値に突き合わせる。
 * 指値を抜けて約定した場合も約定値は指値どおり（不利な滑りは入れない）。
 */
function matchOrders(fromIndex, toIndex) {
  if (!trading.orders.length) return;
  for (let index = fromIndex; index < toIndex; index += 1) {
    if (!trading.orders.length) break;
    const price = state.price[index];
    const filled = trading.orders.filter((order) => isFilled(order, price));
    if (!filled.length) continue;
    trading.orders = trading.orders.filter((order) => !isFilled(order, price));
    filled.forEach((order) => executeFill(order.side, order.qty, order.price, state.t[index]));
  }
}

/** 直近に流れた約定の時刻。まだ 1 件も流れていなければ仮想時刻。 */
function currentTradeTime() {
  if (state.t && state.cursor > 0) return state.t[state.cursor - 1];
  return state.vt;
}

// -------------------------------------------------------- チャートのマーカー

/*
 * ティックチャートは高速再生で間引かれ、シークでも先頭が切り落とされる。
 * マーカーの時刻が系列に無いと描かれないので、一番近い実データ点に寄せる。
 * 系列の範囲外なら null を返し、そのマーカーは出さない。
 */
function snapTickTime(time) {
  const points = state.tickPoints;
  if (!points.length) return null;
  if (time < points[0].time || time > points[points.length - 1].time) return null;

  let low = 0;
  let high = points.length - 1;
  while (low < high) {
    const mid = (low + high) >> 1;
    if (points[mid].time < time) low = mid + 1;
    else high = mid;
  }
  if (low > 0 && Math.abs(points[low - 1].time - time) < Math.abs(points[low].time - time)) {
    return points[low - 1].time;
  }
  return points[low].time;
}

function markerStyle(event) {
  if (event.kind === 'entry') {
    const isBuy = event.side === 'buy';
    return {
      position: isBuy ? 'belowBar' : 'aboveBar',
      color: isBuy ? UP : DOWN,
      shape: isBuy ? 'arrowUp' : 'arrowDown',
      text: (isBuy ? '買' : '売') + intFormat.format(event.qty),
    };
  }
  return {
    position: event.side === 'buy' ? 'belowBar' : 'aboveBar',
    color: event.pnl >= 0 ? UP : DOWN,
    shape: 'circle',
    text: '返済 ' + signedYen(event.pnl),
  };
}

/*
 * 約定イベントから両チャートのマーカーを作り直す。setMarkers は全差し替え
 * なので、シーク後の再描画でもこれを一度呼べば表示が整合する。
 */
function refreshMarkers() {
  const firstBar = state.bars.length ? state.bars[0].time : null;
  const lastBar = state.bars.length ? state.bars[state.bars.length - 1].time : null;
  const minuteMarkers = [];
  const tickMarkers = [];

  trading.events.forEach((event) => {
    const style = markerStyle(event);
    const barTime = Math.floor(event.time / MINUTE) * MINUTE;
    if (firstBar !== null && barTime >= firstBar && barTime <= lastBar) {
      minuteMarkers.push({ ...style, time: barTime });
    }
    const tickTime = snapTickTime(event.time);
    if (tickTime !== null) tickMarkers.push({ ...style, time: tickTime });
  });

  commitMarkerWrites({
    mode: chartMode,
    minuteMarkers,
    tickMarkers,
    ports: {
      minute: (markers) => candleSeries.setMarkers(markers),
      deferMinute: () => { minuteChartDeferred = true; },
      tick: (markers) => tickSeries.setMarkers(markers),
    },
  });
}

// -------------------------------------------------------------- 建玉パネル

function updatePositionPanel() {
  const portfolio = trading.portfolio;
  const held = Math.abs(portfolio.qty);

  if (held === 0) {
    els.posQty.textContent = '—';
    els.posQty.dataset.tone = '';
    els.posAvg.textContent = '—';
  } else {
    els.posQty.textContent = (portfolio.qty > 0 ? '買 ' : '売 ') + intFormat.format(held) + ' 株';
    els.posQty.dataset.tone = portfolio.qty > 0 ? 'up' : 'down';
    els.posAvg.textContent = priceFormat.format(portfolio.avgPrice);
  }

  const lastPrice = state.last === null ? NaN : state.last;
  const unrealized = unrealizedPnl(portfolio, lastPrice);
  els.posPnl.textContent = held === 0 ? '—' : signedYen(unrealized);
  els.posPnl.dataset.tone = held === 0 ? '' : pnlTone(unrealized);

  // 合計損益は建玉が無くても（実現ぶんだけでも）出す。
  const traded = trading.fills.length > 0;
  const total = totalPnl(portfolio, lastPrice);
  els.posTotal.textContent = traded ? signedYen(total) : '—';
  els.posTotal.dataset.tone = traded ? pnlTone(total) : '';

  syncMobileTradeButtons();

  if (!els.pnlModal.hidden) updatePnlModal();
}

/** 建玉・注文・約定履歴をすべて捨てる（先頭に戻す / 読み込み直し）。 */
function resetTrading() {
  trading.portfolio = createPortfolio();
  trading.orders = [];
  trading.fills = [];
  trading.events = [];
  closePnlModal();
  refreshMarkers();
  updatePositionPanel();
  updateBoard();
}

// --------------------------------------------------------- 合計損益モーダル

function updatePnlModal() {
  const portfolio = trading.portfolio;
  const lastPrice = state.last === null ? NaN : state.last;
  const unrealized = unrealizedPnl(portfolio, lastPrice);
  const total = totalPnl(portfolio, lastPrice);
  const held = Math.abs(portfolio.qty);

  els.pnlRealized.textContent = signedYen(portfolio.realized);
  els.pnlRealized.dataset.tone = pnlTone(portfolio.realized);
  els.pnlUnrealized.textContent = held === 0 ? '—' : signedYen(unrealized);
  els.pnlUnrealized.dataset.tone = held === 0 ? '' : pnlTone(unrealized);
  els.pnlTotal.textContent = signedYen(total);
  els.pnlTotal.dataset.tone = pnlTone(total);
  els.pnlPosition.textContent =
    held === 0
      ? 'なし'
      : (portfolio.qty > 0 ? '買 ' : '売 ') + intFormat.format(held) + ' 株 @ ' +
        priceFormat.format(portfolio.avgPrice);
  els.pnlFills.textContent =
    '新規 ' + intFormat.format(portfolio.entryCount) +
    ' / 返済 ' + intFormat.format(portfolio.exitCount);
}

function openPnlModal() {
  updatePnlModal();
  els.pnlModal.hidden = false;
}

function closePnlModal() {
  els.pnlModal.hidden = true;
}

// ------------------------------------------------------------------ clock

function updateClock() {
  if (!state.meta || !state.t.length) {
    els.clock.textContent = '--:--:-- / --:--:--';
    return;
  }
  els.clock.textContent =
    formatClock(state.vt, false) + ' / ' + formatClock(state.t[state.t.length - 1], false);
}

function syncScrubber() {
  if (state.scrubbing || !state.t || !state.t.length) return;
  const first = state.t[0];
  const last = state.t[state.t.length - 1];
  const span = last - first;
  const position = span > 0 ? ((state.vt - first) / span) * 1000 : 0;
  els.scrubber.value = String(Math.max(0, Math.min(1000, Math.round(position))));
}

// -------------------------------------------------- 分足チャートの履歴読込

/*
 * 分足チャートは「一度ユーザーが触ったら、以降はユーザーのもの」にする。
 * 再生ループは毎フレーム分足の可視レンジを書き戻さない（ティックだけ追従）。
 * Lightweight Charts は可視範囲を「最後の足からの相対位置」で保持するので、
 * 右端に居るあいだは新しい足へ自然に追従し、左へパンして履歴を見ている
 * あいだは勝手に引き戻されない。
 *
 * 最古の足まで MINUTE_HISTORY_EDGE_BARS 本以内に近づいたら、既存の
 * /api/minute-context を「その足より前」の cutoff で叩き、さらに古い足を
 * 1 ページぶん前に足す。取得は常に 1 本だけ（単一飛行）で、セッションが
 * 変わると世代が進み、飛んでいた古いレスポンスは破棄される。
 */
const minuteHistory = new MinuteHistorySession({
  edgeThresholdBars: MINUTE_HISTORY_EDGE_BARS,
});

function minuteSessionIdentity(session) {
  if (!session?.stem || !session?.code || !session?.date) return null;
  return session.stem + '|' + session.code + '|' + session.date;
}

/** こちらから可視レンジを動かす区間。範囲変化コールバックの再入を止める。 */
function withProgrammaticMinuteRange(apply) {
  minuteHistory.beginProgrammatic();
  try {
    return minuteViewport.runProgrammatic(apply);
  } finally {
    // 可視レンジ変化の通知は同期とは限らないので、次のタスクまで伏せておく。
    setTimeout(() => minuteHistory.endProgrammatic(), 0);
  }
}

/** いま読み込んでいる分足のうち最も古い足の時刻（次の cutoff の基準）。 */
function earliestMinuteBarTime() {
  if (state.contextBars.length) return state.contextBars[0].time;
  if (state.bars.length) return state.bars[0].time;
  return null;
}

function minuteContextUrl(session, cutoff, limit) {
  return '/api/minute-context?stem=' + encodeURIComponent(session.stem) +
    '&code=' + encodeURIComponent(session.code) +
    '&date=' + encodeURIComponent(cutoff.date) +
    '&time=' + encodeURIComponent(cutoff.time) +
    '&limit=' + limit;
}

function minuteHistoryChartTarget() {
  const realTarget = {
    candleSeries,
    volumeSeries,
    timeScale: minuteChart.timeScale(),
    refreshMarkers,
  };
  if (chartMode === 'minute') {
    return selectMinuteHistoryTarget({
      mode: chartMode,
      realTarget,
      viewport: minuteViewport,
    });
  }
  minuteChartDeferred = true;
  return selectMinuteHistoryTarget({
    mode: chartMode,
    realTarget,
    viewport: minuteViewport,
    deferMinute: () => { minuteChartDeferred = true; },
    refreshMarkers,
  });
}

/**
 * 取得した古い足を contextBars と bars の両方へ、同じ集合だけ前置きする。
 * contextBars にも入れるのは、シークや「先頭に戻す」で bars が contextBars
 * から作り直されるため（そこに入れておかないと履歴が消える）。
 *
 * redrawAll() は使わない — あれは板・テープ・可視レンジまで作り直すので、
 * 再生中の表示とユーザーのパン位置を壊す。ここでは分足の 2 系列と
 * マーカーだけを張り直す。
 */
function prependMinuteHistory(older) {
  const target = minuteHistoryChartTarget();
  const result = applyMinuteHistoryPage({
    state,
    olderBars: older,
    candleSeries: target.candleSeries,
    volumeSeries: target.volumeSeries,
    timeScale: target.timeScale,
    paintCandle: paintedBar,
    paintVolume: paintedVolume,
    runProgrammatic: withProgrammaticMinuteRange,
    refreshMarkers: target.refreshMarkers,
  });
  return result.added;
}

/** 左端に着いたときに 1 ページぶん古い分足を足す。失敗しても再生は止めない。 */
async function loadOlderMinuteBars() {
  if (!state.meta) return;
  const session = state.meta;
  const sessionIdentity = minuteSessionIdentity(session);
  if (!sessionIdentity || !minuteHistory.isReadySession(sessionIdentity)) return;
  const boundary = earliestMinuteBarTime();
  if (boundary === null) return;
  const cutoff = cutoffFromEpochSeconds(boundary);
  if (!cutoff) return;

  // fetch より先に単一飛行の関門を通す（連続するレンジ変化で多重取得しない）。
  const token = minuteHistory.admit(cutoffKey(cutoff), { sessionIdentity });
  if (!token) return;

  try {
    const payload = await requests.fetchLatest(
      'minute-history',
      minuteContextUrl(session, cutoff, minuteHistory.pageSize),
    );

    // Check the generation immediately after the await and before any state mutation.
    if (!minuteHistory.isCurrent(token) ||
        !minuteHistory.isReadySession(sessionIdentity) ||
        minuteSessionIdentity(state.meta) !== sessionIdentity) return;
    const normalized = normalizeMinuteBars(payload?.bars, minuteHistory.pageSize);
    if (!normalized.sourceIsArray ||
        (normalized.receivedCount > 0 && normalized.bars.length === 0)) {
      minuteHistory.completeFailure(token);
      return;
    }
    const added = prependMinuteHistory(normalized.bars);
    minuteHistory.completeSuccess(token, added);
  } catch (error) {
    if (isCancellationError(error)) minuteHistory.completeAbort(token);
    else minuteHistory.completeFailure(token);
  }
}

function onMinuteLogicalRangeChange(range) {
  if (chartMode === 'minute') minuteViewport.capture(range);
  if (chartMode !== 'minute') return;
  if (!range || !state.meta || !state.bars.length) return;
  if (minuteHistory.isProgrammatic()) return;
  const visible = candleSeries.barsInLogicalRange(range);
  if (!visible || !minuteHistory.isNearLeftEdge(visible.barsBefore)) return;
  loadOlderMinuteBars();
}

// ------------------------------------------------------------ view follow

/** ティックチャートは常に仮想時刻を追う（再生中も毎フレーム）。 */
function followTickView() {
  if (!state.t || !state.t.length) return;
  // データが 1 点も入っていないチャートに可視範囲を与えると
  // Lightweight Charts が "Value is null" を投げるため、先頭で弾く。
  if (!state.tickPoints.length) return;
  const tickFrom = Math.max(state.t[0] - 1, state.vt - TICK_WINDOW_SECONDS);
  tickChart.timeScale().setVisibleRange({ from: tickFrom, to: state.vt + TICK_WINDOW_SECONDS * 0.06 });
}

/*
 * 分足の初期表示位置。セッション読み込み・シーク・先頭に戻す のときだけ
 * 呼ぶ（再生中は呼ばない — 呼ぶとユーザーのパン・ズームを毎フレーム
 * 上書きしてしまい、分足チャートを手で動かせなくなる）。
 *
 * 時刻ベースの可視範囲(setVisibleRange)は使わない: プリロードした前日の
 * 分足と当日の分足の間に実バーの無い大きなギャップがあり、その境界を
 * またぐ範囲を時刻で指定すると Lightweight Charts が「範囲内の実バー」
 * をそのまま幅いっぱいに引き伸ばしてしまう（本数が少ないほど顕著）。
 * 論理インデックス(バー本数)ベースの setVisibleLogicalRange なら、
 * ギャップの実時間の長さに関係なく常に一定本数を均等に表示できる。
 */
function followMinuteView() {
  if (chartMode !== 'minute') {
    minuteChartDeferred = true;
    return;
  }
  if (!state.bars.length) return;
  const lastIndex = state.bars.length - 1;
  const from = Math.max(0, lastIndex - (MINUTE_VISIBLE_BARS - 1));
  const to = lastIndex + MINUTE_RIGHT_PADDING_BARS;
  withProgrammaticMinuteRange(() => {
    minuteViewport.runProgrammatic(() => {
      minuteChart.timeScale().setVisibleLogicalRange({ from, to });
    });
  });
}

// -------------------------------------------------------------- rendering

/** シーク後などに、現在の状態からチャート全体を張り直す。 */
function redrawAll() {
  if (chartMode === 'minute') {
    withProgrammaticMinuteRange(() => renderMinuteChart());
  } else {
    minuteChartDeferred = true;
  }
  if (chartMode === 'daily') renderDailyChart();

  const start = Math.max(0, state.cursor - MAX_TICK_POINTS_AFTER_SEEK);
  const points = [];
  for (let index = start; index < state.cursor; index += 1) {
    points.push({ time: state.t[index], value: state.price[index] });
  }
  state.tickPoints = points;
  tickSeries.setData(points);

  rebuildTape();
  resetBoardNavigation(); // シークは不連続なので、板固定でも張り直す
  updateBoard();
  // 系列を setData で差し替えたのでマーカーも張り直す。
  refreshMarkers();
  updatePositionPanel();
  updateClock();
  updateDailyReplayLiveness();
  syncScrubber();
  followTickView();
  followMinuteView(); // シーク・読み込み直後だけ分足の表示位置を作り直す
}

/** vt を targetVt に移し、そこまでの状態を作り直す（描画は最後に一度）。 */
function seekTo(targetVt) {
  state.bars = state.contextBars.slice();
  state.cursor = 0;
  state.last = null;
  state.vt = targetVt;
  state.daily.clearPartial();

  while (state.cursor < state.t.length && state.t[state.cursor] <= targetVt) {
    applyTick(state.cursor, null);
    state.cursor += 1;
  }
  if (state.daily.canAppendTick(minuteSessionIdentity(state.meta))) {
    state.daily.deriveTerminal();
  }
  truncateTradingAt(targetVt);
  redrawAll();
}

// ----------------------------------------------------------- replay engine

function step(timestamp) {
  requestAnimationFrame(step);
  if (!state.playing || !state.t || !state.t.length) {
    state.lastFrame = timestamp;
    return;
  }

  const elapsed = state.lastFrame ? (timestamp - state.lastFrame) / 1000 : 0;
  state.lastFrame = timestamp;
  // タブ復帰などで巨大な dt が来ても飛びすぎないように上限を設ける。
  state.vt += Math.min(elapsed, 0.5) * state.speed;

  if (
    els.skipGaps.checked &&
    state.cursor < state.t.length &&
    state.t[state.cursor] > state.vt + GAP_SKIP_SECONDS
  ) {
    state.vt = state.t[state.cursor];
  }

  const from = state.cursor;
  const touched = [];
  while (state.cursor < state.t.length && state.t[state.cursor] <= state.vt) {
    applyTick(state.cursor, touched);
    state.cursor += 1;
  }

  const framePorts = {
    updateMinute: (bar) => {
      candleSeries.update(paintedBar(bar));
      volumeSeries.update(paintedVolume(bar));
    },
    deferMinute: () => { minuteChartDeferred = true; },
    deriveDaily: () => {
      if (state.daily.canAppendTick(minuteSessionIdentity(state.meta))) {
        state.daily.deriveTerminal();
      }
    },
    updateDaily: updateDailyPartialChart,
    pushTicks: pushTickPoints,
    pushTape: pushTapeRows,
    matchOrders,
    updateBoard: applyTradesToBoard,
    updatePosition: updatePositionPanel,
  };
  runReplayFrame({
    mode: chartMode,
    touchedBars: touched,
    from,
    to: state.cursor,
    ports: {
      ...framePorts,
      commitFrame: ({ mode, touchedBars, from, to }) => commitReplayFrame({
        mode,
        touchedBars,
        from,
        to,
        ports: framePorts,
      }),
      followTick: () => followTickView(),
      updateClock: () => updateClock(),
      syncScrubber: () => syncScrubber(),
      updateLiveness: () => updateDailyReplayLiveness(),
    },
  });
  if (state.cursor >= state.t.length && state.vt >= state.t[state.t.length - 1]) {
    setPlaying(false);
    setStatus('');
  }
}

function pushTickPoints(fromIndex, toIndex) {
  const total = toIndex - fromIndex;
  // 高速再生では 1 フレームに数千件届く。全点を投げると描画が詰まるので
  // 間引くが、最後の 1 点（＝現在値）は必ず入れる。
  const stride = total > MAX_TICK_UPDATES_PER_FRAME ? Math.ceil(total / MAX_TICK_UPDATES_PER_FRAME) : 1;
  for (let index = fromIndex; index < toIndex; index += stride) {
    const point = { time: state.t[index], value: state.price[index] };
    state.tickPoints.push(point);
    tickSeries.update(point);
  }
  if ((toIndex - 1 - fromIndex) % stride !== 0) {
    const point = { time: state.t[toIndex - 1], value: state.price[toIndex - 1] };
    state.tickPoints.push(point);
    tickSeries.update(point);
  }
}

function setPlaying(playing) {
  state.playing = playing;
  state.lastFrame = 0;
  els.playButton.textContent = playing ? '⏸' : '▶';
  updateDailyReplayLiveness();
}

// ------------------------------------------------------------ data loading

async function loadSymbols(prefix) {
  const query = prefix ? '?q=' + encodeURIComponent(prefix) : '';
  const data = await requests.fetchLatest('symbols', '/api/symbols' + query);
  els.symbolList.textContent = '';
  const fragment = document.createDocumentFragment();
  data.symbols.forEach((stem) => {
    const option = document.createElement('option');
    option.value = stem;
    fragment.appendChild(option);
  });
  els.symbolList.appendChild(fragment);
  return data;
}

async function loadSymbolInfo(stem) {
  requests.cancel('session');
  const result = await requests.fetchUntilReady(
    'symbol-info',
    '/api/symbols/' + encodeURIComponent(stem),
    { stem, onStatus: (status) => showCacheStatus(stem, status) },
  );
  const info = result.data;
  els.dateInput.min = info.firstDate;
  els.dateInput.max = info.lastDate;
  if (!els.dateInput.value) els.dateInput.value = info.lastDate;
  showCacheStatus(stem, result.status);
  return info;
}

/**
 * セッション開始（最初の約定）より前の分足を stocks_minute から補助的に
 * 取得する。あくまで「再生前チャートが空にならない」ための演出であり、
 * 失敗しても（データが無い・サーバー未到達など）本編のセッション読み込み
 * は止めず、単に空配列を返す。
 *
 * 遅延読み込み（loadOlderMinuteBars）と同じ 'minute-history' kind と
 * 世代トークンで走らせる。こうしておくと、読み込み中に別の日付へ
 * 切り替えられたときにこの取得もまとめて取り消され、遅れて返ってきた
 * レスポンスが新しいセッションに混ざらない。空応答なら「これ以上
 * さかのぼれない」として、以降のページングも打ち切る。
 */
async function loadMinuteContextBars(session, firstTickSeconds, sessionIdentity) {
  const cutoff = cutoffFromEpochSeconds(firstTickSeconds);
  if (!cutoff) return [];
  // 初回プリロードだけはユーザー操作を待たずに通す。
  const token = minuteHistory.admitInitial(cutoffKey(cutoff), sessionIdentity);
  if (!token) return [];

  try {
    const payload = await requests.fetchLatest(
      'minute-history',
      minuteContextUrl(session, cutoff, MINUTE_CONTEXT_BARS),
    );
    if (!minuteHistory.isCurrent(token)) return [];
    const normalized = normalizeMinuteBars(payload?.bars, MINUTE_CONTEXT_BARS);
    if (!normalized.sourceIsArray ||
        (normalized.receivedCount > 0 && normalized.bars.length === 0)) {
      minuteHistory.completeFailure(token);
      return [];
    }
    minuteHistory.completeSuccess(token, normalized.bars.length);
    return normalized.bars;
  } catch (error) {
    if (isCancellationError(error)) minuteHistory.completeAbort(token);
    else minuteHistory.completeFailure(token);
    return [];
  }
}

async function loadSession(stem, date, direction) {
  requests.cancel('symbol-info');
  // 最初の await より前に、飛んでいる分足履歴の取得を取り消して世代を進める。
  // これ以降、古い世代のレスポンスは isCurrent() / isGeneration() で弾かれる。
  requests.cancel('minute-history');
  requests.cancel('daily-context');
  const requestedIdentity = stem + '|pending|' + date + '|' + direction;
  const generation = minuteHistory.resetSession(requestedIdentity);
  const dailyGeneration = state.daily.resetSession();
  dailyViewport.replace(null);
  clearDailyDisplay();
  setPlaying(false);
  resetBoardNavigation();
  resetTrading(); // 別セッションの建玉・損益・マーカーは持ち越さない
  setStatus('読み込み中… ' + stem + ' ' + date, 'info');
  try {
    const result = await requests.fetchUntilReady(
      'session',
      '/api/session?stem=' + encodeURIComponent(stem) +
      '&date=' + encodeURIComponent(date) +
      '&direction=' + direction,
      { stem, onStatus: (status) => showCacheStatus(stem, status) },
    );
    const session = result.data;
    const sessionIdentity = minuteSessionIdentity(session);
    if (!sessionIdentity || !minuteHistory.setLoadingSession(generation, sessionIdentity)) return;
    const nextMeta = {
      stem: session.stem,
      code: session.code,
      date: session.date,
      count: session.count,
    };
    if (!state.daily.stageActualSession(dailyGeneration, nextMeta)) return;
    if (chartMode === 'daily') void loadDailyContext();
    const nextT = Float64Array.from(session.us, (value) => value / 1e6);
    const nextPrice = Float64Array.from(session.price);
    const nextQty = Float64Array.from(session.qty);
    const nextType = session.type;

    if (!nextT.length) {
      state.meta = nextMeta;
      state.t = nextT;
      state.price = nextPrice;
      state.qty = nextQty;
      state.type = nextType;
      state.contextBars = [];
      state.bars = [];
      state.tickPoints = [];
      state.cursor = 0;
      state.last = null;
      state.vt = 0;
      if (!commitDailySession({
        session: state.daily,
        generation: dailyGeneration,
        meta: nextMeta,
        commit: () => state.daily.commitSession(dailyGeneration, nextMeta),
        publishStatus: refreshDailyStatus,
      })) return;
      state.daily.clearPartial();
      if (chartMode === 'minute') renderMinuteChart();
      else {
        minuteChartDeferred = true;
        renderDailyChart();
      }
      tickSeries.setData([]);
      clearBoard();
      minuteHistory.markSessionReady(generation, sessionIdentity);
      els.dateInput.value = session.date;
      setStatus(session.date + ' は約定がありません', 'error');
      return;
    }

    const contextBars = await loadMinuteContextBars(session, nextT[0], sessionIdentity);
    if (!minuteHistory.isLoadingSession(generation, sessionIdentity)) return;
    state.meta = nextMeta;
    state.t = nextT;
    state.price = nextPrice;
    state.qty = nextQty;
    state.type = nextType;
    state.contextBars = contextBars;
    if (!commitDailySession({
      session: state.daily,
      generation: dailyGeneration,
      meta: nextMeta,
      commit: () => state.daily.commitSession(dailyGeneration, nextMeta),
      publishStatus: refreshDailyStatus,
    })) return;
    els.dateInput.value = session.date;

    board.tick = state.price.length ? inferTickSize() : 1;
    board.priceFormatter = createBoardPriceFormatter(board.tick);
    board.side = 'buy';
    board.spread = rollSpread();
    clearBoard();

    seekTo(state.t[0] - 0.001);
    minuteChart.timeScale().applyOptions({ rightOffset: 3 });
    minuteHistory.markSessionReady(generation, sessionIdentity);
    if (result.status?.state === 'stale-served') {
      const summary = session.code + ' / ' + session.date + ' — 約定 ' +
        intFormat.format(session.count) + ' 件 (' + formatClock(state.t[0], false) +
        '–' + formatClock(state.t[state.t.length - 1], false) + ')';
      setStatus('⚠ 縮退運転（既存キャッシュ）: ' + summary, 'degraded');
    } else {
      setStatus('');
    }
  } catch (error) {
    if (isCancellationError(error)) return;
    requests.cancel('daily-context');
    state.daily.failSession(dailyGeneration);
    refreshDailyStatus();
    setStatus('読み込みに失敗しました: ' + error.message, 'error');
  }
}

// ----------------------------------------------------------------- wiring

els.loadButton.addEventListener('click', () => {
  const stem = els.symbolInput.value.trim().toUpperCase();
  const date = els.dateInput.value;
  if (!stem || !date) {
    setStatus('銘柄と日付を指定してください', 'error');
    return;
  }
  loadSession(stem, date, -1);
});

els.symbolInput.addEventListener('change', async () => {
  const stem = els.symbolInput.value.trim().toUpperCase();
  if (!stem) return;
  try {
    await loadSymbolInfo(stem);
  } catch (error) {
    if (isCancellationError(error)) return;
    setStatus('銘柄が見つかりません: ' + stem, 'error');
  }
});

let symbolSearchDebounceTimer = null;

els.symbolInput.addEventListener('input', () => {
  if (symbolSearchDebounceTimer) clearTimeout(symbolSearchDebounceTimer);
  const prefix = els.symbolInput.value.trim().toUpperCase();
  if (prefix.length < 1 || prefix.length > 3) return;
  symbolSearchDebounceTimer = setTimeout(() => {
    symbolSearchDebounceTimer = null;
    loadSymbols(prefix).catch(() => {});
  }, SYMBOL_SEARCH_DEBOUNCE_MS);
});

els.prevDay.addEventListener('click', () => {
  if (!state.meta) return;
  loadSession(state.meta.stem, addDays(state.meta.date, -1), -1);
});

els.nextDay.addEventListener('click', () => {
  if (!state.meta) return;
  loadSession(state.meta.stem, addDays(state.meta.date, 1), 1);
});

els.playButton.addEventListener('click', () => {
  if (!state.meta || !state.t.length) return;
  if (!state.playing && state.cursor >= state.t.length) seekTo(state.t[0] - 0.001);
  setPlaying(!state.playing);
});

els.resetButton.addEventListener('click', () => {
  if (!state.meta || !state.t.length) return;
  setPlaying(false);
  resetTrading();
  seekTo(state.t[0] - 0.001);
});

els.speedGroup.addEventListener('click', (event) => {
  const button = event.target.closest('button[data-speed]');
  if (!button) return;
  state.speed = Number(button.dataset.speed);
  els.speedGroup.querySelectorAll('button').forEach((item) => item.classList.remove('active'));
  button.classList.add('active');
});

els.scrubber.addEventListener('pointerdown', () => { state.scrubbing = true; });
els.scrubber.addEventListener('pointerup', () => { state.scrubbing = false; });
els.scrubber.addEventListener('input', () => {
  if (!state.meta || !state.t.length) return;
  const first = state.t[0];
  const last = state.t[state.t.length - 1];
  const target = first + ((last - first) * Number(els.scrubber.value)) / 1000;
  seekTo(target);
});

els.chartInterval.addEventListener('click', (event) => {
  const tab = event.target.closest('[data-chart-mode]');
  if (!tab) return;
  setChartMode(tab.dataset.chartMode);
});

els.chartInterval.addEventListener('keydown', (event) => {
  const tab = event.target.closest('[data-chart-mode]');
  if (!tab) return;
  const keys = ['ArrowLeft', 'ArrowRight', 'Home', 'End'];
  if (!keys.includes(event.key)) return;
  event.preventDefault();
  const mode = event.key === 'ArrowLeft' || event.key === 'Home' ? 'minute' : 'daily';
  setChartMode(mode);
  (mode === 'daily' ? els.dailyTab : els.minuteTab).focus();
});

// 分足チャートは触られた瞬間から「ユーザーのもの」。以降、最古の足の近くまで
// スクロールされたら古い足を取りにいく（触られるまでは取りにいかない）。
['wheel', 'pointerdown', 'touchstart'].forEach((type) => {
  els.minuteChart.addEventListener(
    type,
    () => minuteHistory.arm(minuteSessionIdentity(state.meta)),
    { passive: true },
  );
  els.dailyChart.addEventListener(
    type,
    () => state.daily.armHistory(dailySessionIdentity(state.meta)),
    { passive: true, capture: true },
  );
});
minuteChart.timeScale().subscribeVisibleLogicalRangeChange(onMinuteLogicalRangeChange);

els.boardFix.addEventListener('change', () => {
  centerBoardOnCurrent();
});

els.boardCenter.addEventListener('click', centerBoardOnCurrent);

els.board.addEventListener('scroll', onBoardScroll, { passive: true });
els.board.addEventListener('dblclick', (event) => {
  const priceCell = event.target.closest('.board-price');
  if (!priceCell) return;
  event.preventDefault();
  centerBoardOnCurrent();
});

// 発注列は行数ぶん個別にリスナを張らず、板全体で 1 つに委譲する。
els.board.addEventListener('click', (event) => {
  const cell = event.target.closest('.board-order');
  if (!cell || board.topLevel === null || state.last === null) return;
  const level = levelAtRow(board.topLevel, Number(cell.parentElement.dataset.index));
  placeOrder(cell.classList.contains('order-sell') ? 'sell' : 'buy', level);
});

els.cancelSell.addEventListener('click', () => cancelOrders('sell'));
els.cancelBuy.addEventListener('click', () => cancelOrders('buy'));

els.mobileBuyButton.addEventListener('click', () => placeMobileMarketOrder('buy'));
els.mobileSellButton.addEventListener('click', () => placeMobileMarketOrder('sell'));

// 分足/ティック/歩み値の開閉トグル（スマホ幅だけで表示）。開閉状態は保存しない。
document.querySelectorAll('.section-toggle').forEach((button) => {
  button.addEventListener('click', () => {
    const card = button.closest('.card');
    if (!card) return;
    const collapsed = card.classList.toggle('is-collapsed');
    button.setAttribute('aria-expanded', String(!collapsed));
    button.textContent = collapsed ? '＋' : '－';
  });
});

els.pnlModal.addEventListener('click', (event) => {
  if (event.target.closest('[data-close]')) closePnlModal();
});

document.addEventListener('keydown', (event) => {
  // ESC は入力欄にフォーカスがあっても効かせる（成績をすぐ見たいので）。
  if (event.key === 'Escape') {
    event.preventDefault();
    if (els.pnlModal.hidden) openPnlModal();
    else closePnlModal();
    return;
  }
  if (event.target instanceof HTMLInputElement) return;
  if (event.code === 'Space') {
    event.preventDefault();
    els.playButton.click();
  }
});

// ------------------------------------------------------------------ start

buildBoardRows();
if (typeof ResizeObserver !== 'undefined') {
  new ResizeObserver(() => buildBoardRows()).observe(els.board);
  new ResizeObserver(() => applyChartStageSize()).observe(els.chartStage);
} else {
  window.addEventListener('resize', applyChartStageSize);
}
updatePositionPanel();
requestAnimationFrame(step);

(async function bootstrap() {
  try {
    const data = await loadSymbols('');
    if (!data.symbols.length) {
      setStatus('銘柄ファイルが見つかりません', 'error');
      return;
    }
    // 既定は 285A。存在しない環境では一覧の先頭にフォールバックする。
    let preferred = '285A';
    let info = await loadSymbolInfo(preferred).catch(() => null);
    if (!info) {
      preferred = data.symbols[0];
      info = await loadSymbolInfo(preferred);
    }
    els.symbolInput.value = preferred;
    await loadSession(preferred, info.lastDate, -1);
  } catch (error) {
    if (isCancellationError(error)) return;
    setStatus('初期化に失敗しました: ' + error.message, 'error');
  }
})();

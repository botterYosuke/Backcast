import { RequestCoordinator, isCancellationError } from './request-coordinator.mjs';
import {
  applyFill,
  classifyClick,
  createPortfolio,
  isFilled,
  totalPnl,
  unrealizedPnl,
} from './paper-trading.mjs';

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

const BOARD_ROWS = 41;            // 板の行数（奇数・固定）
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

const minuteChart = LightweightCharts.createChart(document.getElementById('minute-chart'), {
  ...chartTheme,
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
};

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
  rows: [],         // {li, ask, price, bid, askText, bidText, className}
  qty: new Map(),   // level -> 記号文字列
  spread: 0,        // 最良買と最良売の間に空ける呼値の数 (0/1/2)
  side: 'buy',      // 直近約定が売り板を食った(buy) / 買い板を食った(sell)
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

function buildBoardRows() {
  const fragment = document.createDocumentFragment();
  for (let index = 0; index < BOARD_ROWS; index += 1) {
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
    // 光り終わったらクラスを外す。付けっぱなしにすると売買の色が戻らない。
    li.addEventListener('animationend', () => li.classList.remove('flash'));
    fragment.appendChild(li);
    board.rows.push({
      li, ask, price, bid, sellOrder, buyOrder,
      askText: '', bidText: '', className: '',
      sellOrderText: '', buyOrderText: '',
    });
  }
  els.board.appendChild(fragment);
}

/** 板を張り直す（中央 = centerLevel）。価格の列はここでだけ動く。 */
function anchorBoard(centerLevel) {
  board.topLevel = centerLevel + (BOARD_ROWS - 1) / 2;
  board.rows.forEach((row, index) => {
    row.price.textContent = priceFormat.format(priceOfLevel(board.topLevel - index));
  });

  // 板が画面に入りきらない高さのときは、中央の行を見えるところへ寄せる。
  // 張り直したときだけ動かすので、板固定中はスクロール位置も固定される。
  const center = board.rows[(BOARD_ROWS - 1) / 2].li;
  els.board.scrollTop = center.offsetTop - (els.board.clientHeight - center.offsetHeight) / 2;
}

function clearBoard() {
  board.topLevel = null;
  board.qty.clear();
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
    row.bidText = '';
    row.sellOrderText = '';
    row.buyOrderText = '';
    row.className = '';
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

function updateBoard() {
  if (state.last === null) {
    clearBoard();
    return;
  }

  const lastLevel = levelOf(state.last);
  const bottomLevel = board.topLevel === null ? null : board.topLevel - BOARD_ROWS + 1;
  if (
    board.topLevel === null ||
    !els.boardFix.checked ||
    lastLevel > board.topLevel ||
    lastLevel < bottomLevel
  ) {
    // 板固定でも、現在値が板から出てしまったら張り直すしかない。
    anchorBoard(lastLevel);
  }

  const askLevel = board.side === 'buy' ? lastLevel : lastLevel + board.spread + 1;
  const bidLevel = board.side === 'buy' ? lastLevel - board.spread - 1 : lastLevel;

  const pendingSell = pendingByLevel('sell');
  const pendingBuy = pendingByLevel('buy');

  board.rows.forEach((row, index) => {
    const level = board.topLevel - index;
    const near = Math.abs(level - lastLevel) <= BOARD_QTY_SPAN;
    const isAsk = near && level >= askLevel;
    const isBid = near && level <= bidLevel;
    const side = isAsk ? 'ask-side' : isBid ? 'bid-side' : '';
    const className = level === lastLevel ? (side + ' last').trim() : side;
    setRowText(row, isAsk ? quantityAt(level) : '', isBid ? quantityAt(level) : '', className);
    setOrderCell(row, 'sellOrder', pendingSell.get(level));
    setOrderCell(row, 'buyOrder', pendingBuy.get(level));
  });

  els.boardQuote.textContent =
    '売 ' + priceFormat.format(priceOfLevel(askLevel)) +
    ' / 買 ' + priceFormat.format(priceOfLevel(bidLevel)) +
    ' ・ スプレッド ' + board.spread;
}

/** 約定した価格の行を光らせる（アニメーションを頭から流し直す）。 */
function flashBoardLevel(level) {
  if (board.topLevel === null) return;
  const index = board.topLevel - level;
  if (index < 0 || index >= board.rows.length) return;
  const li = board.rows[index].li;
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

  candleSeries.setMarkers(minuteMarkers);
  tickSeries.setMarkers(tickMarkers);
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

// ------------------------------------------------------------ view follow

function followViews() {
  if (!state.t || !state.t.length) return;
  // データが 1 点も入っていないチャートに可視範囲を与えると
  // Lightweight Charts が "Value is null" を投げるため、先頭で弾く。
  if (state.tickPoints.length) {
    const tickFrom = Math.max(state.t[0] - 1, state.vt - TICK_WINDOW_SECONDS);
    tickChart.timeScale().setVisibleRange({ from: tickFrom, to: state.vt + TICK_WINDOW_SECONDS * 0.06 });
  }

  if (!state.bars.length) return;

  // 時刻ベースの可視範囲(setVisibleRange)は使わない: プリロードした前日の
  // 分足と当日の分足の間に実バーの無い大きなギャップがあり、その境界を
  // またぐ範囲を時刻で指定すると Lightweight Charts が「範囲内の実バー」
  // をそのまま幅いっぱいに引き伸ばしてしまう（本数が少ないほど顕著）。
  // 論理インデックス(バー本数)ベースの setVisibleLogicalRange なら、
  // ギャップの実時間の長さに関係なく常に一定本数を均等に表示できる —
  // 前日の足は再生が進むにつれて自然に左へスクロールアウトしていく。
  const lastIndex = state.bars.length - 1;
  const from = Math.max(0, lastIndex - (MINUTE_VISIBLE_BARS - 1));
  const to = lastIndex + MINUTE_RIGHT_PADDING_BARS;
  minuteChart.timeScale().setVisibleLogicalRange({ from, to });
}

// -------------------------------------------------------------- rendering

/** シーク後などに、現在の状態からチャート全体を張り直す。 */
function redrawAll() {
  candleSeries.setData(state.bars.map(paintedBar));
  volumeSeries.setData(state.bars.map(paintedVolume));

  const start = Math.max(0, state.cursor - MAX_TICK_POINTS_AFTER_SEEK);
  const points = [];
  for (let index = start; index < state.cursor; index += 1) {
    points.push({ time: state.t[index], value: state.price[index] });
  }
  state.tickPoints = points;
  tickSeries.setData(points);

  rebuildTape();
  board.topLevel = null; // シークは不連続なので、板固定でも張り直す
  updateBoard();
  // 系列を setData で差し替えたのでマーカーも張り直す。
  refreshMarkers();
  updatePositionPanel();
  updateClock();
  syncScrubber();
  followViews();
}

/** vt を targetVt に移し、そこまでの状態を作り直す（描画は最後に一度）。 */
function seekTo(targetVt) {
  state.bars = state.contextBars.slice();
  state.cursor = 0;
  state.last = null;
  state.vt = targetVt;

  while (state.cursor < state.t.length && state.t[state.cursor] <= targetVt) {
    applyTick(state.cursor, null);
    state.cursor += 1;
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

  if (state.cursor > from) {
    for (let index = 0; index < touched.length; index += 1) {
      candleSeries.update(paintedBar(touched[index]));
      volumeSeries.update(paintedVolume(touched[index]));
    }
    pushTickPoints(from, state.cursor);
    pushTapeRows(from, state.cursor);
    // 板の再描画より先に約定させる（約定ぶんの注文を板から消すため）。
    matchOrders(from, state.cursor);
    applyTradesToBoard(from, state.cursor);
    updatePositionPanel(); // 評価損益は現在値が動くたびに更新する
  }

  followViews();
  updateClock();
  syncScrubber();

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
 */
async function loadMinuteContextBars(session, firstTickSeconds) {
  const anchor = new Date(firstTickSeconds * 1000);
  const date =
    anchor.getUTCFullYear() + '-' + pad2(anchor.getUTCMonth() + 1) + '-' + pad2(anchor.getUTCDate());
  const time = pad2(anchor.getUTCHours()) + ':' + pad2(anchor.getUTCMinutes());
  try {
    const data = await getJson(
      '/api/minute-context?stem=' + encodeURIComponent(session.stem) +
      '&code=' + encodeURIComponent(session.code) +
      '&date=' + encodeURIComponent(date) +
      '&time=' + encodeURIComponent(time) +
      '&limit=' + MINUTE_CONTEXT_BARS
    );
    return Array.isArray(data.bars) ? data.bars : [];
  } catch (error) {
    return [];
  }
}

async function loadSession(stem, date, direction) {
  requests.cancel('symbol-info');
  setPlaying(false);
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

    state.meta = { stem: session.stem, code: session.code, date: session.date, count: session.count };
    state.t = Float64Array.from(session.us, (value) => value / 1e6);
    state.price = Float64Array.from(session.price);
    state.qty = Float64Array.from(session.qty);
    state.type = session.type;
    els.dateInput.value = session.date;

    board.tick = state.price.length ? inferTickSize() : 1;
    board.side = 'buy';
    board.spread = rollSpread();
    clearBoard();

    if (!state.t.length) {
      setStatus(session.date + ' は約定がありません', 'error');
      return;
    }

    state.contextBars = await loadMinuteContextBars(session, state.t[0]);
    seekTo(state.t[0] - 0.001);
    minuteChart.timeScale().applyOptions({ rightOffset: 3 });
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

els.symbolInput.addEventListener('input', () => {
  const prefix = els.symbolInput.value.trim().toUpperCase();
  if (prefix.length >= 1 && prefix.length <= 3) loadSymbols(prefix).catch(() => {});
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

els.boardFix.addEventListener('change', () => {
  board.topLevel = null; // 固定を切り替えたら、その場で現在値を中央へ
  updateBoard();
});

els.boardCenter.addEventListener('click', () => {
  board.topLevel = null;
  updateBoard();
});

// 発注列は行数ぶん個別にリスナを張らず、板全体で 1 つに委譲する。
els.board.addEventListener('click', (event) => {
  const cell = event.target.closest('.board-order');
  if (!cell || board.topLevel === null || state.last === null) return;
  const level = board.topLevel - Number(cell.parentElement.dataset.index);
  placeOrder(cell.classList.contains('order-sell') ? 'sell' : 'buy', level);
});

els.cancelSell.addEventListener('click', () => cancelOrders('sell'));
els.cancelBuy.addEventListener('click', () => cancelOrders('buy'));

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
updatePositionPanel();
requestAnimationFrame(step);

(async function bootstrap() {
  try {
    const data = await loadSymbols('');
    if (!data.symbols.length) {
      setStatus('銘柄ファイルが見つかりません', 'error');
      return;
    }
    // 既定は 7203。存在しない環境では一覧の先頭にフォールバックする。
    let preferred = '7203';
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

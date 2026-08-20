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
 */

'use strict';

const UP = '#c0392b';    // 陽線・上昇
const DOWN = '#1d5fa8';  // 陰線・下降
const FLAT = '#9a9489';
const UP_FILL = 'rgba(192, 57, 43, 0.35)';
const DOWN_FILL = 'rgba(29, 95, 168, 0.35)';

const MINUTE = 60;
const TICK_WINDOW_SECONDS = 300;      // ティックチャートの表示幅
const MINUTE_WINDOW_SECONDS = 90 * 60; // 分足チャートの表示幅
const GAP_SKIP_SECONDS = 5;           // これ以上の無約定はスキップ対象
const MAX_TAPE_ROWS = 200;
const MAX_TAPE_INSERTS_PER_FRAME = 40;
const MAX_TICK_UPDATES_PER_FRAME = 600;
const MAX_TICK_POINTS_AFTER_SEEK = 20000;

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
  rdCode: document.getElementById('rd-code'),
  rdPrice: document.getElementById('rd-price'),
  rdChange: document.getElementById('rd-change'),
  rdVwap: document.getElementById('rd-vwap'),
  rdVolume: document.getElementById('rd-volume'),
  rdTicks: document.getElementById('rd-ticks'),
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

async function getJson(url) {
  const response = await fetch(url);
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
  speed: 20,
  bars: [],         // 集計済みの分足
  tickPoints: [],   // ティックチャートに投入済みの点
  open: null,
  last: null,
  cumQty: 0,
  cumValue: 0,
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

  if (state.open === null) state.open = price;
  state.last = price;
  state.cumQty += quantity;
  state.cumValue += price * quantity;

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
  row.innerHTML =
    '<span>' + formatClock(state.t[index], true) + '</span>' +
    '<span class="tape-price">' + priceFormat.format(state.price[index]) + '</span>' +
    '<span>' + intFormat.format(state.qty[index]) + '</span>' +
    '<span>' + (state.type[index] || '') + '</span>';
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

// ---------------------------------------------------------------- readouts

function updateReadouts() {
  if (!state.meta) return;
  els.rdCode.textContent = state.meta.code + '  ' + state.meta.date;

  if (state.last === null) {
    els.rdPrice.textContent = '—';
    els.rdPrice.className = 'readout-value price';
    els.rdChange.textContent = '—';
    els.rdChange.className = 'readout-value';
    els.rdVwap.textContent = '—';
    els.rdVolume.textContent = '0';
    els.rdTicks.textContent = '0 / ' + intFormat.format(state.meta.count);
    return;
  }

  const change = state.last - state.open;
  const ratio = state.open ? (change / state.open) * 100 : 0;
  const tone = change > 0 ? 'up' : change < 0 ? 'down' : '';
  const sign = change > 0 ? '+' : '';

  els.rdPrice.textContent = priceFormat.format(state.last);
  els.rdPrice.className = 'readout-value price ' + tone;
  els.rdChange.textContent =
    sign + priceFormat.format(change) + ' (' + sign + ratio.toFixed(2) + '%)';
  els.rdChange.className = 'readout-value ' + tone;
  els.rdVwap.textContent = state.cumQty
    ? priceFormat.format(state.cumValue / state.cumQty)
    : '—';
  els.rdVolume.textContent = intFormat.format(state.cumQty);
  els.rdTicks.textContent =
    intFormat.format(state.cursor) + ' / ' + intFormat.format(state.meta.count);
}

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
  if (!state.bars.length || !state.tickPoints.length) return;
  const tickFrom = Math.max(state.t[0] - 1, state.vt - TICK_WINDOW_SECONDS);
  tickChart.timeScale().setVisibleRange({ from: tickFrom, to: state.vt + TICK_WINDOW_SECONDS * 0.06 });

  const minuteFrom = Math.max(
    Math.floor(state.t[0] / MINUTE) * MINUTE - MINUTE,
    state.vt - MINUTE_WINDOW_SECONDS
  );
  minuteChart.timeScale().setVisibleRange({ from: minuteFrom, to: state.vt + 5 * MINUTE });
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
  updateReadouts();
  updateClock();
  syncScrubber();
  followViews();
}

/** vt を targetVt に移し、そこまでの状態を作り直す（描画は最後に一度）。 */
function seekTo(targetVt) {
  state.bars = [];
  state.cursor = 0;
  state.open = null;
  state.last = null;
  state.cumQty = 0;
  state.cumValue = 0;
  state.vt = targetVt;

  while (state.cursor < state.t.length && state.t[state.cursor] <= targetVt) {
    applyTick(state.cursor, null);
    state.cursor += 1;
  }
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
    updateReadouts();
  }

  followViews();
  updateClock();
  syncScrubber();

  if (state.cursor >= state.t.length && state.vt >= state.t[state.t.length - 1]) {
    setPlaying(false);
    setStatus('再生が終了しました（' + state.meta.date + '）', 'info');
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
  const data = await getJson('/api/symbols' + query);
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
  const info = await getJson('/api/symbols/' + encodeURIComponent(stem));
  els.dateInput.min = info.firstDate;
  els.dateInput.max = info.lastDate;
  if (!els.dateInput.value) els.dateInput.value = info.lastDate;
  return info;
}

async function loadSession(stem, date, direction) {
  setPlaying(false);
  setStatus('読み込み中… ' + stem + ' ' + date, 'info');
  els.loadButton.disabled = true;
  try {
    const session = await getJson(
      '/api/session?stem=' + encodeURIComponent(stem) +
      '&date=' + encodeURIComponent(date) +
      '&direction=' + direction
    );

    state.meta = { stem: session.stem, code: session.code, date: session.date, count: session.count };
    state.t = Float64Array.from(session.us, (value) => value / 1e6);
    state.price = Float64Array.from(session.price);
    state.qty = Float64Array.from(session.qty);
    state.type = session.type;
    els.dateInput.value = session.date;

    if (!state.t.length) {
      setStatus(session.date + ' は約定がありません', 'error');
      return;
    }

    seekTo(state.t[0] - 0.001);
    minuteChart.timeScale().applyOptions({ rightOffset: 3 });
    setStatus(
      session.code + ' / ' + session.date + ' — 約定 ' + intFormat.format(session.count) + ' 件 (' +
      formatClock(state.t[0], false) + '–' + formatClock(state.t[state.t.length - 1], false) + ')',
      'info'
    );
  } catch (error) {
    setStatus('読み込みに失敗しました: ' + error.message, 'error');
  } finally {
    els.loadButton.disabled = false;
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
    setStatus('');
  } catch (error) {
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

document.addEventListener('keydown', (event) => {
  if (event.target instanceof HTMLInputElement) return;
  if (event.code === 'Space') {
    event.preventDefault();
    els.playButton.click();
  }
});

// ------------------------------------------------------------------ start

requestAnimationFrame(step);

(async function bootstrap() {
  try {
    const status = await getJson('/api/status');
    if (!status.ok) {
      setStatus('データの場所を解決できません: ' + status.error, 'error');
      return;
    }
    const data = await loadSymbols('');
    if (!data.symbols.length) {
      setStatus('銘柄ファイルが見つかりません: ' + status.tradesDir, 'error');
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
    setStatus('初期化に失敗しました: ' + error.message, 'error');
  }
})();

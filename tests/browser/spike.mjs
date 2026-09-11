const source = document.querySelector("#strategy-source");
const mount = document.querySelector("#editor-mount");
const status = document.querySelector("#status");
const output = document.querySelector("#output");
const controls = [...document.querySelectorAll("button")];
const SPIKE_BASE = location.pathname.startsWith("/spike-isolated") ? "/spike-isolated" : "/spike";
const metrics = { longTasks: [] };
let editorView = null;
let toolkit = null;
let assets = null;
let worker = null;

if ("PerformanceObserver" in window) {
  try {
    new PerformanceObserver((list) => {
      metrics.longTasks.push(...list.getEntries().map(({ duration, startTime }) => ({ duration, startTime })));
    }).observe({ type: "longtask", buffered: true });
  } catch {
    metrics.longTaskObserverUnavailable = true;
  }
}

function announce(message) {
  status.textContent = message;
}

async function loadAssets() {
  if (!assets) ({ strategyAssets: assets } = await import(`${SPIKE_BASE}/strategy-assets.mjs`));
  return assets;
}

async function openEditor(options = {}) {
  const config = await loadAssets();
  const bundleUrl = options.bundleUrl ?? config.codeMirror.bundleUrl;
  const fixtureResponse = await fetch("/fixtures/strategy-max-source.py");
  if (!fixtureResponse.ok) throw new Error(`source fixture HTTP ${fixtureResponse.status}`);
  const documentText = await fixtureResponse.text();
  const startedAt = performance.now();
  toolkit = await import(bundleUrl);
  const completionSource = (context) => {
    metrics.completionRequests = (metrics.completionRequests ?? 0) + 1;
    const word = context.matchBefore(/\w*/);
    if (!word || (word.from === word.to && !context.explicit)) return null;
    return {
      from: word.from,
      options: ["on_start", "on_event", "on_end"].map((label) => ({ label, type: "function" })),
    };
  };
  const state = toolkit.EditorState.create({
    doc: documentText,
    extensions: toolkit.createBaseExtensions({
      completionSource,
      lineWrapping: matchMedia("(max-width: 768px)").matches,
    }),
  });
  editorView = new toolkit.EditorView({ state, parent: mount });
  editorView.contentDOM.setAttribute("aria-label", "Python strategy source");
  editorView.contentDOM.setAttribute("aria-describedby", "strategy-help");
  source.value = documentText;
  source.hidden = true;
  source.setAttribute("aria-hidden", "true");
  source.tabIndex = -1;
  mount.hidden = false;
  editorView.focus();
  metrics.editorFirstOpenMs = performance.now() - startedAt;
  const diagnosticsStartedAt = performance.now();
  editorView.dispatch(toolkit.setDiagnostics(editorView.state, [
    { from: 0, to: 1, severity: "warning", message: "beginning diagnostic" },
    { from: documentText.length - 2, to: documentText.length - 1, severity: "error", message: "ending diagnostic" },
  ]));
  metrics.diagnosticCount = 2;
  metrics.diagnosticsMs = performance.now() - diagnosticsStartedAt;
  announce("Enhanced strategy editor ready");
  return metrics.editorFirstOpenMs;
}

function degradeEditor() {
  if (!editorView) return;
  const selection = editorView.state.selection.main;
  source.value = editorView.state.doc.toString();
  source.hidden = false;
  source.removeAttribute("aria-hidden");
  source.tabIndex = 0;
  source.readOnly = false;
  source.setAttribute("aria-readonly", "false");
  source.setSelectionRange(selection.from, selection.to);
  source.dataset.firstVisibleLine = String(editorView.state.doc.lineAt(editorView.viewport.from).number);
  editorView.destroy();
  editorView = null;
  mount.replaceChildren();
  mount.hidden = true;
  source.focus();
  announce("Degraded strategy editor active");
}

function rawCopyRecovery() {
  if (editorView) degradeEditor();
  source.hidden = false;
  source.removeAttribute("aria-hidden");
  source.tabIndex = 0;
  source.readOnly = true;
  source.setAttribute("aria-readonly", "true");
  for (const control of controls) control.disabled = true;
  source.focus();
  announce("Raw copy recovery active");
}

async function createRuntime(options = {}) {
  const config = await loadAssets();
  const loaderUrl = options.loaderUrl ?? config.pyodide.loaderUrl;
  const startedAt = performance.now();
  worker = new Worker(`${SPIKE_BASE}/spike-worker.mjs`, { type: "module" });
  const result = await waitForWorker("ready", () => worker.postMessage({
    type: "init",
    loaderUrl,
    indexUrl: config.pyodide.indexUrl,
    sourceUrl: "/fixtures/strategy-max-source.py",
  }), 20000);
  metrics.pyodideColdStartMs = performance.now() - startedAt;
  output.textContent = JSON.stringify(result);
  announce("Pyodide Worker ready");
  return result;
}

function waitForWorker(expectedType, start, timeoutMs = 5000) {
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error(`${expectedType} timed out`)), timeoutMs);
    const cleanUp = () => {
      worker.removeEventListener("message", onMessage);
      worker.removeEventListener("error", onError);
    };
    const onError = (error) => {
      clearTimeout(timeout);
      cleanUp();
      reject(new Error(error.message));
    };
    const onMessage = ({ data }) => {
      if (data.type === "worker-error") {
        clearTimeout(timeout);
        cleanUp();
        reject(new Error(data.message));
      }
      if (data.type !== expectedType) return;
      clearTimeout(timeout);
      cleanUp();
      resolve(data);
    };
    worker.addEventListener("message", onMessage);
    worker.addEventListener("error", onError);
    start();
  });
}

function makeMaximumEvent() {
  const string128 = "x".repeat(128);
  const orders = Array.from({ length: 256 }, (_, index) => ({
    order_id: `${index}-${string128}`.slice(0, 128), owner: "strategy", side: index % 2 ? "buy" : "sell",
    quantity_remaining: 1000000, limit_price_level: 1000000, eligible_from_session_tick: 99999,
  }));
  const fills = Array.from({ length: 256 }, (_, index) => ({
    fill_delivery_sequence: index + 1, order_id: `${index}-${string128}`.slice(0, 128),
    side: index % 2 ? "buy" : "sell", quantity: 1000000, price_level: 1000000,
    session_tick_index: 99999, replay_time_us: 36000000000,
  }));
  return {
    protocol_version: 1, session: { stem: "A128", code: "A1280", actual_date: "2026-09-10" },
    run_id: 1, strategy_revision: 1, sequence: 1,
    tick: { session_tick_index: 99999, replay_time_us: 36000000000, price_level: 1000000, quantity: 1000000, trade_type: "buy" },
    portfolio: { cash_minor: 1000000000, position_quantity: 1000000, average_price_level: 1000000, realized_pnl_minor: 1000000000, unrealized_pnl_minor: 1000000000, equity_minor: 2000000000 },
    open_orders: orders, fills, fill_cursor: 256,
  };
}

async function callback(event = makeMaximumEvent()) {
  const startedAt = performance.now();
  const result = await waitForWorker("callback", () => worker.postMessage({ type: "callback", event }));
  return { ...result, roundTripMs: performance.now() - startedAt };
}

async function pythonError() {
  return waitForWorker("python-error", () => worker.postMessage({ type: "error" }));
}

async function hardTerminate() {
  const busyLoopStartedAt = performance.now();
  worker.postMessage({ type: "busy-loop" });
  let animationFrames = 0;
  await new Promise((resolve) => {
    const nextFrame = () => {
      animationFrames += 1;
      if (performance.now() - busyLoopStartedAt >= 80) resolve();
      else requestAnimationFrame(nextFrame);
    };
    requestAnimationFrame(nextFrame);
  });
  const terminationStartedAt = performance.now();
  worker.terminate();
  worker = null;
  metrics.hardTerminationMs = performance.now() - terminationStartedAt;
  metrics.busyLoopAnimationFrames = animationFrames;
  await new Promise((resolve) => setTimeout(resolve));
  metrics.busyLoopLongTasks = metrics.longTasks.filter(
    ({ startTime }) => startTime >= busyLoopStartedAt,
  );
  return {
    animationFrames,
    longTasks: metrics.busyLoopLongTasks,
    terminationMs: metrics.hardTerminationMs,
  };
}

async function softInterrupt() {
  if (!crossOriginIsolated || typeof SharedArrayBuffer === "undefined") return { supported: false };
  const buffer = new SharedArrayBuffer(1);
  const view = new Uint8Array(buffer);
  await waitForWorker("interrupt-ready", () => worker.postMessage({ type: "interrupt-buffer", buffer }));
  const interrupted = waitForWorker("interrupted", () => worker.postMessage({ type: "busy-loop" }));
  await new Promise((resolve) => setTimeout(resolve, 50));
  const startedAt = performance.now();
  view[0] = 2;
  const result = await interrupted;
  metrics.softInterruptMs = performance.now() - startedAt;
  return { supported: true, ...result, elapsedMs: metrics.softInterruptMs };
}

function requestUrls() {
  return performance.getEntriesByType("resource").map(({ name, initiatorType }) => ({ name, initiatorType }));
}

function triggerCompletion() {
  return toolkit.startCompletion(editorView);
}

source.readOnly = false;
source.setAttribute("aria-readonly", "false");
announce("Strategy host ready");
document.querySelector("#open").addEventListener("click", () => openEditor().catch((error) => {
  degradeEditor();
  announce(`Degraded strategy editor: ${error.message}`);
}));
document.querySelector("#load").addEventListener("click", () => createRuntime().catch((error) => announce(`Runtime unavailable: ${error.message}`)));
document.querySelector("#degrade").addEventListener("click", degradeEditor);
document.querySelector("#host-failure").addEventListener("click", rawCopyRecovery);

window.strategySpike = {
  callback,
  createRuntime,
  degradeEditor,
  get editorView() { return editorView; },
  hardTerminate,
  makeMaximumEvent,
  metrics,
  openEditor,
  pythonError,
  rawCopyRecovery,
  requestUrls,
  softInterrupt,
  triggerCompletion,
};

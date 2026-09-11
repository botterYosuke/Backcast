let pyodide = null;
let callbackFunction = null;
let stdout = "";
let stderr = "";
const STREAM_LIMIT = 4096;

function appendBounded(current, chunk) {
  return `${current}${chunk}`.slice(-STREAM_LIMIT);
}

async function initialize(message) {
  const startedAt = performance.now();
  const { loadPyodide } = await import(message.loaderUrl);
  pyodide = await loadPyodide({ indexURL: message.indexUrl });
  pyodide.setStdout({ batched: (text) => { stdout = appendBounded(stdout, text); } });
  pyodide.setStderr({ batched: (text) => { stderr = appendBounded(stderr, text); } });
  await pyodide.runPythonAsync("import json", { filename: "strategy-spike-bootstrap.py" });
  const value = await pyodide.runPythonAsync("1 + 1", { filename: "strategy-spike.py" });
  const sourceResponse = await fetch(message.sourceUrl);
  if (!sourceResponse.ok) throw new Error(`source fixture HTTP ${sourceResponse.status}`);
  const source = await sourceResponse.text();
  const sourceBytes = new TextEncoder().encode(source).byteLength;
  const compileStartedAt = performance.now();
  await pyodide.runPythonAsync(source, { filename: "strategy-max-source.py" });
  const compileMs = performance.now() - compileStartedAt;
  await pyodide.runPythonAsync(
    "def __spike_callback(host_event):\n    return {'price_level': host_event['tick']['price_level'], 'orders': len(host_event['open_orders']), 'fills': len(host_event['fills'])}",
    { filename: "strategy-spike-callback.py" },
  );
  callbackFunction = pyodide.globals.get("__spike_callback");
  postMessage({
    type: "ready",
    value,
    pythonVersion: pyodide.version,
    sourceBytes,
    compileMs,
    coldStartMs: performance.now() - startedAt,
  });
}

async function runCallback(message) {
  const startedAt = performance.now();
  const conversionStartedAt = performance.now();
  const eventProxy = pyodide.toPy(message.event);
  const conversionMs = performance.now() - conversionStartedAt;
  let resultProxy;
  try {
    resultProxy = callbackFunction(eventProxy);
    const result = resultProxy.toJs({ dict_converter: Object.fromEntries, create_pyproxies: false });
    postMessage({ type: "callback", result, conversionMs, elapsedMs: performance.now() - startedAt });
  } finally {
    resultProxy?.destroy();
    eventProxy.destroy();
  }
}

async function runError() {
  try {
    await pyodide.runPythonAsync(
      "print('bounded stdout')\nraise RuntimeError('spike exception')",
      { filename: "expected-error.py" },
    );
  } catch (error) {
    postMessage({ type: "python-error", message: String(error), stdout, stderr });
  }
}

async function runBusyLoop() {
  try {
    await pyodide.runPythonAsync("while True:\n    pass", { filename: "busy-loop.py" });
  } catch (error) {
    postMessage({ type: "interrupted", message: String(error) });
  }
}

self.onmessage = async ({ data }) => {
  try {
    if (data.type === "init") await initialize(data);
    if (data.type === "callback") await runCallback(data);
    if (data.type === "error") await runError();
    if (data.type === "interrupt-buffer") {
      pyodide.setInterruptBuffer(new Uint8Array(data.buffer));
      postMessage({ type: "interrupt-ready" });
    }
    if (data.type === "busy-loop") await runBusyLoop();
  } catch (error) {
    postMessage({ type: "worker-error", message: String(error), stack: error?.stack ?? "" });
  }
};

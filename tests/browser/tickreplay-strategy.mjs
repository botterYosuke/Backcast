import { expect, test } from "@playwright/test";

const EDITOR_FIRST_OPEN_BUDGETS = {
  chromium: 350,
  firefox: 350,
  webkit: 350,
  "mobile-chromium": 800,
  "mobile-webkit": 800,
};
const PYODIDE_COLD_START_BUDGETS = {
  chromium: 8000,
  firefox: 8000,
  webkit: 8000,
  "mobile-chromium": 15000,
  "mobile-webkit": 15000,
};

test.beforeEach(async ({ context, page }, testInfo) => {
  if (testInfo.project.name !== "mobile-chromium") return;
  const session = await context.newCDPSession(page);
  await session.send("Emulation.setCPUThrottlingRate", { rate: 4 });
});

async function waitForHost(page, path = "/spike/") {
  await page.goto(path);
  await page.waitForFunction(() => window.strategySpike);
  await expect(page.getByRole("status")).toHaveText("Strategy host ready");
}

function percentile(values, ratio) {
  const sorted = [...values].sort((left, right) => left - right);
  return sorted[Math.min(sorted.length - 1, Math.ceil(sorted.length * ratio) - 1)];
}

test("bootstrap, first open, and first Load have separate fixed lazy boundaries", async ({ page }, testInfo) => {
  const requests = [];
  page.on("request", (request) => requests.push(request.url()));
  await waitForHost(page);

  expect(requests.some((url) => url.includes("/vendor/codemirror/"))).toBe(false);
  expect(requests.some((url) => url.includes("/vendor/pyodide/"))).toBe(false);

  await page.getByRole("button", { name: "Open editor" }).click();
  await expect(page.getByRole("status")).toHaveText("Enhanced strategy editor ready");
  expect(requests.some((url) => url.includes("/vendor/codemirror/"))).toBe(true);
  expect(requests.some((url) => url.includes("/vendor/pyodide/"))).toBe(false);
  const editorOpenMs = await page.evaluate(() => window.strategySpike.metrics.editorFirstOpenMs);
  expect(editorOpenMs).toBeLessThanOrEqual(EDITOR_FIRST_OPEN_BUDGETS[testInfo.project.name]);

  await page.getByRole("button", { name: "Load runtime" }).click();
  await expect(page.getByRole("status")).toHaveText("Pyodide Worker ready", { timeout: 20000 });
  expect(requests.some((url) => url.includes("/vendor/pyodide/"))).toBe(true);
  const coldStartMs = await page.evaluate(() => window.strategySpike.metrics.pyodideColdStartMs);
  expect(coldStartMs).toBeLessThanOrEqual(PYODIDE_COLD_START_BUDGETS[testInfo.project.name]);
  const compile = await page.locator("#output").evaluate((element) => JSON.parse(element.textContent));
  expect(compile.sourceBytes).toBe(262144);
  console.log(`STRATEGY_SPIKE_METRIC ${JSON.stringify({
    project: testInfo.project.name,
    editorOpenMs,
    coldStartMs,
    maximumSourceCompileMs: compile.compileMs,
  })}`);
});

test("CodeMirror exposes the frozen editing, diagnostics, and recovery surface", async ({ page }, testInfo) => {
  await waitForHost(page);
  await page.getByRole("button", { name: "Open editor" }).click();
  const editor = page.getByRole("textbox", { name: "Python strategy source" });
  await expect(editor).toHaveCount(1);
  await expect(page.locator(".cm-lineNumbers")).toBeVisible();
  await expect(page.locator(".cm-lintRange")).toHaveCount(1);
  expect(await page.locator(".cm-line span[class]").count()).toBeGreaterThan(0);
  const editingBehavior = await page.evaluate(() => ({
    source: window.strategySpike.editorView.state.doc.toString(),
    wrapping: getComputedStyle(document.querySelector(".cm-content")).whiteSpace,
    textarea: {
      autocapitalize: document.querySelector("#strategy-source").getAttribute("autocapitalize"),
      autocomplete: document.querySelector("#strategy-source").getAttribute("autocomplete"),
      autocorrect: document.querySelector("#strategy-source").getAttribute("autocorrect"),
      spellcheck: document.querySelector("#strategy-source").getAttribute("spellcheck"),
    },
  }));
  expect(editingBehavior.wrapping).toBe(testInfo.project.name.startsWith("mobile") ? "pre-wrap" : "pre");
  expect(editingBehavior.textarea).toEqual({
    autocapitalize: "off",
    autocomplete: "off",
    autocorrect: "off",
    spellcheck: "false",
  });
  await editor.press("F8");
  await editor.press("F8");
  expect(await page.evaluate(() => window.strategySpike.editorView.state.selection.main.from)).toBeGreaterThan(260000);
  await editor.press("Shift+F8");
  const bracketPosition = await page.evaluate(() => {
    const view = window.strategySpike.editorView;
    const position = view.state.doc.toString().indexOf("\n\ndef traced") + 1;
    view.dispatch({ selection: { anchor: position } });
    view.focus();
    return position;
  });
  await page.keyboard.type("(");
  const bracketText = () => page.evaluate((position) => (
    window.strategySpike.editorView.state.sliceDoc(position, position + 2)
  ), bracketPosition);
  expect(await bracketText()).toBe("()");
  await expect(page.locator(".cm-matchingBracket")).toHaveCount(2);
  const modifier = await page.evaluate(() => /Mac|iPhone|iPad/.test(navigator.platform) ? "Meta" : "Control");
  await page.keyboard.press(`${modifier}+z`);
  expect(await bracketText()).not.toBe("()");
  await page.keyboard.press(`${modifier}+Shift+z`);
  expect(await bracketText()).toBe("()");
  await page.keyboard.press(`${modifier}+z`);
  await page.evaluate(() => {
    const view = window.strategySpike.editorView;
    view.dispatch({ selection: { anchor: 0, head: view.state.doc.line(1).length } });
    view.focus();
  });
  await page.keyboard.press("Tab");
  expect(await page.evaluate(() => window.strategySpike.editorView.state.sliceDoc(0, 4))).toBe("    ");
  await page.keyboard.press("Shift+Tab");
  expect(await page.evaluate(() => window.strategySpike.editorView.state.sliceDoc(0, 1))).toBe("#");
  const diagnosticMetrics = await page.evaluate(() => ({
    count: window.strategySpike.metrics.diagnosticCount,
    elapsedMs: window.strategySpike.metrics.diagnosticsMs,
  }));
  expect(diagnosticMetrics.count).toBe(2);
  expect(diagnosticMetrics.elapsedMs).toBeLessThanOrEqual(testInfo.project.name.startsWith("mobile") ? 250 : 100);
  const inputSamples = await page.evaluate(async () => {
    const view = window.strategySpike.editorView;
    const values = [];
    const originalLength = view.state.doc.length;
    for (let index = 0; index < 5; index += 1) {
      const position = view.state.doc.length;
      view.dispatch({ changes: { from: position, insert: "x" } });
      view.dispatch({ changes: { from: position, to: position + 1, insert: "" } });
    }
    for (let index = 0; index < 100; index += 1) {
      await new Promise(requestAnimationFrame);
      const position = view.state.doc.length;
      const startedAt = performance.now();
      view.dispatch({ changes: { from: position, insert: "x" } });
      values.push(performance.now() - startedAt);
    }
    view.dispatch({ changes: { from: originalLength, to: view.state.doc.length, insert: "" } });
    return values;
  });
  const inputP95Ms = percentile(inputSamples, 0.95);
  expect(inputP95Ms).toBeLessThanOrEqual(testInfo.project.name.startsWith("mobile") ? 64 : 32);
  expect(await page.locator("textarea:not([hidden])").count()).toBe(0);
  expect(await page.evaluate(() => window.strategySpike.requestUrls().some(({ name }) => name.includes("/vendor/pyodide/")))).toBe(false);

  const searchKey = `${modifier}+f`;
  await editor.press(searchKey);
  await expect(page.locator(".cm-search")).toBeVisible();
  await page.keyboard.press("Escape");
  await editor.click();
  await page.evaluate(() => {
    const view = window.strategySpike.editorView;
    view.dispatch({ changes: { from: 0, insert: "on_" }, selection: { anchor: 3 } });
  });
  await page.evaluate(() => window.strategySpike.triggerCompletion());
  await page.waitForFunction(() => window.strategySpike.metrics.completionRequests > 0);
  expect(await page.evaluate(() => window.strategySpike.metrics.completionRequests)).toBeGreaterThan(0);
  await expect(page.locator(".cm-tooltip-autocomplete")).toBeVisible();
  await page.keyboard.press("Escape");

  const expectedSelection = await page.evaluate(() => {
    const view = window.strategySpike.editorView;
    view.dispatch({ selection: { anchor: 17, head: 41 }, scrollIntoView: true });
    view.focus();
    return {
      from: view.state.selection.main.from,
      to: view.state.selection.main.to,
      firstVisibleLine: view.state.doc.lineAt(view.viewport.from).number,
    };
  });
  await page.getByRole("button", { name: "Force degraded editor" }).click();
  await expect(page.getByRole("status")).toHaveText("Degraded strategy editor active");
  const textareaState = await page.locator("#strategy-source").evaluate((element) => ({
    readOnly: element.readOnly,
    selectionStart: element.selectionStart,
    selectionEnd: element.selectionEnd,
    firstVisibleLine: Number(element.dataset.firstVisibleLine),
  }));
  expect(textareaState).toMatchObject({
    readOnly: false,
    selectionStart: expectedSelection.from,
    selectionEnd: expectedSelection.to,
    firstVisibleLine: expectedSelection.firstVisibleLine,
  });

  await page.getByRole("button", { name: "Force raw copy recovery" }).click();
  await expect(page.getByRole("status")).toHaveText("Raw copy recovery active");
  await expect(page.locator("#strategy-source")).toHaveAttribute("readonly", "");
  await expect(page.locator("#strategy-source")).toBeFocused();
  console.log(`STRATEGY_SPIKE_METRIC ${JSON.stringify({
    project: testInfo.project.name,
    diagnosticsMs: diagnosticMetrics.elapsedMs,
    inputP95Ms,
  })}`);
});

test("asset failures reach the editor and runtime feasibility states", async ({ page }) => {
  await waitForHost(page);
  const message = await page.evaluate(async () => {
    try {
      await window.strategySpike.openEditor({ bundleUrl: "/missing-editor.mjs" });
      return "unexpected success";
    } catch (error) {
      return String(error);
    }
  });
  expect(message).toContain("dynamically imported module");
  await expect(page.locator("#strategy-source")).not.toHaveAttribute("readonly", "");
  await expect(page.getByRole("textbox", { name: "Python strategy source" })).toHaveCount(1);

  const runtimeMessage = await page.evaluate(async () => {
    try {
      await window.strategySpike.createRuntime({ loaderUrl: "/missing-pyodide.mjs" });
      return "unexpected success";
    } catch (error) {
      return String(error);
    }
  });
  expect(runtimeMessage).not.toBe("unexpected success");
  await expect(page.locator("#strategy-source")).not.toHaveAttribute("readonly", "");
  await expect(page.getByRole("button", { name: "Open editor" })).toBeEnabled();
});

test("Worker executes maximum-shape plain data, reports Python errors, and terminates", async ({ page }, testInfo) => {
  await waitForHost(page);
  const ready = await page.evaluate(() => window.strategySpike.createRuntime());
  expect(ready.value).toBe(2);
  expect(ready.pythonVersion).toBe("0.27.6");

  const encodedBytes = await page.evaluate(() => new TextEncoder().encode(JSON.stringify(window.strategySpike.makeMaximumEvent())).byteLength);
  expect(encodedBytes).toBeLessThanOrEqual(262144);
  const callbackResult = await page.evaluate(() => window.strategySpike.callback());
  expect(callbackResult.result).toEqual({ price_level: 1000000, orders: 256, fills: 256 });

  const samples = await page.evaluate(async () => {
    const values = [];
    for (let index = 0; index < 10; index += 1) await window.strategySpike.callback();
    const startedAt = performance.now();
    for (let index = 0; index < 100; index += 1) {
      const result = await window.strategySpike.callback();
      values.push(result);
    }
    return { values, elapsedMs: performance.now() - startedAt };
  });
  const p95 = percentile(samples.values.map(({ roundTripMs }) => roundTripMs), 0.95);
  const conversionP95 = percentile(samples.values.map(({ conversionMs }) => conversionMs), 0.95);
  const requiredP95 = testInfo.project.name.startsWith("mobile") ? 30 : 10;
  const requiredEventsPerSecond = testInfo.project.name.startsWith("mobile") ? 30 : 100;
  expect(p95).toBeLessThanOrEqual(requiredP95);
  expect(conversionP95).toBeLessThanOrEqual(10);
  expect(100000 / samples.elapsedMs).toBeGreaterThanOrEqual(requiredEventsPerSecond);

  const pythonError = await page.evaluate(() => window.strategySpike.pythonError());
  expect(pythonError.message).toContain("expected-error.py");
  expect(pythonError.message).toContain("spike exception");
  expect(pythonError.stdout).toContain("bounded stdout");
  const termination = await page.evaluate(() => window.strategySpike.hardTerminate());
  expect(termination.terminationMs).toBeLessThanOrEqual(500);
  expect(termination.animationFrames).toBeGreaterThanOrEqual(2);
  expect(termination.longTasks).toEqual([]);
  const restartStartedAt = await page.evaluate(() => performance.now());
  await page.evaluate(() => window.strategySpike.createRuntime());
  const restartMs = await page.evaluate((startedAt) => performance.now() - startedAt, restartStartedAt);
  expect(restartMs).toBeLessThanOrEqual(testInfo.project.name.startsWith("mobile") ? 18000 : 10000);
  console.log(`STRATEGY_SPIKE_METRIC ${JSON.stringify({
    project: testInfo.project.name,
    encodedBytes,
    firstCallbackMs: callbackResult.elapsedMs,
    callbackP95Ms: p95,
    conversionP95Ms: conversionP95,
    eventsPerSecond: 100000 / samples.elapsedMs,
    terminationMs: termination.terminationMs,
    busyLoopAnimationFrames: termination.animationFrames,
    busyLoopLongTasks: termination.longTasks.length,
    freshWorkerRestartMs: restartMs,
  })}`);
});

test("isolated route supports cooperative interrupt; ordinary route exposes hard-stop-only capability", async ({ page }, testInfo) => {
  if (testInfo.project.name !== "chromium") return;
  await waitForHost(page, "/spike-isolated/");
  expect(await page.evaluate(() => crossOriginIsolated)).toBe(true);
  await page.evaluate(() => window.strategySpike.createRuntime());
  const result = await page.evaluate(() => window.strategySpike.softInterrupt());
  expect(result.supported).toBe(true);
  expect(result.message).toContain("KeyboardInterrupt");
  expect(result.elapsedMs).toBeLessThanOrEqual(250);
  console.log(`STRATEGY_SPIKE_METRIC ${JSON.stringify({ project: testInfo.project.name, softInterruptMs: result.elapsedMs })}`);

  await waitForHost(page, "/spike/");
  expect(await page.evaluate(() => ({ isolated: crossOriginIsolated, shared: typeof SharedArrayBuffer }))).toEqual({
    isolated: false,
    shared: "undefined",
  });
});

test("strict policy, same-origin network, lifecycle primitives, zoom, and touch targets are viable", async ({ page }, testInfo) => {
  const policyViolations = [];
  await page.exposeFunction("recordPolicyViolation", (event) => policyViolations.push(event));
  await page.addInitScript(() => document.addEventListener("securitypolicyviolation", (event) => {
    window.recordPolicyViolation({ blockedURI: event.blockedURI, violatedDirective: event.violatedDirective });
  }));
  await waitForHost(page);
  await page.getByRole("button", { name: "Open editor" }).click();
  const evidence = await page.evaluate(() => ({
    requests: window.strategySpike.requestUrls(),
    lifecycle: {
      visibilitychange: "onvisibilitychange" in document,
      pagehide: "onpagehide" in window,
      pageshow: "onpageshow" in window,
      persisted: "persisted" in PageTransitionEvent.prototype,
      workerTerminate: typeof Worker.prototype.terminate === "function",
    },
    controls: [...document.querySelectorAll("button")].map((element) => {
      const rect = element.getBoundingClientRect();
      return { height: rect.height, width: rect.width };
    }),
  }));
  const offOrigin = evidence.requests.filter(({ name }) => {
    const url = new URL(name);
    return url.protocol !== "data:" && url.origin !== "http://127.0.0.1:8765";
  });
  expect(offOrigin).toEqual([]);
  expect(evidence.requests.some(({ name }) => /cdn|unpkg|jsdelivr|npmjs/.test(new URL(name).hostname))).toBe(false);
  expect(evidence.lifecycle).toEqual({ visibilitychange: true, pagehide: true, pageshow: true, persisted: true, workerTerminate: true });
  expect(evidence.controls.every(({ height, width }) => height >= 44 && width >= 44)).toBe(true);
  expect(policyViolations).toEqual([]);
  console.log(`STRATEGY_SPIKE_METRIC ${JSON.stringify({
    project: testInfo.project.name,
    requestCount: evidence.requests.length,
    policyViolations: policyViolations.length,
  })}`);

  await page.evaluate(() => { document.documentElement.style.zoom = "2"; });
  await expect(page.getByRole("button", { name: "Load runtime" })).toBeVisible();
  if (testInfo.project.name.startsWith("mobile")) {
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth * 2)).toBe(true);
  }
});

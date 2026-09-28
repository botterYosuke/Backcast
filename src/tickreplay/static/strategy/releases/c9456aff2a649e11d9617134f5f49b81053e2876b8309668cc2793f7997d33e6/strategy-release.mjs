'use strict';

import { mountStrategyWorkspace } from './strategy-workspace.mjs';

let assetsPromise = null;
let editorPromise = null;
let controllerModulePromise = null;
let traceControllerModulePromise = null;
let traceTransport = null;

function loadAssets() {
  assetsPromise ??= import('./strategy-assets.mjs').then(({ strategyAssets }) => strategyAssets);
  return assetsPromise;
}

const release = Object.freeze({
  async loadCapability() {
    const module = await (traceControllerModulePromise ??= import('./strategy-trace-controller.mjs'));
    traceTransport ??= module.createNativeTraceHttpTransport();
    return traceTransport.capability();
  },

  async loadEditor() {
    editorPromise ??= Promise.all([
      import('./strategy-editor.mjs'),
      loadAssets().then((assets) => import(assets.codeMirror.bundleUrl)),
    ]).then(([editorModule, toolkit]) => Object.freeze({
      createStrategyEditor: editorModule.createStrategyEditor,
      toolkit,
    }));
    return editorPromise;
  },

  async loadController({ callbacks, performanceClass, mode = 'pyodide' }) {
    if (mode === 'native_trace_v1') {
      const module = await (traceControllerModulePromise ??= import('./strategy-trace-controller.mjs'));
      traceTransport ??= module.createNativeTraceHttpTransport();
      return new module.NativeTraceController({ transport: traceTransport, ...callbacks });
    }
    if (mode !== 'pyodide') throw new TypeError('unknown strategy execution mode');
    const [assets, controllerModule] = await Promise.all([
      loadAssets(),
      controllerModulePromise ??= import('./strategy-controller.mjs'),
    ]);
    const workerUrl = new URL('./strategy-worker.mjs', import.meta.url);
    const runnerUrl = new URL('./strategy-runner.py', import.meta.url).href;
    return new controllerModule.StrategyController({
      createWorker: () => new Worker(workerUrl, { type: 'module' }),
      assets: {
        loaderUrl: assets.pyodide.loaderUrl,
        indexUrl: assets.pyodide.indexUrl,
        runnerUrl,
      },
      performanceClass,
      ...callbacks,
    });
  },
});

export function mount({ root, host, safeCall, ...options }) {
  return mountStrategyWorkspace({ root, host, safeCall, release, ...options });
}

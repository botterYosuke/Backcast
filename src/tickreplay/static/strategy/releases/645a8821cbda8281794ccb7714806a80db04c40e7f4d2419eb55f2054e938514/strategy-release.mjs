'use strict';

import { mountStrategyWorkspace } from './strategy-workspace.mjs';

let assetsPromise = null;
let editorPromise = null;
let controllerModulePromise = null;

function loadAssets() {
  assetsPromise ??= import('./strategy-assets.mjs').then(({ strategyAssets }) => strategyAssets);
  return assetsPromise;
}

const release = Object.freeze({
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

  async loadController({ callbacks, performanceClass }) {
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

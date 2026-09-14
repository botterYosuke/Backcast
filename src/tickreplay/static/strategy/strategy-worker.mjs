'use strict';

import {
  PROTOCOL_VERSION,
  createRequestEnvelope,
  validateCommandBatch,
} from './strategy-protocol.mjs';

const RUNNER_MODULE_PATH = 'strategy_runner.py';
const RUNNER_CALL = `
import strategy_runner
await strategy_runner.handle_json(request_json)
`;

function boundedMessage(error, maximum = 2048) {
  const text = error instanceof Error ? error.message : String(error);
  const bytes = new TextEncoder().encode(text);
  if (bytes.byteLength <= maximum) return text;
  return new TextDecoder().decode(bytes.slice(0, maximum));
}

function destroyProxy(value) {
  if (value && typeof value.destroy === 'function') value.destroy();
}

function normalizedRequest(message) {
  return createRequestEnvelope({
    phase: message.phase,
    requestId: message.request_id,
    session: message.session,
    runId: message.run_id,
    source: message.source,
    sequence: message.sequence,
    payload: message.payload,
  });
}

export class StrategyWorkerRuntime {
  constructor(scope, {
    importModule = (url) => import(url),
    fetchResource = (url) => fetch(url),
  } = {}) {
    this.scope = scope;
    this.importModule = importModule;
    this.fetchResource = fetchResource;
    this.pyodide = null;
    this.runnerReady = false;
    this.interruptView = null;
    this.currentRequest = null;
  }

  install() {
    this.scope.addEventListener('message', (event) => { void this.onMessage(event.data); });
    this.scope.addEventListener('messageerror', () => this.onMessageError());
    return this;
  }

  async onMessage(message) {
    if (message?.kind === 'strategy_interrupt_setup') {
      this.#installInterruptBuffer(message);
      return;
    }
    if (message?.kind !== 'strategy_request') return;
    if (this.currentRequest) {
      this.#postFailure(
        message,
        'protocol_error',
        new Error('Worker accepts only one request at a time'),
      );
      return;
    }
    let request;
    try {
      request = normalizedRequest(message);
    } catch (error) {
      this.#postFailure(message, 'protocol_error', error);
      return;
    }
    this.currentRequest = request;
    try {
      const outcome = request.phase === 'load'
        ? await this.#load(request)
        : await this.#invokeRunner(request.phase, request.payload);
      if (this.currentRequest !== request) return;
      this.#emitLogs(request, outcome);
      if (!outcome.ok) {
        this.#postRunnerFailure(request, outcome.diagnostic);
        return;
      }
      const result = this.#resultFor(request.phase, outcome);
      this.#postResponse(request, { ok: true, result });
    } catch (error) {
      if (this.currentRequest === request) this.#postFailure(request, this.#categoryFor(request.phase), error);
    } finally {
      if (this.currentRequest === request) this.currentRequest = null;
    }
  }

  onMessageError() {
    if (!this.currentRequest) return;
    this.#postFailure(
      this.currentRequest,
      'messageerror',
      new Error('Worker could not deserialize the request'),
    );
    this.currentRequest = null;
  }

  #installInterruptBuffer(message) {
    if (
      message.protocol_version !== PROTOCOL_VERSION
      || typeof SharedArrayBuffer === 'undefined'
      || !(message.buffer instanceof SharedArrayBuffer)
      || message.buffer.byteLength !== Int32Array.BYTES_PER_ELEMENT
    ) return;
    this.interruptView = new Int32Array(message.buffer);
    if (this.pyodide) this.pyodide.setInterruptBuffer(this.interruptView);
  }

  async #load(request) {
    if (this.pyodide || this.runnerReady) throw new Error('Worker is already loaded');
    const { loader_url: loaderUrl, index_url: indexUrl, runner_url: runnerUrl } = request.payload.assets;
    const loader = await this.importModule(loaderUrl);
    if (typeof loader.loadPyodide !== 'function') throw new Error('Pyodide loader has no loadPyodide export');
    this.pyodide = await loader.loadPyodide({
      indexURL: indexUrl,
      jsglobals: Object.create(null),
    });
    if (this.interruptView) this.pyodide.setInterruptBuffer(this.interruptView);
    this.scope.postMessage({
      kind: 'strategy_progress',
      protocol_version: PROTOCOL_VERSION,
      request_id: request.request_id,
      phase: request.phase,
      session: request.session,
      run_id: request.run_id,
      source: request.source,
      sequence: request.sequence,
      stage: 'runtime_ready',
    });
    const response = await this.fetchResource(runnerUrl, { cache: 'force-cache' });
    if (!response.ok) throw new Error(`strategy runner request failed (${response.status})`);
    const runnerSource = await response.text();
    this.pyodide.FS.writeFile(RUNNER_MODULE_PATH, runnerSource, { encoding: 'utf8' });
    const outcome = await this.#invokeRunner('load', {
      source: request.payload.source,
      filename: request.payload.filename,
    });
    if (outcome.ok) this.runnerReady = true;
    return outcome;
  }

  async #invokeRunner(operation, payload) {
    if (!this.pyodide) throw new Error('Pyodide runtime is unavailable');
    if (operation !== 'load' && !this.runnerReady) throw new Error('strategy runner is unavailable');
    const requestJson = JSON.stringify({
      operation,
      ...(operation === 'load' ? payload : { payload }),
    });
    let globalsProxy = null;
    let resultProxy = null;
    try {
      globalsProxy = this.pyodide.toPy({ request_json: requestJson });
      resultProxy = await this.pyodide.runPythonAsync(RUNNER_CALL, {
        filename: RUNNER_MODULE_PATH,
        globals: globalsProxy,
      });
      let result = resultProxy;
      if (typeof resultProxy !== 'string' && typeof resultProxy?.toJs === 'function') {
        result = resultProxy.toJs({ create_pyproxies: false });
      }
      if (typeof result !== 'string') throw new Error('strategy runner returned a non-string result');
      return JSON.parse(result);
    } finally {
      try {
        destroyProxy(resultProxy);
      } finally {
        if (globalsProxy !== resultProxy) destroyProxy(globalsProxy);
      }
    }
  }

  #resultFor(phase, outcome) {
    if (phase === 'load') {
      return {
        callbacks: outcome.callbacks,
        capabilities: { soft_interrupt: this.interruptView !== null },
      };
    }
    if (phase === 'event') return { commands: validateCommandBatch(outcome.commands) };
    return {};
  }

  #emitLogs(request, outcome) {
    for (const stream of ['stdout', 'stderr']) {
      if (!outcome[stream] && !outcome[`${stream}_truncated`]) continue;
      this.scope.postMessage({
        kind: 'strategy_log',
        protocol_version: PROTOCOL_VERSION,
        phase: request.phase,
        request_id: request.request_id,
        session: request.session,
        run_id: request.run_id,
        source: request.source,
        sequence: request.sequence,
        stream,
        text: outcome[stream] ?? '',
        truncated: Boolean(outcome[`${stream}_truncated`]),
      });
    }
  }

  #postRunnerFailure(request, runnerDiagnostic) {
    const diagnostic = this.#diagnostic(
      request,
      runnerDiagnostic?.category ?? 'python_exception',
      runnerDiagnostic?.message ?? 'strategy callback failed',
      runnerDiagnostic?.range ?? null,
    );
    this.#postResponse(request, { ok: false, diagnostic });
  }

  #postFailure(request, category, error) {
    const diagnostic = this.#diagnostic(request, category, boundedMessage(error), null);
    this.#postResponse(request, { ok: false, diagnostic });
  }

  #diagnostic(request, category, message, range) {
    const phase = request?.phase ?? 'load';
    const source = request?.source ?? {
      source_hash: '0'.repeat(64), strategy_revision: 0, load_attempt_id: 'unknown',
    };
    const activeRun = request?.run_id !== null && request?.run_id !== undefined;
    return {
      protocol_version: PROTOCOL_VERSION,
      phase,
      severity: 'error',
      category,
      message: boundedMessage(message),
      source_hash: source.source_hash,
      draft_revision: phase === 'load' ? source.strategy_revision : null,
      loaded_revision: phase === 'load' ? null : source.strategy_revision,
      running_revision: activeRun ? source.strategy_revision : null,
      load_attempt_id: source.load_attempt_id,
      run_id: request?.run_id ?? null,
      sequence: request?.sequence ?? 0,
      range,
    };
  }

  #postResponse(request, outcome) {
    const response = {
      kind: 'strategy_response',
      protocol_version: PROTOCOL_VERSION,
      phase: request?.phase ?? 'load',
      request_id: request?.request_id ?? 'unknown',
      session: request?.session ?? {
        stem: 'unknown', code: 'unknown', actual_date: '1970-01-01', generation: 0,
      },
      run_id: request?.run_id ?? null,
      source: request?.source ?? {
        source_hash: '0'.repeat(64), strategy_revision: 0, load_attempt_id: 'unknown',
      },
      sequence: request?.sequence ?? 0,
      ...outcome,
    };
    try {
      this.scope.postMessage(response);
    } catch (error) {
      const minimal = {
        ...response,
        ok: false,
        result: undefined,
        diagnostic: this.#diagnostic(request, 'serialization_error', error, null),
      };
      delete minimal.result;
      this.scope.postMessage(minimal);
    }
  }

  #categoryFor(phase) {
    if (phase === 'load') return this.pyodide ? 'compile_error' : 'runtime_load_error';
    return 'python_exception';
  }
}

export function installStrategyWorker(scope, dependencies) {
  return new StrategyWorkerRuntime(scope, dependencies).install();
}

if (
  typeof WorkerGlobalScope !== 'undefined'
  && typeof self !== 'undefined'
  && self instanceof WorkerGlobalScope
) installStrategyWorker(self);

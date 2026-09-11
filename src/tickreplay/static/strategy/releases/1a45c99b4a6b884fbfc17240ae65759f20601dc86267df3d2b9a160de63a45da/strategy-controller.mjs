'use strict';

import {
  LIMITS,
  PROTOCOL_VERSION,
  StrategyProtocolError,
  boundedOpenOrders,
  canonicalSessionKey,
  createRequestEnvelope,
  encodedByteLength,
  truncateUtf8,
  validateDiagnostic,
  validateWorkerResponse,
} from './strategy-protocol.mjs';

const ACTIVE_TAGS = new Set(['starting', 'running', 'paused', 'stopping']);
const ERROR_CAUSES = new Set(['timeout', 'worker_or_protocol_error']);
const CAUSE_PRIORITY = Object.freeze({
  replay_end: 0,
  worker_or_protocol_error: 1,
  timeout: 2,
  explicit_stop: 3,
  explicit_load: 4,
  seek: 5,
  reset: 6,
  session_change: 7,
});

export const STRATEGY_DEADLINES = Object.freeze({
  desktop: Object.freeze({ load: 20_000, compile: 3_000, start: 1_000, event: 250, end: 500 }),
  mobile: Object.freeze({ load: 40_000, compile: 6_000, start: 2_000, event: 500, end: 1_000 }),
  interruptGrace: 250,
  hardTerminate: 500,
});

export class StrategyControllerError extends Error {
  constructor(code, message, diagnostic = null) {
    super(message);
    this.name = 'StrategyControllerError';
    this.code = code;
    this.diagnostic = diagnostic;
  }
}

function freeze(value) {
  if (value && typeof value === 'object' && !Object.isFrozen(value)) {
    for (const child of Object.values(value)) freeze(child);
    Object.freeze(value);
  }
  return value;
}

function clone(value) {
  if (value === undefined) return undefined;
  return JSON.parse(JSON.stringify(value));
}

function defaultId(prefix) {
  if (globalThis.crypto?.randomUUID) return `${prefix}-${globalThis.crypto.randomUUID()}`;
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}
function defaultInterruptBuffer() {
  if (!globalThis.crossOriginIsolated || typeof SharedArrayBuffer === 'undefined') return null;
  return new Int32Array(new SharedArrayBuffer(Int32Array.BYTES_PER_ELEMENT));
}
function isErrorEvent(value) {
  return value && typeof value === 'object' && typeof value.message === 'string';
}

export class StrategyController {
  constructor({
    createWorker,
    assets,
    performanceClass = 'desktop',
    deadlines = STRATEGY_DEADLINES,
    makeId = defaultId,
    createInterruptBuffer = defaultInterruptBuffer,
    setTimer = globalThis.setTimeout.bind(globalThis),
    clearTimer = globalThis.clearTimeout.bind(globalThis),
    onStateChange = () => {},
    onDiagnostic = () => {},
    onCleanup = () => ({ canceledOrderIds: [] }),
    onPauseReplay = () => {},
    onManualGateChange = () => {},
  }) {
    if (typeof createWorker !== 'function') throw new TypeError('createWorker is required');
    if (!assets || typeof assets !== 'object') throw new TypeError('strategy assets are required');
    if (!deadlines[performanceClass]) throw new TypeError('unknown performance class');
    this.createWorker = createWorker;
    this.assets = freeze({ ...assets });
    this.performanceClass = performanceClass;
    this.deadlines = deadlines;
    this.makeId = makeId;
    this.createInterruptBuffer = createInterruptBuffer;
    this.setTimer = setTimer;
    this.clearTimer = clearTimer;
    this.onStateChange = onStateChange;
    this.onDiagnostic = onDiagnostic;
    this.onCleanup = onCleanup;
    this.onPauseReplay = onPauseReplay;
    this.onManualGateChange = onManualGateChange;

    this.worker = null;
    this.workerGeneration = 0;
    this.interruptView = null;
    this.pending = null;
    this.loaded = null;
    this.run = null;
    this.nextLoadedRevision = 1;
    this.statusTag = 'idle';
    this.statusPhase = null;
    this.terminalValue = null;
    this.manualBlocked = false;
    this.cleanupKeys = new Set();
    this.invalidationJob = null;
    this.diagnostics = [];
    this.diagnosticBytes = 0;
    this.diagnosticsTruncated = 0;
    this.logsByStream = { stdout: '', stderr: '' };
    this.logTruncated = { stdout: 0, stderr: 0 };
    this.hostCallbackFailures = 0;
  }

  getState() {
    return freeze({
      tag: this.statusTag,
      loadedRevision: this.loaded?.revision ?? 0,
      sourceHash: this.loaded?.sourceHash ?? null,
      session: clone(this.run?.session ?? this.loaded?.session ?? null),
      runId: this.run?.runId ?? null,
      runningRevision: this.run?.strategyRevision ?? null,
      sequence: this.run?.sequence ?? 0,
      inFlight: this.pending !== null,
      phase: this.statusPhase,
      terminal: clone(this.terminalValue),
      diagnosticCount: this.diagnostics.length,
      diagnosticsTruncated: this.diagnosticsTruncated,
      hostCallbackFailures: this.hostCallbackFailures,
    });
  }

  isActive() { return ACTIVE_TAGS.has(this.statusTag) || this.pending !== null; }

  manualEntryBlocked() { return this.manualBlocked || this.isActive(); }

  terminalOutcome() {
    return this.terminalValue === null ? null : freeze(clone(this.terminalValue));
  }

  diagnosticHistory() { return freeze(clone(this.diagnostics)); }

  logs() {
    return freeze({
      stdout: this.logsByStream.stdout,
      stderr: this.logsByStream.stderr,
      stdoutTruncated: this.logTruncated.stdout,
      stderrTruncated: this.logTruncated.stderr,
    });
  }

  async load({
    source,
    sourceHash,
    draftRevision,
    loadAttemptId,
    session,
    filename = 'strategy.py',
    performanceClass = this.performanceClass,
  }) {
    if (!this.deadlines[performanceClass]) throw new StrategyControllerError('precondition', 'unknown performance class');
    if (this.worker || this.run || this.invalidationJob) {
      await this.invalidate('explicit_load');
    }
    const sourceIdentity = {
      source_hash: sourceHash,
      strategy_revision: draftRevision,
      load_attempt_id: loadAttemptId,
    };
    const request = createRequestEnvelope({
      phase: 'load',
      requestId: this.makeId('request'),
      session,
      runId: null,
      source: sourceIdentity,
      sequence: 0,
      payload: {
        source,
        filename,
        assets: {
          loader_url: this.assets.loaderUrl,
          index_url: this.assets.indexUrl,
          runner_url: this.assets.runnerUrl,
        },
      },
    });
    const worker = this.createWorker();
    this.#attachWorker(worker);
    this.worker = worker;
    this.performanceClass = performanceClass;
    try {
      this.interruptView = this.createInterruptBuffer();
      if (this.interruptView) {
        worker.postMessage({
          kind: 'strategy_interrupt_setup',
          protocol_version: PROTOCOL_VERSION,
          buffer: this.interruptView.buffer,
        });
      }
    } catch (error) {
      await this.#terminateWorker();
      throw error;
    }
    this.terminalValue = null;
    this.#setStatus('loading', 'load');
    try {
      const response = await this.#request(request);
      const revision = this.nextLoadedRevision;
      this.nextLoadedRevision += 1;
      this.loaded = freeze({
        source,
        sourceHash,
        draftRevision,
        loadAttemptId,
        revision,
        session: clone(session),
        callbacks: clone(response.result.callbacks ?? ['on_event']),
      });
      this.#setStatus('ready', null);
      return freeze({ ok: true, loadedRevision: revision, sourceHash });
    } catch (error) {
      await this.#terminateWorker();
      this.terminalValue = freeze({ kind: 'load_failed', phase: 'load', code: error.code ?? 'load_failed' });
      this.#setStatus('errored', 'load');
      throw error;
    }
  }

  async start({
    session,
    cursor,
    tickCount,
    lastCommitted,
    portfolio,
    openOrders,
    replayPaused = true,
  }) {
    this.#assertCanStart({ session, cursor, tickCount, lastCommitted, openOrders, replayPaused });
    const snapshot = boundedOpenOrders(openOrders);
    const runId = this.makeId('run');
    const sourceIdentity = this.#runningSourceIdentity();
    const payload = {
      next_session_tick_index: cursor,
      last_committed_session_tick_index: lastCommitted?.sessionTickIndex ?? null,
      last_replay_time_us: lastCommitted?.replayTimeUs ?? null,
      last_price_level: lastCommitted?.priceLevel ?? null,
      portfolio,
      ...snapshot,
    };
    const request = createRequestEnvelope({
      phase: 'start',
      requestId: this.makeId('request'),
      session,
      runId,
      source: sourceIdentity,
      sequence: 0,
      payload,
    });
    this.run = {
      runId,
      session: clone(session),
      strategyRevision: this.loaded.revision,
      sequence: 0,
      cleanupDone: false,
    };
    this.terminalValue = null;
    this.#setManualBlocked(true);
    this.#setStatus('starting', 'start');
    try {
      await this.#request(request);
      this.#assertCurrentRun(request);
      this.#setStatus('paused', null);
      return freeze({ ok: true, runId, runningRevision: this.run.strategyRevision });
    } catch (error) {
      await this.invalidate(error.code === 'timeout' ? 'timeout' : 'worker_or_protocol_error');
      throw error;
    }
  }

  resume() {
    if (this.statusTag !== 'paused') {
      throw new StrategyControllerError('invalid_state', 'strategy must be paused before resume');
    }
    this.#setStatus('running', null);
    return this.getState();
  }

  pause() {
    if (!['running', 'paused'].includes(this.statusTag)) {
      throw new StrategyControllerError('invalid_state', 'strategy is not running');
    }
    if (this.statusTag !== 'paused') this.#setStatus('paused', this.pending?.request.phase ?? null);
    return this.getState();
  }

  async dispatchOneEvent({
    tick,
    fills,
    fillCursor,
    portfolio,
    openOrders,
    openOrdersTotal,
    openOrdersTruncated,
  }) {
    if (this.statusTag !== 'running') {
      throw new StrategyControllerError('invalid_state', 'strategy must be running to dispatch an event');
    }
    if (this.pending) throw new StrategyControllerError('single_flight', 'single-flight callback already pending');
    const sequence = this.run.sequence + 1;
    const request = createRequestEnvelope({
      phase: 'event',
      requestId: this.makeId('request'),
      session: this.run.session,
      runId: this.run.runId,
      source: this.#runningSourceIdentity(),
      sequence,
      payload: {
        tick,
        fills,
        fill_cursor: fillCursor,
        portfolio,
        open_orders: openOrders,
        open_orders_total: openOrdersTotal,
        open_orders_truncated: openOrdersTruncated,
      },
    });
    this.run.sequence = sequence;
    try {
      const response = await this.#request(request);
      this.#assertCurrentRun(request);
      const commands = response.result.commands;
      return freeze({
        kind: 'commands',
        protocolVersion: PROTOCOL_VERSION,
        session: clone(request.session),
        runId: request.run_id,
        strategyRevision: request.source.strategy_revision,
        sequence,
        observedSessionTickIndex: tick.session_tick_index,
        eligibleFromSessionTick: tick.session_tick_index + 1,
        commands,
      });
    } catch (error) {
      await this.invalidate(error.code === 'timeout' ? 'timeout' : 'worker_or_protocol_error');
      throw error;
    }
  }

  invalidate(cause) {
    if (!Object.hasOwn(CAUSE_PRIORITY, cause)) {
      return Promise.reject(new StrategyControllerError('invalid_cause', 'unknown invalidation cause'));
    }
    if (this.invalidationJob) {
      if (CAUSE_PRIORITY[cause] > CAUSE_PRIORITY[this.invalidationJob.cause]) {
        this.invalidationJob.cause = cause;
      }
      return this.invalidationJob.promise;
    }
    const capturedRun = this.run ? clone(this.run) : null;
    const failurePhase = this.statusPhase
      ?? (this.statusTag === 'starting' ? 'start' : 'event');
    this.run = null;
    this.#rejectPending(new StrategyControllerError('invalidated', `strategy request invalidated: ${cause}`));
    if (!capturedRun) return this.#invalidateWithoutRun(cause);

    let resolveJob;
    const promise = new Promise((resolve) => { resolveJob = resolve; });
    const job = { cause, capturedRun, failurePhase, promise, resolve: resolveJob };
    this.invalidationJob = job;
    this.#setStatus('stopping', cause);
    queueMicrotask(() => { void this.#finalizeInvalidation(job); });
    return promise;
  }

  stop(cause = 'explicit_stop') {
    return this.invalidate(cause);
  }

  async end(summary) {
    if (!this.run || !['running', 'paused'].includes(this.statusTag)) {
      throw new StrategyControllerError('invalid_state', 'no active strategy run to end');
    }
    if (this.pending) throw new StrategyControllerError('single_flight', 'cannot finalize with a callback pending');
    const capturedRun = clone(this.run);
    await this.#cleanupRun(capturedRun, 'replay_end', 'end');
    this.#setStatus('stopping', 'end');
    this.run.sequence += 1;
    const request = createRequestEnvelope({
      phase: 'end',
      requestId: this.makeId('request'),
      session: this.run.session,
      runId: this.run.runId,
      source: this.#runningSourceIdentity(),
      sequence: this.run.sequence,
      payload: summary,
    });
    try {
      await this.#request(request);
    } catch (error) {
      if (!error.diagnostic) this.#appendSyntheticDiagnostic('end', error.code ?? 'end_failure', error.message);
    }
    this.run = null;
    await this.#terminateWorker();
    this.#setManualBlocked(false);
    this.terminalValue = freeze({ kind: 'completed', cause: 'replay_end' });
    this.#setStatus('completed', null);
    return this.terminalOutcome();
  }

  #assertCanStart({ session, cursor, tickCount, lastCommitted, openOrders, replayPaused }) {
    if (this.statusTag !== 'ready' || !this.worker || !this.loaded) {
      throw new StrategyControllerError('not_ready', 'strategy is not ready');
    }
    if (!replayPaused) throw new StrategyControllerError('replay_playing', 'replay must be paused');
    if (!Number.isSafeInteger(tickCount) || tickCount <= 0) {
      throw new StrategyControllerError('session_complete', 'session_complete: session has no ticks');
    }
    if (!Number.isSafeInteger(cursor) || cursor < 0 || cursor >= tickCount) {
      throw new StrategyControllerError('session_complete', 'session_complete: no unseen tick remains');
    }
    if (canonicalSessionKey(session) !== canonicalSessionKey(this.loaded.session)) {
      throw new StrategyControllerError('session_mismatch', 'loaded session identity is stale');
    }
    if (!Array.isArray(openOrders) || openOrders.length > LIMITS.openOrders) {
      throw new StrategyControllerError('host_capacity_violation', 'open order capacity exceeds 256');
    }
    if (cursor === 0 && lastCommitted !== null) {
      throw new StrategyControllerError('future_exposure', 'cursor zero cannot have a committed tick');
    }
    if (cursor > 0) {
      if (!lastCommitted || lastCommitted.sessionTickIndex !== cursor - 1) {
        throw new StrategyControllerError('invalid_cursor', 'last committed tick must immediately precede cursor');
      }
      for (const field of ['replayTimeUs', 'priceLevel']) {
        if (!Number.isSafeInteger(lastCommitted[field])) {
          throw new StrategyControllerError('invalid_cursor', 'mid-session start requires prior market state');
        }
      }
    }
  }

  #runningSourceIdentity() {
    return {
      source_hash: this.loaded.sourceHash,
      strategy_revision: this.loaded.revision,
      load_attempt_id: this.loaded.loadAttemptId,
    };
  }

  #assertCurrentRun(request) {
    if (
      !this.run
      || this.run.runId !== request.run_id
      || this.run.strategyRevision !== request.source.strategy_revision
      || canonicalSessionKey(this.run.session) !== canonicalSessionKey(request.session)
    ) throw new StrategyControllerError('stale_result', 'stale strategy result was rejected');
  }

  #attachWorker(worker) {
    if (!worker || typeof worker.addEventListener !== 'function' || typeof worker.postMessage !== 'function') {
      throw new TypeError('createWorker must return a Worker-compatible object');
    }
    this.workerGeneration += 1;
    const generation = this.workerGeneration;
    worker.addEventListener('message', (event) => this.#guardWorkerListener(
      () => this.#onWorkerMessage(worker, generation, event.data),
    ));
    worker.addEventListener('messageerror', () => this.#guardWorkerListener(
      () => this.#onWorkerFailure(worker, generation, 'messageerror'),
    ));
    worker.addEventListener('error', (event) => this.#guardWorkerListener(
      () => this.#onWorkerFailure(
        worker,
        generation,
        isErrorEvent(event) ? event.message : 'worker error',
      ),
    ));
  }

  #guardWorkerListener(listener) {
    try {
      listener();
    } catch (error) {
      this.#rejectPending(error instanceof Error ? error : new Error(String(error)));
      void this.invalidate('worker_or_protocol_error');
    }
  }

  #onWorkerMessage(worker, generation, message) {
    if (worker !== this.worker || generation !== this.workerGeneration) return;
    if (message?.kind === 'strategy_progress') {
      this.#onProgress(message);
      return;
    }
    if (message?.kind === 'strategy_log') {
      this.#onLog(message);
      return;
    }
    if (message?.kind === 'strategy_diagnostic') {
      this.#onStandaloneDiagnostic(message);
      return;
    }
    if (message?.kind !== 'strategy_response') {
      throw new StrategyProtocolError('invalid_kind', 'unknown Worker message kind');
    }
    if (!this.pending || message.request_id !== this.pending.request.request_id) return;
    let response;
    try {
      response = validateWorkerResponse(message, this.#expectedIdentity(this.pending.request));
    } catch (error) {
      this.#settlePending(false, error);
      return;
    }
    if (!response.ok) {
      this.#appendDiagnostic(response.diagnostic);
      this.#settlePending(false, new StrategyControllerError(
        'worker_failure', response.diagnostic.message, response.diagnostic,
      ));
      return;
    }
    this.#settlePending(true, response);
  }

  #onProgress(message) {
    if (!this.pending || this.pending.request.phase !== 'load') return;
    if (message.request_id !== this.pending.request.request_id || message.stage !== 'runtime_ready') return;
    this.#armDeadline(this.pending, this.deadlines[this.performanceClass].compile);
  }

  #onLog(message) {
    if (!['stdout', 'stderr'].includes(message.stream) || typeof message.text !== 'string') {
      throw new StrategyProtocolError('invalid_log', 'invalid strategy log event');
    }
    if (typeof message.truncated !== 'boolean') {
      throw new StrategyProtocolError('invalid_log', 'log truncation flag must be boolean');
    }
    if (!this.pending || !this.#logMatchesRequest(message, this.pending.request)) return;
    const maximum = message.stream === 'stdout' ? LIMITS.stdoutBytes : LIMITS.stderrBytes;
    const previous = this.logsByStream[message.stream];
    const combined = `${previous}${message.text}`;
    const bytes = new TextEncoder().encode(combined).byteLength;
    if (bytes <= maximum) {
      this.logsByStream[message.stream] = combined;
      return;
    }
    this.logsByStream[message.stream] = truncateUtf8(combined, maximum);
    this.logTruncated[message.stream] += 1;
  }

  #logMatchesRequest(message, request) {
    try {
      return message.protocol_version === PROTOCOL_VERSION
        && message.phase === request.phase
        && message.request_id === request.request_id
        && canonicalSessionKey(message.session) === canonicalSessionKey(request.session)
        && message.run_id === request.run_id
        && message.source?.source_hash === request.source.source_hash
        && message.source?.strategy_revision === request.source.strategy_revision
        && message.source?.load_attempt_id === request.source.load_attempt_id
        && message.sequence === request.sequence;
    } catch (error) {
      if (error instanceof StrategyProtocolError) return false;
      throw error;
    }
  }

  #onStandaloneDiagnostic(message) {
    const diagnostic = validateDiagnostic(message.diagnostic);
    if (this.loaded && diagnostic.source_hash !== this.loaded.sourceHash) return;
    if (this.run && diagnostic.run_id !== null && diagnostic.run_id !== this.run.runId) return;
    this.#appendDiagnostic(diagnostic);
  }

  #onWorkerFailure(worker, generation, message) {
    if (worker !== this.worker || generation !== this.workerGeneration) return;
    const error = new StrategyControllerError('worker_error', message);
    if (this.pending) {
      this.#settlePending(false, error);
      return;
    }
    if (this.run) void this.invalidate('worker_or_protocol_error');
  }

  #expectedIdentity(request) {
    return {
      phase: request.phase,
      requestId: request.request_id,
      session: request.session,
      runId: request.run_id,
      source: request.source,
      sequence: request.sequence,
    };
  }

  #request(request) {
    if (this.pending) {
      return Promise.reject(new StrategyControllerError('single_flight', 'single-flight request already pending'));
    }
    if (!this.worker) return Promise.reject(new StrategyControllerError('worker_missing', 'Worker is unavailable'));
    return new Promise((resolve, reject) => {
      const pending = { request, resolve, reject, timer: null, settled: false };
      this.pending = pending;
      this.#armDeadline(pending, this.deadlines[this.performanceClass][request.phase]);
      this.#emitState();
      try {
        this.worker.postMessage(request);
      } catch (error) {
        this.#settlePending(false, new StrategyControllerError('post_message', String(error)));
      }
    });
  }

  #armDeadline(pending, milliseconds) {
    if (pending.timer !== null) this.clearTimer(pending.timer);
    pending.timer = this.setTimer(() => {
      if (pending.settled || this.pending !== pending) return;
      this.#settlePending(false, new StrategyControllerError(
        'timeout', `${pending.request.phase} timeout after ${milliseconds}ms`,
      ));
    }, milliseconds);
  }

  #settlePending(succeeded, value) {
    const pending = this.pending;
    if (!pending || pending.settled) return;
    pending.settled = true;
    if (pending.timer !== null) this.clearTimer(pending.timer);
    this.pending = null;
    this.#emitState();
    if (succeeded) pending.resolve(value);
    else pending.reject(value);
  }

  #rejectPending(error) {
    if (this.pending) this.#settlePending(false, error);
  }

  async #invalidateWithoutRun(cause) {
    if (this.worker) await this.#terminateWorker();
    if (this.statusTag !== 'loading' || cause !== 'explicit_load') this.#setStatus('idle', null);
    this.#setManualBlocked(false);
    const outcome = freeze({ kind: 'stopped', cause, hadRun: false });
    this.terminalValue = outcome;
    return outcome;
  }

  async #finalizeInvalidation(job) {
    try {
      await this.#cleanupRun(job.capturedRun, job.cause, job.failurePhase);
      await this.#terminateWorker();
      this.#setManualBlocked(false);
      const isError = ERROR_CAUSES.has(job.cause);
      const outcome = freeze({
        kind: isError ? 'error' : 'stopped',
        cause: job.cause,
        session: job.capturedRun.session,
        runId: job.capturedRun.runId,
        strategyRevision: job.capturedRun.strategyRevision,
      });
      this.terminalValue = outcome;
      this.#setStatus(isError ? 'errored' : 'idle', null);
      job.resolve(outcome);
    } finally {
      if (this.invalidationJob === job) this.invalidationJob = null;
    }
  }

  async #cleanupRun(run, cause, phase) {
    const key = `${run.session.generation}|${run.runId}|${run.strategyRevision}`;
    if (this.cleanupKeys.has(key)) return;
    this.cleanupKeys.add(key);
    try {
      this.onPauseReplay(freeze({ cause, session: clone(run.session), runId: run.runId }));
    } catch (error) {
      this.hostCallbackFailures += 1;
      this.#appendSyntheticDiagnostic(
        phase,
        'host_callback_error',
        `pause callback failed: ${error instanceof Error ? error.message : String(error)}`,
        run,
      );
    }
    const cleanupRequest = freeze({
      cause,
      session: clone(run.session),
      runId: run.runId,
      strategyRevision: run.strategyRevision,
    });
    try {
      await this.onCleanup(cleanupRequest);
    } catch (error) {
      this.#appendSyntheticDiagnostic(
        phase,
        'host_callback_error',
        `strategy cleanup callback failed: ${error instanceof Error ? error.message : String(error)}`,
        run,
      );
    }
    this.#appendSyntheticDiagnostic(
      phase,
      'strategy_cleanup',
      `strategy cleanup: ${cause}`,
      run,
      cause === 'replay_end' ? 'info' : 'warning',
    );
  }

  async #terminateWorker() {
    const worker = this.worker;
    if (!worker) return;
    this.worker = null;
    this.workerGeneration += 1;
    if (this.interruptView) {
      Atomics.store(this.interruptView, 0, 2);
      await new Promise((resolve) => {
        this.setTimer(resolve, Math.min(this.deadlines.interruptGrace, this.deadlines.hardTerminate));
      });
    }
    worker.terminate();
    this.interruptView = null;
  }

  #appendSyntheticDiagnostic(phase, category, message, run = this.run, severity = 'error') {
    const loaded = this.loaded;
    if (!loaded) return;
    this.#appendDiagnostic(validateDiagnostic({
      protocol_version: PROTOCOL_VERSION,
      phase,
      severity,
      category,
      message,
      source_hash: loaded.sourceHash,
      draft_revision: null,
      loaded_revision: loaded.revision,
      running_revision: run?.strategyRevision ?? null,
      load_attempt_id: loaded.loadAttemptId,
      run_id: run?.runId ?? null,
      sequence: run?.sequence ?? null,
      range: null,
    }));
  }

  #appendDiagnostic(diagnostic) {
    const entry = freeze(clone(diagnostic));
    const bytes = encodedByteLength(entry);
    if (bytes > LIMITS.diagnosticBytes) {
      this.diagnosticsTruncated += 1;
      return;
    }
    while (
      this.diagnostics.length >= LIMITS.diagnosticEntries
      || this.diagnosticBytes + bytes > LIMITS.diagnosticBytes
    ) {
      const removed = this.diagnostics.shift();
      this.diagnosticBytes -= encodedByteLength(removed);
      this.diagnosticsTruncated += 1;
    }
    this.diagnostics.push(entry);
    this.diagnosticBytes += bytes;
    try {
      this.onDiagnostic(entry);
    } catch {
      this.hostCallbackFailures += 1;
    }
    this.#emitState();
  }

  #setStatus(tag, phase) {
    this.statusTag = tag;
    this.statusPhase = phase;
    this.#emitState();
  }

  #setManualBlocked(blocked) {
    if (this.manualBlocked === blocked) return;
    this.manualBlocked = blocked;
    try {
      this.onManualGateChange(blocked);
    } catch {
      this.hostCallbackFailures += 1;
    }
  }

  #emitState() {
    try {
      this.onStateChange(this.getState());
    } catch {
      this.hostCallbackFailures += 1;
    }
  }
}

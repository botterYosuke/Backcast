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
  validateEventPayload,
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
    this.admission = 0;
    this.workerResources = new WeakMap();
    this.retiringWorkers = new Set();
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
    const admission = ++this.admission;
    const invalidating = this.worker || this.run || this.invalidationJob
      ? this.#invalidate('explicit_load', false) : null;
    if (invalidating) await invalidating;
    this.#assertAdmission(admission);
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
      this.workerResources.get(worker).interruptView = this.interruptView;
      if (this.interruptView) {
        worker.postMessage({
          kind: 'strategy_interrupt_setup',
          protocol_version: PROTOCOL_VERSION,
          buffer: this.interruptView.buffer,
        });
      }
    } catch (error) {
      await this.#terminateWorker(worker);
      throw error;
    }
    this.terminalValue = null;
    this.#setStatus('loading', 'load');
    try {
      const response = await this.#request(request, { worker, admission });
      this.#assertAdmission(admission);
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
      this.#assertWorkerOwner({ worker, admission });
      return freeze({ ok: true, loadedRevision: revision, sourceHash });
    } catch (error) {
      await this.#terminateWorker(worker);
      this.#assertAdmission(admission);
      this.terminalValue = freeze({ kind: 'load_failed', phase: 'load', code: error.code ?? 'load_failed' });
      this.#setStatus('errored', 'load');
      this.#assertAdmission(admission);
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
    const owner = { worker: this.worker, admission: this.admission };
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
    try {
      this.#setManualBlocked(true);
      this.#assertWorkerOwner(owner);
      this.#setStatus('starting', 'start');
      await this.#request(request, owner);
      this.#assertWorkerOwner(owner);
      this.#assertCurrentRun(request);
      this.#setStatus('paused', null);
      this.#assertWorkerOwner(owner);
      return freeze({ ok: true, runId, runningRevision: this.run.strategyRevision });
    } catch (error) {
      if (this.run?.runId === request.run_id) {
        await this.invalidate(error.code === 'timeout' ? 'timeout' : 'worker_or_protocol_error');
      }
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

  validateEventSnapshot({
    tick,
    fills,
    fillCursor,
    portfolio,
    openOrders,
    openOrdersTotal,
    openOrdersTruncated,
  }) {
    if (this.run && !Number.isSafeInteger(this.run.sequence + 1)) {
      throw new StrategyProtocolError('invalid_integer', 'next event sequence exceeds the safe integer range');
    }
    return validateEventPayload({
      tick, fills, fill_cursor: fillCursor, portfolio, open_orders: openOrders,
      open_orders_total: openOrdersTotal, open_orders_truncated: openOrdersTruncated,
    });
  }

  async dispatchOneEvent(input) {
    const { tick } = input;
    if (this.statusTag !== 'running') {
      throw new StrategyControllerError('invalid_state', 'strategy must be running to dispatch an event');
    }
    if (this.pending) throw new StrategyControllerError('single_flight', 'single-flight callback already pending');
    const owner = { worker: this.worker, admission: this.admission };
    const sequence = this.run.sequence + 1;
    const request = createRequestEnvelope({
      phase: 'event',
      requestId: this.makeId('request'),
      session: this.run.session,
      runId: this.run.runId,
      source: this.#runningSourceIdentity(),
      sequence,
      payload: this.validateEventSnapshot(input),
    });
    this.run.sequence = sequence;
    try {
      const response = await this.#request(request, owner);
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
      if (this.run?.runId === request.run_id) {
        await this.invalidate(error.code === 'timeout' ? 'timeout' : 'worker_or_protocol_error');
      }
      throw error;
    }
  }

  invalidate(cause) {
    return this.#invalidate(cause, true);
  }

  #invalidate(cause, cancelAdmission) {
    if (!Object.hasOwn(CAUSE_PRIORITY, cause)) {
      return Promise.reject(new StrategyControllerError('invalid_cause', 'unknown invalidation cause'));
    }
    if (cancelAdmission) this.admission += 1;
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
    let resolveJob;
    const promise = new Promise((resolve) => { resolveJob = resolve; });
    const job = { cause, capturedRun, failurePhase, promise, resolve: resolveJob, worker: this.worker };
    this.invalidationJob = job;
    this.#rejectPending(new StrategyControllerError('invalidated', `strategy request invalidated: ${cause}`));
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
    const worker = this.worker;
    const admission = this.admission;
    await this.#cleanupRun(capturedRun, 'replay_end', 'end');
    this.#assertAdmission(admission);
    this.#setStatus('stopping', 'end');
    try {
      this.#assertWorkerOwner({ worker, admission });
      const request = createRequestEnvelope({
        phase: 'end',
        requestId: this.makeId('request'),
        session: this.run.session,
        runId: this.run.runId,
        source: this.#runningSourceIdentity(),
        sequence: this.run.sequence + 1,
        payload: summary,
      });
      this.run.sequence = request.sequence;
      await this.#request(request, { worker, admission });
    } catch (error) {
      this.#assertAdmission(admission);
      if (!error.diagnostic) this.#appendSyntheticDiagnostic('end', error.code ?? 'end_failure', error.message);
    }
    this.#assertAdmission(admission);
    this.run = null;
    await this.#terminateWorker(worker);
    this.#assertAdmission(admission);
    this.#setManualBlocked(false);
    this.#assertAdmission(admission);
    this.terminalValue = freeze({ kind: 'completed', cause: 'replay_end' });
    this.#setStatus('completed', null);
    this.#assertAdmission(admission);
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

  #assertAdmission(admission) {
    if (admission !== this.admission) {
      throw new StrategyControllerError('invalidated', 'strategy Load invalidated by a newer operation');
    }
  }

  #assertWorkerOwner({ worker, admission }) {
    this.#assertAdmission(admission);
    if (!worker || worker !== this.worker || this.workerResources.get(worker)?.termination || this.invalidationJob) {
      throw new StrategyControllerError('invalidated', 'strategy request Worker is no longer current');
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
    const listeners = {
      message: (event) => this.#guardWorkerListener(
        () => this.#onWorkerMessage(worker, generation, event.data),
      ),
      messageerror: () => this.#guardWorkerListener(
        () => this.#onWorkerFailure(worker, generation, 'messageerror'),
      ),
      error: (event) => this.#guardWorkerListener(
        () => this.#onWorkerFailure(
          worker,
          generation,
          isErrorEvent(event) ? event.message : 'worker error',
        ),
      ),
    };
    this.workerResources.set(worker, { listeners, termination: null, interruptView: null });
    for (const [type, listener] of Object.entries(listeners)) worker.addEventListener(type, listener);
  }

  #guardWorkerListener(listener) {
    const owner = { worker: this.worker, admission: this.admission };
    try {
      listener();
    } catch (error) {
      this.#rejectPending(error instanceof Error ? error : new Error(String(error)));
      if (this.worker === owner.worker && this.admission === owner.admission) {
        void this.invalidate('worker_or_protocol_error');
      }
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
    const pending = this.pending;
    let response;
    try {
      response = validateWorkerResponse(message, this.#expectedIdentity(pending.request));
    } catch (error) {
      this.#settlePending(false, error, pending);
      return;
    }
    if (!response.ok) {
      this.#appendDiagnostic(response.diagnostic);
      this.#settlePending(false, new StrategyControllerError(
        'worker_failure', response.diagnostic.message, response.diagnostic,
      ), pending);
      return;
    }
    this.#settlePending(true, response, pending);
  }

  #onProgress(message) {
    if (!this.pending || this.pending.request.phase !== 'load') return;
    if (!this.#logMatchesRequest(message, this.pending.request) || message.stage !== 'runtime_ready') return;
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

  #request(request, owner) {
    try {
      this.#assertWorkerOwner(owner);
    } catch (error) {
      return Promise.reject(error);
    }
    if (this.pending) {
      return Promise.reject(new StrategyControllerError('single_flight', 'single-flight request already pending'));
    }
    if (!this.worker) return Promise.reject(new StrategyControllerError('worker_missing', 'Worker is unavailable'));
    return new Promise((resolve, reject) => {
      const pending = { request, owner, resolve, reject, timer: null, settled: false };
      this.pending = pending;
      this.#armDeadline(pending, this.deadlines[this.performanceClass][request.phase]);
      this.#emitState();
      try {
        this.#assertWorkerOwner(owner);
        if (pending.settled || this.pending !== pending) return;
        owner.worker.postMessage(request);
      } catch (error) {
        this.#settlePending(false, error instanceof StrategyControllerError
          ? error : new StrategyControllerError('post_message', String(error)), pending);
      }
    });
  }

  #armDeadline(pending, milliseconds) {
    if (pending.timer !== null) this.clearTimer(pending.timer);
    pending.timer = this.setTimer(() => {
      if (pending.settled || this.pending !== pending) return;
      this.#settlePending(false, new StrategyControllerError(
        'timeout', `${pending.request.phase} timeout after ${milliseconds}ms`,
      ), pending);
    }, milliseconds);
  }

  #settlePending(succeeded, value, pending = this.pending) {
    if (!pending || pending.settled) return;
    pending.settled = true;
    if (pending.timer !== null) this.clearTimer(pending.timer);
    if (this.pending === pending) {
      this.pending = null;
      this.#emitState();
    }
    if (succeeded) pending.resolve(value);
    else pending.reject(value);
  }

  #rejectPending(error) {
    if (this.pending) this.#settlePending(false, error);
  }

  async #finalizeInvalidation(job) {
    try {
      if (job.capturedRun) await this.#cleanupRun(job.capturedRun, job.cause, job.failurePhase);
      await this.#terminateWorker(job.worker);
      await Promise.all([...this.retiringWorkers]);
      this.#setManualBlocked(false);
      const isError = ERROR_CAUSES.has(job.cause);
      const outcome = freeze({
        kind: isError ? 'error' : 'stopped',
        cause: job.cause,
        ...(job.capturedRun ? {
          session: job.capturedRun.session,
          runId: job.capturedRun.runId,
          strategyRevision: job.capturedRun.strategyRevision,
        } : { hadRun: false }),
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

  #terminateWorker(worker = this.worker) {
    if (!worker) return Promise.resolve();
    const resource = this.workerResources.get(worker);
    if (resource.termination) return resource.termination;
    if (this.worker === worker) {
      this.worker = null;
      this.interruptView = null;
      this.workerGeneration += 1;
    }
    for (const [type, listener] of Object.entries(resource.listeners)) {
      worker.removeEventListener?.(type, listener);
    }
    resource.termination = (async () => {
      if (resource.interruptView) {
        Atomics.store(resource.interruptView, 0, 2);
        await new Promise((resolve) => {
          this.setTimer(resolve, Math.min(this.deadlines.interruptGrace, this.deadlines.hardTerminate));
        });
      }
      worker.terminate();
    })();
    this.retiringWorkers.add(resource.termination);
    resource.termination.then(
      () => this.retiringWorkers.delete(resource.termination),
      () => this.retiringWorkers.delete(resource.termination),
    );
    return resource.termination;
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

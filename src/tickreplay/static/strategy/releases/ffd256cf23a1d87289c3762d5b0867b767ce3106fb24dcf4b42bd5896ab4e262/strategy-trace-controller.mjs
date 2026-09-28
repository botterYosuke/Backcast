'use strict';

import {
  NATIVE_TRACE_CONTRACT,
  NativeTraceProtocolError,
  createNativeTraceSubmitRequest,
  validateNativeTraceArtifact,
  validateNativeTraceCapability,
  validateNativeTraceJob,
} from './strategy-trace-protocol.mjs';

const ACTIVE_TAGS = new Set(['loading', 'queued', 'running', 'validating', 'starting', 'paused', 'stopping']);
const TERMINAL_JOB_STATES = new Set(['ready', 'failed', 'cancelled', 'timed_out']);
const FAILURE_JOB_STATES = new Set(['failed', 'cancelled', 'timed_out']);
const JOB_STAGE = Object.freeze({ queued: 0, running: 1, validating: 2, ready: 3 });

export class NativeTraceControllerError extends Error {
  constructor(code, message = code, detail = null) {
    super(message);
    this.name = 'NativeTraceControllerError';
    this.code = code;
    this.detail = detail;
  }
}

function clone(value) {
  if (value === undefined) return undefined;
  return JSON.parse(JSON.stringify(value));
}

function freeze(value) {
  if (value && typeof value === 'object' && !Object.isFrozen(value)) {
    for (const child of Object.values(value)) freeze(child);
    Object.freeze(value);
  }
  return value;
}

function same(left, right) {
  return JSON.stringify(left) === JSON.stringify(right);
}

function sessionDate(session) {
  return session?.actual_date ?? session?.actualDate ?? session?.date ?? null;
}

function sessionKey(session, dataHash = session?.dataHash ?? session?.data_hash) {
  return [session?.stem, session?.code, sessionDate(session), session?.generation, dataHash].join('|');
}

function defaultId(prefix) {
  if (globalThis.crypto?.randomUUID) return `${prefix}-${globalThis.crypto.randomUUID()}`;
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

async function defaultHashText(text) {
  if (!globalThis.crypto?.subtle) throw new NativeTraceControllerError('hash_unavailable');
  const digest = await globalThis.crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, '0')).join('');
}

function defaultPoll(delayMs) {
  return new Promise((resolve) => globalThis.setTimeout(resolve, delayMs));
}

async function responseJson(response) {
  let payload;
  try { payload = await response.json(); } catch {
    throw new NativeTraceControllerError('invalid_response', 'server returned invalid JSON');
  }
  if (!response.ok) {
    throw new NativeTraceControllerError(
      payload?.error?.code ?? 'http_error',
      payload?.error?.message ?? `native strategy request failed (${response.status})`,
    );
  }
  return payload;
}

export function createNativeTraceHttpTransport({
  fetchImpl = globalThis.fetch?.bind(globalThis),
  baseUrl = '/api/native-strategy',
} = {}) {
  if (typeof fetchImpl !== 'function') throw new TypeError('fetch implementation is required');
  const request = (path, options = {}) => fetchImpl(`${baseUrl}${path}`, {
    credentials: 'same-origin',
    ...options,
    headers: { Accept: 'application/json', ...(options.headers ?? {}) },
  });
  return Object.freeze({
    async capability() {
      return validateNativeTraceCapability(await responseJson(await request('/capability')));
    },
    async submit(payload) {
      return responseJson(await request('/jobs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      }));
    },
    async status(jobId) {
      return responseJson(await request(`/jobs/${encodeURIComponent(jobId)}`));
    },
    async cancel(jobId) {
      return responseJson(await request(`/jobs/${encodeURIComponent(jobId)}`, { method: 'DELETE' }));
    },
    async fetchTrace(traceId) {
      const response = await request(`/traces/${encodeURIComponent(traceId)}`);
      if (!response.ok) await responseJson(response);
      const contentType = response.headers.get('content-type') ?? '';
      const cacheControl = response.headers.get('cache-control') ?? '';
      const etag = response.headers.get('etag');
      if (!contentType.toLowerCase().startsWith('application/json')) {
        throw new NativeTraceControllerError('invalid_trace_headers', 'trace is not JSON');
      }
      if (cacheControl !== 'private,max-age=31536000,immutable' || etag !== `"${traceId}"`) {
        throw new NativeTraceControllerError('invalid_trace_headers', 'trace immutability headers are invalid');
      }
      return response.text();
    },
  });
}

export class NativeTraceController {
  constructor({
    transport,
    poll = () => defaultPoll(100),
    hashText = defaultHashText,
    makeId = defaultId,
    onStateChange = () => {},
    onDiagnostic = () => {},
    onCleanup = () => ({ canceledOrderIds: [] }),
    onPauseReplay = () => {},
    onManualGateChange = () => {},
  } = {}) {
    for (const method of ['submit', 'status', 'cancel', 'fetchTrace']) {
      if (typeof transport?.[method] !== 'function') throw new TypeError(`transport.${method} is required`);
    }
    this.transport = transport;
    this.poll = poll;
    this.hashText = hashText;
    this.makeId = makeId;
    this.onStateChange = onStateChange;
    this.onDiagnostic = onDiagnostic;
    this.onCleanup = onCleanup;
    this.onPauseReplay = onPauseReplay;
    this.onManualGateChange = onManualGateChange;
    this.operationRevision = 0;
    this.loadedRevision = 0;
    this.attempt = null;
    this.loaded = null;
    this.run = null;
    this.statusTag = 'idle';
    this.statusPhase = null;
    this.manualBlocked = false;
    this.invalidation = null;
    this.terminal = null;
    this.diagnostics = [];
    this.hostCallbackFailures = 0;
  }

  getState() {
    return freeze({
      mode: NATIVE_TRACE_CONTRACT,
      tag: this.statusTag,
      phase: this.statusPhase,
      operationRevision: this.operationRevision,
      loadedRevision: this.loaded?.revision ?? 0,
      sourceHash: this.loaded?.sourceHash ?? null,
      traceId: this.loaded?.traceId ?? null,
      inputId: this.loaded?.inputId ?? null,
      session: clone(this.run?.session ?? this.loaded?.session ?? null),
      runId: this.run?.runId ?? null,
      runningRevision: this.run?.strategyRevision ?? null,
      sequence: this.run?.sequence ?? 0,
      inFlight: Boolean(this.attempt),
      terminal: clone(this.terminal),
      diagnosticCount: this.diagnostics.length,
      diagnosticsTruncated: 0,
      hostCallbackFailures: this.hostCallbackFailures,
    });
  }

  isActive() { return ACTIVE_TAGS.has(this.statusTag) || Boolean(this.attempt) || Boolean(this.run); }

  manualEntryBlocked() { return this.manualBlocked; }

  diagnosticHistory() { return freeze(clone(this.diagnostics)); }

  logs() { return freeze({ stdout: '', stderr: '', stdoutTruncated: 0, stderrTruncated: 0 }); }

  terminalOutcome() { return this.terminal === null ? null : freeze(clone(this.terminal)); }

  async load({
    source,
    sourceHash,
    draftRevision,
    loadAttemptId,
    session,
    sessionDataHash = session?.dataHash ?? session?.data_hash,
    tickCount,
  }) {
    const revision = ++this.operationRevision;
    this.#supersedeAttempt('explicit_load');
    if (this.run || this.loaded) this.#cleanup('explicit_load');
    const request = createNativeTraceSubmitRequest({
      source,
      sourceHash,
      session: {
        stem: session?.stem,
        code: session?.code,
        date: sessionDate(session),
        dataHash: sessionDataHash,
      },
    });
    if (!Number.isSafeInteger(tickCount) || tickCount <= 0) {
      throw new NativeTraceControllerError('invalid_session', 'tick count is required');
    }
    const attempt = this.#createAttempt(revision);
    this.attempt = attempt;
    this.loaded = null;
    this.terminal = null;
    this.#setManualBlocked(true);
    this.#setStatus('loading', 'submit');
    try {
      const rawSubmit = Promise.resolve(this.transport.submit(request));
      rawSubmit.then((value) => this.#cancelLateJob(attempt, value), () => {});
      let snapshot = validateNativeTraceJob(await this.#race(attempt, rawSubmit));
      this.#assertAttempt(attempt);
      attempt.jobId = snapshot.jobId;
      attempt.inputId = snapshot.inputId;
      attempt.snapshot = snapshot;
      if (snapshot.state !== 'ready') this.#setStatus(snapshot.state, snapshot.state);
      while (!TERMINAL_JOB_STATES.has(snapshot.state)) {
        await this.#race(attempt, Promise.resolve(this.poll()));
        const next = validateNativeTraceJob(await this.#race(
          attempt,
          Promise.resolve(this.transport.status(snapshot.jobId)),
        ));
        this.#assertAttempt(attempt);
        this.#validateJobProgress(snapshot, next);
        snapshot = next;
        attempt.snapshot = snapshot;
        if (snapshot.state !== 'ready') this.#setStatus(snapshot.state, snapshot.state);
      }
      if (FAILURE_JOB_STATES.has(snapshot.state)) {
        throw new NativeTraceControllerError(
          snapshot.error?.code ?? snapshot.state,
          snapshot.error?.message ?? `native strategy job ${snapshot.state}`,
        );
      }
      const text = await this.#race(attempt, Promise.resolve(this.transport.fetchTrace(snapshot.traceId)));
      if (typeof text !== 'string') throw new NativeTraceControllerError('invalid_trace', 'trace response must be text');
      const actualTraceId = await this.#race(attempt, Promise.resolve(this.hashText(text)));
      if (actualTraceId !== snapshot.traceId) {
        throw new NativeTraceControllerError('trace_hash_mismatch', 'trace content hash differs');
      }
      let payload;
      try { payload = JSON.parse(text); } catch {
        throw new NativeTraceControllerError('invalid_trace', 'trace response is invalid JSON');
      }
      const artifact = validateNativeTraceArtifact(payload);
      this.#validateArtifactIdentity({ artifact, request, snapshot, tickCount });
      this.#assertAttempt(attempt);
      const loadedRevision = ++this.loadedRevision;
      this.loaded = freeze({
        sourceHash,
        draftRevision,
        loadAttemptId,
        revision: loadedRevision,
        session: clone(session),
        sessionDataHash,
        tickCount,
        inputId: snapshot.inputId,
        traceId: snapshot.traceId,
        artifact,
        decisions: new Map(artifact.decisions.map((row) => [row.observed_session_tick_index, row])),
      });
      this.attempt = null;
      this.#setStatus('ready', null);
      return freeze({
        ok: true,
        loadedRevision,
        sourceHash,
        inputId: snapshot.inputId,
        traceId: snapshot.traceId,
      });
    } catch (error) {
      const current = this.attempt === attempt;
      if (current) {
        this.attempt = null;
        this.loaded = null;
        this.#setManualBlocked(false);
        this.terminal = freeze({ kind: 'load_failed', code: error.code ?? 'load_failed' });
        this.#setStatus(error.code === 'invalidated' ? 'idle' : 'errored', 'load');
      }
      throw error;
    }
  }

  async start({ session, cursor, tickCount, lastCommitted, openOrders, replayPaused = true }) {
    if (!this.loaded || this.statusTag !== 'ready') throw new NativeTraceControllerError('not_ready');
    if (!replayPaused) throw new NativeTraceControllerError('replay_playing');
    if (cursor !== 0 || lastCommitted !== null) throw new NativeTraceControllerError('cursor_policy');
    if (tickCount !== this.loaded.tickCount) throw new NativeTraceControllerError('session_mismatch');
    const dataHash = session?.dataHash ?? session?.data_hash ?? this.loaded.sessionDataHash;
    if (sessionKey(session, dataHash) !== sessionKey(this.loaded.session, this.loaded.sessionDataHash)) {
      throw new NativeTraceControllerError('session_mismatch');
    }
    if (!Array.isArray(openOrders)) throw new NativeTraceControllerError('invalid_open_orders');
    if (openOrders.some((order) => order?.owner === 'manual')) {
      throw new NativeTraceControllerError('manual_conflict', 'manual orders cannot be mixed with native trace');
    }
    if (openOrders.length > 0) throw new NativeTraceControllerError('strategy_conflict');
    const runId = this.makeId('native-run');
    this.run = { runId, session: clone(session), strategyRevision: this.loaded.revision, sequence: 0 };
    this.#setStatus('paused', null);
    return freeze({ ok: true, runId, runningRevision: this.run.strategyRevision });
  }

  resume() {
    if (this.statusTag !== 'paused' || !this.run) throw new NativeTraceControllerError('invalid_state');
    this.#setStatus('running', null);
    return this.getState();
  }

  pause() {
    if (!this.run || !['running', 'paused'].includes(this.statusTag)) {
      throw new NativeTraceControllerError('invalid_state');
    }
    this.#setStatus('paused', null);
    return this.getState();
  }

  validateEventSnapshot(input) {
    const index = input?.tick?.session_tick_index;
    if (!Number.isSafeInteger(index) || index < 0) throw new NativeTraceControllerError('invalid_tick');
    return freeze(clone(input));
  }

  async dispatchOneEvent(input) {
    if (!this.run || this.statusTag !== 'running') throw new NativeTraceControllerError('invalid_state');
    const observed = input?.tick?.session_tick_index;
    if (observed !== this.run.sequence) throw new NativeTraceControllerError('invalid_sequence');
    const row = this.loaded.decisions.get(observed);
    const sequence = ++this.run.sequence;
    return freeze({
      kind: 'commands',
      protocolVersion: 1,
      session: clone(this.run.session),
      runId: this.run.runId,
      strategyRevision: this.run.strategyRevision,
      sequence,
      observedSessionTickIndex: observed,
      eligibleFromSessionTick: observed + 1,
      commands: clone(row?.commands ?? []),
    });
  }

  async end() {
    if (!this.run) throw new NativeTraceControllerError('invalid_state');
    const captured = this.run;
    this.run = null;
    this.#cleanup('replay_end', captured);
    this.#setManualBlocked(false);
    this.terminal = freeze({ kind: 'completed', cause: 'replay_end' });
    this.#setStatus('completed', null);
    return this.terminalOutcome();
  }

  invalidate(cause = 'explicit_stop') {
    if (this.invalidation) return this.invalidation;
    this.operationRevision += 1;
    const attempt = this.attempt;
    const run = this.run;
    this.attempt = null;
    this.run = null;
    this.loaded = null;
    if (attempt) {
      attempt.cancelled = true;
      attempt.rejectCancel(new NativeTraceControllerError('invalidated', `native trace invalidated: ${cause}`));
    }
    this.#setStatus('stopping', cause);
    const work = (async () => {
      if (attempt?.jobId) await this.#cancelJob(attempt, attempt.jobId);
      if (run) this.#cleanup(cause, run);
      this.#setManualBlocked(false);
      this.terminal = null;
      this.#setStatus('idle', null);
    })();
    this.invalidation = work.finally(() => { this.invalidation = null; });
    return this.invalidation;
  }

  stop(cause = 'explicit_stop') { return this.invalidate(cause); }

  #createAttempt(revision) {
    let rejectCancel;
    const cancellation = new Promise((_, reject) => { rejectCancel = reject; });
    cancellation.catch(() => {});
    return {
      revision, cancellation, rejectCancel, cancelled: false,
      jobId: null, inputId: null, snapshot: null, canceledJobs: new Set(),
    };
  }

  #race(attempt, operation) { return Promise.race([operation, attempt.cancellation]); }

  #assertAttempt(attempt) {
    if (this.attempt !== attempt || attempt.cancelled || attempt.revision !== this.operationRevision) {
      throw new NativeTraceControllerError('invalidated', 'native trace operation was superseded');
    }
  }

  #supersedeAttempt(cause) {
    const previous = this.attempt;
    if (!previous) return;
    this.attempt = null;
    previous.cancelled = true;
    previous.rejectCancel(new NativeTraceControllerError('invalidated', `native trace invalidated: ${cause}`));
    if (previous.jobId) void this.#cancelJob(previous, previous.jobId);
  }

  #cancelLateJob(attempt, value) {
    if (!attempt.cancelled) return;
    try {
      const snapshot = validateNativeTraceJob(value);
      void this.#cancelJob(attempt, snapshot.jobId);
    } catch { /* Invalid late replies have no trusted job identifier to cancel. */ }
  }

  async #cancelJob(attempt, jobId) {
    if (attempt.canceledJobs.has(jobId)) return;
    attempt.canceledJobs.add(jobId);
    try { validateNativeTraceJob(await this.transport.cancel(jobId)); } catch { /* cancellation is best effort */ }
  }

  #validateJobProgress(previous, next) {
    if (next.jobId !== previous.jobId || next.inputId !== previous.inputId) {
      throw new NativeTraceControllerError('stale_status', 'job identity changed');
    }
    if (next.revision < previous.revision) {
      throw new NativeTraceControllerError('stale_status', 'job revision moved backwards');
    }
    if (next.revision === previous.revision && !same(next, previous)) {
      throw new NativeTraceControllerError('stale_status', 'job changed without a revision');
    }
    const before = JOB_STAGE[previous.state];
    const after = JOB_STAGE[next.state];
    if (before !== undefined && after !== undefined && after < before) {
      throw new NativeTraceControllerError('stale_status', 'job state moved backwards');
    }
  }

  #validateArtifactIdentity({ artifact, request, snapshot, tickCount }) {
    const matches = artifact.input_id === snapshot.inputId
      && artifact.source_hash === request.sourceHash
      && artifact.session.stem === request.session.stem
      && artifact.session.code === request.session.code
      && artifact.session.date === request.session.date
      && artifact.session.data_hash === request.session.dataHash
      && artifact.session.tick_count === tickCount;
    if (!matches) throw new NativeTraceControllerError('trace_identity_mismatch');
  }

  #cleanup(cause, run = this.run) {
    if (!run) return;
    try {
      this.onCleanup({
        runId: run.runId,
        strategyRevision: run.strategyRevision,
        session: clone(run.session),
        cause,
      });
    } catch { this.hostCallbackFailures += 1; }
  }

  #setManualBlocked(blocked) {
    if (blocked === this.manualBlocked) return;
    this.manualBlocked = blocked;
    try { this.onManualGateChange(blocked); } catch { this.hostCallbackFailures += 1; }
  }

  #setStatus(tag, phase) {
    this.statusTag = tag;
    this.statusPhase = phase;
    try { this.onStateChange(this.getState()); } catch { this.hostCallbackFailures += 1; }
  }
}

export { NativeTraceProtocolError };

'use strict';

import {
  BUNDLED_RL_TRACE_CONTRACT,
  createBundledRlSubmitRequest,
  validateBundledRlCapability,
  validateBundledRlJob,
  validateBundledRlJobSubmission,
  validateBundledRlTraceArtifact,
} from './strategy-trace-protocol.mjs';

const ACTIVE_TAGS = new Set(['loading', 'queued', 'running', 'validating', 'starting', 'paused', 'stopping']);
const TERMINAL_JOB_STATES = new Set(['ready', 'failed', 'cancelled', 'timed_out']);
const FAILURE_JOB_STATES = new Set(['failed', 'cancelled', 'timed_out']);
const JOB_STAGE = Object.freeze({ queued: 0, running: 1, validating: 2, ready: 3 });
const SAFE_POSITIONS = new Set([0, 100]);

export class BundledRlTraceControllerError extends Error {
  constructor(code, message = code, detail = null) { super(message); this.name = 'BundledRlTraceControllerError'; this.code = code; this.detail = detail; }
}
function clone(value) { return value === undefined ? undefined : JSON.parse(JSON.stringify(value)); }
function freeze(value) { if (value && typeof value === 'object' && !Object.isFrozen(value)) { for (const child of Object.values(value)) freeze(child); Object.freeze(value); } return value; }
function sessionDate(session) { return session?.actual_date ?? session?.actualDate ?? session?.date ?? null; }
function sessionKey(session, dataHash = session?.dataHash ?? session?.data_hash) { return [session?.stem, session?.code, sessionDate(session), session?.generation, dataHash].join('|'); }
function defaultId(prefix) { if (globalThis.crypto?.randomUUID) return `${prefix}-${globalThis.crypto.randomUUID()}`; return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2)}`; }
async function defaultHashText(text) {
  if (!globalThis.crypto?.subtle) throw new BundledRlTraceControllerError('hash_unavailable');
  const digest = await globalThis.crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, '0')).join('');
}
function defaultPoll(delayMs) { return new Promise((resolve) => globalThis.setTimeout(resolve, delayMs)); }
async function responseJson(response) {
  let payload;
  try { payload = await response.json(); } catch { throw new BundledRlTraceControllerError('invalid_response', 'server returned invalid JSON'); }
  if (!response.ok) throw new BundledRlTraceControllerError(payload?.error?.code ?? 'http_error', payload?.error?.message ?? `bundled RL request failed (${response.status})`);
  return payload;
}
function hasImmutableTraceCachePolicy(value) {
  const directives = new Map();
  for (const rawDirective of value.split(',')) {
    const raw = rawDirective.trim(); if (!raw) return false;
    const separator = raw.indexOf('=');
    const name = (separator === -1 ? raw : raw.slice(0, separator)).trim().toLowerCase();
    const directiveValue = separator === -1 ? null : raw.slice(separator + 1).trim();
    if (!name || directives.has(name)) return false;
    directives.set(name, directiveValue);
  }
  return directives.get('private') === null && directives.get('immutable') === null && directives.get('max-age') === '31536000';
}
function sameJobSnapshot(left, right) {
  const sameError = left.error === null && right.error === null
    || left.error?.code === right.error?.code && left.error?.message === right.error?.message;
  return left.jobId === right.jobId
    && left.inputId === right.inputId
    && left.state === right.state
    && left.revision === right.revision
    && left.traceId === right.traceId
    && sameError;
}
function samePreset(left, right) {
  return left.id === right.id
    && left.revision === right.revision
    && left.digest === right.digest;
}

export function createBundledRlTraceHttpTransport({ fetchImpl = globalThis.fetch?.bind(globalThis), baseUrl = '/api/strategies/bundled-rl' } = {}) {
  if (typeof fetchImpl !== 'function') throw new TypeError('fetch implementation is required');
  const request = (path, options = {}) => fetchImpl(`${baseUrl}${path}`, { credentials: 'same-origin', ...options, headers: { Accept: 'application/json', ...(options.headers ?? {}) } });
  return Object.freeze({
    async capability() { return validateBundledRlCapability(await responseJson(await request('/capability'))); },
    async submit(payload) { return responseJson(await request('/jobs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })); },
    async status(jobId) { return responseJson(await request(`/jobs/${encodeURIComponent(jobId)}`)); },
    async cancel(jobId) { return responseJson(await request(`/jobs/${encodeURIComponent(jobId)}`, { method: 'DELETE' })); },
    async fetchTrace(traceId) {
      const response = await request(`/traces/${encodeURIComponent(traceId)}`);
      if (!response.ok) await responseJson(response);
      const contentType = response.headers.get('content-type') ?? '';
      const cacheControl = response.headers.get('cache-control') ?? '';
      const etag = response.headers.get('etag');
      if (!contentType.toLowerCase().startsWith('application/json')) throw new BundledRlTraceControllerError('invalid_trace_headers', 'trace is not JSON');
      if (!hasImmutableTraceCachePolicy(cacheControl) || etag !== `"${traceId}"`) throw new BundledRlTraceControllerError('invalid_trace_headers', 'trace immutability headers are invalid');
      return response.text();
    },
  });
}

function validateSafeBrokerSnapshot({ portfolio, openOrders, openOrdersTotal, openOrdersTruncated }) {
  const position = portfolio?.position_quantity;
  if (!Number.isSafeInteger(position) || !SAFE_POSITIONS.has(position)) throw new BundledRlTraceControllerError('unsafe_position', 'Bundled RL requires position 0 or 100');
  if (!Array.isArray(openOrders)) throw new BundledRlTraceControllerError('invalid_open_orders');
  if (!Number.isSafeInteger(openOrdersTotal) || openOrdersTotal < openOrders.length) throw new BundledRlTraceControllerError('invalid_open_orders');
  if (typeof openOrdersTruncated !== 'boolean' || openOrdersTruncated !== (openOrdersTotal > openOrders.length)) throw new BundledRlTraceControllerError('invalid_open_orders');
  if (openOrdersTruncated || openOrdersTotal !== 0 || openOrders.length !== 0) throw new BundledRlTraceControllerError('open_order_conflict', 'Bundled RL requires a complete empty open-order snapshot');
  return position;
}

export class BundledRlTraceController {
  constructor({ transport, poll = () => defaultPoll(100), hashText = defaultHashText, makeId = defaultId, onStateChange = () => {}, onDiagnostic = () => {}, onCleanup = () => ({ canceledOrderIds: [] }), onPauseReplay = () => {}, onManualGateChange = () => {} } = {}) {
    for (const method of ['submit', 'status', 'cancel', 'fetchTrace']) if (typeof transport?.[method] !== 'function') throw new TypeError(`transport.${method} is required`);
    Object.assign(this, { transport, poll, hashText, makeId, onStateChange, onDiagnostic, onCleanup, onPauseReplay, onManualGateChange });
    this.operationRevision = 0; this.loadedRevision = 0; this.attempt = null; this.loaded = null; this.run = null;
    this.statusTag = 'idle'; this.statusPhase = null; this.manualBlocked = false; this.invalidation = null; this.terminal = null;
    this.diagnostics = []; this.hostCallbackFailures = 0; this.cancelledJobs = new Set(); this.cancellations = new Map();
  }
  getState() { return freeze({ mode: BUNDLED_RL_TRACE_CONTRACT, tag: this.statusTag, phase: this.statusPhase, operationRevision: this.operationRevision, loadedRevision: this.loaded?.revision ?? 0, sourceHash: null, traceId: this.loaded?.traceId ?? null, inputId: this.loaded?.inputId ?? null, session: clone(this.run?.session ?? this.loaded?.session ?? null), runId: this.run?.runId ?? null, runningRevision: this.run?.strategyRevision ?? null, sequence: this.run?.sequence ?? 0, inFlight: Boolean(this.attempt), terminal: clone(this.terminal), diagnosticCount: this.diagnostics.length, diagnosticsTruncated: 0, hostCallbackFailures: this.hostCallbackFailures }); }
  isActive() { return ACTIVE_TAGS.has(this.statusTag) || Boolean(this.attempt) || Boolean(this.run); }
  manualEntryBlocked() { return this.manualBlocked; }
  diagnosticHistory() { return freeze(clone(this.diagnostics)); }
  logs() { return freeze({ stdout: '', stderr: '', stdoutTruncated: 0, stderrTruncated: 0 }); }
  terminalOutcome() { return this.terminal === null ? null : freeze(clone(this.terminal)); }

  async load({ session, sessionDataHash = session?.dataHash ?? session?.data_hash, tickCount }) {
    const revision = ++this.operationRevision;
    this.#supersedeAttempt('explicit_load');
    const previousLoaded = this.loaded;
    if (this.run || previousLoaded) this.#cleanup('explicit_load');
    if (previousLoaded?.jobId) void this.#cancelJob(previousLoaded.jobId).catch((error) => this.#reportCancellationFailure(error));
    const request = createBundledRlSubmitRequest({ session: { stem: session?.stem, code: session?.code, date: sessionDate(session), dataHash: sessionDataHash } });
    if (!Number.isSafeInteger(tickCount) || tickCount <= 0) throw new BundledRlTraceControllerError('invalid_session', 'tick count is required');
    const attempt = this.#createAttempt(revision); this.attempt = attempt; this.loaded = null; this.terminal = null; this.#setManualBlocked(true); this.#setStatus('loading', 'submit');
    try {
      const rawSubmit = Promise.resolve(this.transport.submit(request));
      rawSubmit.then((value) => this.#cancelLateJob(attempt, value), () => {});
      let snapshot = validateBundledRlJobSubmission(await this.#race(attempt, rawSubmit));
      this.#assertAttempt(attempt); attempt.jobId = snapshot.jobId; attempt.snapshot = snapshot;
      if (snapshot.state !== 'ready') this.#setStatus(snapshot.state, snapshot.state);
      while (!TERMINAL_JOB_STATES.has(snapshot.state)) {
        await this.#race(attempt, Promise.resolve(this.poll()));
        const next = validateBundledRlJob(await this.#race(attempt, Promise.resolve(this.transport.status(snapshot.jobId))));
        this.#assertAttempt(attempt); this.#validateJobProgress(snapshot, next); snapshot = next; attempt.snapshot = snapshot;
        if (snapshot.state !== 'ready') this.#setStatus(snapshot.state, snapshot.state);
      }
      if (FAILURE_JOB_STATES.has(snapshot.state)) throw new BundledRlTraceControllerError(snapshot.error?.code ?? snapshot.state, snapshot.error?.message ?? `bundled RL job ${snapshot.state}`);
      const text = await this.#race(attempt, Promise.resolve(this.transport.fetchTrace(snapshot.traceId)));
      if (typeof text !== 'string') throw new BundledRlTraceControllerError('invalid_trace', 'trace response must be text');
      if (await this.#race(attempt, Promise.resolve(this.hashText(text))) !== snapshot.traceId) throw new BundledRlTraceControllerError('trace_hash_mismatch', 'trace content hash differs');
      let payload; try { payload = JSON.parse(text); } catch { throw new BundledRlTraceControllerError('invalid_trace', 'trace response is invalid JSON'); }
      const artifact = validateBundledRlTraceArtifact(payload);
      this.#validateArtifactIdentity({ artifact, request, snapshot, tickCount }); this.#assertAttempt(attempt);
      const loadedRevision = ++this.loadedRevision;
      this.loaded = freeze({ revision: loadedRevision, session: clone(session), sessionDataHash, tickCount, inputId: snapshot.inputId, traceId: snapshot.traceId, jobId: snapshot.jobId, artifact, decisions: new Map(artifact.decisions.map((row) => [row.observedSessionTickIndex, row])) });
      this.attempt = null; this.#setStatus('ready', null);
      return freeze({ ok: true, loadedRevision, inputId: snapshot.inputId, traceId: snapshot.traceId });
    } catch (error) {
      if (this.attempt === attempt) { this.attempt = null; this.loaded = null; this.#setManualBlocked(false); this.terminal = freeze({ kind: 'load_failed', code: error.code ?? 'load_failed' }); this.#setStatus(error.code === 'invalidated' ? 'idle' : 'errored', 'load'); }
      if (!attempt.cancelled && error.code !== 'invalidated' && attempt.jobId) {
        void this.#cancelJob(attempt.jobId).catch((cancelError) => this.#reportCancellationFailure(cancelError));
      }
      throw error;
    }
  }

  async start({ session, cursor, tickCount, lastCommitted, portfolio, openOrders, openOrdersTotal, openOrdersTruncated, replayPaused = true }) {
    if (!this.loaded || this.statusTag !== 'ready') throw new BundledRlTraceControllerError('not_ready');
    if (!replayPaused) throw new BundledRlTraceControllerError('replay_playing');
    if (cursor !== 0 || lastCommitted !== null) throw new BundledRlTraceControllerError('cursor_policy');
    if (tickCount !== this.loaded.tickCount) throw new BundledRlTraceControllerError('session_mismatch');
    const dataHash = session?.dataHash ?? session?.data_hash ?? this.loaded.sessionDataHash;
    if (sessionKey(session, dataHash) !== sessionKey(this.loaded.session, this.loaded.sessionDataHash)) throw new BundledRlTraceControllerError('session_mismatch');
    validateSafeBrokerSnapshot({ portfolio, openOrders, openOrdersTotal, openOrdersTruncated });
    const runId = this.makeId('bundled-rl-run');
    this.run = { runId, session: clone(session), strategyRevision: this.loaded.revision, sequence: 0 };
    this.#setStatus('paused', null);
    return freeze({ ok: true, runId, runningRevision: this.run.strategyRevision });
  }
  resume() { if (this.statusTag !== 'paused' || !this.run) throw new BundledRlTraceControllerError('invalid_state'); this.#setStatus('running', null); return this.getState(); }
  pause() { if (!this.run || !['running', 'paused'].includes(this.statusTag)) throw new BundledRlTraceControllerError('invalid_state'); this.#setStatus('paused', null); return this.getState(); }
  validateEventSnapshot(input) {
    const index = input?.tick?.session_tick_index;
    if (!Number.isSafeInteger(index) || index < 0) throw new BundledRlTraceControllerError('invalid_tick');
    validateSafeBrokerSnapshot({ portfolio: input.portfolio, openOrders: input.openOrders, openOrdersTotal: input.openOrdersTotal, openOrdersTruncated: input.openOrdersTruncated });
    return freeze(clone(input));
  }
  async dispatchOneEvent(input) {
    if (!this.run || this.statusTag !== 'running') throw new BundledRlTraceControllerError('invalid_state');
    const observed = input?.tick?.session_tick_index;
    if (observed !== this.run.sequence) throw new BundledRlTraceControllerError('invalid_sequence');
    const position = validateSafeBrokerSnapshot({ portfolio: input.portfolio, openOrders: input.openOrders, openOrdersTotal: input.openOrdersTotal, openOrdersTruncated: input.openOrdersTruncated });
    const row = this.loaded.decisions.get(observed);
    let commands = [];
    if (row) {
      const branch = row.positionBranches.find((item) => item.positionQuantity === position);
      if (!branch) throw new BundledRlTraceControllerError('missing_position_branch');
      const delta = branch.targetQuantity - position;
      if (delta !== 0) commands = [{ type: 'submit_market', client_intent_id: `${this.run.runId}:${row.observedSessionTickIndex}:${position}-${branch.targetQuantity}`, side: delta > 0 ? 'buy' : 'sell', quantity: Math.abs(delta) }];
    }
    const sequence = ++this.run.sequence;
    return freeze({ kind: 'commands', protocolVersion: 1, session: clone(this.run.session), runId: this.run.runId, strategyRevision: this.run.strategyRevision, sequence, observedSessionTickIndex: observed, eligibleFromSessionTick: observed + 1, commands });
  }
  async end() { if (!this.run) throw new BundledRlTraceControllerError('invalid_state'); const captured = this.run; this.run = null; this.#cleanup('replay_end', captured); this.#setManualBlocked(false); this.terminal = freeze({ kind: 'completed', cause: 'replay_end' }); this.#setStatus('completed', null); return this.terminalOutcome(); }
  invalidate(cause = 'explicit_stop') {
    if (this.invalidation) return this.invalidation;
    this.operationRevision += 1; const attempt = this.attempt; const run = this.run; const loaded = this.loaded;
    this.attempt = null; this.run = null; this.loaded = null;
    if (attempt) { attempt.cancelled = true; attempt.rejectCancel(new BundledRlTraceControllerError('invalidated', `bundled RL trace invalidated: ${cause}`)); }
    if (run) this.#cleanup(cause, run); this.#setManualBlocked(false); this.#setStatus('stopping', cause);
    const work = (async () => { try { if (attempt?.jobId) await this.#cancelJob(attempt.jobId); else if (loaded?.jobId) await this.#cancelJob(loaded.jobId); } finally { this.terminal = null; this.#setStatus('idle', null); } })();
    this.invalidation = work.finally(() => { this.invalidation = null; }); return this.invalidation;
  }
  stop(cause = 'explicit_stop') { return this.invalidate(cause); }

  #createAttempt(revision) { let rejectCancel; const cancellation = new Promise((_, reject) => { rejectCancel = reject; }); cancellation.catch(() => {}); return { revision, cancellation, rejectCancel, cancelled: false, jobId: null, snapshot: null }; }
  #race(attempt, operation) { return Promise.race([operation, attempt.cancellation]); }
  #assertAttempt(attempt) { if (this.attempt !== attempt || attempt.cancelled || attempt.revision !== this.operationRevision) throw new BundledRlTraceControllerError('invalidated', 'bundled RL operation was superseded'); }
  #supersedeAttempt(cause) { const previous = this.attempt; if (!previous) return; this.attempt = null; previous.cancelled = true; previous.rejectCancel(new BundledRlTraceControllerError('invalidated', `bundled RL trace invalidated: ${cause}`)); if (previous.jobId) void this.#cancelJob(previous.jobId).catch((error) => this.#reportCancellationFailure(error)); }
  #cancelLateJob(attempt, value) { if (!attempt.cancelled) return; try { const job = validateBundledRlJobSubmission(value); attempt.jobId = job.jobId; void this.#cancelJob(job.jobId).catch((error) => this.#reportCancellationFailure(error)); } catch { /* Invalid late replies have no trusted job ID. */ } }
  async #cancelJob(jobId) {
    if (this.cancelledJobs.has(jobId)) return null;
    if (this.cancellations.has(jobId)) return this.cancellations.get(jobId);
    const work = Promise.resolve(this.transport.cancel(jobId)).then((result) => { this.cancelledJobs.add(jobId); this.cancellations.delete(jobId); return result; });
    this.cancellations.set(jobId, work);
    try { return await work; } finally { if (this.cancellations.get(jobId) === work) this.cancellations.delete(jobId); }
  }
  #reportCancellationFailure(error) { const diagnostic = freeze({ phase: 'cancel', category: error?.code ?? 'cancel_failed', message: error?.message ?? 'Bundled RL job cancellation failed' }); this.diagnostics.push(diagnostic); this.#hostCall('onDiagnostic', diagnostic); }
  #validateJobProgress(previous, next) {
    if (next.jobId !== previous.jobId || next.inputId !== previous.inputId) throw new BundledRlTraceControllerError('stale_status');
    if (next.revision < previous.revision) throw new BundledRlTraceControllerError('stale_status');
    if (next.revision === previous.revision) {
      if (!sameJobSnapshot(previous, next)) throw new BundledRlTraceControllerError('stale_status');
      return;
    }
    const priorStage = JOB_STAGE[previous.state];
    const nextStage = JOB_STAGE[next.state];
    if (priorStage !== undefined && nextStage !== undefined && nextStage < priorStage) throw new BundledRlTraceControllerError('stale_status');
  }
  #validateArtifactIdentity({ artifact, request, snapshot, tickCount }) {
    if (artifact.inputId !== snapshot.inputId) throw new BundledRlTraceControllerError('trace_identity_mismatch');
    if (!samePreset(artifact.preset, request.preset)) throw new BundledRlTraceControllerError('trace_identity_mismatch');
    const expected = request.session;
    if (artifact.session.stem !== expected.stem || artifact.session.code !== expected.code || artifact.session.date !== expected.date || artifact.session.dataHash !== expected.dataHash || artifact.session.tickCount !== tickCount) throw new BundledRlTraceControllerError('trace_session_mismatch');
  }
  #cleanup(cause, run = this.run) { if (!run) return; try { this.#hostCall('onPauseReplay', { cause, runId: run.runId }); this.#hostCall('onCleanup', { cause, session: clone(run.session), runId: run.runId, strategyRevision: run.strategyRevision }); } catch { /* host failure is recorded by #hostCall */ } }
  #setManualBlocked(blocked) { if (this.manualBlocked === blocked) return; this.manualBlocked = blocked; this.#hostCall('onManualGateChange', blocked); }
  #setStatus(tag, phase) { this.statusTag = tag; this.statusPhase = phase; this.#hostCall('onStateChange', this.getState()); }
  #hostCall(name, value) { try { return this[name](value); } catch (error) { this.hostCallbackFailures += 1; if (name !== 'onDiagnostic') { const diagnostic = freeze({ phase: 'host_callback', category: 'host_callback_failed', message: `${name}: ${error?.message ?? error}` }); this.diagnostics.push(diagnostic); try { this.onDiagnostic(diagnostic); } catch { this.hostCallbackFailures += 1; } } return null; } }
}

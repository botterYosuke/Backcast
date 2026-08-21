const READY_STATES = new Set(['fresh', 'stale-served']);
const FAILED_STATES = new Set(['corrupt', 'missing']);

function abortError() {
  const error = new Error('Request was superseded');
  error.name = 'AbortError';
  return error;
}

function defaultWait(milliseconds, signal) {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(abortError());
      return;
    }

    const onElapsed = () => {
      signal.removeEventListener('abort', onAbort);
      resolve();
    };
    const onAbort = () => {
      clearTimeout(timer);
      reject(abortError());
    };
    const timer = setTimeout(onElapsed, milliseconds);
    signal.addEventListener('abort', onAbort, { once: true });
  });
}

export class SupersededRequestError extends Error {
  constructor() {
    super('Request was superseded by a newer request');
    this.name = 'SupersededRequestError';
  }
}

export class OperationFailedError extends Error {
  constructor(status) {
    super(status.error || `Cache operation ended in state: ${status.state}`);
    this.name = 'OperationFailedError';
    this.status = status;
  }
}

export function isCancellationError(error) {
  return error instanceof SupersededRequestError || error?.name === 'AbortError';
}

export class OperationStatusGuard {
  constructor() {
    this.serverEpoch = null;
    this.operations = new Map();
  }

  begin(stem, handshake) {
    if (!handshake?.serverEpoch || !Number.isInteger(handshake.operationId)) {
      throw new Error('Pending response is missing serverEpoch or operationId');
    }
    if (this.serverEpoch !== handshake.serverEpoch) {
      this.serverEpoch = handshake.serverEpoch;
      this.operations.clear();
    }
    this.operations.set(stem, {
      serverEpoch: handshake.serverEpoch,
      operationId: handshake.operationId,
      revision: -1,
    });
  }

  accept(stem, status) {
    const expected = this.operations.get(stem);
    if (!expected) return { accepted: false, reason: 'no-handshake' };
    if (status?.serverEpoch !== expected.serverEpoch) {
      return { accepted: false, restart: true, reason: 'epoch-mismatch' };
    }
    if (status.operationId !== expected.operationId) {
      return { accepted: false, reason: 'operation-mismatch' };
    }
    if (!Number.isInteger(status.revision) || status.revision <= expected.revision) {
      return { accepted: false, reason: 'revision-not-increasing' };
    }

    expected.revision = status.revision;
    return {
      accepted: true,
      terminal: READY_STATES.has(status.state) || FAILED_STATES.has(status.state),
      ready: READY_STATES.has(status.state),
    };
  }
}

export class RequestCoordinator {
  constructor({
    fetchJson,
    wait = defaultWait,
    pollDelayMs = 250,
    maxPollDelayMs = 2000,
    maxPollFailures = 5,
    startRetries = 1,
    onActivityChange = () => {},
  }) {
    if (typeof fetchJson !== 'function') throw new TypeError('fetchJson is required');
    this.fetchJson = fetchJson;
    this.wait = wait;
    this.pollDelayMs = pollDelayMs;
    this.maxPollDelayMs = maxPollDelayMs;
    this.maxPollFailures = maxPollFailures;
    this.startRetries = startRetries;
    this.onActivityChange = onActivityChange;
    this.active = new Map();
    this.selectedStem = null;
    this.nextRequestId = 1;
    this.statusGuard = new OperationStatusGuard();
  }

  selectStem(stem) {
    if (this.selectedStem === stem) return;
    this.selectedStem = stem;
    for (const entry of this.active.values()) {
      if (entry.stem && entry.stem !== stem) this.#abort(entry);
    }
  }

  cancel(kind) {
    const entry = this.active.get(kind);
    if (entry) this.#abort(entry);
  }

  async fetchLatest(kind, url, { stem = null } = {}) {
    if (stem) this.selectStem(stem);
    const entry = this.#begin(kind, stem);
    try {
      return await this.#request(url, entry);
    } finally {
      this.#finish(entry);
    }
  }

  async fetchUntilReady(kind, url, { stem, onStatus = () => {} }) {
    this.selectStem(stem);
    const entry = this.#begin(kind, stem);
    let terminalStatus = null;
    try {
      while (true) {
        const response = await this.#requestWithRetries(url, entry);
        if (!response?.pending) return { data: response, status: terminalStatus };

        this.statusGuard.begin(stem, response);
        const outcome = await this.#pollOperation(stem, entry, onStatus);
        if (outcome.restart) continue;
        terminalStatus = outcome.status;
        if (!outcome.ready) throw new OperationFailedError(outcome.status);
      }
    } finally {
      this.#finish(entry);
    }
  }

  #begin(kind, stem) {
    const previous = this.active.get(kind);
    if (previous) this.#abort(previous);
    const entry = {
      kind,
      stem,
      requestId: this.nextRequestId,
      controller: new AbortController(),
    };
    this.nextRequestId += 1;
    this.active.set(kind, entry);
    this.onActivityChange(kind, true);
    return entry;
  }

  #abort(entry) {
    if (this.active.get(entry.kind) !== entry) return;
    entry.controller.abort();
    this.active.delete(entry.kind);
    this.onActivityChange(entry.kind, false);
  }

  #finish(entry) {
    if (this.active.get(entry.kind) !== entry) return;
    this.active.delete(entry.kind);
    this.onActivityChange(entry.kind, false);
  }

  #assertCurrent(entry) {
    if (this.active.get(entry.kind) !== entry) throw new SupersededRequestError();
    if (entry.stem && this.selectedStem !== entry.stem) {
      throw new SupersededRequestError();
    }
    if (entry.controller.signal.aborted) throw new SupersededRequestError();
  }

  async #request(url, entry) {
    this.#assertCurrent(entry);
    try {
      const response = await this.fetchJson(url, { signal: entry.controller.signal });
      this.#assertCurrent(entry);
      return response;
    } catch (error) {
      if (isCancellationError(error) || this.active.get(entry.kind) !== entry) {
        throw new SupersededRequestError();
      }
      throw error;
    }
  }

  async #requestWithRetries(url, entry) {
    for (let attempt = 0; ; attempt += 1) {
      try {
        return await this.#request(url, entry);
      } catch (error) {
        if (isCancellationError(error) || attempt >= this.startRetries) throw error;
        await this.wait(this.#backoff(attempt), entry.controller.signal);
      }
    }
  }

  async #pollOperation(stem, entry, onStatus) {
    let failures = 0;
    while (true) {
      let status;
      try {
        status = await this.#request(
          `/api/status?stem=${encodeURIComponent(stem)}`,
          entry,
        );
        failures = 0;
      } catch (error) {
        if (isCancellationError(error)) throw error;
        failures += 1;
        if (failures > this.maxPollFailures) throw error;
        await this.wait(this.#backoff(failures - 1), entry.controller.signal);
        continue;
      }

      const decision = this.statusGuard.accept(stem, status);
      this.#assertCurrent(entry);
      if (decision.restart) return { restart: true };
      if (decision.accepted) onStatus(status);
      if (decision.accepted && decision.terminal) {
        return { restart: false, ready: decision.ready, status };
      }
      await this.wait(this.pollDelayMs, entry.controller.signal);
    }
  }

  #backoff(attempt) {
    return Math.min(this.pollDelayMs * (2 ** attempt), this.maxPollDelayMs);
  }
}

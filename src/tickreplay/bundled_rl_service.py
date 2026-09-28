"""Bounded job lifecycle for the sole fixed Bundled RL worker."""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import cast

from .bundled_rl_runtime import (
    WORKER_PROTOCOL_VERSION,
    BundledRlProcess,
    BundledRlRuntimeError,
)
from .strategy_trace import (
    CONTRACT_VERSION,
    PRESET,
    TraceStore,
    TraceStoreError,
    TraceValidationError,
    canonical_session_bytes,
    compute_input_id,
    session_data_hash,
    validate_trace,
)

ACTIVE_STATES = frozenset({"queued", "running", "validating"})


class BundledRlUnavailableError(RuntimeError):
    """Bundled RL is disabled or failed its self-check."""


class BundledRlJobNotFoundError(LookupError):
    """The process-local job does not exist."""


class BundledRlQueueFullError(RuntimeError):
    """The bounded queue has no remaining capacity."""


class BundledRlManagerClosedError(RuntimeError):
    """The job manager has begun shutdown."""


def _public_error(code: str, message: str) -> Mapping[str, str]:
    return MappingProxyType({"code": code, "message": message})


@dataclass(frozen=True, slots=True)
class JobSnapshot:
    job_id: str
    input_id: str
    state: str
    revision: int
    trace_id: str | None = None
    error: Mapping[str, str] | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "jobId": self.job_id,
            "inputId": self.input_id,
            "state": self.state,
            "revision": self.revision,
            "traceId": self.trace_id,
            "error": None if self.error is None else dict(self.error),
        }


class BundledRlManager:
    """Own fixed-policy jobs while the parent treats trace/model as opaque."""

    def __init__(
        self,
        *,
        available: bool,
        unavailable_reason: str | None,
        process: BundledRlProcess | None,
        store: TraceStore | None,
        runtime_manifest: dict[str, object] | None,
        runtime_fingerprint: str | None,
        max_concurrency: int = 1,
        max_queue: int = 4,
        timeout_seconds: float = 30.0,
    ) -> None:
        if available and (
            process is None
            or store is None
            or runtime_manifest is None
            or runtime_fingerprint is None
        ):
            raise ValueError("available Bundled RL manager requires its fixed runtime")
        if max_concurrency < 1 or max_queue < 1 or timeout_seconds <= 0:
            raise ValueError("Bundled RL limits must be positive")
        self.available = available
        self.unavailable_reason = unavailable_reason
        self.process = process
        self.store = store
        self.runtime_manifest = runtime_manifest
        self.runtime_fingerprint = runtime_fingerprint
        self.max_concurrency = max_concurrency
        self.max_queue = max_queue
        self.timeout_seconds = timeout_seconds
        self._lock = threading.Lock()
        self._closed = False
        self._jobs: dict[str, JobSnapshot] = {}
        self._by_input: dict[str, str] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._timers: dict[str, threading.Timer] = {}
        self._pool = (
            ThreadPoolExecutor(
                max_workers=max_concurrency, thread_name_prefix="bundled-rl"
            )
            if available
            else None
        )

    def capability(self) -> dict[str, object]:
        result: dict[str, object] = {
            "available": self.available,
            "contractVersion": CONTRACT_VERSION,
            "preset": dict(PRESET),
        }
        if not self.available:
            result["unavailableReason"] = (
                self.unavailable_reason or "Bundled RL is unavailable."
            )
        return result

    def submit(self, *, session: Mapping[str, object]) -> JobSnapshot:
        if not self.available:
            raise BundledRlUnavailableError("Bundled RL is unavailable.")
        session_bytes = canonical_session_bytes(session)
        assert self.runtime_fingerprint is not None
        input_id = compute_input_id(
            session_bytes=session_bytes, runtime_fingerprint=self.runtime_fingerprint
        )
        data_hash = session_data_hash(session)
        request = self._request(session=session, data_hash=data_hash, input_id=input_id)
        with self._lock:
            if self._closed:
                raise BundledRlManagerClosedError("Bundled RL is shutting down.")
            existing = self._by_input.get(input_id)
            if existing is not None:
                return self._jobs[existing]
            active = sum(job.state in ACTIVE_STATES for job in self._jobs.values())
            if active >= self.max_concurrency + self.max_queue:
                raise BundledRlQueueFullError("Bundled RL queue is full.")
            job_id = uuid.uuid4().hex
            snapshot = JobSnapshot(job_id, input_id, "queued", 0)
            self._jobs[job_id] = snapshot
            self._by_input[input_id] = job_id
            self._cancel_events[job_id] = threading.Event()
            assert self._pool is not None
            self._pool.submit(self._run, job_id, request)
            return snapshot

    def _request(
        self, *, session: Mapping[str, object], data_hash: str, input_id: str
    ) -> dict[str, object]:
        assert (
            self.runtime_manifest is not None and self.runtime_fingerprint is not None
        )
        return {
            "kind": "run",
            "workerProtocolVersion": WORKER_PROTOCOL_VERSION,
            "contractVersion": CONTRACT_VERSION,
            "preset": dict(PRESET),
            "runtimeManifest": self.runtime_manifest,
            "runtimeFingerprint": self.runtime_fingerprint,
            "inputId": input_id,
            "session": {**session, "dataHash": data_hash},
        }

    def snapshot(self, job_id: str) -> JobSnapshot:
        with self._lock:
            try:
                return self._jobs[job_id]
            except KeyError as error:
                raise BundledRlJobNotFoundError(
                    "Bundled RL job was not found."
                ) from error

    def cancel(self, job_id: str) -> JobSnapshot:
        with self._lock:
            try:
                current = self._jobs[job_id]
            except KeyError as error:
                raise BundledRlJobNotFoundError(
                    "Bundled RL job was not found."
                ) from error
            if current.state not in ACTIVE_STATES:
                return current
            self._cancel_events[job_id].set()
            return self._terminal_locked(
                current,
                "cancelled",
                _public_error("cancelled", "Bundled RL execution was cancelled."),
            )

    def _run(self, job_id: str, request: dict[str, object]) -> None:
        with self._lock:
            current = self._jobs[job_id]
            if current.state != "queued":
                return
            current = replace(current, state="running", revision=current.revision + 1)
            self._jobs[job_id] = current
            deadline = time.monotonic() + self.timeout_seconds
            timer = threading.Timer(
                self.timeout_seconds, self._time_out, args=(job_id,)
            )
            timer.daemon = True
            self._timers[job_id] = timer
            timer.start()
            cancel_event = self._cancel_events[job_id]
        try:
            assert self.process is not None
            response = self.process.execute(
                request, cancel_event=cancel_event, timeout_seconds=self.timeout_seconds
            )
        except TimeoutError:
            self._time_out(job_id)
            return
        except Exception:  # noqa: BLE001 - diagnostics remain private
            self._fail_if_active(
                job_id, "execution_failed", "Bundled RL execution failed."
            )
            return
        with self._lock:
            current = self._jobs[job_id]
            if current.state != "running":
                return
            validating = replace(
                current, state="validating", revision=current.revision + 1
            )
            self._jobs[job_id] = validating
        try:
            response_value = response
            if (
                set(response_value) != {"kind", "trace"}
                or response_value["kind"] != "trace"
            ):
                raise TraceValidationError("worker response shape mismatch")
            trace = validate_trace(response_value["trace"])
            self._validate_identity(trace, request)
        except (TraceValidationError, BundledRlRuntimeError, TypeError, ValueError):
            self._fail_if_active(
                job_id, "invalid_artifact", "Bundled RL returned an invalid trace."
            )
            return
        assert self.store is not None
        try:
            prepared = self.store.prepare(trace)
        except TraceStoreError:
            self._fail_if_active(
                job_id,
                "artifact_store_failed",
                "Bundled RL trace could not be stored.",
            )
            return
        try:
            with self._lock:
                current = self._jobs[job_id]
                if current != validating:
                    return
                if time.monotonic() >= deadline:
                    self._time_out_locked(current)
                    return
            try:
                trace_id = self.store.commit(prepared)
            except Exception:  # noqa: BLE001 - storage details remain private
                self._fail_if_active(
                    job_id,
                    "artifact_store_failed",
                    "Bundled RL trace could not be stored.",
                )
                return
            with self._lock:
                current = self._jobs[job_id]
                if current != validating:
                    return
                if time.monotonic() >= deadline:
                    self._time_out_locked(current)
                    return
                timer = self._timers.pop(job_id, None)
                if timer is not None:
                    timer.cancel()
                self._jobs[job_id] = replace(
                    current,
                    state="ready",
                    revision=current.revision + 1,
                    trace_id=trace_id,
                )
        finally:
            self.store.discard(prepared)

    def _validate_identity(
        self, trace: dict[str, object], request: dict[str, object]
    ) -> None:
        session = cast(dict[str, object], request["session"])
        expected_session = {
            "stem": session["stem"],
            "code": session["code"],
            "date": session["date"],
            "dataHash": session["dataHash"],
            "tickCount": session["count"],
        }
        if (
            trace["inputId"] != request["inputId"]
            or trace["runtimeFingerprint"] != request["runtimeFingerprint"]
        ):
            raise TraceValidationError("trace runtime identity mismatch")
        if trace["preset"] != PRESET or trace["session"] != expected_session:
            raise TraceValidationError("trace input identity mismatch")

    def _time_out(self, job_id: str) -> None:
        with self._lock:
            current = self._jobs.get(job_id)
            if current is not None and current.state in ACTIVE_STATES:
                self._time_out_locked(current)

    def _time_out_locked(self, current: JobSnapshot) -> JobSnapshot:
        self._cancel_events[current.job_id].set()
        return self._terminal_locked(
            current,
            "timed_out",
            _public_error("timed_out", "Bundled RL execution timed out."),
        )

    def _fail_if_active(self, job_id: str, code: str, message: str) -> None:
        with self._lock:
            current = self._jobs[job_id]
            if current.state in ACTIVE_STATES:
                self._terminal_locked(current, "failed", _public_error(code, message))

    def _terminal_locked(
        self, current: JobSnapshot, state: str, error: Mapping[str, str]
    ) -> JobSnapshot:
        timer = self._timers.pop(current.job_id, None)
        if timer is not None:
            timer.cancel()
        terminal = replace(
            current,
            state=state,
            revision=current.revision + 1,
            trace_id=None,
            error=error,
        )
        self._jobs[current.job_id] = terminal
        if self._by_input.get(current.input_id) == current.job_id:
            self._by_input.pop(current.input_id, None)
        return terminal

    def shutdown(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for snapshot in tuple(self._jobs.values()):
                if snapshot.state in ACTIVE_STATES:
                    self._cancel_events[snapshot.job_id].set()
                    self._terminal_locked(
                        snapshot,
                        "cancelled",
                        _public_error("shutdown", "Bundled RL execution was stopped."),
                    )
        if self._pool is not None:
            self._pool.shutdown(wait=True, cancel_futures=True)

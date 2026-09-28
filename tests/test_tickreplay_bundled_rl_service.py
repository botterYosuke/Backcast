from __future__ import annotations

import threading
import time

import pytest

from tickreplay.bundled_rl_runtime import runtime_fingerprint, runtime_manifest
from tickreplay.bundled_rl_service import (
    BundledRlJobNotFoundError,
    BundledRlManager,
    BundledRlQueueFullError,
)
from tickreplay.bundled_rl_worker import handle
from tickreplay.strategy_trace import TraceStore, TraceStoreError


def _session() -> dict[str, object]:
    base = 1_788_167_660_000_000
    return {
        "stem": "1301",
        "code": "13010",
        "date": "2026-09-01",
        "count": 4,
        "us": [base, base + 1, base + 10_000_000, base + 10_000_001],
        "price": [100.0, 100.1, 100.2, 100.3],
        "qty": [1, 1, 1, 1],
        "type": ["", "", "", ""],
    }


class WorkerAdapter:
    def __init__(self, gate: threading.Event | None = None) -> None:
        self.gate = gate
        self.calls = 0

    def execute(self, request, *, cancel_event, timeout_seconds):
        self.calls += 1
        if self.gate is not None:
            while not self.gate.wait(0.005):
                if cancel_event.is_set():
                    raise RuntimeError("cancelled")
        return handle(request)


def _manager(
    tmp_path,
    process=None,
    *,
    store=None,
    timeout_seconds=5,
    max_concurrency=1,
    max_queue=4,
) -> BundledRlManager:
    manifest = runtime_manifest()
    return BundledRlManager(
        available=True,
        unavailable_reason=None,
        process=process or WorkerAdapter(),
        store=store or TraceStore(tmp_path),
        runtime_manifest=manifest,
        runtime_fingerprint=runtime_fingerprint(manifest),
        timeout_seconds=timeout_seconds,
        max_concurrency=max_concurrency,
        max_queue=max_queue,
    )


def _ready(manager: BundledRlManager, job_id: str):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        snapshot = manager.snapshot(job_id)
        if snapshot.state not in {"queued", "running", "validating"}:
            return snapshot
        time.sleep(0.01)
    raise AssertionError("job did not finish")


def _session_with_last_price(price: float) -> dict[str, object]:
    session = _session()
    return {**session, "price": [100.0, 100.1, 100.2, price]}


class BlockingCommitStore(TraceStore):
    def __init__(self, root) -> None:
        super().__init__(root)
        self.started = threading.Event()
        self.release = threading.Event()

    def commit(self, prepared):
        self.started.set()
        if not self.release.wait(5):
            raise TraceStoreError("test commit was not released")
        return super().commit(prepared)


class SlowCommitStore(TraceStore):
    def commit(self, prepared):
        time.sleep(0.1)
        return super().commit(prepared)


class FailingPrepareStore(TraceStore):
    def prepare(self, trace):
        raise TraceStoreError("prepare failed")


class FailingCommitStore(TraceStore):
    def commit(self, prepared):
        raise TraceStoreError("commit failed")


def test_service_deduplicates_fixed_input_and_publishes_immutable_trace(tmp_path):
    process = WorkerAdapter()
    manager = _manager(tmp_path, process)
    first = manager.submit(session=_session())
    ready = _ready(manager, first.job_id)
    assert ready.state == "ready" and ready.trace_id
    second = manager.submit(session=_session())
    assert second == ready
    assert process.calls == 1
    assert manager.store.read(ready.trace_id)["inputId"] == ready.input_id
    manager.shutdown()


def test_cancel_is_job_scoped_idempotent_and_late_result_cannot_publish(tmp_path):
    gate = threading.Event()
    manager = _manager(tmp_path, WorkerAdapter(gate))
    submitted = manager.submit(session=_session())
    cancelled = manager.cancel(submitted.job_id)
    assert cancelled.state == "cancelled"
    assert manager.cancel(submitted.job_id) == cancelled
    gate.set()
    time.sleep(0.05)
    assert manager.snapshot(submitted.job_id).state == "cancelled"
    assert list(tmp_path.iterdir()) == []
    manager.shutdown()


def test_queue_admission_is_bounded_without_starting_extra_children(tmp_path):
    gate = threading.Event()
    process = WorkerAdapter(gate)
    manager = _manager(tmp_path, process, max_concurrency=1, max_queue=1)
    first = manager.submit(session=_session_with_last_price(100.3))
    deadline = time.monotonic() + 1
    while process.calls == 0 and time.monotonic() < deadline:
        time.sleep(0.005)
    second = manager.submit(session=_session_with_last_price(100.4))

    with pytest.raises(BundledRlQueueFullError):
        manager.submit(session=_session_with_last_price(100.5))

    assert process.calls == 1
    assert manager.snapshot(first.job_id).state == "running"
    assert manager.snapshot(second.job_id).state == "queued"
    manager.shutdown()


def test_running_timeout_is_terminal_and_late_result_cannot_publish(tmp_path):
    gate = threading.Event()
    manager = _manager(
        tmp_path,
        WorkerAdapter(gate),
        timeout_seconds=0.03,
    )
    submitted = manager.submit(session=_session())

    terminal = _ready(manager, submitted.job_id)

    assert terminal.state == "timed_out"
    assert terminal.revision == 2
    assert terminal.error == {
        "code": "timed_out",
        "message": "Bundled RL execution timed out.",
    }
    assert manager._timers == {}
    gate.set()
    time.sleep(0.05)
    assert manager.snapshot(submitted.job_id) == terminal
    assert list(tmp_path.iterdir()) == []
    manager.shutdown()


def test_cancel_remains_responsive_while_artifact_commit_is_blocked(tmp_path):
    store = BlockingCommitStore(tmp_path)
    manager = _manager(tmp_path, store=store)
    submitted = manager.submit(session=_session())
    assert store.started.wait(1)
    result = {}
    completed = threading.Event()

    def cancel() -> None:
        result["snapshot"] = manager.cancel(submitted.job_id)
        completed.set()

    thread = threading.Thread(target=cancel)
    thread.start()
    try:
        assert completed.wait(0.25), "artifact I/O blocked cancellation"
        assert result["snapshot"].state == "cancelled"
    finally:
        store.release.set()
        thread.join(timeout=1)

    time.sleep(0.05)
    assert manager.snapshot(submitted.job_id).state == "cancelled"
    manager.shutdown()


def test_artifact_commit_cannot_publish_ready_after_deadline(tmp_path):
    manager = _manager(
        tmp_path,
        store=SlowCommitStore(tmp_path),
        timeout_seconds=0.03,
    )
    submitted = manager.submit(session=_session())

    terminal = _ready(manager, submitted.job_id)

    assert terminal.state == "timed_out"
    time.sleep(0.15)
    assert manager.snapshot(submitted.job_id) == terminal
    manager.shutdown()


@pytest.mark.parametrize(
    ("store_type", "expected_code"),
    [
        (FailingPrepareStore, "artifact_store_failed"),
        (FailingCommitStore, "artifact_store_failed"),
    ],
)
def test_artifact_store_failures_are_terminal_and_leave_no_temp_files(
    tmp_path, store_type, expected_code
):
    manager = _manager(
        tmp_path,
        store=store_type(tmp_path),
        timeout_seconds=0.1,
    )
    submitted = manager.submit(session=_session())

    terminal = _ready(manager, submitted.job_id)

    assert terminal.state == "failed"
    assert terminal.error == {
        "code": expected_code,
        "message": "Bundled RL trace could not be stored.",
    }
    assert list(tmp_path.iterdir()) == []
    manager.shutdown()


def test_unavailable_and_unknown_jobs_fail_closed(tmp_path):
    manager = BundledRlManager(
        available=False,
        unavailable_reason="disabled",
        process=None,
        store=None,
        runtime_manifest=None,
        runtime_fingerprint=None,
    )
    assert manager.capability()["available"] is False
    with pytest.raises(BundledRlJobNotFoundError):
        manager.snapshot("missing")

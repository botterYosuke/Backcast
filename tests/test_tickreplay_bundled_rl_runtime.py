from __future__ import annotations

import os
import subprocess
import sys
import threading

import pytest

from tickreplay.bundled_rl_runtime import (
    NUMPY_VERSION,
    WORKER_PATH,
    BundledRlProcess,
    BundledRlRuntimeError,
    fixed_worker_argv,
    runtime_fingerprint,
    runtime_manifest,
    self_check,
    validate_runtime_manifest,
)


def test_runtime_manifest_and_fixed_argv_are_repository_owned():
    manifest = runtime_manifest()
    validate_runtime_manifest(manifest)
    assert manifest["numpy"] == NUMPY_VERSION
    assert len(runtime_fingerprint(manifest)) == 64
    assert fixed_worker_argv() == (sys.executable, "-I", str(WORKER_PATH))


def test_manifest_rejects_dependency_or_asset_identity_mismatch():
    manifest = runtime_manifest()
    manifest["numpy"] = "0.0.0"
    with pytest.raises(BundledRlRuntimeError, match="requires NumPy"):
        validate_runtime_manifest(manifest)


def test_fixed_child_handshake_recomputes_identity():
    manifest, fingerprint = self_check(BundledRlProcess(), timeout_seconds=10)
    assert fingerprint == runtime_fingerprint(manifest)


def test_process_rejects_unknown_worker_request_without_leaking_diagnostics():
    process = BundledRlProcess()
    with pytest.raises(BundledRlRuntimeError, match="rejected"):
        process.execute(
            {"kind": "unknown"}, cancel_event=threading.Event(), timeout_seconds=10
        )


def test_process_terminates_child_that_closes_pipes_without_exiting():
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import os,time; os.close(1); os.close(2); time.sleep(60)",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=os.name != "nt",
    )
    runtime = BundledRlProcess()
    try:
        with pytest.raises(BundledRlRuntimeError, match="did not exit"):
            runtime._communicate(
                process,
                b"request",
                cancel_event=threading.Event(),
                timeout_seconds=5,
            )
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=1)

    assert process.returncode is not None

"""Fixed subprocess boundary and reproducible identity for Bundled RL."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import selectors
import signal
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .strategy_trace import (
    CONTRACT_VERSION,
    MAX_ARTIFACT_BYTES,
    PRESET,
    PROPOSAL_CONTRACT,
    canonical_json_bytes,
)

WORKER_PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 64 * 1024 * 1024
MAX_RESPONSE_BYTES = MAX_ARTIFACT_BYTES + 64 * 1024
MAX_DIAGNOSTIC_BYTES = 64 * 1024
NUMPY_VERSION = "2.4.5"
PROVENANCE_SHA256 = "63a6c959bce0b5c8d374d7f973234824be8a6b3fdc0ae33765568664aff697ce"
SUPPORTED_SYSTEMS = frozenset({"Darwin", "Linux"})
SUPPORTED_MACHINES = frozenset({"x86_64", "arm64", "aarch64"})
PACKAGE_DIR = Path(__file__).resolve().parent
WORKER_PATH = PACKAGE_DIR / "bundled_rl_worker.py"
INFERENCE_PATH = PACKAGE_DIR / "bundled_rl_inference.py"
MODEL_PATH = PACKAGE_DIR / "models" / "rl_scalp_evolution_v1.json"
PROVENANCE_PATH = PACKAGE_DIR / "models" / "rl_scalp_evolution_v1.provenance.json"


class BundledRlRuntimeError(RuntimeError):
    """The fixed child runtime is unavailable or violated its contract."""


def _file_identity(path: Path) -> dict[str, object]:
    try:
        data = path.read_bytes()
    except OSError as error:
        raise BundledRlRuntimeError(
            f"bundled runtime asset is unreadable: {path.name}"
        ) from error
    return {
        "name": path.name,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def runtime_manifest() -> dict[str, object]:
    """Return the complete server-owned child identity without importing NumPy."""
    try:
        numpy_version = importlib.metadata.version("numpy")
    except importlib.metadata.PackageNotFoundError as error:
        raise BundledRlRuntimeError("NumPy is not installed") from error
    system = platform.system()
    machine = platform.machine().lower()
    python_version = (
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    )
    manifest: dict[str, object] = {
        "contractVersion": CONTRACT_VERSION,
        "proposalContract": PROPOSAL_CONTRACT,
        "preset": dict(PRESET),
        "workerProtocolVersion": WORKER_PROTOCOL_VERSION,
        "framing": "uint32be-json-v1",
        "python": {
            "implementation": platform.python_implementation(),
            "version": python_version,
        },
        "numpy": numpy_version,
        "platform": {"system": system, "machine": machine},
        "assets": {
            "model": _file_identity(MODEL_PATH),
            "provenance": _file_identity(PROVENANCE_PATH),
            "inference": _file_identity(INFERENCE_PATH),
            "worker": _file_identity(WORKER_PATH),
        },
    }
    return manifest


def validate_runtime_manifest(manifest: dict[str, object]) -> None:
    python_identity = manifest.get("python")
    platform_identity = manifest.get("platform")
    if not isinstance(python_identity, dict) or not isinstance(platform_identity, dict):
        raise BundledRlRuntimeError("runtime identity is malformed")
    if python_identity.get("implementation") != "CPython" or sys.version_info[
        :2
    ] not in {(3, 11), (3, 12), (3, 13)}:
        raise BundledRlRuntimeError("Bundled RL requires CPython 3.11-3.13")
    if platform_identity.get("system") not in SUPPORTED_SYSTEMS:
        raise BundledRlRuntimeError(
            "Bundled RL is unsupported on this operating system"
        )
    if platform_identity.get("machine") not in SUPPORTED_MACHINES:
        raise BundledRlRuntimeError("Bundled RL is unsupported on this architecture")
    if manifest.get("numpy") != NUMPY_VERSION:
        raise BundledRlRuntimeError(f"Bundled RL requires NumPy {NUMPY_VERSION}")
    assets = manifest.get("assets")
    if not isinstance(assets, dict):
        raise BundledRlRuntimeError("runtime asset identity is malformed")
    model = assets.get("model")
    provenance = assets.get("provenance")
    if (
        not isinstance(model, dict)
        or model.get("sha256") != PRESET["digest"]
        or model.get("size") != 11_932
    ):
        raise BundledRlRuntimeError("bundled model identity mismatch")
    if (
        not isinstance(provenance, dict)
        or provenance.get("sha256") != PROVENANCE_SHA256
    ):
        raise BundledRlRuntimeError("bundled provenance identity mismatch")


def runtime_fingerprint(manifest: dict[str, object]) -> str:
    return hashlib.sha256(canonical_json_bytes(manifest)).hexdigest()


def fixed_worker_argv() -> tuple[str, ...]:
    return (sys.executable, "-I", str(WORKER_PATH))


def _decode_frame(data: bytes) -> dict[str, object]:
    if len(data) < 4:
        raise BundledRlRuntimeError("worker response is truncated")
    (size,) = struct.unpack(">I", data[:4])
    if size == 0 or size > MAX_RESPONSE_BYTES or len(data) != size + 4:
        raise BundledRlRuntimeError("worker response frame is invalid")

    def reject_constant(_value: str) -> None:
        raise ValueError("non-finite JSON number")

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = value
        return result

    try:
        value = json.loads(
            data[4:].decode("utf-8"),
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicates,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise BundledRlRuntimeError("worker response is invalid JSON") from error
    if not isinstance(value, dict):
        raise BundledRlRuntimeError("worker response must be an object")
    return value


class BundledRlProcess:
    """Spawn only the repository-owned worker with bounded transport."""

    def __init__(self) -> None:
        self.argv = fixed_worker_argv()

    def execute(
        self,
        request: dict[str, object],
        *,
        cancel_event: threading.Event,
        timeout_seconds: float,
    ) -> dict[str, object]:
        payload = canonical_json_bytes(request)
        if not payload or len(payload) > MAX_REQUEST_BYTES:
            raise BundledRlRuntimeError("worker request exceeds its limit")
        frame = struct.pack(">I", len(payload)) + payload
        environment = {
            key: os.environ[key]
            for key in ("PATH", "LANG", "LC_ALL", "SYSTEMROOT")
            if key in os.environ
        }
        environment.update({"PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"})
        try:
            process = subprocess.Popen(
                self.argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                shell=False,
                start_new_session=os.name != "nt",
            )
        except OSError as error:
            raise BundledRlRuntimeError("worker process could not start") from error
        stdout, stderr = self._communicate(
            process, frame, cancel_event=cancel_event, timeout_seconds=timeout_seconds
        )
        if stderr:
            raise BundledRlRuntimeError("worker wrote unexpected diagnostics")
        if process.returncode != 0:
            raise BundledRlRuntimeError("worker process failed")
        response = _decode_frame(stdout)
        if response.get("kind") == "error":
            raise BundledRlRuntimeError("worker rejected its request")
        return response

    def _communicate(
        self,
        process: subprocess.Popen[bytes],
        frame: bytes,
        *,
        cancel_event: threading.Event,
        timeout_seconds: float,
    ) -> tuple[bytes, bytes]:
        assert (
            process.stdin is not None
            and process.stdout is not None
            and process.stderr is not None
        )
        writer = threading.Thread(
            target=self._write, args=(process.stdin, frame), daemon=True
        )
        writer.start()
        deadline = time.monotonic() + timeout_seconds
        output = bytearray()
        diagnostics = bytearray()
        selector = selectors.DefaultSelector()
        for stream, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream.fileno(), selectors.EVENT_READ, (stream, name))
        failure: str | None = None
        try:
            while selector.get_map():
                if cancel_event.is_set():
                    failure = "cancelled"
                    break
                if time.monotonic() >= deadline:
                    failure = "timed_out"
                    break
                for key, _ in selector.select(0.02):
                    stream, name = key.data
                    chunk = os.read(key.fd, 64 * 1024)
                    if not chunk:
                        selector.unregister(key.fd)
                        stream.close()
                        continue
                    target = output if name == "stdout" else diagnostics
                    limit = (
                        MAX_RESPONSE_BYTES + 4
                        if name == "stdout"
                        else MAX_DIAGNOSTIC_BYTES
                    )
                    target.extend(chunk[: limit + 1 - len(target)])
                    if len(target) > limit:
                        failure = f"{name}_limit"
                        break
                if failure is not None:
                    break
        finally:
            selector.close()
        if failure is not None:
            self._terminate(process)
            if failure == "timed_out":
                raise TimeoutError("worker process timed out")
            raise BundledRlRuntimeError(f"worker process {failure}")
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self._terminate(process)
            writer.join(timeout=0.2)
            raise BundledRlRuntimeError("worker process did not exit") from None
        writer.join(timeout=0.2)
        return bytes(output), bytes(diagnostics)

    @staticmethod
    def _write(stream: Any, frame: bytes) -> None:
        try:
            stream.write(frame)
            stream.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes]) -> None:
        try:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
            process.wait(timeout=0.2)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
            process.wait(timeout=1)


def self_check(
    process: BundledRlProcess, *, timeout_seconds: float
) -> tuple[dict[str, object], str]:
    manifest = runtime_manifest()
    validate_runtime_manifest(manifest)
    fingerprint = runtime_fingerprint(manifest)
    response = process.execute(
        {
            "kind": "handshake",
            "runtimeManifest": manifest,
            "runtimeFingerprint": fingerprint,
        },
        cancel_event=threading.Event(),
        timeout_seconds=timeout_seconds,
    )
    expected = {
        "kind": "handshake",
        "workerProtocolVersion": WORKER_PROTOCOL_VERSION,
        "runtimeFingerprint": fingerprint,
    }
    if response != expected:
        raise BundledRlRuntimeError("worker handshake identity mismatch")
    return manifest, fingerprint

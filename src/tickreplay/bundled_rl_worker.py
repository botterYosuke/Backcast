"""Private fixed child process for repository-owned Bundled RL inference."""

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tickreplay.bundled_rl_inference import (  # noqa: E402
    build_bundled_rl_trace,
    verify_bundled_assets,
)
from tickreplay.bundled_rl_runtime import (  # noqa: E402
    MAX_REQUEST_BYTES,
    WORKER_PROTOCOL_VERSION,
    runtime_fingerprint,
    runtime_manifest,
    validate_runtime_manifest,
)
from tickreplay.strategy_trace import (  # noqa: E402
    CONTRACT_VERSION,
    PRESET,
    PROPOSAL_CONTRACT,
    canonical_json_bytes,
    canonical_session_bytes,
    compute_input_id,
    session_data_hash,
    validate_trace,
)


def _exact_dict(value: Any, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("invalid worker request")
    return value


def _read_request() -> dict[str, object]:
    header = sys.stdin.buffer.read(4)
    if len(header) != 4:
        raise ValueError("truncated worker request")
    (size,) = struct.unpack(">I", header)
    if size == 0 or size > MAX_REQUEST_BYTES:
        raise ValueError("invalid worker request size")
    payload = sys.stdin.buffer.read(size)
    if len(payload) != size or sys.stdin.buffer.read(1):
        raise ValueError("invalid worker request frame")

    def reject_constant(_value: str) -> None:
        raise ValueError("non-finite JSON number")

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = value
        return result

    value = json.loads(
        payload.decode("utf-8"),
        parse_constant=reject_constant,
        object_pairs_hook=reject_duplicates,
    )
    if not isinstance(value, dict):
        raise ValueError("worker request must be an object")
    return value


def _verify_runtime(request: dict[str, Any]) -> str:
    supplied_manifest = request["runtimeManifest"]
    supplied_fingerprint = request["runtimeFingerprint"]
    if not isinstance(supplied_manifest, dict) or not isinstance(
        supplied_fingerprint, str
    ):
        raise ValueError("invalid runtime identity")
    actual_manifest = runtime_manifest()
    validate_runtime_manifest(actual_manifest)
    actual_fingerprint = runtime_fingerprint(actual_manifest)
    if (
        supplied_manifest != actual_manifest
        or supplied_fingerprint != actual_fingerprint
    ):
        raise ValueError("runtime identity mismatch")
    verify_bundled_assets()
    return actual_fingerprint


def _handshake(request: dict[str, object]) -> dict[str, object]:
    value = _exact_dict(request, {"kind", "runtimeManifest", "runtimeFingerprint"})
    if value["kind"] != "handshake":
        raise ValueError("invalid handshake")
    fingerprint = _verify_runtime(value)
    return {
        "kind": "handshake",
        "workerProtocolVersion": WORKER_PROTOCOL_VERSION,
        "runtimeFingerprint": fingerprint,
    }


def _run(request: dict[str, object]) -> dict[str, object]:
    value = _exact_dict(
        request,
        {
            "kind",
            "workerProtocolVersion",
            "contractVersion",
            "preset",
            "runtimeManifest",
            "runtimeFingerprint",
            "inputId",
            "session",
        },
    )
    if (
        value["kind"] != "run"
        or value["workerProtocolVersion"] != WORKER_PROTOCOL_VERSION
    ):
        raise ValueError("invalid worker protocol")
    if value["contractVersion"] != CONTRACT_VERSION or value["preset"] != PRESET:
        raise ValueError("invalid policy identity")
    fingerprint = _verify_runtime(value)
    session = _exact_dict(
        value["session"],
        {"stem", "code", "date", "count", "us", "price", "qty", "type", "dataHash"},
    )
    observable = {
        key: session[key]
        for key in ("stem", "code", "date", "count", "us", "price", "qty", "type")
    }
    session_bytes = canonical_session_bytes(observable)
    if session["dataHash"] != session_data_hash(observable):
        raise ValueError("session digest mismatch")
    input_id = compute_input_id(
        session_bytes=session_bytes, runtime_fingerprint=fingerprint
    )
    if value["inputId"] != input_id:
        raise ValueError("input identity mismatch")
    rows = build_bundled_rl_trace(timestamps_us=session["us"], prices=session["price"])
    trace = {
        "contractVersion": CONTRACT_VERSION,
        "preset": dict(PRESET),
        "session": {
            "stem": session["stem"],
            "code": session["code"],
            "date": session["date"],
            "dataHash": session["dataHash"],
            "tickCount": session["count"],
        },
        "inputId": input_id,
        "runtimeFingerprint": fingerprint,
        "proposalContract": PROPOSAL_CONTRACT,
        "decisions": [
            {
                "observedSessionTickIndex": row.observed_session_tick_index,
                "eligibleFromSessionTick": row.eligible_from_session_tick,
                "decisionTimeUs": row.decision_time_us,
                "positionBranches": [
                    {
                        "positionQuantity": branch.position_quantity,
                        "targetQuantity": branch.target_quantity,
                    }
                    for branch in row.branches
                ],
            }
            for row in rows
        ],
    }
    return {"kind": "trace", "trace": validate_trace(trace)}


def handle(request: dict[str, object]) -> dict[str, object]:
    if request.get("kind") == "handshake":
        return _handshake(request)
    if request.get("kind") == "run":
        return _run(request)
    raise ValueError("unsupported worker request")


def _write_response(value: dict[str, object]) -> None:
    payload = canonical_json_bytes(value)
    sys.stdout.buffer.write(struct.pack(">I", len(payload)))
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()


def main() -> int:
    try:
        response = handle(_read_request())
    except Exception:  # noqa: BLE001 - public parent receives no private diagnostics
        _write_response({"kind": "error"})
        return 0
    _write_response(response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

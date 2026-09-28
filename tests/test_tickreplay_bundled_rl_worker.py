from __future__ import annotations

import pytest

from tickreplay.bundled_rl_runtime import (
    WORKER_PROTOCOL_VERSION,
    runtime_fingerprint,
    runtime_manifest,
)
from tickreplay.bundled_rl_worker import handle
from tickreplay.strategy_trace import (
    CONTRACT_VERSION,
    PRESET,
    canonical_session_bytes,
    compute_input_id,
    session_data_hash,
)


def _session() -> dict[str, object]:
    base = 1_788_167_660_000_000  # 2026-09-01 09:01 exchange-wall-clock
    session = {
        "stem": "1301",
        "code": "13010",
        "date": "2026-09-01",
        "count": 4,
        "us": [base, base + 1, base + 10_000_000, base + 10_000_001],
        "price": [100.0, 100.1, 100.2, 100.3],
        "qty": [1, 1, 1, 1],
        "type": ["", "", "", ""],
    }
    return {**session, "dataHash": session_data_hash(session)}


def _request() -> dict[str, object]:
    manifest = runtime_manifest()
    fingerprint = runtime_fingerprint(manifest)
    session = _session()
    observable = {
        key: session[key]
        for key in ("stem", "code", "date", "count", "us", "price", "qty", "type")
    }
    return {
        "kind": "run",
        "workerProtocolVersion": WORKER_PROTOCOL_VERSION,
        "contractVersion": CONTRACT_VERSION,
        "preset": dict(PRESET),
        "runtimeManifest": manifest,
        "runtimeFingerprint": fingerprint,
        "inputId": compute_input_id(
            session_bytes=canonical_session_bytes(observable),
            runtime_fingerprint=fingerprint,
        ),
        "session": session,
    }


def test_worker_returns_target_only_ordered_position_branches():
    response = handle(_request())
    assert response["kind"] == "trace"
    trace = response["trace"]
    assert trace["runtimeFingerprint"] == _request()["runtimeFingerprint"]
    for row in trace["decisions"]:
        assert [branch["positionQuantity"] for branch in row["positionBranches"]] == [
            0,
            100,
        ]
        assert all(
            set(branch) == {"positionQuantity", "targetQuantity"}
            for branch in row["positionBranches"]
        )


def test_worker_rejects_forged_runtime_or_input_identity():
    request = _request()
    request["runtimeFingerprint"] = "0" * 64
    with pytest.raises(ValueError, match="runtime identity mismatch"):
        handle(request)
    request = _request()
    request["inputId"] = "0" * 64
    with pytest.raises(ValueError, match="input identity mismatch"):
        handle(request)

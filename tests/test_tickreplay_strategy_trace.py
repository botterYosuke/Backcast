from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from tickreplay.strategy_trace import (
    PRESET,
    TraceStore,
    TraceStoreError,
    TraceValidationError,
    canonical_session_bytes,
    canonical_trace_bytes,
    compute_input_id,
    session_data_hash,
    validate_trace,
)


def _session() -> dict[str, object]:
    return {
        "stem": "1301",
        "code": "13010",
        "date": "2026-08-17",
        "count": 3,
        "us": [1, 1, 2],
        "price": [100.0, 101.0, 102.0],
        "qty": [1, 2, 3],
        "type": ["", "A", "B"],
    }


def _trace() -> dict[str, object]:
    session = _session()
    runtime = "d" * 64
    input_id = compute_input_id(
        session_bytes=canonical_session_bytes(session), runtime_fingerprint=runtime
    )
    return {
        "contractVersion": "bundled_rl_trace_v1",
        "preset": dict(PRESET),
        "session": {
            "stem": "1301",
            "code": "13010",
            "date": "2026-08-17",
            "dataHash": session_data_hash(session),
            "tickCount": 3,
        },
        "inputId": input_id,
        "runtimeFingerprint": runtime,
        "proposalContract": "backcast_position_target_v1",
        "decisions": [
            {
                "observedSessionTickIndex": 0,
                "eligibleFromSessionTick": 1,
                "decisionTimeUs": 1,
                "positionBranches": [
                    {"positionQuantity": 0, "targetQuantity": 100},
                    {"positionQuantity": 100, "targetQuantity": 0},
                ],
            }
        ],
    }


def test_session_hash_preserves_equal_timestamp_order_and_rejects_decrease():
    session = _session()
    assert session_data_hash(session) == session_data_hash(
        dict(reversed(session.items()))
    )
    reversed_prices = {**session, "price": [101.0, 100.0, 102.0]}
    assert session_data_hash(reversed_prices) != session_data_hash(session)
    with pytest.raises(TraceValidationError, match="nondecreasing"):
        canonical_session_bytes({**session, "us": [2, 1, 3]})


def test_input_identity_is_framed_and_runtime_sensitive():
    session = canonical_session_bytes(_session())
    first = compute_input_id(session_bytes=session, runtime_fingerprint="a" * 64)
    assert first != compute_input_id(
        session_bytes=session, runtime_fingerprint="b" * 64
    )
    assert len(first) == 64


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda trace: trace.update(source="forbidden"), "unknown field"),
        (
            lambda trace: trace["decisions"][0].update(eligibleFromSessionTick=2),
            r"observed \+ 1",
        ),
        (
            lambda trace: trace["decisions"][0]["positionBranches"].reverse(),
            "must be 0",
        ),
        (
            lambda trace: trace["decisions"][0]["positionBranches"][0].update(
                targetQuantity=50
            ),
            "0 or 100",
        ),
        (
            lambda trace: trace["decisions"][0].update(
                observedSessionTickIndex=2, eligibleFromSessionTick=3
            ),
            "out of range",
        ),
    ],
)
def test_trace_rejects_forbidden_noncausal_or_wrong_branch_data(mutate, message):
    trace = _trace()
    mutate(trace)
    with pytest.raises(TraceValidationError, match=message):
        validate_trace(trace)


def test_trace_is_exact_target_only_canonical_json():
    trace = _trace()
    encoded = canonical_trace_bytes(trace)
    assert encoded.endswith(b"\n")
    assert json.loads(encoded) == validate_trace(trace)
    assert b"q_values" not in encoded and b"common_features" not in encoded


def test_trace_store_is_concurrent_immutable_and_detects_tampering(tmp_path):
    store = TraceStore(tmp_path)
    trace = _trace()
    with ThreadPoolExecutor(max_workers=4) as pool:
        trace_ids = list(pool.map(lambda _: store.publish(trace), range(8)))
    assert len(set(trace_ids)) == 1
    trace_id = trace_ids[0]
    assert store.read(trace_id) == validate_trace(trace)
    (tmp_path / f"{trace_id}.json").write_bytes(b"{}\n")
    with pytest.raises(TraceStoreError, match="hash mismatch"):
        store.read(trace_id)

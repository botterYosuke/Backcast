from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from copy import deepcopy
from datetime import UTC, datetime, time
from pathlib import Path

import numpy as np
import pytest

from tickreplay.bundled_rl_inference import (
    MODEL_PATH,
    MODEL_SHA256,
    MODEL_SIZE_BYTES,
    NUMPY_VERSION,
    PROVENANCE_PATH,
    PROVENANCE_SHA256,
    BundledRlModelError,
    _policy_from_document,
    build_bundled_rl_trace,
    load_bundled_policy,
)

ORACLE_PATH = Path(__file__).parent / "fixtures" / "bundled-rl" / "model-oracle.json"


def _wall_clock_us(hour: int, minute: int, second: int, microsecond: int = 0) -> int:
    value = datetime(2025, 9, 1, hour, minute, second, microsecond, tzinfo=UTC)
    return int(value.timestamp() * 1_000_000)


def _branch(row, position_quantity: int):
    return next(
        branch
        for branch in row.branches
        if branch.position_quantity == position_quantity
    )


def test_bundled_asset_and_provenance_have_frozen_identities() -> None:
    model_bytes = MODEL_PATH.read_bytes()
    provenance_bytes = PROVENANCE_PATH.read_bytes()

    assert len(model_bytes) == MODEL_SIZE_BYTES == 11_932
    assert hashlib.sha256(model_bytes).hexdigest() == MODEL_SHA256
    assert hashlib.sha256(provenance_bytes).hexdigest() == PROVENANCE_SHA256
    assert np.__version__ == NUMPY_VERSION == "2.4.5"

    provenance = json.loads(provenance_bytes)
    assert provenance["copyBasis"] == "local_user_authorized"
    assert provenance["publicRedistributionRights"] == "unconfirmed"
    assert provenance["sourceRepositoryCommit"] == (
        "2166235559e31357d1cf8e84b7c6305ece06667e"
    )


def test_policy_matches_upstream_float32_oracle() -> None:
    policy = load_bundled_policy()
    oracle = json.loads(ORACLE_PATH.read_text(encoding="utf-8"))

    assert oracle["numpyVersion"] == NUMPY_VERSION
    assert oracle["returnFeatures"] == ["m0/m1", "m0/m2", "m0/m3"]
    for case in oracle["cases"]:
        decision = policy.decide_encoded(case["encoded"])
        assert decision.q_values == pytest.approx(case["q"], abs=1e-7)
        assert decision.target_quantity == case["targetQuantity"]


def test_policy_rejects_wrong_position_and_unencoded_shape() -> None:
    policy = load_bundled_policy()

    with pytest.raises(BundledRlModelError, match="position quantity"):
        policy.decide((0.0,) * 6, 1)
    with pytest.raises(BundledRlModelError, match="six common features"):
        policy.decide((0.0,) * 5, 0)
    with pytest.raises(BundledRlModelError, match="seven encoded features"):
        policy.decide_encoded((0.0,) * 6)
    with pytest.raises(BundledRlModelError, match="finite numbers"):
        policy.decide_encoded((0.0, 0.0, 0.0, 0.0, 0.0, object(), 0.0))


@pytest.mark.parametrize("mutation", ["genome", "shape", "nonfinite"])
def test_policy_document_validation_fails_closed(mutation: str) -> None:
    document = json.loads(MODEL_PATH.read_text(encoding="utf-8"))
    changed = deepcopy(document)
    if mutation == "genome":
        changed["genome"]["window"] = 6
        message = "genome"
    elif mutation == "shape":
        changed["network"]["weights"][0].pop()
        message = "shape"
    else:
        changed["network"]["biases"][0][0] = float("nan")
        message = "finite"

    with pytest.raises(BundledRlModelError, match=message):
        _policy_from_document(changed)


def test_scheduler_uses_last_same_time_print_and_next_tick_causality() -> None:
    timestamps_us = [
        _wall_clock_us(9, 0, 50),
        _wall_clock_us(9, 1, 0),
        _wall_clock_us(9, 1, 0),
        _wall_clock_us(9, 1, 1),
        _wall_clock_us(9, 1, 10),
        _wall_clock_us(9, 1, 11),
    ]
    rows = build_bundled_rl_trace(
        timestamps_us=timestamps_us,
        prices=[100.0, 101.0, 102.0, 103.0, 104.0, 105.0],
    )

    assert [row.observed_session_tick_index for row in rows[:2]] == [2, 4]
    assert [row.eligible_from_session_tick for row in rows[:2]] == [3, 5]
    assert rows[0].decision_time_us == _wall_clock_us(9, 1, 0)
    assert rows[0].common_features[:4] == (1.0, 0.0, 0.0, 0.0)
    assert rows[1].common_features[:4] == pytest.approx((1.0, 2.0, 0.0, 0.0))
    assert tuple(branch.position_quantity for branch in rows[0].branches) == (0, 100)
    assert all(branch.target_quantity in (0, 100) for branch in rows[0].branches)


def test_scheduler_gap_keeps_only_latest_slot_without_using_future_mark() -> None:
    timestamps_us = [
        _wall_clock_us(9, 0, 50),
        _wall_clock_us(9, 1, 35),
        _wall_clock_us(9, 1, 36),
    ]
    first = build_bundled_rl_trace(
        timestamps_us=timestamps_us,
        prices=[100.0, 140.0, 141.0],
    )
    changed_future = build_bundled_rl_trace(
        timestamps_us=timestamps_us,
        prices=[100.0, 999.0, 141.0],
    )

    row = first[0]
    other = changed_future[0]
    assert len(first) == len(changed_future) == 1
    assert row.observed_session_tick_index == 1
    assert row.decision_time_us == _wall_clock_us(9, 1, 30)
    assert row.common_features[:4] == (0.0, 0.0, 0.0, 0.0)
    assert row.common_features == other.common_features
    assert row.branches == other.branches


def test_scheduler_resets_return_history_at_lunch() -> None:
    timestamps_us = [
        _wall_clock_us(11, 24, 19),
        _wall_clock_us(11, 24, 20),
        _wall_clock_us(11, 24, 21),
        _wall_clock_us(11, 24, 30),
        _wall_clock_us(11, 24, 31),
        _wall_clock_us(12, 30, 50),
        _wall_clock_us(12, 31, 0),
        _wall_clock_us(12, 31, 1),
    ]
    rows = build_bundled_rl_trace(
        timestamps_us=timestamps_us,
        prices=[100.0, 101.0, 102.0, 104.0, 105.0, 200.0, 210.0, 211.0],
    )

    afternoon = next(
        row for row in rows if row.decision_time_us == _wall_clock_us(12, 31, 0)
    )
    assert afternoon.common_features[:4] == (1.0, 0.0, 0.0, 0.0)


def test_scheduler_uses_three_cumulative_decision_mark_returns() -> None:
    timestamps_us = [
        _wall_clock_us(9, 1, 0),
        _wall_clock_us(9, 1, 1),
        _wall_clock_us(9, 1, 10),
        _wall_clock_us(9, 1, 11),
        _wall_clock_us(9, 1, 20),
        _wall_clock_us(9, 1, 21),
        _wall_clock_us(9, 1, 30),
        _wall_clock_us(9, 1, 31),
    ]
    prices = [100.1, 100.1, 100.3, 100.3, 100.6, 100.6, 101.0, 101.0]
    rows = build_bundled_rl_trace(timestamps_us=timestamps_us, prices=prices)

    latest = next(
        row for row in rows if row.decision_time_us == _wall_clock_us(9, 1, 30)
    )
    expected = (
        10_000 * (101.0 / 100.6 - 1.0) / 30,
        2.0,
        2.0,
    )
    assert latest.common_features[1:4] == pytest.approx(expected, abs=2e-6)


def test_scheduler_includes_both_half_session_final_slots() -> None:
    timestamps_us = [
        _wall_clock_us(11, 24, 29),
        _wall_clock_us(11, 24, 30),
        _wall_clock_us(11, 24, 31),
        _wall_clock_us(14, 49, 29),
        _wall_clock_us(14, 49, 30),
        _wall_clock_us(14, 49, 31),
    ]
    rows = build_bundled_rl_trace(
        timestamps_us=timestamps_us,
        prices=[100.0, 101.0, 102.0, 103.0, 104.0, 105.0],
    )
    decision_times = {row.decision_time_us for row in rows}

    assert _wall_clock_us(11, 24, 30) in decision_times
    assert _wall_clock_us(14, 49, 30) in decision_times
    for decision_time_us in decision_times:
        wall_time = datetime.fromtimestamp(decision_time_us / 1_000_000, tz=UTC).time()
        assert time(9, 1) <= wall_time <= time(11, 24, 30) or (
            time(12, 31) <= wall_time <= time(14, 49, 30)
        )


def test_scheduler_rejects_invalid_columns_and_omits_final_tick_barrier() -> None:
    with pytest.raises(BundledRlModelError, match="same length"):
        build_bundled_rl_trace(timestamps_us=[1], prices=[])
    with pytest.raises(BundledRlModelError, match="nondecreasing"):
        build_bundled_rl_trace(timestamps_us=[2, 1], prices=[1.0, 1.0])
    with pytest.raises(BundledRlModelError, match="finite positive"):
        build_bundled_rl_trace(timestamps_us=[1], prices=[float("nan")])

    rows = build_bundled_rl_trace(
        timestamps_us=[_wall_clock_us(9, 0, 50), _wall_clock_us(9, 1, 0)],
        prices=[100.0, 101.0],
    )
    assert rows == ()


def test_scheduler_is_host_timezone_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timestamps_us = [
        _wall_clock_us(9, 0, 50),
        _wall_clock_us(9, 1, 0),
        _wall_clock_us(9, 1, 1),
    ]
    baseline = build_bundled_rl_trace(
        timestamps_us=timestamps_us,
        prices=[100.0, 101.0, 102.0],
    )
    original_timezone = os.environ.get("TZ")
    try:
        monkeypatch.setenv("TZ", "America/New_York")
        if hasattr(os, "tzset"):
            os.tzset()
        changed = build_bundled_rl_trace(
            timestamps_us=timestamps_us,
            prices=[100.0, 101.0, 102.0],
        )
    finally:
        if original_timezone is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", original_timezone)
        if hasattr(os, "tzset"):
            os.tzset()

    assert changed == baseline


def test_module_loads_from_unrelated_cwd_without_moomoo(tmp_path: Path) -> None:
    command = [
        sys.executable,
        "-I",
        "-c",
        (
            "import sys; "
            f"sys.path.insert(0, {str(Path(__file__).parents[1] / 'src')!r}); "
            "from tickreplay.bundled_rl_inference import load_bundled_policy; "
            "print(load_bundled_policy().model_sha256)"
        ),
    ]
    completed = subprocess.run(
        command,
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        env={key: value for key, value in os.environ.items() if "MOOMOO" not in key},
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == MODEL_SHA256
    assert "/Users/sasac/_moomoo" not in completed.stderr


def test_position_branches_can_differ() -> None:
    rows = build_bundled_rl_trace(
        timestamps_us=[
            _wall_clock_us(9, 0, 50),
            _wall_clock_us(9, 1, 0),
            _wall_clock_us(9, 1, 1),
        ],
        prices=[100.0, 100.0, 100.0],
    )

    assert _branch(rows[0], 0).target_quantity == 0
    assert _branch(rows[0], 100).target_quantity == 100

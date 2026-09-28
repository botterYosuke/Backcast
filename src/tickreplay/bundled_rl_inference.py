"""Pinned inference and causal scheduling for the repository-owned RL policy.

This module is a child-process boundary. The FastAPI process must treat the model
and trace as opaque and must not import NumPy or this module.
"""

from __future__ import annotations

import hashlib
import json
import math
from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np

NUMPY_VERSION = "2.4.5"
MODEL_SIZE_BYTES = 11_932
MODEL_SHA256 = "528b9dc44928984c471731bb64ef82fa4a299a2ffd5707d1ce2a482223d28ef1"
PROVENANCE_SHA256 = "63a6c959bce0b5c8d374d7f973234824be8a6b3fdc0ae33765568664aff697ce"
MODEL_PATH = Path(__file__).with_name("models") / "rl_scalp_evolution_v1.json"
PROVENANCE_PATH = (
    Path(__file__).with_name("models") / "rl_scalp_evolution_v1.provenance.json"
)

MODEL_SPEC_ID = "rl_scalp_evolutionary_fqi/v1"
INPUT_WIDTH = 7
POSITION_QUANTITIES = (0, 100)
TARGET_QUANTITIES = frozenset(POSITION_QUANTITIES)
ACTION_MARGIN = np.float32(0.1)
DECISION_STEP = timedelta(seconds=10)
MICROSECONDS_PER_SECOND = 1_000_000
MAX_SAFE_INTEGER = 2**53 - 1
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
EXPECTED_GENOME = {
    "action_margin_bps": 1.0,
    "clock": True,
    "day": False,
    "depth": 2,
    "gamma": 0.98,
    "input_mode": "raw",
    "learning_rate": 0.001,
    "minute": False,
    "width": 16,
    "window": 3,
}
EXPECTED_WEIGHT_SHAPES = ((7, 16), (16, 16), (16, 2))
EXPECTED_BIAS_SHAPES = ((16,), (16,), (2,))
SESSION_WINDOWS = (
    (time(9, 1), time(11, 24, 30)),
    (time(12, 31), time(14, 49, 30)),
)


class BundledRlModelError(ValueError):
    """The bundled model, inference input, or session violates its contract."""


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """A model decision for one explicit portfolio state."""

    position_quantity: int
    target_quantity: int
    q_values: tuple[float, float]


@dataclass(frozen=True, slots=True)
class DecisionRow:
    """One causal decision trace row before transport serialization."""

    observed_session_tick_index: int
    eligible_from_session_tick: int
    decision_time_us: int
    common_features: tuple[float, float, float, float, float, float]
    branches: tuple[PolicyDecision, PolicyDecision]


@dataclass(frozen=True, slots=True)
class BundledRlPolicy:
    """Immutable float32 MLP loaded from the frozen checkpoint."""

    weights: tuple[np.ndarray, np.ndarray, np.ndarray]
    biases: tuple[np.ndarray, np.ndarray, np.ndarray]
    model_sha256: str = MODEL_SHA256

    def decide(
        self,
        common_features: Sequence[float],
        position_quantity: int,
    ) -> PolicyDecision:
        """Evaluate six common features plus an actual 0/100 position."""
        if position_quantity not in POSITION_QUANTITIES:
            raise BundledRlModelError("position quantity must be exactly 0 or 100")
        if len(common_features) != INPUT_WIDTH - 1:
            raise BundledRlModelError("policy requires six common features")
        encoded = (*common_features, position_quantity // 100)
        return self.decide_encoded(encoded)

    def decide_encoded(self, encoded_features: Sequence[float]) -> PolicyDecision:
        """Evaluate an already encoded seven-element oracle/test vector."""
        if len(encoded_features) != INPUT_WIDTH:
            raise BundledRlModelError("policy requires seven encoded features")
        try:
            values = np.asarray([encoded_features], dtype=np.float32)
        except (TypeError, ValueError, OverflowError) as error:
            raise BundledRlModelError(
                "encoded features must be finite numbers"
            ) from error
        if values.shape != (1, INPUT_WIDTH) or not np.isfinite(values).all():
            raise BundledRlModelError("encoded features must be finite numbers")
        position = float(values[0, -1])
        if position not in (0.0, 1.0):
            raise BundledRlModelError("encoded position must be exactly 0 or 1")

        activation = values
        for weights, bias in zip(self.weights[:-1], self.biases[:-1], strict=True):
            activation = np.tanh(activation @ weights + bias)
        output = activation @ self.weights[-1] + self.biases[-1]
        q_values = (float(output[0, 0]), float(output[0, 1]))
        target_quantity = 100 if output[0, 1] > output[0, 0] + ACTION_MARGIN else 0
        return PolicyDecision(int(position) * 100, target_quantity, q_values)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_verified_asset(
    path: Path, expected_sha256: str, *, size: int | None
) -> bytes:
    try:
        data = path.read_bytes()
    except OSError as error:
        raise BundledRlModelError(
            f"bundled asset is unreadable: {path.name}"
        ) from error
    if size is not None and len(data) != size:
        raise BundledRlModelError(f"bundled asset has wrong size: {path.name}")
    if _sha256(data) != expected_sha256:
        raise BundledRlModelError(f"bundled asset digest mismatch: {path.name}")
    return data


def verify_bundled_assets() -> None:
    """Fail closed unless dependency and both repository assets are exact."""
    if np.__version__ != NUMPY_VERSION:
        raise BundledRlModelError(
            f"NumPy {NUMPY_VERSION} is required; found {np.__version__}"
        )
    _read_verified_asset(MODEL_PATH, MODEL_SHA256, size=MODEL_SIZE_BYTES)
    provenance_bytes = _read_verified_asset(
        PROVENANCE_PATH,
        PROVENANCE_SHA256,
        size=None,
    )
    try:
        provenance = json.loads(provenance_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BundledRlModelError("bundled provenance is invalid JSON") from error
    if not isinstance(provenance, dict):
        raise BundledRlModelError("bundled provenance must be an object")
    if provenance.get("copyBasis") != "local_user_authorized":
        raise BundledRlModelError("bundled provenance has an invalid copy basis")
    if provenance.get("publicRedistributionRights") != "unconfirmed":
        raise BundledRlModelError("bundled provenance has an invalid rights status")


def _as_float32_array(value: Any, shape: tuple[int, ...], path: str) -> np.ndarray:
    try:
        result = np.asarray(value, dtype=np.float32)
    except (TypeError, ValueError, OverflowError) as error:
        raise BundledRlModelError(
            f"{path} must be a rectangular float array"
        ) from error
    if result.shape != shape:
        raise BundledRlModelError(f"{path} must have shape {shape}")
    if not np.isfinite(result).all():
        raise BundledRlModelError(f"{path} must contain only finite values")
    result.setflags(write=False)
    return result


def _policy_from_document(document: Any) -> BundledRlPolicy:
    if not isinstance(document, dict):
        raise BundledRlModelError("model document must be an object")
    if document.get("spec_id") != MODEL_SPEC_ID:
        raise BundledRlModelError("model spec is unsupported")
    if document.get("genome") != EXPECTED_GENOME:
        raise BundledRlModelError("model genome is unsupported")
    config = document.get("config")
    if not isinstance(config, dict) or config.get("qty") != 100:
        raise BundledRlModelError("model target quantity is unsupported")
    if config.get("step_seconds") != 10:
        raise BundledRlModelError("model decision step is unsupported")
    network = document.get("network")
    if not isinstance(network, dict) or set(network) != {"weights", "biases"}:
        raise BundledRlModelError("model network must contain weights and biases")
    weights_value = network["weights"]
    biases_value = network["biases"]
    if not isinstance(weights_value, list) or len(weights_value) != 3:
        raise BundledRlModelError("model must contain three weight layers")
    if not isinstance(biases_value, list) or len(biases_value) != 3:
        raise BundledRlModelError("model must contain three bias layers")
    weights = (
        _as_float32_array(
            weights_value[0], EXPECTED_WEIGHT_SHAPES[0], "network.weights[0]"
        ),
        _as_float32_array(
            weights_value[1], EXPECTED_WEIGHT_SHAPES[1], "network.weights[1]"
        ),
        _as_float32_array(
            weights_value[2], EXPECTED_WEIGHT_SHAPES[2], "network.weights[2]"
        ),
    )
    biases = (
        _as_float32_array(
            biases_value[0], EXPECTED_BIAS_SHAPES[0], "network.biases[0]"
        ),
        _as_float32_array(
            biases_value[1], EXPECTED_BIAS_SHAPES[1], "network.biases[1]"
        ),
        _as_float32_array(
            biases_value[2], EXPECTED_BIAS_SHAPES[2], "network.biases[2]"
        ),
    )
    return BundledRlPolicy(weights=weights, biases=biases)


@lru_cache(maxsize=1)
def load_bundled_policy() -> BundledRlPolicy:
    """Load and verify the only model supported by the fixed child runtime."""
    verify_bundled_assets()
    model_bytes = _read_verified_asset(
        MODEL_PATH,
        MODEL_SHA256,
        size=MODEL_SIZE_BYTES,
    )
    try:
        document = json.loads(model_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BundledRlModelError("bundled model is invalid JSON") from error
    return _policy_from_document(document)


def _validated_columns(
    timestamps_us: Sequence[int],
    prices: Sequence[float],
) -> tuple[tuple[int, ...], tuple[float, ...], date | None]:
    if len(timestamps_us) != len(prices):
        raise BundledRlModelError("timestamps_us and prices must have the same length")
    validated_timestamps: list[int] = []
    validated_prices: list[float] = []
    session_date: date | None = None
    for index, (timestamp_us, price) in enumerate(
        zip(timestamps_us, prices, strict=True)
    ):
        if (
            isinstance(timestamp_us, bool)
            or not isinstance(timestamp_us, int)
            or not 0 <= timestamp_us <= MAX_SAFE_INTEGER
        ):
            raise BundledRlModelError(
                f"timestamps_us[{index}] must be a nonnegative safe integer"
            )
        if validated_timestamps and timestamp_us < validated_timestamps[-1]:
            raise BundledRlModelError("timestamps_us must be nondecreasing")
        if isinstance(price, bool) or not isinstance(price, int | float):
            raise BundledRlModelError(
                f"prices[{index}] must be a finite positive number"
            )
        numeric_price = float(price)
        if not math.isfinite(numeric_price) or numeric_price <= 0:
            raise BundledRlModelError(
                f"prices[{index}] must be a finite positive number"
            )
        try:
            wall_clock = EPOCH + timedelta(microseconds=timestamp_us)
        except OverflowError as error:
            raise BundledRlModelError(
                f"timestamps_us[{index}] is out of range"
            ) from error
        if session_date is None:
            session_date = wall_clock.date()
        elif wall_clock.date() != session_date:
            raise BundledRlModelError(
                "timestamps_us must belong to one wall-clock date"
            )
        validated_timestamps.append(timestamp_us)
        validated_prices.append(numeric_price)
    return tuple(validated_timestamps), tuple(validated_prices), session_date


def _timestamp_us(value: datetime) -> int:
    return int((value - EPOCH).total_seconds()) * MICROSECONDS_PER_SECOND


def _normalize_return(newer: float, older: float) -> float:
    raw = np.float32(10_000.0 * (newer / older - 1.0))
    return float(np.clip(raw / np.float32(30.0), -2.0, 2.0))


def _common_features(
    timestamps_us: tuple[int, ...],
    prices: tuple[float, ...],
    slot_us: int,
    decision_marks: list[float],
) -> tuple[tuple[float, float, float, float, float, float], float] | None:
    mark_index = bisect_right(timestamps_us, slot_us) - 1
    if mark_index < 0:
        return None
    current_mark = prices[mark_index]
    prior_mark_index = (
        bisect_right(
            timestamps_us,
            slot_us - int(DECISION_STEP.total_seconds()) * MICROSECONDS_PER_SECOND,
        )
        - 1
    )
    tick_sign = 0.0
    if prior_mark_index >= 0:
        tick_sign = float(
            (current_mark > prices[prior_mark_index])
            - (current_mark < prices[prior_mark_index])
        )

    marks = [*decision_marks, current_mark]
    returns = [
        _normalize_return(current_mark, marks[-lag - 1]) if len(marks) > lag else 0.0
        for lag in range(1, 4)
    ]
    wall_clock = EPOCH + timedelta(microseconds=slot_us)
    seconds = wall_clock.hour * 3600 + wall_clock.minute * 60 + wall_clock.second
    angle = 2.0 * math.pi * seconds / 86_400.0
    staged = np.asarray(
        [tick_sign, *returns, math.sin(angle), math.cos(angle)],
        dtype=np.float32,
    )
    common_features = (
        float(staged[0]),
        float(staged[1]),
        float(staged[2]),
        float(staged[3]),
        float(staged[4]),
        float(staged[5]),
    )
    return common_features, current_mark


def build_bundled_rl_trace(
    *,
    timestamps_us: Sequence[int],
    prices: Sequence[float],
) -> tuple[DecisionRow, ...]:
    """Build sparse two-position rows with an exact next-tick eligibility index."""
    timestamps, validated_prices, session_date = _validated_columns(
        timestamps_us,
        prices,
    )
    if session_date is None:
        return ()
    policy = load_bundled_policy()
    rows_by_observed_index: dict[int, DecisionRow] = {}

    for window_start, window_end in SESSION_WINDOWS:
        decision_marks: list[float] = []
        slot = datetime.combine(session_date, window_start, tzinfo=UTC)
        final_slot = datetime.combine(session_date, window_end, tzinfo=UTC)
        while slot <= final_slot:
            slot_us = _timestamp_us(slot)
            barrier_start = bisect_left(timestamps, slot_us)
            if barrier_start >= len(timestamps):
                slot += DECISION_STEP
                continue
            barrier_timestamp = timestamps[barrier_start]
            observed_index = bisect_right(timestamps, barrier_timestamp) - 1
            if observed_index == len(timestamps) - 1:
                slot += DECISION_STEP
                continue
            prepared = _common_features(
                timestamps,
                validated_prices,
                slot_us,
                decision_marks,
            )
            if prepared is None:
                slot += DECISION_STEP
                continue
            common_features, current_mark = prepared
            decision_marks = [*decision_marks[-2:], current_mark]
            branches = (
                policy.decide(common_features, POSITION_QUANTITIES[0]),
                policy.decide(common_features, POSITION_QUANTITIES[1]),
            )
            rows_by_observed_index[observed_index] = DecisionRow(
                observed_session_tick_index=observed_index,
                eligible_from_session_tick=observed_index + 1,
                decision_time_us=slot_us,
                common_features=common_features,
                branches=branches,
            )
            slot += DECISION_STEP

    return tuple(
        rows_by_observed_index[index] for index in sorted(rows_by_observed_index)
    )

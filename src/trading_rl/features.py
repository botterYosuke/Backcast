"""Feature engineering for the post-open trading window.

Computes strictly causal (t' <= t) microstructure features from OpeningSessionData
or raw tick arrays with zero lookahead bias.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from .data import OpeningSessionData

OPENING_WINDOW_US: int = 900_000_000  # default 15 minutes in microseconds
OPENING_DELAY_NORM_SECONDS: float = 900.0  # opening-delay normalization (fixed)


@dataclass(frozen=True)
class FeatureConfig:
    """Configuration for microstructure feature calculation."""

    return_windows: tuple[int, ...] = (5, 20, 100)
    range_window: int = 20
    vwap_window: int = 20
    volume_window: int = 20
    include_cumulative: bool = True
    epsilon: float = 1e-5
    window_seconds: float = 900.0

    @property
    def window_us(self) -> int:
        return int(round(float(self.window_seconds) * 1_000_000))


def compute_opening_delay_seconds(
    t_open_us: int,
    session_date: str | None,
) -> float:
    """Compute opening delay in seconds: max(0, T_open - 09:00:00 JST).

    In TSE, standard opening is 09:00:00 JST. If a stock has special quotes
    (特別気配), T_open is delayed (e.g. 09:03:00 -> 180s delay).
    """
    if not session_date:
        return 0.0

    try:
        dt_0900 = datetime.fromisoformat(f"{session_date} 09:00:00").replace(tzinfo=UTC)
        t_0900_us = int(dt_0900.timestamp() * 1_000_000)
        delay_us = t_open_us - t_0900_us
        return max(0.0, float(delay_us) / 1_000_000.0)
    except Exception:
        return 0.0


def extract_features(
    session: OpeningSessionData,
    config: FeatureConfig | None = None,
) -> np.ndarray:
    """Extract causal 2D feature matrix (N_ticks, K_features) from an OpeningSessionData.

    Strictly causal: row i depends only on ticks 0..i.
    """
    if config is None:
        config = FeatureConfig()

    return compute_microstructure_features(
        epoch_us=session.epoch_us,
        price=session.price,
        qty=session.qty,
        trade_type=session.trade_type,
        t_open_us=session.t_open_us,
        p_open=session.p_open,
        session_date=session.date,
        config=config,
    )


def compute_microstructure_features(
    epoch_us: np.ndarray,
    price: np.ndarray,
    qty: np.ndarray,
    trade_type: np.ndarray | None = None,
    *,
    t_open_us: int | None = None,
    p_open: float | None = None,
    session_date: str | None = None,
    config: FeatureConfig | None = None,
) -> np.ndarray:
    """Compute causal microstructure features from raw tick arrays.

    Returns:
        np.ndarray: float32 matrix of shape (N, K).
    """
    if config is None:
        config = FeatureConfig()

    n = len(price)
    if n == 0:
        names = get_feature_names(config)
        return np.empty((0, len(names)), dtype=np.float32)

    price_f64 = price.astype(np.float64, copy=False)
    qty_f64 = qty.astype(np.float64, copy=False)

    p_open_val = float(p_open if p_open is not None else price_f64[0])
    if p_open_val <= 0:
        p_open_val = float(price_f64[0]) if price_f64[0] > 0 else 1.0

    t_open_val = int(t_open_us if t_open_us is not None else epoch_us[0])

    features: list[np.ndarray] = []

    # 1. Normalized elapsed time tau in [0, 1] over the configured window
    tau = (epoch_us.astype(np.float64) - float(t_open_val)) / float(
        max(1, config.window_us)
    )
    tau = np.clip(tau, 0.0, 1.0)
    features.append(tau)

    # 2. Opening delay in seconds & normalized
    delay_s = compute_opening_delay_seconds(t_open_val, session_date)
    delay_arr = np.full(n, delay_s / OPENING_DELAY_NORM_SECONDS, dtype=np.float64)
    features.append(delay_arr)

    # 3. Relative return from open: (P_t - P_open) / P_open
    ret_from_open = (price_f64 - p_open_val) / p_open_val
    features.append(ret_from_open)

    # 4. Cumulative session High/Low distance: (P_t - Low_t) / (High_t - Low_t + eps)
    if config.include_cumulative:
        cum_high = np.maximum.accumulate(price_f64)
        cum_low = np.minimum.accumulate(price_f64)
        cum_range = cum_high - cum_low
        cum_pos_in_range = np.where(
            cum_range > config.epsilon,
            (price_f64 - cum_low) / (cum_range + config.epsilon),
            0.5,
        )
        features.append(cum_pos_in_range)

    # 5. Rolling High/Low distance over range_window
    w_range = max(1, config.range_window)
    pad_price = np.pad(price_f64, (w_range - 1, 0), mode="edge")
    windows = np.lib.stride_tricks.sliding_window_view(pad_price, window_shape=w_range)
    roll_high = np.max(windows, axis=-1)
    roll_low = np.min(windows, axis=-1)
    roll_range = roll_high - roll_low
    roll_pos_in_range = np.where(
        roll_range > config.epsilon,
        (price_f64 - roll_low) / (roll_range + config.epsilon),
        0.5,
    )
    features.append(roll_pos_in_range)

    # 6. Rolling log returns over configured windows
    safe_price = np.maximum(price_f64, config.epsilon)
    log_price = np.log(safe_price)
    for w in config.return_windows:
        w_ret = max(1, w)
        pad_log_price = np.pad(log_price, (w_ret, 0), mode="edge")
        log_ret = log_price - pad_log_price[:n]
        features.append(log_ret)

    # 7. VWAP deviations:
    # Cumulative VWAP from open
    pv = price_f64 * qty_f64
    cum_pv = np.cumsum(pv)
    cum_v = np.cumsum(qty_f64)
    cum_vwap = np.where(cum_v > 0, cum_pv / np.maximum(cum_v, 1.0), price_f64)
    cum_vwap_dev = (price_f64 - cum_vwap) / np.maximum(cum_vwap, config.epsilon)
    features.append(cum_vwap_dev)

    # Rolling VWAP over vwap_window
    w_vwap = max(1, config.vwap_window)
    pad_cum_pv = np.pad(cum_pv, (w_vwap, 0), mode="constant", constant_values=0.0)
    pad_cum_v = np.pad(cum_v, (w_vwap, 0), mode="constant", constant_values=0.0)
    roll_pv = cum_pv - pad_cum_pv[:n]
    roll_v = cum_v - pad_cum_v[:n]
    roll_vwap = np.where(roll_v > 0, roll_pv / np.maximum(roll_v, 1.0), price_f64)
    roll_vwap_dev = (price_f64 - roll_vwap) / np.maximum(roll_vwap, config.epsilon)
    features.append(roll_vwap_dev)

    # 8. Rolling volume intensity: log(1 + rolling_volume) and normalized rate
    w_vol = max(1, config.volume_window)
    pad_cum_v_for_vol = np.pad(cum_v, (w_vol, 0), mode="constant", constant_values=0.0)
    roll_vol = cum_v - pad_cum_v_for_vol[:n]
    log_roll_vol = np.log1p(roll_vol)
    features.append(log_roll_vol)

    # Volume rate: volume per second in the window
    pad_epoch = np.pad(epoch_us.astype(np.float64), (w_vol, 0), mode="edge")
    time_elapsed_s = np.maximum(
        (epoch_us.astype(np.float64) - pad_epoch[:n]) / 1_000_000.0, 0.05
    )
    vol_per_sec = roll_vol / time_elapsed_s
    features.append(np.log1p(vol_per_sec))

    # 9. Tick-rule aggressor skew: sign of the price change (zero changes carry
    # the last non-zero sign forward, first tick is 0), volume-weighted over the
    # trailing volume_window ticks. Strictly causal. The raw ``Type`` column is the
    # trading SESSION (1=morning, 2=afternoon), NOT the trade side, so it is unused.
    diff = np.zeros(n, dtype=np.float64)
    diff[1:] = np.diff(price_f64)
    raw_sign = np.sign(diff)
    last_nonzero_idx = np.maximum.accumulate(
        np.where(raw_sign != 0.0, np.arange(n), 0)
    )
    tick_sign = raw_sign[last_nonzero_idx]
    signed_qty = tick_sign * qty_f64
    cum_signed = np.cumsum(signed_qty)
    pad_cum_signed = np.pad(cum_signed, (w_vol, 0), mode="constant", constant_values=0.0)
    roll_signed = cum_signed - pad_cum_signed[:n]
    skew = np.where(roll_vol > 0, roll_signed / np.maximum(roll_vol, 1.0), 0.0)
    features.append(np.clip(skew, -1.0, 1.0))

    # Stack into 2D array (N, K)
    matrix = np.column_stack(features).astype(np.float32)

    # Sanitize any NaNs or Infs
    np.nan_to_num(matrix, copy=False, nan=0.0, posinf=1.0, neginf=-1.0)
    return matrix


def get_feature_names(config: FeatureConfig | None = None) -> list[str]:
    """Return the list of feature column names for the given config."""
    if config is None:
        config = FeatureConfig()

    names = [
        "tau",
        "opening_delay_norm",
        "return_from_open",
    ]
    if config.include_cumulative:
        names.append("cum_pos_in_range")

    names.append(f"roll_pos_in_range_{config.range_window}")

    for w in config.return_windows:
        names.append(f"log_ret_{w}")

    names.extend(
        [
            "cum_vwap_dev",
            f"roll_vwap_dev_{config.vwap_window}",
            f"log_roll_vol_{config.volume_window}",
            f"log_vol_rate_{config.volume_window}",
            f"tick_rule_skew_{config.volume_window}",
        ]
    )
    return names

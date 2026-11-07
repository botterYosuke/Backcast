"""Unit tests for opening 15-minute microstructure feature engineering (trading_rl.features)."""

from __future__ import annotations

import numpy as np
import pytest

from trading_rl.data import OpeningSessionData
from trading_rl.env import TickTradingEnv
from trading_rl.features import (
    FeatureConfig,
    compute_microstructure_features,
    compute_opening_delay_seconds,
    extract_features,
    get_feature_names,
)


def _make_mock_session(
    prices: list[float],
    qtys: list[int] | None = None,
    trade_types: list[str] | None = None,
    *,
    date: str = "2026-08-14",
    t_open_us: int = 1786698000000000,  # 2026-08-14 09:00:00 UTC
    interval_us: int = 1_000_000,
) -> OpeningSessionData:
    n = len(prices)
    if qtys is None:
        qtys = [100] * n
    if trade_types is None:
        trade_types = ["1"] * n
    epoch_us = np.array([t_open_us + i * interval_us for i in range(n)], dtype=np.int64)
    price_arr = np.array(prices, dtype=np.float64)
    qty_arr = np.array(qtys, dtype=np.int64)
    type_arr = np.array(trade_types, dtype=object)

    return OpeningSessionData(
        stem="7203",
        code="72030",
        date=date,
        t_open_us=t_open_us,
        p_open=float(prices[0]) if prices else 100.0,
        v_open=qtys[0] if qtys else 0,
        epoch_us=epoch_us,
        price=price_arr,
        qty=qty_arr,
        trade_type=type_arr,
    )


def test_extract_features_shape_and_names():
    """Verify feature matrix shape matches feature names length."""
    session = _make_mock_session([100.0, 101.0, 102.0, 100.5, 103.0])
    config = FeatureConfig(return_windows=(2, 3), range_window=3, vwap_window=3)
    feat = extract_features(session, config)
    names = get_feature_names(config)

    assert feat.shape == (5, len(names))
    assert feat.dtype == np.float32
    assert not np.isnan(feat).any()
    assert not np.isinf(feat).any()


def test_strictly_causal_zero_lookahead():
    """Modifying future tick t+1 must have zero effect on features at 0..t."""
    prices_orig = [100.0, 101.0, 102.0, 103.0, 104.0]
    prices_mod = [100.0, 101.0, 102.0, 999.0, 0.1]

    session_orig = _make_mock_session(prices_orig)
    session_mod = _make_mock_session(prices_mod)

    feat_orig = extract_features(session_orig)
    feat_mod = extract_features(session_mod)

    # First 3 steps (indices 0, 1, 2) must be bitwise identical
    np.testing.assert_allclose(feat_orig[:3], feat_mod[:3], rtol=1e-6, atol=1e-6)

    # Step 3 and 4 should differ
    assert not np.allclose(feat_orig[3:], feat_mod[3:])


def test_opening_delay_calculation():
    """Verify opening delay from 09:00:00 JST."""
    # 2026-08-14 09:00:00 UTC
    t_0900 = 1786698000000000

    # Case 1: opened right at 09:00:00
    delay_0 = compute_opening_delay_seconds(t_0900, "2026-08-14")
    assert delay_0 == 0.0

    # Case 2: opened at 09:03:00 (180s delayed special quotes)
    t_0903 = t_0900 + 180 * 1_000_000
    delay_180 = compute_opening_delay_seconds(t_0903, "2026-08-14")
    assert abs(delay_180 - 180.0) < 1e-3

    # Case 3: invalid date gracefully returns 0.0
    assert compute_opening_delay_seconds(t_0903, "invalid-date") == 0.0


def test_extreme_and_edge_cases():
    """Verify stability on empty, single-element, and flat price series."""
    # Empty
    empty_feat = compute_microstructure_features(
        epoch_us=np.empty(0, dtype=np.int64),
        price=np.empty(0, dtype=np.float64),
        qty=np.empty(0, dtype=np.int64),
    )
    names = get_feature_names()
    assert empty_feat.shape == (0, len(names))

    # Single tick
    single_session = _make_mock_session([100.0])
    single_feat = extract_features(single_session)
    assert single_feat.shape == (1, len(names))
    assert not np.isnan(single_feat).any()
    assert not np.isinf(single_feat).any()

    # Flat price series with zero quantity
    flat_session = _make_mock_session([100.0] * 20, qtys=[0] * 20)
    flat_feat = extract_features(flat_session)
    assert flat_feat.shape == (20, len(names))
    assert not np.isnan(flat_feat).any()
    assert not np.isinf(flat_feat).any()


def test_env_integration_with_features():
    """Verify TickTradingEnv operates with precomputed and auto-computed features."""
    session = _make_mock_session([100.0, 101.0, 102.0, 101.5, 103.0])

    # 1. Custom precomputed features passed explicitly
    feat = extract_features(session)
    # warmup=0: the 1s-spaced mock ticks all fall inside the default 15s warmup,
    # so reset() would start at the last tick; here we test row alignment at tick 0.
    env_custom = TickTradingEnv(
        session, extra_features=feat, opening_warmup_seconds=0.0
    )
    obs, info = env_custom.reset()
    assert obs.shape == (5 + feat.shape[1],)
    # Check extra feature slice matches precomputed row 0
    np.testing.assert_allclose(obs[5:], feat[0], rtol=1e-5)

    # 2. Auto features flag in env
    env_auto = TickTradingEnv(
        session, auto_features=True, opening_warmup_seconds=0.0
    )
    obs_auto, _ = env_auto.reset()
    assert obs_auto.shape == (5 + feat.shape[1],)
    np.testing.assert_allclose(obs_auto, obs, rtol=1e-5)


def _skew_column(feat: np.ndarray, config: FeatureConfig) -> np.ndarray:
    names = get_feature_names(config)
    return feat[:, names.index(f"tick_rule_skew_{config.volume_window}")]


def test_tick_rule_skew_is_not_constant_and_uses_price_changes():
    """Skew follows the tick rule; the session-flag Type column is ignored."""
    config = FeatureConfig(volume_window=3)
    names = get_feature_names(config)
    assert "tick_rule_skew_3" in names
    assert "trade_aggressor_skew" not in names

    # up, up, flat (carries +), down, down, flat (carries -)
    prices = [100.0, 101.0, 102.0, 102.0, 101.0, 100.0, 100.0]
    session = _make_mock_session(prices, qtys=[10, 10, 10, 10, 10, 10, 10])
    skew = _skew_column(extract_features(session, config), config)

    assert skew[0] == 0.0  # first tick has no sign
    assert skew[1] == pytest.approx(0.5)  # signed (0 + 10) / total (10 + 10)
    assert np.all(np.abs(skew) <= 1.0)
    assert skew[2] > 0 and skew[3] > 0  # rising then flat-carry: still buying
    assert skew[-1] < 0  # falling then flat-carry: selling
    assert len(np.unique(np.round(skew, 6))) > 2  # not constant

    # Changing the Type column (session flag) must not change anything
    session_b = _make_mock_session(
        prices, qtys=[10] * 7, trade_types=["2"] * len(prices)
    )
    np.testing.assert_array_equal(
        extract_features(session, config), extract_features(session_b, config)
    )


def test_tick_rule_skew_value_and_zero_carry_forward():
    config = FeatureConfig(volume_window=4)
    # signs: 0, +, 0->carry +, - ; qty 10, 20, 30, 40
    prices = [100.0, 101.0, 101.0, 100.0]
    session = _make_mock_session(prices, qtys=[10, 20, 30, 40])
    skew = _skew_column(extract_features(session, config), config)
    # window of 4 ticks covers all: signed = 0*10 + 20 + 30 - 40 = 10 ; total = 100
    assert skew[3] == pytest.approx(10 / 100)
    # tick 2: signed = 0 + 20 + 30 = 50 ; total = 60
    assert skew[2] == pytest.approx(50 / 60)


def test_tick_rule_skew_is_causal():
    config = FeatureConfig(volume_window=3)
    base = [100.0, 101.0, 100.0, 102.0, 103.0]
    changed = [100.0, 101.0, 100.0, 50.0, 10.0]
    a = _skew_column(extract_features(_make_mock_session(base), config), config)
    b = _skew_column(extract_features(_make_mock_session(changed), config), config)
    np.testing.assert_array_equal(a[:3], b[:3])


def test_feature_count_and_window_config():
    config = FeatureConfig()
    assert len(get_feature_names(config)) == 13
    session = _make_mock_session(
        [100.0, 101.0, 102.0], date="2026-08-14", interval_us=300_000_000
    )
    # tau = elapsed / window, clipped to [0, 1]
    feat_600 = extract_features(session, FeatureConfig(window_seconds=600.0))
    feat_900 = extract_features(session, FeatureConfig())
    tau_idx = get_feature_names().index("tau")
    np.testing.assert_allclose(feat_600[:, tau_idx], [0.0, 0.5, 1.0], atol=1e-6)
    np.testing.assert_allclose(feat_900[:, tau_idx], [0.0, 1 / 3, 2 / 3], atol=1e-6)

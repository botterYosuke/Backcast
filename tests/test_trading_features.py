"""Unit tests for opening 15-minute microstructure feature engineering (trading_rl.features)."""

from __future__ import annotations

import numpy as np

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
    env_custom = TickTradingEnv(session, extra_features=feat)
    obs, info = env_custom.reset()
    assert obs.shape == (5 + feat.shape[1],)
    # Check extra feature slice matches precomputed row 0
    np.testing.assert_allclose(obs[5:], feat[0], rtol=1e-5)

    # 2. Auto features flag in env
    env_auto = TickTradingEnv(session, auto_features=True)
    obs_auto, _ = env_auto.reset()
    assert obs_auto.shape == (5 + feat.shape[1],)
    np.testing.assert_allclose(obs_auto, obs, rtol=1e-5)

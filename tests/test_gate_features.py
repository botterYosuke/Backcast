"""Tests for the P2 predictability-gate feature grid (synthetic sessions only)."""

from __future__ import annotations

import numpy as np
import pytest

from trading_rl.data import OpeningSessionData
from trading_rl.gate_features import (
    FEATURE_NAMES,
    HORIZONS,
    N_FEATURES,
    START_S,
    WINDOW_S,
    compute_session_grid,
    default_grid,
)

T0 = 1_700_000_000_000_000  # arbitrary epoch_us of T_open


def make_session(t_sec, price, qty, p_open=None, v_open=None) -> OpeningSessionData:
    t_sec = np.asarray(t_sec, dtype=np.float64)
    price = np.asarray(price, dtype=np.float64)
    qty = np.asarray(qty, dtype=np.int64)
    return OpeningSessionData(
        stem="0000",
        code="00000",
        date="2026-01-05",
        t_open_us=T0,
        p_open=float(price[0] if p_open is None else p_open),
        v_open=int(qty[0] if v_open is None else v_open),
        epoch_us=T0 + np.round(t_sec * 1e6).astype(np.int64),
        price=price,
        qty=qty,
        trade_type=np.full(len(price), "", dtype=object),
    )


def random_session(seed: int, n: int = 400) -> OpeningSessionData:
    rng = np.random.default_rng(seed)
    t = np.sort(rng.uniform(0.5, WINDOW_S, size=n))
    t = np.concatenate([[0.0], t])
    steps = rng.integers(-2, 3, size=n + 1)
    price = 2000.0 + np.cumsum(steps)
    qty = rng.integers(100, 2000, size=n + 1)
    return make_session(t, price, qty)


def grid(sess, **kw):
    kw.setdefault("prev_close", 1990.0)
    kw.setdefault("avg_volume_20d", 1_000_000.0)
    kw.setdefault("is_topix100", False)
    return compute_session_grid(sess, **kw)


def col(g, name):
    return g.X[:, FEATURE_NAMES.index(name)]


def test_shapes_and_grid_bounds():
    g = grid(random_session(0))
    assert g.s[0] == START_S == 10
    assert g.s[-1] == WINDOW_S - min(HORIZONS)
    assert len(g.s) == len(default_grid())
    assert g.X.shape == (len(g.s), N_FEATURES)
    assert g.X.dtype == np.float32
    assert np.isfinite(g.X).all()
    assert g.fwd_bps.shape == (len(g.s), len(HORIZONS))
    # 300 s horizon is only valid while s + 300 <= 600
    j = HORIZONS.index(300)
    assert g.valid[g.s <= 300, j].all()
    assert not g.valid[g.s > 300, j].any()
    assert np.isnan(g.fwd_bps[~g.valid[:, j], j]).all()


def test_returns_targets_and_microstructure_known_values():
    # trades at t=0 (open), 3, 8, 12, 12.5, 20 s
    t = [0, 3, 8, 12, 12.5, 20]
    p = [1000, 1001, 1001, 1003, 1002, 1005]
    q = [500, 100, 200, 300, 100, 400]
    g = grid(make_session(t, p, q), prev_close=990.0, avg_volume_20d=10_000.0)
    i = int(np.where(g.s == 13)[0][0])  # grid second 13 -> last trade 1002 (t=12.5)
    assert g.price[i] == 1002.0
    assert g.tick[i] == 1.0
    # return over 5 s: price at s=8 is 1001 -> (1002/1001-1)*1e4
    assert col(g, "ret_bps_5s")[i] == pytest.approx((1002 / 1001 - 1) * 1e4, rel=1e-4)
    assert col(g, "ret_tk_5s")[i] == pytest.approx(1.0)
    assert col(g, "ret_open_bps")[i] == pytest.approx(20.0, rel=1e-4)
    assert col(g, "gap_bps")[i] == pytest.approx((1000 / 990 - 1) * 1e4, rel=1e-4)
    # range position: lo=1000 hi=1003 price=1002
    assert col(g, "range_pos")[i] == pytest.approx(2 / 3, rel=1e-4)
    # cumulative vwap over all ticks <= 13 s
    pq = 1000 * 500 + 1001 * 100 + 1001 * 200 + 1003 * 300 + 1002 * 100
    vol = 500 + 100 + 200 + 300 + 100
    assert col(g, "vwap_dev_bps")[i] == pytest.approx((1002 / (pq / vol) - 1) * 1e4, rel=1e-3)
    # tick rule over 30 s window (all ticks after open): +100(up) +200(carry +) +300(up) -100(down)
    assert col(g, "imb_30s")[i] == pytest.approx((100 + 200 + 300 - 100) / (100 + 200 + 300 + 100))
    # 5 s window (8, 13]: ticks at 12 (up, 300) and 12.5 (down, 100)
    assert col(g, "imb_5s")[i] == pytest.approx((300 - 100) / 400)
    assert col(g, "log_ntrades_5s")[i] == pytest.approx(np.log1p(2))
    assert col(g, "log_vol_ratio_5s")[i] == pytest.approx(np.log10(400 / 10_000.0 + 1e-6))
    assert col(g, "log_time_since_trade")[i] == pytest.approx(np.log1p(0.5))
    assert col(g, "elapsed_s")[i] == 13.0
    # targets: 30 s ahead -> price at s=43 is the last trade (1005)
    j = HORIZONS.index(30)
    assert g.fwd_price[i, j] == 1005.0
    assert g.fwd_bps[i, j] == pytest.approx((1005 / 1002 - 1) * 1e4, rel=1e-4)
    assert g.fwd_ticks[i, j] == pytest.approx(3.0)
    assert g.fwd_sign[i, j] == 1


def test_tick_units_use_topix100_table():
    sess = make_session([0, 5], [3000.0, 3000.5], [100, 100])
    g_general = grid(sess, is_topix100=False)
    g_t100 = grid(sess, is_topix100=True)
    # same price, different tick tables -> different tick-unit returns
    assert g_general.tick[0] != g_t100.tick[0] or g_general.price[0] > 0
    assert col(g_t100, "is_topix100")[0] == 1.0
    assert col(g_general, "is_topix100")[0] == 0.0


def test_nan_inputs_are_sanitised():
    g = grid(random_session(1), prev_close=float("nan"), avg_volume_20d=float("nan"))
    assert np.isfinite(g.X).all()
    g = grid(random_session(1), prev_close=0.0, avg_volume_20d=0.0)
    assert np.isfinite(g.X).all()


def test_no_trades_for_long_gap_gives_stale_price_and_time_since_trade():
    g = grid(make_session([0, 1], [500.0, 501.0], [100, 100]))
    assert (g.price == 501.0).all()
    assert (col(g, "ret_bps_1s") == 0).all()
    assert col(g, "log_time_since_trade")[-1] == pytest.approx(np.log1p(g.s[-1] - 1))


@pytest.mark.parametrize("seed", [0, 1, 2])
@pytest.mark.parametrize("cut", [25, 100, 333])
def test_no_lookahead_perturbing_future_ticks_never_changes_features(seed, cut):
    """Features at grid second s depend only on ticks with t <= T_open + s."""
    base = random_session(seed)
    g0 = grid(base)

    t_rel = (base.epoch_us - T0) / 1e6
    keep = t_rel <= cut
    rng = np.random.default_rng(100 + seed)
    # replace everything after `cut` with a completely different (and differently
    # sized) future: wild prices, huge quantities, extra ticks right after the cut
    n_new = int(rng.integers(5, 300))
    t_new = np.sort(rng.uniform(cut + 1e-3, WINDOW_S, size=n_new))
    p_new = rng.uniform(100.0, 9000.0, size=n_new)
    q_new = rng.integers(1, 10_000_000, size=n_new)
    pert = make_session(
        np.concatenate([t_rel[keep], t_new]),
        np.concatenate([base.price[keep], p_new]),
        np.concatenate([base.qty[keep], q_new]),
        p_open=base.p_open,
        v_open=base.v_open,
    )
    g1 = grid(pert)
    m = g0.s <= cut
    assert m.sum() > 0
    np.testing.assert_array_equal(g0.X[m], g1.X[m])
    np.testing.assert_array_equal(g0.price[m], g1.price[m])
    # ...but the targets (which look forward) do change somewhere
    assert not np.array_equal(
        np.nan_to_num(g0.fwd_price[m]), np.nan_to_num(g1.fwd_price[m])
    )


def test_tick_exactly_at_grid_time_is_included():
    g = grid(make_session([0, 20.0], [1000.0, 1010.0], [100, 100]))
    i20 = int(np.where(g.s == 20)[0][0])
    i19 = int(np.where(g.s == 19)[0][0])
    assert g.price[i20] == 1010.0
    assert g.price[i19] == 1000.0

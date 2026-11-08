"""ScalpEnv: queue_est fill rule, sequential cancel, per-session window, fill model."""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from trading_rl import fill_model as fm
from trading_rl.data import OpeningSessionData
from trading_rl.scalp_env import (
    US,
    ScalpConfig,
    ScalpEnv,
    ScalpSession,
    build_scalp_session,
    morning_window_s,
)

T_OPEN = int(np.datetime64("2026-08-14T09:00:00", "us").astype(np.int64))
KEEP, CANCEL, FLAT = 0, 1, 8
BID0, BID1 = 2, 3
ASK0, ASK1 = 5, 6


def make_raw(ticks, t_open=T_OPEN) -> OpeningSessionData:
    ticks = sorted(ticks)
    epoch = np.array([t_open + round(t * US) for t, *_ in ticks], dtype=np.int64)
    price = np.array([x[1] for x in ticks], dtype=np.float64)
    qty = np.array([x[2] if len(x) > 2 else 100 for x in ticks], dtype=np.int64)
    return OpeningSessionData(
        stem="1234", code="12340", date="2026-08-14", t_open_us=t_open,
        p_open=float(price[0]), v_open=1000, epoch_us=epoch, price=price, qty=qty,
        trade_type=np.full(len(price), "", dtype=object),
    )


def make_session(ticks, *, window_s=600, pmkt=0.0, qhat=0.0) -> ScalpSession:
    sess = build_scalp_session(
        make_raw(ticks), prev_close=100.0, avg_volume_20d=1e6, is_topix100=False,
        window_s=window_s,
    )
    n = window_s - 10
    return dataclasses.replace(
        sess,
        fill_pmkt=np.full((n, 2, 3), pmkt, dtype=np.float32),
        fill_qhat=np.full((n, 2, 3), qhat, dtype=np.float32),
    )


def run(sess, actions, *, seed=0, **cfg):
    env = ScalpEnv(config=ScalpConfig(**cfg), seed=seed)
    env.reset(options={"session": sess})
    done, s, info = False, sess.start_s, {}
    while not done:
        _, _, done, _, info = env.step(actions.get(s, KEEP))
        s += 1
    return env, info["episode"]


BASE = [(0.0, 100.0), (9.0, 100.0)]


def test_queue_fills_after_queue_ahead_is_consumed():
    # bid_k0 at 100 behind 200 shares: needs 300 shares printed at 100
    ticks = BASE + [(11.0, 100.0), (11.5, 100.0), (12.0, 100.0), (30.0, 101.0)]
    sess = make_session(ticks, qhat=200.0)
    env, ep = run(sess, {10: BID0}, fill_rule="queue_est")
    entries = [f for f in env.fills if f["kind"] == "entry"]
    assert len(entries) == 1 and entries[0]["us"] == 12 * US and entries[0]["price"] == 100.0
    # same tape: trade_through never fills (no print < 100), touch fills at the first print
    env_tt, _ = run(sess, {10: BID0}, fill_rule="trade_through")
    assert not env_tt.fills
    env_to, _ = run(sess, {10: BID0}, fill_rule="touch")
    assert env_to.fills[0]["us"] == 11 * US


def test_queue_consumption_carries_across_steps():
    ticks = BASE + [(15.0, 100.0), (20.0, 100.0), (25.0, 100.0)]
    sess = make_session(ticks, qhat=200.0)
    env, _ = run(sess, {10: BID0}, fill_rule="queue_est")
    assert env.fills[0]["us"] == 25 * US


def test_queue_print_through_fills_regardless_of_queue():
    ticks = BASE + [(11.0, 99.0)]
    sess = make_session(ticks, qhat=1e9)
    env, _ = run(sess, {10: BID0}, fill_rule="queue_est")
    assert env.fills[0]["us"] == 11 * US and env.fills[0]["price"] == 100.0


def test_marketable_draw_fills_at_activation():
    sess = make_session(BASE + [(40.0, 100.0)], pmkt=1.0, qhat=1e9)
    env, ep = run(sess, {10: BID0, 20: ASK0}, fill_rule="queue_est", latency_ms=200)
    assert [f["us"] for f in env.fills] == [10 * US + 200_000, 20 * US + 200_000]
    assert ep["n_limit_marketable"] == 2 and ep["n_trips"] == 1


def test_queue_needs_fill_model_arrays():
    sess = build_scalp_session(make_raw(BASE), prev_close=100.0, avg_volume_20d=1e6,
                               is_topix100=False)
    env = ScalpEnv(config=ScalpConfig(fill_rule="queue_est"))
    with pytest.raises(ValueError, match="fill_model"):
        env.reset(options={"session": sess})


def test_sequential_cancel_delays_the_replacement():
    # bid 100 placed at 10; replaced by bid 99 at 12; a print at 98 at 12.3 s
    ticks = BASE + [(12.3, 98.0), (40.0, 98.0)]
    sess = make_session(ticks)
    acts = {10: BID0, 12: BID1}
    env, _ = run(sess, acts, fill_rule="trade_through", latency_ms=200)
    # no sequential cancel: the new bid 99 is live at 12.2 -> fills at 12.3
    assert env.fills[0]["us"] == int(12.3 * US) and env.fills[0]["price"] == 99.0
    env, _ = run(sess, acts, fill_rule="trade_through", latency_ms=200, sequential_cancel=True)
    # sequential: old bid gone at 12.2, new bid only from 12.4 -> the 12.3 print misses
    # both; the bid 99 fills on the next print through it (40 s)
    assert env.fills[0]["us"] == 40 * US and env.fills[0]["price"] == 99.0


def test_sequential_cancel_market_flatten_waits_for_cancel():
    # long at 10.5 via marketable bid; ask exit working; flatten at 20 -> market at 20.4
    ticks = BASE + [(20.3, 105.0), (60.0, 105.0)]
    sess = make_session(ticks, pmkt=1.0)
    sess = dataclasses.replace(sess, fill_pmkt=sess.fill_pmkt.copy())
    sess.fill_pmkt[:, 1, :] = 0.0  # asks rest
    sess.fill_qhat[:] = 1e9
    env, _ = run(sess, {10: BID0, 11: ASK1, 20: FLAT}, fill_rule="queue_est", latency_ms=200,
                 sequential_cancel=True)
    exits = [f for f in env.fills if f["kind"] == "exit"]
    assert exits[0]["us"] == 20 * US + 400_000 and exits[0]["order"] == "market"
    # the market sell uses the last print before 20.4 s (105 at 20.3) - slippage
    assert exits[0]["price"] == pytest.approx(105.0 - 0.5)


def test_custom_window_length_and_forced_close():
    ticks = BASE + [(500.0, 100.0), (950.0, 103.0)]
    sess = make_session(ticks, window_s=1000, pmkt=1.0)
    assert sess.X.shape[0] == 990 and len(sess.cnt) == 1001
    env, ep = run(sess, {10: BID0}, fill_rule="queue_est")
    forced = [f for f in env.fills if f["forced"]]
    assert forced and forced[0]["us"] == 1000 * US
    assert ep["window_s"] == 1000


def test_morning_window():
    t = int(np.datetime64("2026-08-14T09:00:00.500", "us").astype(np.int64))
    assert morning_window_s(t) == 8939  # 11:29:00 - 09:00:00.5, floored
    t = int(np.datetime64("2026-08-14T09:20:00", "us").astype(np.int64))
    assert morning_window_s(t) == 7740


def test_feature_groups_need_opening_window():
    with pytest.raises(ValueError, match="600"):
        build_scalp_session(make_raw(BASE), prev_close=100.0, avg_volume_20d=1e6,
                            is_topix100=False, features="base+A", window_s=1000)


class _Const:
    def __init__(self, v):
        self.v = v

    def predict_proba(self, R):
        return np.column_stack([1 - np.full(len(R), self.v), np.full(len(R), self.v)])

    def predict(self, R):
        return R[:, 0] * 0 + np.log1p(self.v * 1000)


def test_build_with_fill_model_shapes_and_order():
    ticks = BASE + [(t, 100.0 + (t % 3)) for t in np.arange(10.0, 100.0, 0.7)]
    model = fm.FillModel(_Const(0.25), _Const(0.5), shrink=2.0)
    sess = build_scalp_session(make_raw(ticks), prev_close=100.0, avg_volume_20d=1e6,
                               is_topix100=False, window_s=120, fill_model=model)
    assert sess.fill_pmkt.shape == (110, 2, 3) and sess.fill_qhat.shape == (110, 2, 3)
    assert np.allclose(sess.fill_pmkt, 0.25)
    assert np.allclose(sess.fill_qhat, 500.0 * 2.0, rtol=1e-4)


def test_expand_rows_side_symmetry():
    F = np.array([[1.0, 2.0, 3.0, 1.0, 0.8, 5.0, 2.0, 4.6, 10.0, 1.0, 9.5]])
    rb = fm.expand_rows(F, np.array([1.0]), np.array([0]))[0]
    rs = fm.expand_rows(F, np.array([-1.0]), np.array([0]))[0]
    names = list(fm.ROW_NAMES)
    assert rb[names.index("s_ret1")] == 1.0 and rs[names.index("s_ret1")] == -1.0
    assert rb[names.index("s_pos5")] == pytest.approx(0.8)
    assert rs[names.index("s_pos5")] == pytest.approx(0.2)


def test_push_tape_groups_prints_within_20ms():
    from trading_rl.scalp_env import push_tape, push_tape_index

    t = np.array([0, 5_000, 19_999, 20_000, 100_000, 100_010], dtype=np.int64)
    assert push_tape_index(t).tolist() == [2, 3, 5]
    raw = make_raw([(0.0, 100.0, 100), (0.005, 101.0, 200), (0.019999, 102.0, 300),
                    (0.02, 103.0, 400), (0.1, 104.0, 500), (0.10001, 105.0, 600)])
    pt = push_tape(raw)
    assert pt.price.tolist() == [102.0, 103.0, 105.0]
    assert pt.qty.tolist() == [600, 400, 1100]
    assert (pt.epoch_us - T_OPEN).tolist() == [19_999, 20_000, 100_010]


def test_obs_tape_changes_features_not_fills():
    ticks = BASE + [(10.0 + i * 0.005, 100.0 + (i % 2)) for i in range(40)] + [(30.0, 99.0)]
    kw = dict(prev_close=100.0, avg_volume_20d=1e6, is_topix100=False)
    a = build_scalp_session(make_raw(ticks), **kw)
    b = build_scalp_session(make_raw(ticks), obs_tape="push20", **kw)
    assert not np.array_equal(a.X, b.X)  # trade counts differ
    assert np.array_equal(a.price, b.price) and np.array_equal(a.t_rel, b.t_rel)

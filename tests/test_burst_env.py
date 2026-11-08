"""Burst-moment practice ground (trading_rl.burst_env)."""

from __future__ import annotations

import numpy as np
import pytest

from trading_rl import burst_env as be
from trading_rl.data import OpeningSessionData

US = be.US
W = 200


def _raw(seed: int = 0, n: int = 3000, flat: bool = False, t_open_tod: int = 9 * 3600) -> OpeningSessionData:
    rng = np.random.default_rng(seed)
    t = np.sort(rng.integers(0, W * US, n)).astype(np.int64)
    t[0] = 0
    p = np.full(n, 1000.0) if flat else 1000.0 + np.cumsum(rng.choice([-1.0, 0.0, 1.0], n))
    q = rng.integers(1, 20, n).astype(np.int64) * 100
    t0 = t_open_tod * US
    return OpeningSessionData(
        stem="9999", code="9999", date="2024-03-01", t_open_us=t0, p_open=float(p[0]),
        v_open=int(q[0]), epoch_us=t + t0, price=p, qty=q, trade_type=np.full(n, "", dtype=object),
    )


def _grid(raw: OpeningSessionData) -> be.SessionGrid:
    return be.session_grid(raw, prev_close=995.0, avg_volume_20d=1e6, is_topix100=False, window_s=W)


def _data(raws, score_fn=None, thr=0.5, ep_s=be.EP_S) -> be.BurstData:
    parts, sess = [], []
    for raw in raws:
        g = _grid(raw)
        s = np.zeros(len(g.secs), np.float32) if score_fn is None else score_fn(g)
        if score_fn is None:
            s[[0, 70]] = 0.9  # triggers at secs 10 and 80
        e = be.session_episodes(g, s, thr, ep_s)
        e.update(date=g.date, stem=g.stem, held=False, t100=False, tod0=g.tod_open_s)
        parts.append(e)
        sess.append((g.date, g.stem, False))
    return be.concat_episodes(parts, sess)


# ------------------------------------------------------------------ causality
def test_features_do_not_see_the_future():
    raw = _raw(1)
    cut = 120
    pert = _raw(1)
    after = (pert.epoch_us - pert.t_open_us) > cut * US
    pert.price[after] += np.random.default_rng(5).choice([-3.0, 3.0], after.sum())
    pert.qty[after] *= 7
    g0, g1 = _grid(raw), _grid(pert)
    m = g0.secs <= cut
    np.testing.assert_array_equal(g0.X[m], g1.X[m])
    assert not np.array_equal(g0.X[~m], g1.X[~m])  # the perturbation does matter later


def test_observations_do_not_see_the_future():
    """Same actions -> identical observations up to the cut, whatever happens after."""
    cut = 50  # episode from s0=10: decisions d_j = 10 + j

    def trig(g):
        s = np.zeros(len(g.secs), np.float32)
        s[0] = 0.9
        return s

    raw, pert = _raw(2), _raw(2)
    after = (pert.epoch_us - pert.t_open_us) > cut * US
    pert.price[after] += 5.0
    d0, d1 = _data([raw], trig), _data([pert], trig)
    for lat in be.LATENCIES_MS:
        cfg = be.BurstConfig(latency_ms=lat)
        b0, b1 = be.BurstBatch(d0, np.array([0]), cfg), be.BurstBatch(d1, np.array([0]), cfg)
        acts = [1, 0, 0, 2, 0, 2, 0, 0, 1] * 10
        for j in range(be.EP_S):
            o0, o1 = b0.obs(), b1.obs()
            if 10 + j <= cut:
                np.testing.assert_array_equal(o0, o1)
            b0.step(np.array([acts[j]]))
            b1.step(np.array([acts[j]]))
        assert b0.done and b1.done


def test_detector_score_is_in_obs_and_causal():
    d = _data([_raw(3)])
    bb = be.BurstBatch(d, np.arange(len(d)), be.BurstConfig())
    o = bb.obs()
    np.testing.assert_allclose(o[:, be.N_MARKET], be.logit(d.score[:, 0]))


# ------------------------------------------------------------------ costs
@pytest.mark.parametrize("slip", [0.5, 1.0])
def test_market_round_trip_costs_two_slips_on_a_flat_tape(slip):
    d = _data([_raw(4, flat=True)])
    res = be.run_policy(d, lambda o, m, b: np.where(b.j == 0, 1, np.where(b.j == 5, 2, 0)),
                        be.BurstConfig(slip_ticks=slip))
    np.testing.assert_allclose(res["trips"]["pnl"], -2 * slip)
    np.testing.assert_allclose(res["trips"]["hold_s"], 5)
    res = be.run_policy(d, lambda o, m, b: np.where(b.j == 0, 2, np.where(b.j == 3, 1, 0)),
                        be.BurstConfig(slip_ticks=slip))
    np.testing.assert_allclose(res["trips"]["pnl"], -2 * slip)
    assert (res["trips"]["dir"] == -1).all()


def _jump_data():
    """Price 1000 until 10.5 s, 1010 after (one episode from s0=10)."""
    t = np.arange(0, W * US, 100_000, dtype=np.int64)
    p = np.where(t < 10_500_000, 1000.0, 1010.0)
    t0 = 9 * 3600 * US
    raw = OpeningSessionData(stem="9998", code="9998", date="2024-03-01", t_open_us=t0, p_open=1000.0,
                             v_open=100, epoch_us=t + t0, price=p, qty=np.full(len(t), 100),
                             trade_type=np.full(len(t), "", dtype=object))

    def trig(g):
        s = np.zeros(len(g.secs), np.float32)
        s[0] = 0.9
        return s

    return _data([raw], trig)


def test_latency_decides_the_fill_price():
    d = _jump_data()
    buy0 = lambda o, m, b: np.where(b.j == 0, 1, 0)  # noqa: E731
    fast = be.run_policy(d, buy0, be.BurstConfig(latency_ms=200))["trips"]
    slow = be.run_policy(d, buy0, be.BurstConfig(latency_ms=1000))["trips"]
    # forced exit at 1010 - 0.5; entry 1000.5 (200 ms) vs 1010.5 (1 s, after the jump)
    np.testing.assert_allclose(fast["pnl"], 1010 - 0.5 - 1000.5)
    np.testing.assert_allclose(slow["pnl"], -1.0)
    assert fast["forced"].all()


def test_worst_fill_uses_the_worst_print_in_the_latency_window():
    d = _jump_data()
    buy0 = lambda o, m, b: np.where(b.j == 0, 1, 0)  # noqa: E731
    w = be.run_policy(d, buy0, be.BurstConfig(latency_ms=1000, fill_price="worst"))["trips"]
    np.testing.assert_allclose(w["pnl"], -1.0)  # bought at max(1000, 1010) + 0.5
    sell0 = lambda o, m, b: np.where(b.j == 0, 2, 0)  # noqa: E731
    s = be.run_policy(d, sell0, be.BurstConfig(latency_ms=1000, fill_price="worst"))["trips"]
    # sold at min(1000, ...) - 0.5 = 999.5, bought back at 1010.5
    np.testing.assert_allclose(s["pnl"], 999.5 - 1010.5)


# ------------------------------------------------------------------ episode end
def test_forced_close_at_episode_end():
    d = _data([_raw(6)])
    res = be.run_policy(d, lambda o, m, b: np.where(b.j == 3, 2, 0), be.BurstConfig())
    T = res["trips"]
    assert len(T["pnl"]) == len(d) and T["forced"].all()
    assert (T["exit_j"] == be.EP_S).all() and (T["hold_s"] == be.EP_S - 3).all()
    ex = d.fills["px200"][:, be.EP_S] + 0.5  # short covered at last + 0.5 tick (tick 1)
    en = d.fills["px200"][:, 3] - 0.5
    np.testing.assert_allclose(T["pnl"], (en - ex) / d.ref_tick)


def test_rewards_sum_to_pnl_without_shaping_and_penalty_only_on_losses():
    d = _data([_raw(7)])
    rng = np.random.default_rng(0)
    for cfg in (be.BurstConfig(), be.BurstConfig(unrealized_loss_penalty_per_s=0.01)):
        bb = be.BurstBatch(d, np.arange(len(d)), cfg)
        tot = np.zeros(len(d))
        while not bb.done:
            m = bb.mask()
            a = np.array([rng.choice(np.flatnonzero(r)) for r in m])
            r, _ = bb.step(a)
            tot += r
        pnl = np.bincount(np.searchsorted(np.arange(len(d)), bb.trip_table()["ep"]),
                          bb.trip_table()["pnl"], len(d))
        if cfg.unrealized_loss_penalty_per_s == 0:
            np.testing.assert_allclose(tot, pnl, atol=1e-4)
        else:
            assert (tot <= pnl + 1e-4).all()


def test_mask_and_invalid_actions():
    d = _data([_raw(8)])
    bb = be.BurstBatch(d, np.arange(len(d)), be.BurstConfig())
    bb.step(np.full(len(d), 1))
    assert (bb.mask()[:, be.A_BUY] == False).all()  # noqa: E712
    bb.step(np.full(len(d), 1))  # buy while long -> no-op
    assert bb.n_invalid == len(d) and (bb.pos == 1).all()


def test_gym_wrapper_matches_batch():
    d = _data([_raw(9)])
    env = be.BurstEnv(d)
    obs, info = env.reset(options={"index": 1})
    bb = be.BurstBatch(d, np.array([1]), be.BurstConfig())
    acts = [2, 0, 0, 1, 1, 0] + [0] * 60
    total = 0.0
    for j in range(be.EP_S):
        np.testing.assert_allclose(obs, bb.obs()[0])
        obs, r, done, trunc, info = env.step(acts[j])
        bb.step(np.array([acts[j]]))
        total += r
        if done:
            break
    assert done and j == be.EP_S - 1
    np.testing.assert_allclose(total, info["trips"]["pnl"].sum(), atol=1e-5)


def test_triggers_do_not_overlap_and_fit_in_the_session():
    secs = np.arange(10, 300)
    score = np.ones(len(secs))
    tr = be.trigger_seconds(secs, score, 0.5, W=300, ep_s=60)
    assert tr == [10, 71, 132, 193]  # next trigger only after s0 + 60, last ends <= 300


# ------------------------------------------------------------------ train only
def test_detector_refuses_non_train_rows():
    X = np.random.default_rng(0).normal(size=(500, 3)).astype(np.float32)
    y = X[:, 0] > 1
    ok = np.full(500, "2024-05-01")
    bad = np.full(500, "2025-07-01")
    with pytest.raises(ValueError):
        be.fit_detector(X, y, bad, X, ok, max_iter=5)
    with pytest.raises(ValueError):
        be.fit_detector(X, y, ok, X, bad, max_iter=5)
    det = be.fit_detector(X, y, ok, X, ok, q=0.9, max_iter=5)
    assert det.meta["date_max"] <= be.TRAIN[1]
    assert np.isclose(det.threshold, np.quantile(det.score(X), 0.9))


def test_saved_detector_was_built_on_train_only():
    path = be.MODEL_DIR / "detector_v1.pkl"
    if not path.exists():
        pytest.skip("detector_v1.pkl not built")
    det = be.Detector.load(path)
    assert det.meta["train_period"] == be.TRAIN
    assert be.TRAIN[0] <= det.meta["date_min"] and det.meta["date_max"] <= be.TRAIN[1]
    lo, hi = det.meta["thr_dates"]
    assert be.TRAIN[0] <= lo and hi <= be.TRAIN[1]


def test_saved_episode_sets_respect_the_split_periods():
    for split, (lo, hi) in be.SPLITS.items():
        path = be.DEFAULT_DIR / f"episodes_{split}.npz"
        if not path.exists():
            continue
        d = be.BurstData.load(path)
        assert lo <= min(d.date.tolist()) and max(d.date.tolist()) <= hi <= be.LAST_USABLE_DATE
        if split == "train":
            assert not d.held.any() and not d.sess_held.any()


def test_detector_save_never_overwrites(tmp_path):
    X = np.random.default_rng(0).normal(size=(300, 2)).astype(np.float32)
    ok = np.full(300, "2024-05-01")
    det = be.fit_detector(X, X[:, 0] > 0, ok, X, ok, max_iter=3)
    p = tmp_path / "d.pkl"
    det.save(p)
    with pytest.raises(FileExistsError):
        det.save(p)


# ------------------------------------------------------------------ baselines
def test_fast_rule_evaluator_matches_the_stepped_rule_policy():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "burst"))
    import burst_rules as br

    def trig(g):
        s = np.zeros(len(g.secs), np.float32)
        s[::7] = 0.9  # many triggers
        return s

    d = _data([_raw(s) for s in range(10, 14)], trig)
    exits = [(2, 2, 10), (3, br.INF, 60), (br.INF, 5, 20), (5, 3, 5)]
    for cfg in (be.BurstConfig(), be.BurstConfig(latency_ms=1000, slip_ticks=1.0, fill_price="worst")):
        for m, mode, smin in ((1, 1, -br.INF), (5, -1, 0.0), (30, 1, 1.0)):
            fast = br.rule_trips_fast(d, cfg, m, mode, smin, exits)
            assert len(fast[0]["pnl"]) >= 10 and (~fast[0]["forced"]).any()
            for (tp, sl, T), f in zip(exits, fast, strict=True):
                slow = be.run_policy(d, br.RulePolicy(m=m, mode=mode, tp=tp, sl=sl, T=T, smin=smin), cfg)["trips"]
                o = np.argsort(slow["ep"], kind="stable")
                for k in ("ep", "entry_j", "exit_j", "dir", "forced"):
                    np.testing.assert_array_equal(np.asarray(slow[k])[o], np.asarray(f[k]))
                np.testing.assert_allclose(np.asarray(slow["pnl"])[o], f["pnl"], atol=1e-9)

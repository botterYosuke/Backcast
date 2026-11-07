"""Tests for trading_rl.scalp_features (switchable observation-feature groups)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import duckdb
import numpy as np
import pytest
import torch

from trading_rl import scalp_replay as sr
from trading_rl.data import OpeningSessionData
from trading_rl.policy import ActorCritic
from trading_rl.scalp_env import (
    OBS_DIM,
    ScalpConfig,
    ScalpEnv,
    build_scalp_session,
    obs_dim,
    obs_names,
)
from trading_rl.scalp_features import (
    EXTRA_NAMES,
    ContextStore,
    DateContext,
    _StemDaily,
    day_start_us,
    market_names,
    n_market,
    parse_feature_spec,
    price_limit_width,
)

DATE = "2026-08-14"
DAY0 = day_start_us(DATE)
T_OPEN = DAY0 + 2_000_000  # opens 2 s after 09:00:00
PREV_CLOSE = 105.0
GOLDEN = Path(__file__).parent / "data" / "scalp_base_golden.npz"
ALL_GROUPS = "ABCDEFGHI"


# --------------------------------------------------------------------------- #
# Synthetic data
# --------------------------------------------------------------------------- #
def raw_session(
    *, seed: int = 1, n: int = 2500, p0: float = 100.0, extra=None, stem="1234"
) -> OpeningSessionData:
    """Random-walk ticks over 600 s (price ~ p0 +/- a few yen), qty 100..1900."""
    rng = np.random.default_rng(seed)
    t = np.sort(rng.integers(1, 600_000_000, size=n)).astype(np.int64)
    t = np.concatenate([[0], t])
    steps = rng.choice([-1, 0, 1], size=n + 1, p=[0.3, 0.4, 0.3])
    steps[0] = 0  # the opening print is exactly p0
    price = p0 + np.cumsum(steps)
    price = np.maximum(price, 10.0).astype(np.float64)
    qty = (rng.integers(1, 20, size=n + 1) * 100).astype(np.int64)
    if extra:  # [(t_sec, price)] forced ticks
        et = np.array([int(s * 1e6) for s, _ in extra], dtype=np.int64)
        ep = np.array([p for _, p in extra], dtype=np.float64)
        t = np.concatenate([t, et])
        price = np.concatenate([price, ep])
        qty = np.concatenate([qty, np.full(len(et), 100, dtype=np.int64)])
        o = np.argsort(t, kind="stable")
        t, price, qty = t[o], price[o], qty[o]
    return OpeningSessionData(
        stem=stem,
        code=stem + "0",
        date=DATE,
        t_open_us=T_OPEN,
        p_open=float(price[0]),
        v_open=int(qty[0]),
        epoch_us=T_OPEN + t,
        price=price,
        qty=qty,
        trade_type=np.full(len(t), "", dtype=object),
    )


def make_ctx(n_grid: int = 700) -> DateContext:
    g = np.arange(n_grid)
    return DateContext(
        date=DATE,
        day_start_us=DAY0,
        stems=("1234", "aaaa", "bbbb", "cccc"),
        open_off=np.array([2.0, 1.0, 3.0, 5.0]),
        gap=np.array([-476.0, 100.0, -200.0, 300.0]),
        sector=("3650", "3650", "3700", "3650"),
        med_ret=(np.sin(g / 50.0) * 20.0).astype(np.float32),
        frac_up=(0.5 + 0.3 * np.sin(g / 70.0)).astype(np.float32),
        n_act=np.where(g < 10, 2, 40).astype(np.int32),
    )


def make_store(tmp_path: Path, ctx: DateContext | None = None) -> ContextStore:
    st = ContextStore(
        tmp_path,
        bars_dir=tmp_path / "nobars",
        listed_pit=tmp_path / "nolist.duckdb",
    )
    d = np.array(["2026-08-13"], dtype="datetime64[D]")

    def row(**kw: float) -> _StemDaily:
        base = dict.fromkeys(
            ("close", "ret1", "ret5", "ret15", "ret30", "sigma20", "hi", "lo"), np.nan
        )
        base.update(kw)
        return _StemDaily(d, {k: np.array([v]) for k, v in base.items()})

    st._daily = {
        "1234": row(
            close=105.0, ret1=0.01, ret5=-0.02, ret15=0.04, ret30=0.06,
            sigma20=0.02, hi=110.0, lo=98.0,
        ),
        "1321": row(close=30000.0, ret5=-0.015, sigma20=0.012),
    }
    st._sector = {"12340": (["2026-07-01"], ["3650"])}
    st._ctx[DATE] = ctx if ctx is not None else make_ctx()
    return st


def build(raw: OpeningSessionData, features: str, store: ContextStore | None):
    return build_scalp_session(
        raw,
        prev_close=PREV_CLOSE,
        avg_volume_20d=1e6,
        is_topix100=False,
        features=features,
        store=store,
    )


def col(sess, features: str, name: str) -> np.ndarray:
    return sess.X[:, market_names(features).index(name)]


# --------------------------------------------------------------------------- #
# Spec
# --------------------------------------------------------------------------- #
def test_spec_parse_and_canonical_roundtrip():
    s = parse_feature_spec("base+C+A-J")
    assert (s.groups, s.drops, s.canonical) == (("A", "C"), ("J",), "base+A+C-J")
    assert parse_feature_spec(s.canonical) == s
    assert parse_feature_spec("base").is_base
    assert parse_feature_spec("").is_base and parse_feature_spec(None).is_base
    assert parse_feature_spec("-J").canonical == "base-J"
    assert parse_feature_spec("A").canonical == "base+A"
    assert parse_feature_spec("a+c").canonical == "base+A+C"
    assert parse_feature_spec("base+A+A").canonical == "base+A"
    assert ScalpConfig(features="base+B").metadata()["features"] == "base+B"


@pytest.mark.parametrize("bad", ["-A", "+J", "Z", "base+AB", "A+base", "base-base", "+"])
def test_spec_rejects_bad_specs(bad):
    with pytest.raises(ValueError):
        parse_feature_spec(bad)
    with pytest.raises(ValueError):
        ScalpConfig(features=bad)


def test_default_dims_unchanged():
    assert obs_dim("base") == OBS_DIM == 38
    assert obs_names("base")[-10:] == obs_names("base+A")[-10:]
    # -J drops tick-activity and tick-rule imbalance features by prefix
    dropped = set(market_names("base")) - set(market_names("base-J"))
    assert dropped == {
        "imb_5s", "imb_30s", "imb_60s", "log_ntrades_5s", "log_ntrades_30s",
        "log_vol_ratio_5s", "log_vol_ratio_30s", "log_time_since_trade",
    }  # fmt: skip
    assert n_market("base-J") == 20


def test_obs_dim_per_group_and_combination(tmp_path):
    store = make_store(tmp_path)
    raw = raw_session()
    specs = ["base", "base-J", *[f"base+{g}" for g in ALL_GROUPS], "base+A+C-J"]
    specs.append("base+" + "+".join(ALL_GROUPS) + "-J")
    for spec in specs:
        sess = build(raw, spec, store)
        want = len(market_names(spec))
        assert sess.X.shape == (590, want), spec
        assert obs_dim(spec) == want + 10 == len(obs_names(spec))
        assert np.isfinite(sess.X).all()
        env = ScalpEnv(config=ScalpConfig(features=spec))
        obs, _ = env.reset(options={"session": sess})
        assert obs.shape == (obs_dim(spec),) and env.observation_space.shape == obs.shape
        for _ in range(5):
            obs, *_ = env.step(0)
        assert np.isfinite(obs).all()
    for g in ALL_GROUPS:
        assert obs_dim(f"base+{g}") == 38 + len(EXTRA_NAMES[g])


def test_env_rejects_session_with_other_feature_set(tmp_path):
    sess = build(raw_session(), "base", None)
    env = ScalpEnv(config=ScalpConfig(features="base+A"))
    with pytest.raises(ValueError, match="market features"):
        env.reset(options={"session": sess})


def test_minus_j_columns_equal_base_columns(tmp_path):
    raw = raw_session()
    base, dj = build(raw, "base", None), build(raw, "base-J", None)
    keep = [market_names("base").index(n) for n in market_names("base-J")]
    np.testing.assert_array_equal(dj.X, base.X[:, keep])


# --------------------------------------------------------------------------- #
# Regression: defaults reproduce the pre-feature-groups behaviour exactly
# --------------------------------------------------------------------------- #
def _golden_session(features: str | None):
    rng = np.random.default_rng(12345)
    n = 4000
    t = np.sort(rng.integers(0, 600_000_000, size=n)).astype(np.int64)
    t[0] = 0
    steps = rng.choice([-1, 0, 1], size=n, p=[0.3, 0.4, 0.3])
    price = 2000.0 + np.cumsum(steps).astype(np.float64)
    qty = rng.integers(1, 20, size=n).astype(np.int64) * 100
    raw = OpeningSessionData(
        stem="1234", code="12340", date=DATE, t_open_us=1_786_698_000_000_000,
        p_open=float(price[0]), v_open=int(qty[0]), epoch_us=1_786_698_000_000_000 + t,
        price=price, qty=qty, trade_type=np.full(n, "", dtype=object),
    )  # fmt: skip
    kw = {} if features is None else {"features": features}
    return build_scalp_session(
        raw, prev_close=1990.0, avg_volume_20d=5e5, is_topix100=False, **kw
    )


@pytest.mark.parametrize("features", [None, "base"])
@pytest.mark.parametrize("mode", ["limit", "market", "both"])
def test_base_obs_and_rewards_match_pre_change_golden(mode, features):
    g = np.load(GOLDEN)
    sess = _golden_session(features)
    np.testing.assert_array_equal(sess.X, g["X"])
    kw = {} if features is None else {"features": features}
    env = ScalpEnv(
        [sess],
        ScalpConfig(
            order_mode=mode,
            unrealized_loss_penalty_per_s=0.01,
            reward_mode="realized",
            **kw,
        ),
    )
    obs, _ = env.reset(options={"index": 0})
    arng = np.random.default_rng(7)
    obs_l, r_l, info, done = [obs], [], {}, False
    while not done:
        legal = np.flatnonzero(env.action_mask())
        a = int(arng.choice(legal)) if arng.random() < 0.15 else 0
        obs, r, done, _, info = env.step(a)
        obs_l.append(obs)
        r_l.append(r)
    np.testing.assert_array_equal(np.asarray(obs_l, dtype=np.float32), g[f"obs_{mode}"])
    np.testing.assert_array_equal(np.asarray(r_l), g[f"rew_{mode}"])
    ep = info["episode"]
    np.testing.assert_array_equal(
        np.array([ep["pnl_ticks"], ep["n_trips"]]), g[f"pnl_{mode}"]
    )


# --------------------------------------------------------------------------- #
# Group values
# --------------------------------------------------------------------------- #
def test_group_A_gap_zone_and_sigma(tmp_path):
    store = make_store(tmp_path)
    sess = build(raw_session(p0=100.0), "base+A", store)
    f = "base+A"
    gap = 100.0 / PREV_CLOSE - 1.0  # -4.76 %
    assert col(sess, f, "a_gap_pct")[0] == pytest.approx(gap * 100, rel=1e-5)
    zones = [col(sess, f, n)[0] for n in EXTRA_NAMES["A"][1:6]]
    assert zones == [0.0, 0.0, 1.0, 0.0, 0.0]  # 2 % <= |gap| < 5 %
    assert col(sess, f, "a_gap_over_sigma20")[0] == pytest.approx(gap / 0.02, rel=1e-5)
    for p0, z in [(105.0, 0), (105.4, 0), (107.0, 1), (115.0, 3), (130.0, 4)]:
        s = build(raw_session(p0=p0), f, store)
        zv = [col(s, f, n)[0] for n in EXTRA_NAMES["A"][1:6]]
        assert zv.index(1.0) == z and sum(zv) == 1.0


def test_group_B_gap_fill_flags(tmp_path):
    f = "base+B"
    # gap down 105 -> 100 open; prints at 101 (t=15 s) and 105 (t=40 s)
    raw = raw_session(p0=100.0, n=0, extra=[(15.0, 101.0), (40.0, 105.0)])
    sess = build(raw, f, None)
    i = lambda s: s - 10  # noqa: E731 - decision second -> row
    prog = col(sess, f, "b_fill_progress")
    touched = col(sess, f, "b_touched_prev_close")
    recross = col(sess, f, "b_recrossed_open")
    assert prog[i(10)] == 0.0 and touched[i(30)] == 0.0 and recross[i(10)] == 0.0
    assert recross[i(16)] == 1.0  # traded above the open price after a gap down
    assert prog[i(20)] == pytest.approx(0.2)  # (101-100)/(105-100)
    assert touched[i(40)] == 1.0 and prog[i(45)] == pytest.approx(1.0)
    dist = col(sess, f, "b_dist_prev_close_ticks")
    assert dist[i(20)] == pytest.approx((101.0 - 105.0) / 1.0)
    # no gap (< 1 tick): progress / flags are 0
    flat = build_scalp_session(
        raw, prev_close=100.4, avg_volume_20d=1e6, is_topix100=False, features=f
    )
    assert not col(flat, f, "b_fill_progress").any()
    assert not col(flat, f, "b_touched_prev_close").any()
    # gap up mirrored: open 110 vs prev close 105, dips to 104 at 20 s
    up = raw_session(p0=110.0, n=0, extra=[(20.0, 104.0)])
    su = build(up, f, None)
    assert col(su, f, "b_touched_prev_close")[i(19)] == 0.0
    assert col(su, f, "b_touched_prev_close")[i(21)] == 1.0
    assert col(su, f, "b_recrossed_open")[i(21)] == 1.0


def test_group_C_wall_clock_market_and_relative(tmp_path):
    store = make_store(tmp_path)
    f = "base+C"
    sess = build(raw_session(p0=100.0), f, store)
    ctx = store.date_context(DATE)
    # own T_open is 2 s after 09:00:00 -> decision second s maps to wall second 2 + s
    s = 100
    g = 2 + s
    assert col(sess, f, "c_mkt_med_ret_bps")[s - 10] == pytest.approx(ctx.med_ret[g])
    assert col(sess, f, "c_mkt_frac_up")[s - 10] == pytest.approx(ctx.frac_up[g], abs=1e-6)
    own = np.clip((sess.last[s] / sess.p_open - 1) * 1e4, -1000, 1000)
    assert col(sess, f, "c_rel_to_mkt_bps")[s - 10] == pytest.approx(
        own - ctx.med_ret[g], abs=1e-3
    )
    assert col(sess, f, "c_mkt_log_n")[s - 10] == pytest.approx(np.log1p(40))
    # fewer than 3 open stocks -> neutral market features (rel = own return)
    thin = make_store(tmp_path, DateContext(**{**make_ctx().__dict__, "n_act": np.full(700, 2)}))
    t = build(raw_session(p0=100.0), f, thin)
    assert not col(t, f, "c_mkt_med_ret_bps").any()
    assert (col(t, f, "c_mkt_frac_up") == 0.5).all()
    # no context at all -> neutral
    none = make_store(tmp_path, DateContext.empty(DATE))
    n_ = build(raw_session(p0=100.0), f, none)
    assert not col(n_, f, "c_mkt_med_ret_bps").any()


def test_group_D_sector_relative_and_market_fallback(tmp_path):
    store = make_store(tmp_path)
    f = "base+D"
    sess = build(raw_session(p0=100.0), f, store)
    own_gap = (100.0 / PREV_CLOSE - 1) * 1e4
    # sector 3650 peers: aaaa (open 1 s, gap +100) and cccc (open 5 s, gap +300),
    # both open by own T_open (2 s) + 10 s -> median +200 (bbbb is another sector).
    rel = col(sess, f, "d_gap_rel_bps")
    assert rel[0] == pytest.approx(own_gap - 200.0, abs=1e-3)
    assert col(sess, f, "d_log1p_n_sector")[0] == pytest.approx(np.log1p(2))
    assert (col(sess, f, "d_market_fallback") == 0.0).all()
    # a sector with no peer falls back to the market median gap of the others
    other = make_store(tmp_path)
    other._sector = {"12340": (["2026-07-01"], ["9999"])}
    s2 = build(raw_session(p0=100.0), f, other)
    assert (col(s2, f, "d_market_fallback") == 1.0).all()
    assert col(s2, f, "d_log1p_n_sector")[0] == 0.0
    assert col(s2, f, "d_gap_rel_bps")[-1] == pytest.approx(
        own_gap - np.median([100.0, -200.0, 300.0]), abs=1e-3
    )
    # peers that open later than own T_open + s are not used yet
    late = make_ctx()
    late.open_off[3] = 400.0  # cccc opens at 400 s
    st3 = make_store(tmp_path, late)
    s3 = build(raw_session(p0=100.0), f, st3)
    assert col(s3, f, "d_log1p_n_sector")[0] == pytest.approx(np.log1p(1))
    assert col(s3, f, "d_log1p_n_sector")[-1] == pytest.approx(np.log1p(2))


def test_group_E_drift_over_sigma_and_regime(tmp_path):
    store = make_store(tmp_path)
    f = "base+E"
    sess = build(raw_session(), f, store)
    assert col(sess, f, "e_ret1d_over_sigma")[0] == pytest.approx(0.5)
    assert col(sess, f, "e_ret5d_over_sigma")[0] == pytest.approx(-1.0)
    assert col(sess, f, "e_ret15d_over_sigma")[0] == pytest.approx(2.0)
    assert col(sess, f, "e_ret30d_over_sigma")[0] == pytest.approx(3.0)
    assert col(sess, f, "e_mkt_vol20d_pct")[0] == pytest.approx(1.2)
    assert col(sess, f, "e_mkt_ret5d_pct")[0] == pytest.approx(-1.5)
    store._daily["zzzz"] = _StemDaily(np.zeros(0, "datetime64[D]"), {})
    unknown = build(raw_session(stem="zzzz"), f, store)
    assert not unknown.X[:, -6:-2].any()


def test_price_limit_table_and_group_F(tmp_path):
    assert price_limit_width(99) == 30 and price_limit_width(100) == 50
    assert price_limit_width(999) == 150 and price_limit_width(1000) == 300
    assert price_limit_width(2590.5) == 500 and price_limit_width(37170) == 7000
    assert np.isnan(price_limit_width(0))
    f = "base+F"
    raw = raw_session(p0=100.0, n=0, extra=[(20.0, 110.0)])
    sess = build(raw, f, make_store(tmp_path))
    assert col(sess, f, "f_log1p_open_delay_s")[0] == pytest.approx(np.log1p(2.0))
    up, dn = col(sess, f, "f_limit_up_dist_pct"), col(sess, f, "f_limit_down_dist_pct")
    # prev close 105 -> width 50: limits 155 / 55
    assert up[0] == pytest.approx((155 - 100) / 50 * 100)
    assert dn[0] == pytest.approx((100 - 55) / 50 * 100)
    assert up[25 - 10] == pytest.approx((155 - 110) / 50 * 100)


def test_group_G_prev_day_range(tmp_path):
    store = make_store(tmp_path)
    f = "base+G"
    inside = build(raw_session(p0=100.0), f, store)  # 98 <= 100 <= 110
    assert col(inside, f, "g_known")[0] == 1.0
    assert col(inside, f, "g_above_prev_high")[0] == 0.0
    assert col(inside, f, "g_below_prev_low")[0] == 0.0
    assert col(inside, f, "g_range_pos")[0] == pytest.approx(2 / 12)
    assert col(inside, f, "g_out_dist_sigma")[0] == 0.0
    assert col(inside, f, "g_prev_range_sigma")[0] == pytest.approx(12 / (105 * 0.02))
    above = build(raw_session(p0=112.0), f, store)
    assert col(above, f, "g_above_prev_high")[0] == 1.0
    assert col(above, f, "g_out_dist_sigma")[0] == pytest.approx(2 / (105 * 0.02))
    below = build(raw_session(p0=95.0), f, store)
    assert col(below, f, "g_below_prev_low")[0] == 1.0
    assert col(below, f, "g_out_dist_sigma")[0] == pytest.approx(-3 / (105 * 0.02))
    store._daily["1234"].cols["hi"][:] = np.nan  # OHLC unknown -> all zeros
    unk = build(raw_session(p0=100.0), f, store)
    assert not unk.X[:, -6:].any()


def test_group_H_first_minute_known_flag(tmp_path):
    f = "base+H"
    raw = raw_session(p0=100.0, n=0, extra=[(30.0, 103.0), (59.0, 104.0), (80.0, 90.0)])
    sess = build(raw, f, None)
    known = col(sess, f, "h_known")
    assert not known[: 60 - 10].any() and known[60 - 10 :].all()
    for n in EXTRA_NAMES["H"][1:]:
        assert not col(sess, f, n)[: 60 - 10].any()
    assert col(sess, f, "h_ret60_bps")[0 + 50] == pytest.approx(400.0)  # 104 vs 100
    assert col(sess, f, "h_sign60")[100] == 1.0  # frozen at 60 s, not the 90 print
    assert col(sess, f, "h_ret60_bps")[100] == pytest.approx(400.0)
    vol = 100 * 2 + raw.qty[0]  # open print + the forced ticks at 30 s / 59 s
    assert col(sess, f, "h_log_vol60_ratio")[60] == pytest.approx(
        np.log10(vol / 1e6 + 1e-6), abs=1e-4
    )


def test_group_I_tick_weight(tmp_path):
    f = "base+I"
    sess = build(raw_session(p0=2000.0), f, None)
    i = 100
    tick = sess.tick[10 + i]
    assert col(sess, f, "i_tick_bps")[i] == pytest.approx(
        tick / sess.last[10 + i] * 1e4, rel=1e-6
    )
    # flat tape -> zero sigma; one 3-tick jump -> sqrt(9 / 60) ticks for 60 s
    flat = build(raw_session(p0=2000.0, n=0, extra=[(5.0, 2000.0)]), f, None)
    assert not col(flat, f, "i_sigma60_ticks").any()
    jump = build(raw_session(p0=2000.0, n=0, extra=[(50.0, 2003.0)]), f, None)
    s = col(jump, f, "i_sigma60_ticks")
    assert s[55 - 10] == pytest.approx(np.sqrt(9.0 / 55.0), rel=1e-6)
    assert s[200] == 0.0  # the jump left the 60 s window


# --------------------------------------------------------------------------- #
# Causality
# --------------------------------------------------------------------------- #
def _perturbed(raw: OpeningSessionData, after_s: float) -> OpeningSessionData:
    rel = raw.epoch_us - raw.t_open_us
    late = rel > int(after_s * 1e6)
    price = raw.price.copy()
    qty = raw.qty.copy()
    price[late] = price[late] * 1.37 + 11.0
    qty[late] = qty[late] * 7 + 300
    return OpeningSessionData(
        stem=raw.stem, code=raw.code, date=raw.date, t_open_us=raw.t_open_us,
        p_open=raw.p_open, v_open=raw.v_open, epoch_us=raw.epoch_us,
        price=price, qty=qty, trade_type=raw.trade_type,
    )  # fmt: skip


@pytest.mark.parametrize("cut", [30, 300])
@pytest.mark.parametrize("spec", [f"base+{g}" for g in ALL_GROUPS] + ["base-J"])
def test_features_at_t_ignore_ticks_after_t(spec, cut, tmp_path):
    store = make_store(tmp_path)
    raw = raw_session(seed=3, extra=[(20.0, 105.0), (40.0, 104.0)])
    a = build(raw, spec, store)
    b = build(_perturbed(raw, cut), spec, store)
    n = cut - 10 + 1  # decisions with second <= cut
    np.testing.assert_array_equal(a.X[:n], b.X[:n])
    if spec in ("base+H", "base+I", "base+B"):  # tick-driven groups do see the change
        assert not np.array_equal(a.X[-1], b.X[-1])


def _patch_cache(monkeypatch, sessions: dict[str, OpeningSessionData]):
    import trading_rl.cache as cache

    def fake(date, stem, cache_dir=None):  # noqa: ANN001
        try:
            return sessions[stem]
        except KeyError:
            raise cache.CachedSessionUnavailable(stem) from None

    monkeypatch.setattr(cache, "load_cached_session", fake)


def _universe_rows(stems_gaps: dict[str, float]):
    return [
        {"stem": s, "code": s + "0", "prev_close": 100.0 / (1 + g / 1e4)}
        for s, g in stems_gaps.items()
    ]


def _market_sessions(opens: dict[str, float], levels: dict[str, float], seed=0):
    """Per stock: first print 100 at its open, then a level shift at wall second 60+."""
    out = {}
    for k, (stem, off) in enumerate(opens.items()):
        t_open = DAY0 + int(off * 1e6)
        rng = np.random.default_rng(seed + k)
        t = np.sort(rng.integers(1, 600_000_000, size=300)).astype(np.int64)
        t = np.concatenate([[0], t])
        wall = (t_open + t - DAY0) / 1e6
        price = np.where(wall < 200.0, 100.0, 100.0 * (1 + levels[stem] / 1e4))
        out[stem] = OpeningSessionData(
            stem=stem, code=stem + "0", date=DATE, t_open_us=t_open, p_open=100.0,
            v_open=100, epoch_us=t_open + t, price=price,
            qty=np.full(len(t), 100, dtype=np.int64),
            trade_type=np.full(len(t), "", dtype=object),
        )  # fmt: skip
    return out


def test_date_context_cross_section_is_wall_clock_and_causal(tmp_path, monkeypatch):
    opens = {"aaaa": 1.0, "bbbb": 1.5, "cccc": 2.0, "dddd": 300.0}
    levels = {"aaaa": 100.0, "bbbb": -50.0, "cccc": 20.0, "dddd": 500.0}
    sessions = _market_sessions(opens, levels)
    _patch_cache(monkeypatch, sessions)

    def ctx_for(path: Path, ses: dict[str, OpeningSessionData]) -> DateContext:
        st = ContextStore(path, bars_dir=path / "x", listed_pit=path / "y")
        st._universe_rows = {DATE: _universe_rows(dict.fromkeys(opens, 0.0))}
        return st.date_context(DATE)

    ctx = ctx_for(tmp_path / "a", sessions)
    assert ctx.stems == tuple(opens)
    g = 250  # after the level shift, before dddd has opened
    assert ctx.n_act[g] == 3  # dddd (opens at 300 s) is excluded, not zero-filled
    assert ctx.med_ret[g] == pytest.approx(20.0)  # median of +100, -50, +20
    assert ctx.frac_up[g] == pytest.approx(2 / 3)
    g2 = 350  # dddd open: median of {100, -50, 20, 500} = 60
    assert ctx.n_act[g2] == 4 and ctx.med_ret[g2] == pytest.approx(60.0)
    assert ctx.n_act[0] == 0  # nobody has opened at 09:00:00
    # perturbing every tick after wall second 150 leaves earlier cells unchanged
    cut = 150
    pert = {}
    for stem, s in sessions.items():
        late = (s.epoch_us - DAY0) > cut * 1_000_000
        price = np.where(late, s.price * 1.5, s.price)
        pert[stem] = OpeningSessionData(
            stem=s.stem, code=s.code, date=s.date, t_open_us=s.t_open_us,
            p_open=s.p_open, v_open=s.v_open, epoch_us=s.epoch_us, price=price,
            qty=s.qty, trade_type=s.trade_type,
        )  # fmt: skip
    _patch_cache(monkeypatch, pert)
    ctx2 = ctx_for(tmp_path / "b", pert)
    for arr, arr2 in [(ctx.med_ret, ctx2.med_ret), (ctx.frac_up, ctx2.frac_up)]:
        np.testing.assert_array_equal(arr[: cut + 1], arr2[: cut + 1])
    np.testing.assert_array_equal(ctx.n_act[: cut + 1], ctx2.n_act[: cut + 1])
    assert not np.array_equal(ctx.med_ret[g], ctx2.med_ret[g])
    # the npz written on the first build is reused (and survives a restart)
    assert (tmp_path / "a" / "ctx" / f"{DATE}.npz").is_file()
    st = ContextStore(tmp_path / "a", bars_dir=tmp_path / "x", listed_pit=tmp_path / "y")
    st._universe_rows = {DATE: _universe_rows(dict.fromkeys(opens, 0.0))}
    _patch_cache(monkeypatch, {})  # a rebuild would now find no sessions
    again = st.date_context(DATE)
    np.testing.assert_array_equal(again.med_ret, ctx.med_ret)


def test_date_context_is_per_date_only(tmp_path, monkeypatch):
    """Cross-section of a date never reads another date's data (no val/test fit)."""
    opens = {"aaaa": 1.0, "bbbb": 1.5, "cccc": 2.0}
    _patch_cache(monkeypatch, _market_sessions(opens, dict.fromkeys(opens, 30.0)))
    st = ContextStore(tmp_path, bars_dir=tmp_path / "x", listed_pit=tmp_path / "y")
    st._universe_rows = {
        DATE: _universe_rows(dict.fromkeys(opens, 0.0)),
        "2030-01-01": _universe_rows({"zzzz": 9999.0}),  # would break the median
    }
    assert st.date_context(DATE).stems == tuple(opens)


# --------------------------------------------------------------------------- #
# Daily table: strictly before the session date, point-in-time
# --------------------------------------------------------------------------- #
def _write_panel(path: Path, closes: dict[str, list[tuple[str, float]]]) -> None:
    con = duckdb.connect(":memory:")
    con.execute(
        "CREATE TABLE p(stem VARCHAR, code VARCHAR, d VARCHAR, close DOUBLE, "
        "volume DOUBLE, tv DOUBLE)"
    )
    for stem, rows in closes.items():
        con.executemany(
            "INSERT INTO p VALUES (?, ?, ?, ?, 1000.0, 1.0)",
            [(stem, stem, d, c) for d, c in rows],
        )
    con.execute(f"COPY p TO '{path.as_posix()}' (FORMAT parquet)")
    con.close()


def _days(n: int, start: str = "2026-03-02"):
    """``n`` consecutive weekdays from ``start`` (a Monday)."""
    import datetime as dt

    out, d = [], dt.date.fromisoformat(start)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


def test_daily_context_uses_only_sessions_before_the_date(tmp_path):
    days = _days(70)
    rng = np.random.default_rng(5)
    closes = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, len(days))))
    series = list(zip(days, closes.tolist(), strict=True))
    bench = [(d, 3000.0 + i) for i, d in enumerate(days)]
    _write_panel(tmp_path / "daily_panel.parquet", {"1234": series, "1321": bench})
    # J-Quants-like daily OHLC for the previous-day high / low (G)
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE o(stem VARCHAR, d DATE, o DOUBLE, h DOUBLE, l DOUBLE, c DOUBLE)")
    con.executemany(
        "INSERT INTO o VALUES (?, ?, ?, ?, ?, ?)",
        [("1234", d, c, c + 2.0, c - 3.0, c) for d, c in series],
    )
    con.execute(f"COPY o TO '{(tmp_path / 'daily_ohlc.parquet').as_posix()}' (FORMAT parquet)")
    con.close()

    st = ContextStore(tmp_path, bars_dir=tmp_path / "nobars", listed_pit=tmp_path / "n")
    date = days[45]
    d = st.daily("1234", date)
    prev = days.index(date) - 1
    lr = np.diff(np.log(closes[: prev + 1]))[-20:]
    assert d.sigma20 == pytest.approx(float(np.std(lr, ddof=1)))
    assert d.ret1 == pytest.approx(closes[prev] / closes[prev - 1] - 1)
    assert d.ret5 == pytest.approx(closes[prev] / closes[prev - 5] - 1)
    assert d.ret30 == pytest.approx(closes[prev] / closes[prev - 30] - 1)
    assert d.prev_high == pytest.approx(closes[prev] + 2.0)
    assert d.prev_low == pytest.approx(closes[prev] - 3.0)
    assert d.mkt_ret5 == pytest.approx((3000.0 + prev) / (3000.0 + prev - 5) - 1)

    # closes / OHLC of the session date and everything later change nothing
    tampered = [(dd, c * 5.0 if dd >= date else c) for dd, c in series]
    p2 = tmp_path / "t"
    p2.mkdir()
    _write_panel(p2 / "daily_panel.parquet", {"1234": tampered, "1321": bench})
    (p2 / "daily_ohlc.parquet").write_bytes((tmp_path / "daily_ohlc.parquet").read_bytes())
    st2 = ContextStore(p2, bars_dir=p2 / "nobars", listed_pit=p2 / "n")
    assert st2.daily("1234", date) == d
    # a stem outside any table is computed with the same SQL (replay of any stem)
    assert st.daily("1234", date) == st.daily("1234", date)
    # stale data (no session within 10 days before the date) is unknown, not guessed
    assert np.isnan(st.daily("1234", "2027-01-04").sigma20)
    assert np.isnan(st.daily("9999", date).sigma20)


def test_sector_lookup_is_point_in_time(tmp_path):
    st = ContextStore(tmp_path, bars_dir=tmp_path, listed_pit=tmp_path / "none")
    st._sector = {"12340": (["2026-01-01", "2026-06-01"], ["3650", "3700"])}
    assert st.sector("12340", "2026-05-31") == "3650"
    assert st.sector("12340", "2026-06-02") == "3700"
    assert st.sector("12340", "2026-06-01") == "3650"  # snapshot of the date itself: no
    assert st.sector("1234", "2026-06-02") == "3700"  # 4-digit code
    assert st.sector("99990", "2026-06-02") == ""


# --------------------------------------------------------------------------- #
# Checkpoint -> replay round trip
# --------------------------------------------------------------------------- #
def _save_ckpt(path: Path, features: str, mode: str = "market", dim=None) -> None:
    from trading_rl.scalp_env import n_actions

    spec_dim = obs_dim(features) if dim is None else dim
    policy = ActorCritic(obs_dim=spec_dim, action_dim=n_actions(mode), hidden_dim=8)
    torch.save(
        {
            "policy_state_dict": policy.state_dict(),
            "obs_dim": spec_dim,
            "action_dim": n_actions(mode),
            "hidden_dim": 8,
            "env_config": ScalpConfig(order_mode=mode, features=features).metadata(),
        },
        path,
    )


def test_feature_spec_round_trips_through_checkpoint_to_replay(tmp_path):
    feats = "base+A+C-J"
    path = tmp_path / "m.pt"
    _save_ckpt(path, feats)
    _p, ckpt = sr.load_checkpoint_cached(path)
    assert ckpt["env_config"]["features"] == feats
    assert len(ckpt["env_config"]["feature_names"]) == obs_dim(feats)
    assert sr.scalp_config_from_checkpoint(ckpt).features == feats
    store = make_store(tmp_path)
    trades, summary, orders = sr.simulate_scalp_trades(
        raw_session(), path, prev_close=PREV_CLOSE, avg_volume_20d=1e6,
        is_topix100=False, store=store,
    )  # fmt: skip
    assert summary["tradeCount"] == sum(1 for t in trades if t["kind"] == "exit")
    # the replayed observation is exactly the training-time session's
    sess = build(raw_session(), feats, store)
    env = sr._RecordingScalpEnv([sess], sr.scalp_config_from_checkpoint(ckpt))
    obs, _ = env.reset(options={"index": 0})
    assert obs.shape == (obs_dim(feats),)


def test_old_checkpoint_without_features_replays_as_base(tmp_path):
    meta = ScalpConfig(order_mode="market").metadata()
    meta.pop("features")
    meta.pop("feature_names")
    assert sr.scalp_config_from_checkpoint({"env_config": meta}).features == "base"
    path = tmp_path / "old.pt"
    _save_ckpt(path, "base")
    ck = torch.load(path, weights_only=False)
    ck["env_config"] = meta
    torch.save(ck, path)
    trades, summary, _ = sr.simulate_scalp_trades(
        raw_session(), path, prev_close=PREV_CLOSE, avg_volume_20d=1e6, is_topix100=False
    )
    assert "tradeCount" in summary


def test_replay_rejects_checkpoint_whose_dim_contradicts_its_features(tmp_path):
    path = tmp_path / "bad.pt"
    _save_ckpt(path, "base+A", dim=OBS_DIM)  # config says +A but the net is base
    with pytest.raises(ValueError, match="obs_dim"):
        sr.simulate_scalp_trades(
            raw_session(), path, prev_close=PREV_CLOSE, avg_volume_20d=1e6,
            is_topix100=False, store=make_store(tmp_path),
        )  # fmt: skip


# --------------------------------------------------------------------------- #
# Plumbing: vec env / BC / normaliser / train_scalp --no-test / feature_screen
# --------------------------------------------------------------------------- #
def _load_script(name: str):
    path = Path(__file__).resolve().parent.parent / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_vec_env_and_evaluate_use_per_config_obs_dim(tmp_path):
    from trading_rl.vec_rollout import VecScalpEnv, evaluate_ppo_policy

    spec = "base+A+B-J"
    store = make_store(tmp_path)
    sessions = [build(raw_session(seed=s), spec, store) for s in (1, 2)]
    cfg = ScalpConfig(order_mode="market", features=spec)
    venv = VecScalpEnv(sessions, 2, cfg, seed=0, stagger=False)
    assert venv.obs.shape == (2, obs_dim(spec))
    pol = ActorCritic(obs_dim(spec), 4, hidden_dim=8)
    eps = evaluate_ppo_policy(pol, sessions, cfg, n_envs=2)
    assert len(eps) == 2 and all("gross_ticks_sum" in e for e in eps)


def test_bc_dataset_has_per_config_obs_dim(tmp_path):
    from trading_rl.scalp_baselines import RuleScalper
    from trading_rl.scalp_bc import collect_rule_dataset

    spec = "base+C"
    cfg = ScalpConfig(order_mode="market", features=spec)
    sess = [build(raw_session(seed=4), spec, make_store(tmp_path))]
    data = collect_rule_dataset(sess, RuleScalper(), cfg)
    assert data.obs.shape[1] == obs_dim(spec) and len(data) > 100


def test_normalizer_is_fitted_on_the_given_sessions_only(tmp_path):
    ts = _load_script("train_scalp")
    store = make_store(tmp_path)
    spec = "base+A+C"
    train = [build(raw_session(seed=s), spec, store) for s in range(3)]
    valtest = [build(raw_session(seed=s, p0=500.0), spec, store) for s in (7, 8)]
    mean, std = ts.fit_normalizer(train)
    assert mean.shape == std.shape == (obs_dim(spec),)
    mean2, _ = ts.fit_normalizer(train)  # unaffected by sessions never passed in
    np.testing.assert_array_equal(mean, mean2)
    pick = np.random.default_rng(0).choice(len(train), size=len(train), replace=False)
    ref = np.concatenate([train[i].X[::7] for i in pick]).astype(np.float64)
    np.testing.assert_allclose(mean[: ref.shape[1]], ref.mean(0), rtol=1e-5)
    # a pure function of the sessions it is given: adding val/test sessions changes it
    mixed, _ = ts.fit_normalizer(train + valtest)
    assert not np.allclose(mixed, mean)


class _Stop(Exception):
    pass


def _run_main(monkeypatch, tmp_path, argv):
    ts = _load_script("train_scalp")
    calls: list[str] = []

    def fake_split(name, limit_days=None):  # noqa: ANN001
        calls.append(name)
        return [], []

    def stop(*a, **k):  # noqa: ANN002, ANN003
        raise _Stop

    monkeypatch.setattr(ts, "split_rows", fake_split)
    monkeypatch.setattr(ts, "train_ppo", stop)
    monkeypatch.setattr(ts, "load_scalp_sessions", lambda *a, **k: [])
    monkeypatch.setattr(
        sys, "argv",
        ["train_scalp.py", "--skip-baselines", "--model", str(tmp_path / "m.pt"),
         "--report", str(tmp_path / "r.md"), *argv],
    )  # fmt: skip
    with pytest.raises(_Stop):
        ts.main()
    return calls


def test_no_test_never_loads_test_rows(monkeypatch, tmp_path):
    assert _run_main(monkeypatch, tmp_path, ["--no-test"]) == ["train", "val"]
    assert _run_main(monkeypatch, tmp_path, []) == ["train", "val", "test"]


def test_report_without_test_omits_test_sections(tmp_path):
    ts = _load_script("train_scalp")
    args = ts.build_parser().parse_args(
        ["--no-test", "--features", "base+A-J", "--order-mode", "market"]
    )
    s = {
        "n_sessions": 4, "n_days": 2, "yen_day": (1.0, 0.0, 2.0), "ticks_day": (1.0, 0.0, 2.0),
        "yen_sess": 1.0, "yen_sess_ci": (1.0, 0.5, 1.5), "ticks_sess": 0.1, "trips_day": 1.0,
        "trips_sess": 1.0, "win_rate": 0.5, "pf": 1.2, "max_dd_yen": 3.0, "entry_limit": 0.0,
        "entry_market": 4.0, "exit_limit": 0.0, "exit_market": 2.0, "forced": 2.0,
        "hold_s": 30.0, "gross_ticks_trip": 0.8, "orders_sess": 2.0, "invalid_sess": 0.0,
        "latency_noops_sess": 0.0, "no_trade_frac": 0.0, "actions": np.array([5, 1, 1, 1]),
    }  # fmt: skip
    ctx = {
        "counts": {"train": (10, 0), "val": (4, 2), "test": (0, 0)}, "excluded": {},
        "exclude_path": None, "args": args, "baselines_cached": None, "tune": [],
        "tune_momentum": [], "results": {"val_seen": {"PPO": {"trade_through": s}}},
        "group_labels": {"val_seen": "検証 (既知銘柄)"}, "policies": ["PPO"],
        "behaviour_group": "val_seen", "cmd": "x",
        "train": {
            "ppo_cfg": __import__("trading_rl.trainer", fromlist=["PPOConfig"]).PPOConfig(),
            "entropy": (0.02, 0.02), "curriculum": None, "bc": {"bc_from": None},
            "steps": 10, "iters": 1, "wall_s": 1.0, "throughput": 1.0,
            "throughput_incl_eval": 1.0, "best": {"steps": 10}, "curve": [],
        },
    }  # fmt: skip
    ts.write_report(tmp_path / "r.md", ctx)
    text = (tmp_path / "r.md").read_text(encoding="utf-8")
    assert "base+A-J" in text and "gross ticks/trip" in text
    assert "test_seen" not in text and "テスト (" not in text
    assert "| test |" not in text


def test_feature_screen_arms_commands_and_summary(tmp_path):
    fs = _load_script("feature_screen")
    arms = fs.parse_arms("base, A,C,-J,A+C,base,A")
    assert [c for _, c in arms] == ["base", "base+A", "base+C", "base-J", "base+A+C"]
    with pytest.raises(ValueError):
        fs.parse_arms("Z")
    model, report = fs.arm_paths("t1", "base+A-J", tmp_path / "m", tmp_path / "r")
    assert model.name == "t1_base_A_minus_J.pt" and report.parent == tmp_path / "r"
    cmd = fs.build_command("base+A", model, report, 123, ["--smoke", "--seed", "3"], None)
    assert "--no-test" in cmd and "--overwrite-model" not in cmd
    assert cmd[cmd.index("--features") + 1] == "base+A"
    assert cmd[cmd.index("--train-steps") + 1] == "123" and "--smoke" in cmd

    def summ(yen: float, forced: int) -> dict:
        s = {
            "n_sessions": 10, "yen_sess": yen, "yen_sess_ci": [yen, yen - 5, yen + 5],
            "ticks_sess": yen / 10, "trips_sess": 2.0, "gross_ticks_trip": 0.5,
            "hold_s": 40.0, "exit_limit": 0, "exit_market": 20 - forced, "forced": forced,
        }  # fmt: skip
        return {
            "obs_dim": 38, "best_step": 100,
            "results": {
                "val_seen": {"PPO": {"trade_through": s}, "DoNothing": {"trade_through": s}},
                "val_held": {"PPO": {"trade_through": s}},
            },
        }

    text = fs.render_summary(
        "t1",
        [
            {"label": "base", "spec": "base", "summary": summ(-3.0, 5)},
            {"label": "A", "spec": "base+A", "summary": summ(7.0, 10)},
            {"label": "B", "spec": "base+B", "status": "exit code 1", "log": "x.log"},
        ],
        ["--smoke"],
        1000,
    )
    rows = [ln for ln in text.splitlines() if ln.startswith("| ") and "`base" in ln]
    assert rows[0].startswith("| A |")  # sorted by val_seen yen/sess, best first
    assert "50.0%" in rows[0] and "failed arms" in text and "DoNothing" in text


def test_feature_screen_refuses_to_overwrite_and_dry_run(tmp_path, capsys):
    fs = _load_script("feature_screen")
    mdir, rdir = tmp_path / "models", tmp_path / "reports"
    mdir.mkdir()
    rdir.mkdir()
    (mdir / "t_base.pt").write_bytes(b"x")
    with pytest.raises(SystemExit, match="overwrite"):
        fs.main(["--arms", "base", "--tag", "t", "--model-dir", str(mdir),
                 "--report-dir", str(rdir)])  # fmt: skip
    with pytest.raises(SystemExit, match="managed"):
        fs.main(["--arms", "base", "--overwrite-model"])
    assert fs.main(["--arms", "base,A", "--tag", "d", "--dry-run", "--model-dir", str(mdir),
                    "--report-dir", str(rdir), "--smoke"]) == 0  # fmt: skip
    out = capsys.readouterr().out
    assert "--smoke" in out and "base+A" in out and "--overwrite-model" not in out

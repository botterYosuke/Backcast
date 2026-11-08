"""Tests for feature groups K-Q (trading_rl.scalp_features_ext)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from test_scalp_features import (
    DATE,
    DAY0,
    PREV_CLOSE,
    T_OPEN,
    _perturbed,
    _save_ckpt,
    build,
    col,
    make_store,
    raw_session,
)

from trading_rl import scalp_features_ext as fx
from trading_rl import scalp_replay as sr
from trading_rl.data import OpeningSessionData
from trading_rl.scalp_env import ScalpConfig, ScalpEnv, obs_dim, obs_names
from trading_rl.scalp_ext_build import classify_title
from trading_rl.scalp_features import (
    EXTRA_NAMES,
    market_names,
    n_market,
    parse_feature_spec,
)

EXT = "KLMNOPQ"
ALL_EXT = "base+" + "+".join(EXT)
OFF = (T_OPEN - DAY0) / 1e6  # stock opens 2 s after 09:00:00 -> clock g = s + 2


def _ed(tmp_path: Path):
    return fx.ext_data(tmp_path)


def _weekdays(end: str, n: int) -> np.ndarray:
    days = np.arange(np.datetime64(end, "D") - np.timedelta64(2 * n + 10, "D"), np.datetime64(end, "D"))
    days = days[(days.astype("datetime64[D]").astype(int) + 4) % 7 < 5]  # Mon-Fri
    return days[-n:]


def _series(d: np.ndarray, c: np.ndarray, adjf: float = 1.0) -> fx._Series:
    return fx._Series(d, np.asarray(c, dtype=np.float64), np.full(len(c), adjf))


def _etf_grid(base: float, first: int, slope: float = 1e-4) -> np.ndarray:
    g = np.arange(fx.ETF_GRID_LEN, dtype=np.float64)
    out = base * (1.0 + slope * g)
    out[:first] = np.nan
    return out.astype(np.float32)


def _install_k(ed, *, first_1570=100, beta=1.5, seed=0):
    grids = np.stack([_etf_grid(20000.0, first_1570), _etf_grid(30000.0, 1), _etf_grid(2000.0, 1)])
    ed._etf[DATE] = fx.EtfDay(DATE, fx.ETF_STEMS, grids)
    d_prev = np.datetime64("2026-08-13")
    for stem, c in (("1570", 20000.0), ("1321", 30000.0), ("1306", 2000.0)):
        ed._series = ed._series or {}
        ed._series[stem] = _series(np.array([d_prev]), [c])
    days = _weekdays("2026-08-14", 80)
    rng = np.random.default_rng(seed)
    rt = rng.normal(0, 0.01, len(days) + 5)
    ct = 1000.0 * np.cumprod(1 + rt)
    all_days = np.concatenate([days, np.arange(np.datetime64("2026-08-14"), np.datetime64("2026-08-19"))])
    ed._topix = _series(all_days, ct[: len(all_days)])
    ed._topix_loaded = True
    rs = beta * rt[: len(all_days)] + rng.normal(0, 0.0005, len(all_days))
    cs = 500.0 * np.cumprod(1 + rs)
    ed._series["1234"] = _series(all_days, cs)
    return ed


# --------------------------------------------------------------------------- #
# Spec / dimensions / base unchanged
# --------------------------------------------------------------------------- #
def test_spec_parses_new_groups_and_mixes_with_old():
    assert parse_feature_spec("base+K").canonical == "base+K"
    assert parse_feature_spec("base+Q+K+L+M+N+O+P").canonical == ALL_EXT
    assert parse_feature_spec("base+K+C+G-J").canonical == "base+C+G+K-J"
    assert parse_feature_spec("k+l").canonical == "base+K+L"
    assert parse_feature_spec(ALL_EXT).groups == tuple(EXT)
    for bad in ("-K", "base-Q", "base+KL", "base+R"):
        with pytest.raises(ValueError):
            parse_feature_spec(bad)
        with pytest.raises(ValueError):
            ScalpConfig(features=bad)


def test_column_counts_and_names():
    want = {"K": 26, "L": 9, "M": 14, "N": 14, "O": 9, "P": 4, "Q": 11}
    for g, n in want.items():
        assert len(EXTRA_NAMES[g]) == n == len(fx.EXT_NAMES[g])
        assert n_market(f"base+{g}") == 28 + n
        assert obs_dim(f"base+{g}") == 38 + n
        assert all(x.startswith(g.lower() + "_") for x in EXTRA_NAMES[g])
    assert n_market(ALL_EXT) == 28 + sum(want.values()) == 115
    assert obs_dim(ALL_EXT) == 125 == len(obs_names(ALL_EXT))
    names = market_names("base+A+B+C+D+E+F+G+H+I+K+L+M+N+O+P+Q")
    assert len(names) == len(set(names))
    assert market_names("base+K")[:28] == market_names("base")


def test_existing_groups_unchanged_when_new_groups_added(tmp_path):
    store = make_store(tmp_path)
    raw = raw_session(seed=5)
    old = build(raw, "base+A+C+G+H", store)
    new = build(raw, "base+A+C+G+H+K+L+M+N+O+P+Q", store)
    np.testing.assert_array_equal(new.X[:, : old.X.shape[1]], old.X)
    assert new.X.shape[1] == old.X.shape[1] + 87


@pytest.mark.parametrize("spec", [f"base+{g}" for g in EXT] + [ALL_EXT, ALL_EXT + "-J"])
def test_obs_finite_and_env_runs_without_any_ext_cache(spec, tmp_path):
    store = make_store(tmp_path)  # empty ext dir: everything unknown -> zeros
    sess = build(raw_session(), spec, store)
    assert sess.X.shape == (590, n_market(spec))
    assert np.isfinite(sess.X).all()
    env = ScalpEnv(config=ScalpConfig(features=spec))
    obs, _ = env.reset(options={"session": sess})
    for _ in range(5):
        obs, *_ = env.step(0)
    assert obs.shape == (obj := obs_dim(spec),) and np.isfinite(obs).all() and obj > 38
    for n in ("k_known_1570", "l_known", "m_covered", "n_known", "p_known"):
        if n in market_names(spec):
            assert not col(sess, spec, n).any()


# --------------------------------------------------------------------------- #
# K - index real time
# --------------------------------------------------------------------------- #
def test_group_K_returns_known_flags_gap_and_residual(tmp_path):
    store = make_store(tmp_path)
    ed = _install_k(_ed(tmp_path), beta=1.5)
    raw = raw_session(seed=2)
    sess = build(raw, "base+K", store)
    c = lambda n: col(sess, "base+K", n)  # noqa: E731
    s = np.arange(10, 600)
    g = s + 2
    # 1570 only known from its first print (clock second 100)
    assert (c("k_known_1570") == (g >= 100)).all()
    assert not c("k_1570_ret15_bps")[g < 100].any() and not c("k_1570_gap_bps")[g < 100].any()
    assert c("k_known_1321").all() and c("k_known_1306").all()
    # linear +1 bp/s grid: ret over lag L at clock g is (1+1e-4 g)/(1+1e-4 (g-L)) - 1
    k = 300 - 10

    def lin(lag: int) -> float:
        return ((1 + 1e-4 * 302) / (1 + 1e-4 * (302 - lag)) - 1) * 1e4

    assert c("k_1306_ret15_bps")[k] == pytest.approx(lin(15), abs=0.01)
    assert c("k_1306_ret60_bps")[k] == pytest.approx(lin(60), abs=0.01)
    assert c("k_1570_ret5_bps")[k] == pytest.approx(lin(5), abs=0.01)
    assert c("k_1570_ret30_bps")[k] == pytest.approx(lin(30), abs=0.01)
    # since the ETF's own first print (1570: g=100)
    assert c("k_1570_since_open_bps")[k] == pytest.approx(
        ((1 + 1e-4 * 302) / (1 + 1e-4 * 100) - 1) * 1e4, abs=0.01
    )
    # gap = first print / previous close - 1; vs_prev_close = last / previous close - 1
    assert c("k_1570_gap_bps")[k] == pytest.approx((1 + 1e-4 * 100 - 1) * 1e4, abs=0.01)
    assert c("k_1306_vs_prev_close_bps")[k] == pytest.approx(1e-4 * 302 * 1e4, abs=0.01)
    # residual = stock return - beta * 1306 return over the same window
    beta, _corr, n = ed.beta_corr("1234", DATE)
    assert n == 60 and beta == pytest.approx(1.5, abs=0.1)
    assert c("k_beta60")[k] == pytest.approx(beta)
    assert c("k_beta_known").all()
    last = sess.last
    for lag, name in ((15, "k_resid_ret15_bps"), (60, "k_resid_ret60_bps")):
        sk = np.arange(10, 600)
        stock = (last[sk] / last[np.maximum(sk - lag, 0)] - 1) * 1e4
        gi = sk + 2
        gl = np.floor(OFF + np.maximum(sk - lag, 0)).astype(int)
        grid = ed._etf[DATE].series("1306").astype(np.float64)
        idx = (grid[gi] / grid[np.maximum(gl, 1)] - 1) * 1e4
        np.testing.assert_allclose(c(name), np.clip(stock - np.clip(beta, -1, 4) * idx, -2000, 2000), atol=1e-3)
    # since the stock's own open
    stock_so = (last[s] / sess.p_open - 1) * 1e4
    idx_so = (ed._etf[DATE].series("1306").astype(np.float64)[g] / ed._etf[DATE].series("1306")[2] - 1) * 1e4
    np.testing.assert_allclose(
        c("k_resid_since_open_bps"), np.clip(stock_so - beta * idx_so, -2000, 2000), atol=1e-3
    )


def test_group_K_ignores_etf_prints_after_t(tmp_path):
    store = make_store(tmp_path)
    ed = _install_k(_ed(tmp_path))
    raw = raw_session(seed=2)
    a = build(raw, "base+K", store)
    cut_g = 250
    grids = ed._etf[DATE].grid.copy()
    grids[:, cut_g + 1 :] *= 1.9
    ed._etf[DATE] = fx.EtfDay(DATE, fx.ETF_STEMS, grids)
    b = build(raw, "base+K", store)
    n = cut_g - 2 - 10 + 1  # decisions with clock second <= cut_g
    np.testing.assert_array_equal(a.X[:n], b.X[:n])
    assert not np.array_equal(a.X[-1], b.X[-1])


def test_group_K_beta_uses_only_days_before_the_session(tmp_path):
    store = make_store(tmp_path)
    ed = _install_k(_ed(tmp_path))
    raw = raw_session(seed=2)
    a = build(raw, "base+K+N", store)
    # corrupt the session-date row and everything after (stock and TOPIX)
    for s in (ed._topix, ed._series["1234"]):
        late = s.d >= np.datetime64(DATE)
        s.c[late] = s.c[late] * 3.7
        s._ret = None
    ed._beta.clear()
    b = build(raw, "base+K+N", store)
    np.testing.assert_array_equal(a.X, b.X)
    # but the day before does matter
    for s in (ed._topix, ed._series["1234"]):
        j = int(np.searchsorted(s.d, np.datetime64(DATE))) - 3
        s.c[j] *= 1.2
        s._ret = None
    ed._beta.clear()
    c = build(raw, "base+K+N", store)
    assert not np.array_equal(a.X, c.X)


def test_etf_grid_builder_never_looks_past_its_instant(tmp_path):
    """build_etf puts a print at time t into grid second ceil(t)."""
    import duckdb

    from trading_rl.scalp_ext_build import build_etf

    trades = tmp_path / "trades"
    trades.mkdir()
    day = "2025-03-03"
    for stem, rows in {
        "1306": [("09:00:00.500000", 100.0), ("09:00:01.000000", 101.0), ("09:00:02.250000", 102.0)],
        "1321": [("09:00:00.000000", 50.0)],
    }.items():
        con = duckdb.connect(str(trades / f"{stem}.duckdb"))
        con.execute(
            "CREATE TABLE stocks_board (Price DOUBLE, Qty BIGINT, Type VARCHAR, source VARCHAR, "
            "Code VARCHAR, Timestamp VARCHAR)"
        )
        for t, p in rows:
            con.execute("INSERT INTO stocks_board VALUES (?, 1, '1', 'x', ?, ?)", [p, stem + "0", f"{day} {t}"])
        con.close()
    assert build_etf(tmp_path, [day], trades_dir=trades, progress=lambda m: None) == 1
    z = np.load(tmp_path / "ext" / "etf" / f"{day}.npz")
    g = z["grid"][list(z["stems"]).index("1306")]
    assert np.isnan(g[0])  # the 09:00:00.5 print is not visible at 09:00:00.0
    assert g[1] == 101.0  # visible at 09:00:01.0 (the exact-second print wins)
    assert g[2] == 101.0 and g[3] == 102.0  # 09:00:02.25 visible from second 3
    assert z["grid"][list(z["stems"]).index("1321")][0] == 50.0
    assert np.isnan(z["grid"][list(z["stems"]).index("1570")]).all()


# --------------------------------------------------------------------------- #
# L - sector peers
# --------------------------------------------------------------------------- #
def _peer_day(sectors=("3650", "3650", "3700", "3650"), opens=(2.0, 1.0, 3.0, 5.0), G=900):
    slopes = np.array([0.0, 2.0, 7.0, -1.0])
    g = np.arange(G)
    R = np.full((4, G), np.nan, dtype=np.float32)
    for i in range(4):
        R[i, g >= opens[i]] = slopes[i] * (g[g >= opens[i]] - opens[i])
    return fx.PeerDay(DATE, ("1234", "aaaa", "bbbb", "cccc"), sectors, np.array(opens), R)


def test_group_L_same_sector_peers_excluding_self(tmp_path):
    store = make_store(tmp_path)
    ed = _ed(tmp_path)
    ed._peer[DATE] = _peer_day()
    sess = build(raw_session(seed=4), "base+L", store)
    c = lambda n: col(sess, "base+L", n)  # noqa: E731
    k = 100 - 10  # s = 100 -> g = 102
    assert c("l_known").all()
    assert c("l_log1p_n_peers")[k] == pytest.approx(np.log1p(2))  # aaaa + cccc, not bbbb/self
    # since-open mean of aaaa (2*(102-1)=202) and cccc (-1*(102-5)=-97)
    assert c("l_peer_since_open_bps")[k] == pytest.approx((202 - 97) / 2, abs=0.01)
    # 15 s look-back ~ slope * 15
    assert c("l_peer_ret15_bps")[k] == pytest.approx((30 - 15) / 2, abs=0.5)
    assert c("l_peer_ret60_bps")[k] == pytest.approx((120 - 60) / 2, abs=2.5)
    stock = (sess.last[100] / sess.p_open - 1) * 1e4
    assert c("l_rel_since_open_bps")[k] == pytest.approx(stock - 52.5, abs=0.01)
    assert c("l_peer_frac_up")[k] == pytest.approx(0.5)
    # peer that opens later is excluded until it has printed
    ed._peer.clear()
    ed._peer[DATE] = _peer_day(opens=(2.0, 1.0, 3.0, 400.0))
    late = build(raw_session(seed=4), "base+L", store)
    n = np.array([np.expm1(v) for v in col(late, "base+L", "l_log1p_n_peers")]).round()
    assert n[100 - 10] == 1 and n[500 - 10] == 2


def test_group_L_unknown_without_same_sector_peer_or_cache(tmp_path):
    store = make_store(tmp_path)
    ed = _ed(tmp_path)
    ed._peer[DATE] = _peer_day(sectors=("3650", "3700", "3700", "3800"))
    sess = build(raw_session(), "base+L", store)
    assert not sess.X[:, -9:].any()
    ed._peer[DATE] = None
    assert not build(raw_session(), "base+L", store).X[:, -9:].any()


def test_group_L_ignores_peer_prints_after_t(tmp_path):
    store = make_store(tmp_path)
    ed = _ed(tmp_path)
    pd = _peer_day()
    ed._peer[DATE] = pd
    raw = raw_session(seed=4)
    a = build(raw, "base+L", store)
    cut_g = 300
    R = pd.R.copy()
    R[:, cut_g + 1 :] += 777.0
    ed._peer[DATE] = fx.PeerDay(DATE, pd.stems, pd.sector, pd.open_off, R)
    b = build(raw, "base+L", store)
    n = cut_g - 2 - 10 + 1
    np.testing.assert_array_equal(a.X[:n], b.X[:n])
    assert not np.array_equal(a.X[-1], b.X[-1])


# --------------------------------------------------------------------------- #
# M - disclosures
# --------------------------------------------------------------------------- #
def _us(day: str, hms: str) -> int:
    return fx._midnight_us(day) + sum(int(x) * m for x, m in zip(hms.split(":"), (3600, 60, 1), strict=True)) * 1_000_000


def _install_m(ed, rows, days=("2025-03-03", "2025-03-04")):
    rows = sorted(rows, key=lambda r: r[0])
    ts = np.array([r[0] for r in rows], dtype=np.int64)
    flags = np.array([r[1] for r in rows], dtype=np.int8).reshape(-1, 7)
    ed._disc = {
        "1234": fx.Disc(
            ts, flags,
            np.array([r[2] for r in rows], dtype=np.float64),
            np.array([r[3] for r in rows], dtype=np.float64),
            np.array([r[4] for r in rows], dtype=bool),
        )
    }  # fmt: skip
    ed._tdnet_days = set(days)
    ed._calendar = np.array(["2025-02-28", "2025-03-03", "2025-03-04"], dtype="datetime64[D]")
    ed._calendar_loaded = True
    ed._sched = {}
    return ed


NAN = float("nan")


def test_classify_title_keywords():
    assert classify_title("2025年3月期 決算短信〔日本基準〕(連結)") == (1, 0, 0, 0, 0, 0)
    assert classify_title("業績予想の上方修正に関するお知らせ") == (0, 1, 0, 0, 0, 0)
    assert classify_title("通期業績予想の下方修正のお知らせ") == (0, 0, 1, 0, 0, 0)
    assert classify_title("自己株式の取得に係る事項の決定に関するお知らせ") == (0, 0, 0, 1, 0, 0)
    assert classify_title("剰余金の配当に関するお知らせ") == (0, 0, 0, 0, 1, 0)
    assert classify_title("役員人事に関するお知らせ") == (0, 0, 0, 0, 0, 1)


def test_group_M_window_is_previous_close_to_t_open(tmp_path):
    ed = _ed(tmp_path)
    t_open = _us("2025-03-04", "09:00:02")
    _install_m(
        ed,
        [
            (_us("2025-03-03", "14:59:59"), [0, 0, 0, 0, 0, 1, 0], NAN, NAN, False),  # before 15:00: out
            (_us("2025-03-03", "15:45:00"), [1, 0, 0, 0, 0, 0, 0], NAN, NAN, False),  # after 15:00: in
            (_us("2025-03-04", "08:30:00"), [0, 0, 0, 1, 0, 0, 0], NAN, NAN, False),  # pre-open: in
            (t_open, [0, 0, 0, 0, 1, 0, 0], NAN, NAN, False),  # exactly T_open: out
            (_us("2025-03-04", "09:10:00"), [0, 1, 0, 0, 0, 0, 0], NAN, NAN, False),  # after: out
        ],
    )
    v = fx.disclosure_scalars(ed, "1234", "2025-03-04", t_open)
    assert v["m_covered"] == 1.0
    assert v["m_log1p_n_disc"] == pytest.approx(np.log1p(2))
    assert (v["m_earn_disc"], v["m_buyback"]) == (1.0, 1.0)
    assert (v["m_dividend"], v["m_up_rev"], v["m_other"]) == (0.0, 0.0, 0.0)
    # moving T_open before the pre-open item drops it (strict <)
    v2 = fx.disclosure_scalars(ed, "1234", "2025-03-04", _us("2025-03-04", "08:30:00"))
    assert v2["m_buyback"] == 0.0 and v2["m_log1p_n_disc"] == pytest.approx(np.log1p(1))


def test_group_M_window_starts_at_15_00_also_after_the_close_extension(tmp_path):
    ed = _ed(tmp_path)
    ed._calendar = np.array(["2025-02-28", "2025-03-03", "2025-03-04"], dtype="datetime64[D]")
    ed._calendar_loaded = True
    ed._tdnet_days = {"2025-03-03", "2025-03-04"}
    ed._sched = {}
    rows = [
        (_us("2025-03-03", "14:59:00"), [0, 0, 0, 0, 0, 1, 0]),
        (_us("2025-03-03", "15:00:00"), [1, 0, 0, 0, 0, 0, 0]),  # exactly 15:00 is in
    ]
    ed._disc = {"1234": fx.Disc(
        np.array([r[0] for r in rows]), np.array([r[1] for r in rows], dtype=np.int8),
        np.full(2, NAN), np.full(2, NAN), np.zeros(2, dtype=bool),
    )}  # fmt: skip
    v = fx.disclosure_scalars(ed, "1234", "2025-03-04", _us("2025-03-04", "09:00:00"))
    assert (v["m_earn_disc"], v["m_other"], v["m_log1p_n_disc"]) == (1.0, 0.0, pytest.approx(np.log1p(1)))
    assert fx.disc_window_start_us("2025-03-03") == _us("2025-03-03", "15:00:00")


def test_group_M_revision_surprise_and_earnings_due(tmp_path):
    ed = _ed(tmp_path)
    t_open = _us("2025-03-04", "09:00:02")
    _install_m(
        ed,
        [
            (_us("2025-03-03", "15:30:00"), [0, 0, 0, 0, 0, 0, 1], -0.25, NAN, True),
            (_us("2025-03-03", "16:00:00"), [0, 0, 0, 0, 0, 0, 1], NAN, 0.4, False),
        ],
    )
    ed._sched = {"1234": [(np.array(["2025-02-01"], "datetime64[D]"), np.array(["2025-03-04"], "datetime64[D]"))]}
    v = fx.disclosure_scalars(ed, "1234", "2025-03-04", t_open)
    assert (v["m_rev_known"], v["m_rev_pct"], v["m_down_rev"], v["m_up_rev"]) == (1.0, -0.25, 1.0, 0.0)
    assert (v["m_surprise_known"], v["m_op_surprise"]) == (1.0, pytest.approx(0.4))
    assert v["m_earn_disc"] == 1.0 and v["m_earn_due_today"] == 1.0 and v["m_earn_due_prev"] == 0.0
    # a quarterly statement (not an EarnForecastRevision record) also carries the revision
    _install_m(ed, [(_us("2025-03-03", "15:30:00"), [0, 0, 0, 0, 0, 0, 1], 0.1, NAN, False)])
    v = fx.disclosure_scalars(ed, "1234", "2025-03-04", t_open)
    assert (v["m_rev_known"], v["m_rev_pct"], v["m_up_rev"], v["m_down_rev"]) == (1.0, pytest.approx(0.1), 1.0, 0.0)
    ed._sched = {"1234": [(np.array(["2025-02-01"], "datetime64[D]"), np.array(["2025-03-04"], "datetime64[D]"))]}
    # a schedule published on/after the session date is not known yet
    ed._sched = {"1234": [(np.array(["2025-03-04"], "datetime64[D]"), np.array(["2025-03-04"], "datetime64[D]"))]}
    assert fx.disclosure_scalars(ed, "1234", "2025-03-04", t_open)["m_earn_due_today"] == 0.0


def test_group_M_not_covered_gives_zero_flags(tmp_path):
    ed = _ed(tmp_path)
    rows = [(_us("2025-03-03", "15:45:00"), [1, 0, 0, 0, 0, 0, 0], NAN, NAN, False)]
    _install_m(ed, rows, days=("2025-03-03",))  # no TDnet file for the session day
    t_open = _us("2025-03-04", "09:00:02")
    assert not any(fx.disclosure_scalars(ed, "1234", "2025-03-04", t_open).values())
    _install_m(ed, rows, days=("2025-03-04",))  # no file for the previous day
    assert not any(fx.disclosure_scalars(ed, "1234", "2025-03-04", t_open).values())
    # TDnet coverage ends 2026-07-17
    ed._tdnet_days = {"2026-07-20", "2026-07-21"}
    ed._calendar = np.array(["2026-07-17", "2026-07-20", "2026-07-21"], dtype="datetime64[D]")
    assert not any(fx.disclosure_scalars(ed, "1234", "2026-07-21", _us("2026-07-21", "09:00:02")).values())
    # and through the session path (DATE = 2026-08-14 is past the end)
    store = make_store(tmp_path)
    sess = build(raw_session(), "base+M", store)
    assert not sess.X[:, -14:].any()


# --------------------------------------------------------------------------- #
# N - index regime
# --------------------------------------------------------------------------- #
def test_group_N_regime_values_and_no_lookahead(tmp_path):
    store = make_store(tmp_path)
    ed = _install_k(_ed(tmp_path))
    days = _weekdays("2026-08-14", 80)
    rng = np.random.default_rng(3)
    c1321 = 30000.0 * np.cumprod(1 + rng.normal(0, 0.01, len(days) + 5))
    all_days = np.concatenate([days, np.arange(np.datetime64("2026-08-14"), np.datetime64("2026-08-19"))])
    ed._series["1321"] = _series(all_days, c1321[: len(all_days)])
    raw = raw_session(seed=1)
    sess = build(raw, "base+N", store)
    c = lambda n: col(sess, "base+N", n)[0]  # noqa: E731
    top = ed._topix
    i = int(np.searchsorted(top.d, np.datetime64(DATE))) - 1  # last session before date
    r = top.ret()
    assert c("n_known") == 1.0
    assert c("n_topix_ret1_pct") == pytest.approx(r[i] * 100, abs=1e-4)
    assert c("n_topix_ret5_pct") == pytest.approx((top.c[i] / top.c[i - 5] - 1) * 100, abs=1e-4)
    assert c("n_topix_ret20_pct") == pytest.approx((top.c[i] / top.c[i - 20] - 1) * 100, abs=1e-4)
    assert c("n_topix_vol20_pct") == pytest.approx(np.std(r[i - 19 : i + 1], ddof=1) * 100, abs=1e-4)
    assert c("n_topix_dist_ma25_pct") == pytest.approx((top.c[i] / top.c[i - 24 : i + 1].mean() - 1) * 100, abs=1e-4)
    beta, corr, _ = ed.beta_corr("1234", DATE)
    assert c("n_beta60") == pytest.approx(beta) and c("n_corr60") == pytest.approx(corr)
    assert 0.5 < corr <= 1.0 and c("n_beta_known") == 1.0
    # the session date and later do not matter
    for s in (top, ed._series["1321"], ed._series["1234"]):
        s.c[s.d >= np.datetime64(DATE)] *= 5.0
        s._ret = None
    ed._beta.clear()
    np.testing.assert_array_equal(build(raw, "base+N", store).X, sess.X)


def test_group_N_stale_or_missing_daily_is_unknown(tmp_path):
    store = make_store(tmp_path)
    ed = _install_k(_ed(tmp_path))
    ed._series["1321"] = _series(_weekdays("2026-06-01", 60), np.linspace(100, 110, 60))  # stale
    sess = build(raw_session(), "base+N", store)
    assert col(sess, "base+N", "n_known").sum() == 0
    assert col(sess, "base+N", "n_topix_ret1_pct").sum() == 0  # all-or-nothing index block


# --------------------------------------------------------------------------- #
# O - large prints
# --------------------------------------------------------------------------- #
def _ticks(rows: list[tuple[float, float, int]]) -> OpeningSessionData:
    t = np.array([int(r[0] * 1e6) for r in rows], dtype=np.int64)
    p = np.array([r[1] for r in rows], dtype=np.float64)
    q = np.array([r[2] for r in rows], dtype=np.int64)
    return OpeningSessionData(
        stem="1234", code="12340", date=DATE, t_open_us=T_OPEN, p_open=float(p[0]),
        v_open=int(q[0]), epoch_us=T_OPEN + t, price=p, qty=q,
        trade_type=np.full(len(t), "", dtype=object),
    )  # fmt: skip


def test_group_O_large_share_and_tick_rule_imbalance(tmp_path):
    store = make_store(tmp_path)  # avg_volume_20d = 1e6 -> threshold max(2e-4*1e6, 500) = 500
    raw = _ticks(
        [
            (0.0, 100.0, 5000),  # opening print: excluded
            (5.0, 101.0, 100_000),  # large, up-tick = buy
            (20.0, 100.0, 600),  # large, down-tick = sell
            (40.0, 100.0, 100),  # small
            (50.0, 100.0, 700),  # large, zero tick keeps the sell sign
        ]
    )
    sess = build(raw, "base+O", store)
    c = lambda n, s: col(sess, "base+O", n)[s - 10]  # noqa: E731
    assert c("o_known", 60) == 1.0
    assert c("o_large_share_open", 10) == pytest.approx(1.0)  # opening print not counted
    assert c("o_large_imb_open", 10) == pytest.approx(1.0)
    assert c("o_large_share_open", 60) == pytest.approx(101_300 / 101_400)
    assert c("o_large_imb_open", 60) == pytest.approx((100_000 - 600 - 700) / 101_300)
    assert c("o_large_share30", 60) == pytest.approx(700 / 800)  # ticks in (30, 60]
    assert c("o_large_imb30", 60) == pytest.approx(-1.0)
    assert c("o_large_share60", 60) == pytest.approx(101_300 / 101_400)
    assert c("o_log1p_nlarge60", 60) == pytest.approx(np.log1p(3))
    assert c("o_log1p_nlarge_open", 60) == pytest.approx(np.log1p(3))
    assert c("o_large_share30", 300) == 0.0 and c("o_large_imb60", 300) == 0.0


def test_group_O_unknown_without_adv_and_causal(tmp_path):
    store = make_store(tmp_path)
    from trading_rl.scalp_env import build_scalp_session

    raw = raw_session(seed=9)
    s0 = build_scalp_session(raw, prev_close=PREV_CLOSE, avg_volume_20d=0.0, is_topix100=False,
                             features="base+O", store=store)  # fmt: skip
    assert not s0.X[:, -9:].any()
    a = build(raw, "base+O", store)
    for cut in (30, 300):
        b = build(_perturbed(raw, cut), "base+O", store)
        n = cut - 10 + 1
        np.testing.assert_array_equal(a.X[:n], b.X[:n])
    assert not np.array_equal(a.X[-1], build(_perturbed(raw, 30), "base+O", store).X[-1])


# --------------------------------------------------------------------------- #
# P - previous-day close flow
# --------------------------------------------------------------------------- #
def test_group_P_previous_day_flow_known_flag(tmp_path):
    store = make_store(tmp_path)
    ed = _ed(tmp_path)
    ed._calendar = np.array(["2026-08-12", "2026-08-13", "2026-08-14"], dtype="datetime64[D]")
    ed._calendar_loaded = True
    ed._prevday = {("1234", "2026-08-13"): (12.5, -4.0, 0.2), ("1234", "2026-08-12"): (99.0, 99.0, 0.9)}
    sess = build(raw_session(), "base+P", store)
    row = sess.X[0, -4:]
    np.testing.assert_allclose(row, [1.0, 12.5, -4.0, 0.2], atol=1e-6)
    ed._prevday = {("1234", "2026-08-12"): (99.0, 99.0, 0.9)}  # only the day before is there
    assert not build(raw_session(), "base+P", store).X[:, -4:].any()
    ed._prevday = {("1234", "2026-08-13"): (1e6, -1e6, 5.0)}  # clipped
    np.testing.assert_allclose(build(raw_session(), "base+P", store).X[0, -4:], [1, 500, -500, 1])


# --------------------------------------------------------------------------- #
# Q - calendar
# --------------------------------------------------------------------------- #
def _q(date: str, ed=None) -> dict[str, float]:
    return fx.calendar_scalars(ed, date)


def test_group_Q_known_dates_weekday_rule():
    v = _q("2025-03-14")  # 2nd Friday of March 2025: major SQ
    assert (v["q_dow_fri"], v["q_sq_day"], v["q_sq_major"]) == (1.0, 1.0, 1.0)
    assert sum(v[f"q_dow_{d}"] for d in ("mon", "tue", "wed", "thu", "fri")) == 1.0
    v = _q("2025-04-11")  # 2nd Friday of April: SQ but not a major month
    assert (v["q_sq_day"], v["q_sq_major"]) == (1.0, 0.0)
    assert _q("2025-03-07")["q_sq_day"] == 0.0 and _q("2025-03-21")["q_sq_day"] == 0.0
    assert _q("2024-06-14")["q_sq_major"] == 1.0 and _q("2026-03-13")["q_sq_major"] == 1.0
    assert _q("2026-01-09")["q_sq_day"] == 1.0
    v = _q("2025-03-31")  # Monday, last business day of the month
    assert (v["q_month_end"], v["q_month_start"], v["q_dow_mon"]) == (1.0, 0.0, 1.0)
    assert (v["q_after_gap2"], v["q_after_long"]) == (1.0, 0.0)  # weekend = 2 days
    v = _q("2025-04-01")
    assert (v["q_month_start"], v["q_month_end"], v["q_after_gap2"]) == (1.0, 0.0, 0.0)
    v = _q("2025-03-12")
    assert not any(v[k] for k in ("q_month_end", "q_month_start", "q_after_gap2", "q_sq_day"))
    assert _q("2025-03-15")["q_dow_fri"] == 0.0 and sum(_q("2025-03-15")[f"q_dow_{d}"] for d in ("mon", "tue", "wed", "thu", "fri")) == 0


def test_group_Q_uses_trading_calendar_for_holidays(tmp_path):
    ed = _ed(tmp_path)
    # Golden Week 2025: 5/3-5/6 closed, 5/7 is the first day back
    days = [d for d in np.arange(np.datetime64("2025-04-20"), np.datetime64("2025-05-30"))
            if (int(d.astype(int)) + 3) % 7 < 5 and str(d) not in ("2025-04-29", "2025-05-05", "2025-05-06")]  # fmt: skip
    ed._calendar = np.array(days, dtype="datetime64[D]")
    ed._calendar_loaded = True
    v = _q("2025-05-07", ed)
    assert (v["q_after_gap2"], v["q_after_long"]) == (1.0, 1.0)  # 5/3 Sat .. 5/6 closed
    assert _q("2025-05-02", ed)["q_after_long"] == 0.0
    # SQ on a holiday moves to the previous trading day: 2nd Friday = 5/9 here is open
    assert _q("2025-05-09", ed)["q_sq_day"] == 1.0 and _q("2025-05-09", ed)["q_sq_major"] == 0.0
    # make 5/9 a holiday -> SQ moves to Thursday 5/8
    ed._calendar = np.array([d for d in days if str(d) != "2025-05-09"], dtype="datetime64[D]")
    assert _q("2025-05-08", ed)["q_sq_day"] == 1.0
    # month-end uses the next trading day: 4/30 is a month end when 5/1 is open
    assert _q("2025-04-30", ed)["q_month_end"] == 1.0 and _q("2025-05-01", ed)["q_month_start"] == 1.0


def test_group_Q_constant_over_the_session(tmp_path):
    store = make_store(tmp_path)
    sess = build(raw_session(), "base+Q", store)
    assert (sess.X[:, -11:] == sess.X[0, -11:]).all()
    assert col(sess, "base+Q", "q_dow_fri")[0] == 1.0  # 2026-08-14 is a Friday
    assert col(sess, "base+Q", "q_sq_day")[0] == 1.0  # ... the 2nd Friday of August, not major
    assert col(sess, "base+Q", "q_sq_major")[0] == 0.0


# --------------------------------------------------------------------------- #
# Replay round trip
# --------------------------------------------------------------------------- #
def test_replay_with_ext_groups_uses_the_store_context(tmp_path):
    store = make_store(tmp_path)
    ed = _install_k(_ed(tmp_path))
    ed._peer[DATE] = _peer_day()
    feats = ALL_EXT
    path = tmp_path / "m.pt"
    _save_ckpt(path, feats)
    _p, ckpt = sr.load_checkpoint_cached(path)
    assert ckpt["env_config"]["features"] == feats
    assert len(ckpt["env_config"]["feature_names"]) == obs_dim(feats) == 125
    trades, summary, _orders = sr.simulate_scalp_trades(
        raw_session(), path, prev_close=PREV_CLOSE, avg_volume_20d=1e6,
        is_topix100=False, store=store,
    )  # fmt: skip
    assert summary["tradeCount"] == sum(1 for t in trades if t["kind"] == "exit")
    sess = build(raw_session(), feats, store)
    assert col(sess, feats, "k_known_1306").all() and col(sess, feats, "l_known").all()
    # a checkpoint whose dim contradicts its features is still rejected
    bad = tmp_path / "bad.pt"
    _save_ckpt(bad, feats, dim=obs_dim("base+K"))
    with pytest.raises(ValueError, match="obs_dim"):
        sr.simulate_scalp_trades(
            raw_session(), bad, prev_close=PREV_CLOSE, avg_volume_20d=1e6,
            is_topix100=False, store=store,
        )  # fmt: skip


# --------------------------------------------------------------------------- #
# Real cache smoke test (skipped when the ext cache has not been built)
# --------------------------------------------------------------------------- #
REAL = Path(__file__).resolve().parents[1] / ".cache" / "trading_rl"


@pytest.mark.skipif(not (REAL / "ext" / "etf" / "2025-03-04.npz").is_file(), reason="no ext cache")
def test_real_cache_session_has_finite_known_columns():
    from trading_rl.scalp_env import load_scalp_session
    from trading_rl.universe import load_universe

    rows = load_universe(REAL, "2025-03-04", "2025-03-04")
    r = rows[0]
    row = (str(r["date"]), str(r["stem"]), float(r["prev_close"]), float(r["avg_volume_20d"]),
           bool(r["is_topix100"]))  # fmt: skip
    sess = load_scalp_session(row, REAL, ALL_EXT)
    assert sess.X.shape == (590, n_market(ALL_EXT)) and np.isfinite(sess.X).all()
    for n in ("k_known_1306", "k_known_1321", "n_known", "m_covered", "o_known"):
        assert col(sess, ALL_EXT, n)[-1] == 1.0, n


def test_feature_screen_accepts_ext_group_arms():
    from test_scalp_features import _load_script

    fs = _load_script("feature_screen")
    arms = fs.parse_arms("base+K,L,base+K+L+M+N+O+P+Q,base+C+K-J,Q+A")
    assert [c for _, c in arms] == [
        "base+K", "base+L", ALL_EXT, "base+C+K-J", "base+A+Q",
    ]  # fmt: skip
    assert fs.arm_id(ALL_EXT) == "base_K_L_M_N_O_P_Q"

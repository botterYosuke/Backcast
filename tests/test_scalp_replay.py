"""Tests for the limit-order scalper replay (trading_rl.scalp_replay)."""

from __future__ import annotations

from pathlib import Path

import duckdb
import numpy as np
import pytest
import torch

from trading_rl import scalp_replay as sr
from trading_rl.data import OpeningSessionData
from trading_rl.policy import ActorCritic
from trading_rl.scalp_env import (
    N_ACTIONS,
    N_MARKET,
    OBS_DIM,
    ScalpConfig,
    ScalpEnv,
    build_scalp_session,
)

T_OPEN_US = 1_786_000_000_000_000


def make_session(n_sec: int = 620, step_us: int = 500_000) -> OpeningSessionData:
    """Saw-tooth ticks (period 8 s, range 5 yen) every 0.5 s."""
    t = np.arange(0, n_sec * 1_000_000, step_us, dtype=np.int64)
    sec = t / 1_000_000
    price = 1000.0 + np.round(2.5 + 2.5 * np.sin(sec * 2 * np.pi / 8.0))
    return OpeningSessionData(
        stem="1234",
        code="12340",
        date="2026-08-14",
        t_open_us=T_OPEN_US,
        p_open=float(price[0]),
        v_open=100,
        epoch_us=T_OPEN_US + t,
        price=price,
        qty=np.full(len(t), 100, dtype=np.int64),
        trade_type=np.full(len(t), "", dtype=object),
    )


class _Const:
    def __init__(self, action: int) -> None:
        self.action = action

    def eval(self) -> None:
        pass

    def predict(self, obs, deterministic=True) -> int:  # noqa: ANN001
        return self.action


class _EntryExit:
    """Flat -> bid_k0, in a position -> ask_k0 (limit entry and limit exit)."""

    def eval(self) -> None:
        pass

    def predict(self, obs, deterministic=True) -> int:  # noqa: ANN001
        return 2 if obs[N_MARKET] == 0.0 else 5


def _patch_policy(monkeypatch, policy) -> None:
    ckpt = {"env_config": ScalpConfig().metadata()}
    monkeypatch.setattr(sr, "load_checkpoint_cached", lambda _p: (policy, ckpt))


INFO = {"prev_close": 1000.0, "avg_volume_20d": 1_000_000.0, "is_topix100": False}


def test_checkpoint_kind_and_period():
    assert sr.checkpoint_kind({"env_config": ScalpConfig().metadata()}) == "scalp"
    assert sr.checkpoint_kind({"env_config": {"window_seconds": 600.0}}) == "market"
    assert sr.checkpoint_kind({"env_config": None}) == "market"
    assert sr.checkpoint_kind({}) == "market"
    assert sr.period_of("2023-12-29") == "pre_train"
    assert sr.period_of("2024-01-04") == "train"
    assert sr.period_of("2025-06-30") == "train"
    assert sr.period_of("2025-07-01") == "val"
    assert sr.period_of("2025-12-31") == "val"
    assert sr.period_of("2026-01-01") == "test"
    assert sr.period_of("2026-08-19") == "test"


def test_scalp_config_from_checkpoint_rejects_other_window():
    meta = ScalpConfig(latency_ms=120, fill_rule="touch").metadata()
    cfg = sr.scalp_config_from_checkpoint({"env_config": meta})
    assert (cfg.latency_ms, cfg.fill_rule) == (120, "touch")
    with pytest.raises(ValueError):
        sr.scalp_config_from_checkpoint({"env_config": {**meta, "window_seconds": 900}})


def test_limit_entry_and_exit_with_order_events(monkeypatch):
    _patch_policy(monkeypatch, _EntryExit())
    sess = make_session()
    trades, summary, orders = sr.simulate_scalp_trades(sess, "x.pt", **INFO)

    lo, hi = T_OPEN_US + 10_000_000, T_OPEN_US + 600_000_000
    assert trades and all(lo <= t["us"] <= hi for t in trades)
    assert all(t["qty"] == 100 and t["side"] in ("buy", "sell") for t in trades)
    assert [t["kind"] for t in trades[:2]] == ["entry", "exit"]
    assert trades[0]["order"] == trades[1]["order"] == "limit"
    assert trades[0]["side"] == "buy" and trades[1]["side"] == "sell"
    assert trades[0]["pnl"] == 0.0
    assert trades[1]["pnl"] == pytest.approx((trades[1]["price"] - trades[0]["price"]) * 100)
    assert summary["tradeCount"] == len([t for t in trades if t["kind"] == "exit"])
    assert summary["limitExits"] >= 1
    assert summary["totalPnl"] == pytest.approx(
        sum(t["pnl"] for t in trades if t["kind"] == "exit")
    )

    # order lifecycle: every placed order ends exactly once (fill or cancel)
    actions: dict[int, list[str]] = {}
    for o in orders:
        assert o["action"] in ("place", "cancel", "fill")
        assert lo <= o["us"] <= hi
        actions.setdefault(o["id"], []).append(o["action"])
    for oid, seq in actions.items():
        assert oid >= 0
        assert seq[0] == "place" and len(seq) == 2 and seq[1] in ("fill", "cancel"), seq
    # every limit trade fill is announced as an order fill at the same time/price
    fills = {(o["us"], o["price"]) for o in orders if o["action"] == "fill"}
    assert {(t["us"], t["price"]) for t in trades if t["order"] == "limit"} == fills
    # placements become effective after the 50 ms latency
    assert orders[0]["us"] - T_OPEN_US == 10_050_000


def test_forced_close_and_open_orders_are_cancelled_at_window_end(monkeypatch):
    _patch_policy(monkeypatch, _Const(2))  # always bid_k0
    trades, summary, orders = sr.simulate_scalp_trades(make_session(), "x.pt", **INFO)
    exits = [t for t in trades if t["kind"] == "exit"]
    assert exits and exits[-1]["forced"] is True and exits[-1]["order"] == "market"
    assert exits[-1]["us"] == T_OPEN_US + 600_000_000
    assert summary["forcedExits"] == 1
    assert summary["limitExits"] == 0
    placed = {o["id"] for o in orders if o["action"] == "place"}
    ended = [o["id"] for o in orders if o["action"] in ("fill", "cancel")]
    assert sorted(ended) == sorted(placed)  # nothing left resting


def test_flatten_cancels_resting_orders(monkeypatch):
    seq = iter([2, 8] + [0] * 1000)  # bid, then market_flatten

    class P(_Const):
        def predict(self, obs, deterministic=True) -> int:  # noqa: ANN001
            return next(seq)

    _patch_policy(monkeypatch, P(0))
    sess = make_session()
    flat = OpeningSessionData(
        **{**sess.__dict__, "price": np.full(len(sess.price), 1000.0)}
    )  # flat price: the bid never fills
    _trades, _summary, orders = sr.simulate_scalp_trades(flat, "x.pt", **INFO)
    assert [(o["action"], o["us"] - T_OPEN_US) for o in orders] == [
        ("place", 10_050_000),
        ("cancel", 11_050_000),
    ]


def test_recording_env_matches_plain_scalp_env(monkeypatch):
    """The recorder must not change fills versus an unmodified ScalpEnv."""
    policy = _EntryExit()
    _patch_policy(monkeypatch, policy)
    sess = make_session()
    trades, _s, _o = sr.simulate_scalp_trades(sess, "x.pt", **INFO)

    env = ScalpEnv([build_scalp_session(sess, **INFO)], ScalpConfig())
    obs, _ = env.reset(options={"index": 0})
    done = False
    while not done:
        obs, _r, term, trunc, _i = env.step(policy.predict(obs))
        done = term or trunc
    assert [(f["abs_us"], f["price"], f["kind"], f["order"]) for f in env.fills] == [
        (t["us"], t["price"], t["kind"], t["order"]) for t in trades
    ]
    assert sum(t["pnl"] for t in trades if t["kind"] == "exit") == pytest.approx(
        env.episode_summary()["pnl_yen100"]
    )


def test_real_checkpoint_replays_within_window():
    model = Path("models/ppo_scalp_top50_open10m.pt")
    if not model.is_file():
        pytest.skip("scalp checkpoint not available")
    trades, summary, orders = sr.simulate_scalp_trades(make_session(), model, **INFO)
    lo, hi = T_OPEN_US + 10_000_000, T_OPEN_US + 600_000_000
    assert all(lo <= t["us"] <= hi for t in trades)
    assert all(lo <= o["us"] <= hi for o in orders)
    assert summary["tradeCount"] == sum(1 for t in trades if t["kind"] == "exit")


def test_checkpoint_roundtrip_kind(tmp_path):
    policy = ActorCritic(obs_dim=OBS_DIM, action_dim=N_ACTIONS, hidden_dim=8)
    path = tmp_path / "s.pt"
    torch.save(
        {
            "policy_state_dict": policy.state_dict(),
            "obs_dim": OBS_DIM,
            "action_dim": N_ACTIONS,
            "hidden_dim": 8,
            "env_config": ScalpConfig().metadata(),
        },
        path,
    )
    _p, ckpt = sr.load_checkpoint_cached(path)
    assert sr.checkpoint_kind(ckpt) == "scalp"


# ------------------------------------------------------- lookup_session_info
def _write_parquet(path: Path, header: str, rows: list[str]) -> None:
    con = duckdb.connect(":memory:")
    con.execute(f"CREATE TABLE t AS SELECT * FROM (VALUES {','.join(rows)}) AS v({header})")
    con.execute(f"COPY t TO '{path.as_posix()}' (FORMAT PARQUET)")
    con.close()


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "MIN_CALENDAR_BREADTH", 2)
    d = tmp_path / "cache"
    d.mkdir()
    # 25 calendar days 2026-07-01..25; two symbols (breadth 2)
    rows = []
    days = [f"2026-07-{n:02d}" for n in range(1, 26)]
    for i, day in enumerate(days):
        rows.append(f"('aaaa','{day}',{100 + i}.0,{1000 + i}.0)")
        rows.append(f"('bbbb','{day}',{50 + i}.0,{500 + i}.0)")
    rows.append("('cccc','2026-07-20',10.0,7.0)")  # breadth stays 3 on that day only
    _write_parquet(d / "daily_panel.parquet", "stem,d,close,volume", rows)
    _write_parquet(
        d / "universe.parquet",
        "date,stem,prev_close,avg_volume_20d,is_topix100",
        [
            "(DATE '2026-07-25','bbbb',777.0,888.0,true)",
            "(DATE '2026-06-01','aaaa',1.0,1.0,true)",
        ],
    )
    return d


def test_lookup_prefers_universe(cache_dir):
    info = sr.lookup_session_info("BBBB", "2026-07-25", cache_dir)
    assert info == sr.ScalpSessionInfo(777.0, 888.0, True, True, "universe", "universe")


def test_lookup_panel_is_point_in_time(cache_dir):
    info = sr.lookup_session_info("aaaa", "2026-07-25", cache_dir)
    assert info is not None and not info.in_universe and info.source == "daily_panel"
    # P(D) = 07-24 (strictly before the date): close 100 + 23
    assert info.prev_close == 123.0
    # 20 calendar days 07-05..07-24 -> volumes 1004..1023
    assert info.avg_volume_20d == pytest.approx(sum(1000 + i for i in range(4, 24)) / 20)
    # is_topix100 from the nearest universe row of the same stem
    assert info.is_topix100 is True and info.topix100_source == "universe_nearest"
    # the date itself is excluded, later dates move the window
    later = sr.lookup_session_info("aaaa", "2026-07-26", cache_dir)
    assert later is not None and later.prev_close == 124.0


def test_lookup_unknown_stem_or_missing_sources(cache_dir, tmp_path):
    assert sr.lookup_session_info("zzzz", "2026-07-25", cache_dir) is None
    assert sr.lookup_session_info("aaaa", "2026-07-01", cache_dir) is None  # no prior day
    empty = tmp_path / "empty"
    empty.mkdir()
    assert sr.lookup_session_info("aaaa", "2026-07-25", empty) is None
    # cccc has no close on P(D) -> do not guess
    assert sr.lookup_session_info("cccc", "2026-07-25", cache_dir) is None


# ------------------------------------------------------------- v2: order modes
class _Masked:
    """Policy double that records the masks it is given and returns a fixed plan."""

    def __init__(self, actions) -> None:
        self.actions = iter(actions)
        self.masks: list = []

    def eval(self) -> None:
        pass

    def predict(self, obs, deterministic=True, mask=None) -> int:  # noqa: ANN001
        self.masks.append(None if mask is None else list(mask))
        return next(self.actions, 0)


def _patch_mode(monkeypatch, policy, mode: str, masking: bool) -> None:
    meta = ScalpConfig(order_mode=mode).metadata()
    meta["action_masking"] = masking
    monkeypatch.setattr(sr, "load_checkpoint_cached", lambda _p: (policy, {"env_config": meta}))


def test_replay_market_mode_checkpoint_decodes_and_uses_masks(monkeypatch):
    # market mode: 0 keep, 1 market_buy, 2 market_sell, 3 market_flatten
    pol = _Masked([1, 0, 0, 3, 2])
    _patch_mode(monkeypatch, pol, "market", True)
    trades, summary, orders = sr.simulate_scalp_trades(make_session(), "x.pt", **INFO)
    assert orders == []  # no limit orders exist in market mode
    assert [(t["side"], t["kind"], t["order"]) for t in trades[:3]] == [
        ("buy", "entry", "market"),
        ("sell", "exit", "market"),
        ("sell", "entry", "market"),
    ]
    assert trades[-1]["forced"] is True  # short held to the forced close
    assert summary["marketExits"] == 1 and summary["forcedExits"] == 1
    assert pol.masks[0] == [True, True, True, False]  # flat: no flatten
    assert pol.masks[1] == [True, False, True, True]  # long: no market buy
    assert len(pol.masks) == 590 and all(m is not None for m in pol.masks)


def test_replay_both_mode_market_order_cancels_resting_limit(monkeypatch):
    # both mode: 2 = bid_k0 (never fills at a flat 1000 tape), 9 = market_buy
    pol = _Masked([2, 9])
    _patch_mode(monkeypatch, pol, "both", True)
    flat = OpeningSessionData(
        **{**make_session().__dict__, "price": np.full(1240, 1000.0)}
    )
    trades, _s, orders = sr.simulate_scalp_trades(flat, "x.pt", **INFO)
    assert [(o["action"], o["us"] - T_OPEN_US) for o in orders] == [
        ("place", 10_050_000),
        ("cancel", 11_050_000),  # dropped by the market buy of the next decision
    ]
    assert trades[0]["order"] == "market" and trades[0]["side"] == "buy"
    assert trades[0]["price"] == pytest.approx(1000.5)  # tick 1 at 1000 -> +0.5


def test_replay_unmasked_checkpoint_gets_no_mask_kwarg(monkeypatch):
    class Strict(_Const):
        def predict(self, obs, deterministic=True) -> int:  # noqa: ANN001
            return 0

    ckpt = {"env_config": {"fill_rule": "trade_through", "action_names": ["keep"] * 9}}
    monkeypatch.setattr(sr, "load_checkpoint_cached", lambda _p: (Strict(0), ckpt))
    sr.simulate_scalp_trades(make_session(), "x.pt", **INFO)  # must not raise
    assert sr.uses_action_mask(ckpt) is False
    cfg = sr.scalp_config_from_checkpoint(ckpt)
    assert cfg.order_mode == "limit"  # old checkpoints have no order_mode


def test_scalp_config_from_checkpoint_reads_order_mode_not_shaping():
    meta = ScalpConfig(
        order_mode="both", inventory_penalty_ticks_per_s=0.01, idle_penalty_ticks_per_s=0.5
    ).metadata()
    cfg = sr.scalp_config_from_checkpoint({"env_config": meta})
    assert cfg.order_mode == "both"
    assert cfg.inventory_penalty_ticks_per_s == 0.0 and cfg.idle_penalty_ticks_per_s == 0.0


def test_real_policy_masked_replay_in_every_mode(monkeypatch, tmp_path):
    for mode in ("limit", "market", "both"):
        from trading_rl.scalp_env import n_actions

        policy = ActorCritic(obs_dim=OBS_DIM, action_dim=n_actions(mode), hidden_dim=8)
        meta = ScalpConfig(order_mode=mode).metadata()
        meta["action_masking"] = True
        path = tmp_path / f"{mode}.pt"
        torch.save(
            {
                "policy_state_dict": policy.state_dict(),
                "obs_dim": OBS_DIM,
                "action_dim": n_actions(mode),
                "hidden_dim": 8,
                "env_config": meta,
            },
            path,
        )
        trades, summary, _orders = sr.simulate_scalp_trades(make_session(), path, **INFO)
        assert sr.checkpoint_kind(sr.load_checkpoint_cached(path)[1]) == "scalp"
        assert summary["tradeCount"] == sum(1 for t in trades if t["kind"] == "exit")

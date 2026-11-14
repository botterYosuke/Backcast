"""Burst-RL replay for the web UI (trading_rl.burst_replay) and its app endpoints."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from trading_rl import burst_env as be
from trading_rl import burst_replay as br
from trading_rl.data import OpeningSessionData

US = be.US
T_OPEN = int(np.datetime64("2026-08-14T09:00:00", "us").astype(np.int64))
KEY = "burst/ppo_burst_slip1_s0.pt"


def make_raw() -> OpeningSessionData:
    t = np.arange(0, 1200, 0.5)
    p = 1000.0 + np.round(3 * np.sin(t / 7.0))
    epoch = (T_OPEN + np.round(t * US)).astype(np.int64)
    return OpeningSessionData(
        stem="7203", code="72030", date="2026-08-14", t_open_us=T_OPEN, p_open=float(p[0]),
        v_open=1000, epoch_us=epoch, price=p, qty=np.full(len(p), 100, dtype=np.int64),
        trade_type=np.full(len(p), "", dtype=object),
    )


class _Const:
    def predict_proba(self, X):
        return np.column_stack([np.full(len(X), 0.1), np.full(len(X), 0.9)])


def buy_then_sell(obs, mask, bb):
    """Buy at the trigger, sell 5 s later."""
    return np.full(bb.B, be.A_BUY if bb.j == 0 else (be.A_SELL if bb.j == 5 else be.A_WAIT))


@pytest.fixture
def fake_detector(monkeypatch):
    det = be.Detector(_Const(), 0.5, {})
    monkeypatch.setattr(br, "load_detector", lambda: det)
    return det


def test_keys_labels_and_periods():
    assert br.parse_key("predictor/h10_q0.01_O3") is None
    assert br.parse_key("burst/../x.pt") is None
    assert br.parse_key("burst/ppo_burst_slip1_s0.pt") in (None, br.MODELS_DIR / "ppo_burst_slip1_s0.pt")
    assert br.model_label(Path("ppo_burst_slip1_s0.pt")).endswith("seed0・本命）")
    assert "基準の約定" in br.model_label(Path("ppo_burst_plain_s2.pt"))
    assert [br.period_of(d) for d in ("2023-12-29", "2024-05-01", "2025-07-01", "2026-03-02", "2026-08-20")] == [
        "pre_train", "train", "val", "val2", "final"]


def test_replay_trades_follow_the_policy_on_every_burst_episode(fake_detector):
    raw = make_raw()
    trades, summary, episodes, cfg = br.simulate_burst_trades(
        raw, KEY, prev_close=995.0, avg_volume_20d=1e6, is_topix100=False, policy=buy_then_sell)
    # constant score >= threshold -> back-to-back 60 s episodes from T_open + 10 s
    assert [e["start_us"] - T_OPEN for e in episodes[:3]] == [10 * US, 71 * US, 132 * US]
    assert summary["burstEpisodes"] == len(episodes) and summary["tradeCount"] == len(episodes)
    assert [t["kind"] for t in trades[:4]] == ["entry", "exit", "entry", "exit"]
    e, x = trades[0], trades[1]
    assert e["side"] == "buy" and x["side"] == "sell" and e["order"] == x["order"] == "market"
    assert e["us"] == T_OPEN + 10 * US + 200_000 and x["us"] == T_OPEN + 15 * US + 200_000
    # market fill = last print at decision + 200 ms +/- 0.5 tick (tick 1 yen at 1000)
    last = lambda s: raw.price[np.searchsorted(raw.epoch_us, T_OPEN + int(s * US), side="right") - 1]  # noqa: E731
    assert e["price"] == pytest.approx(last(10.2) + 0.5) and x["price"] == pytest.approx(last(15.2) - 0.5)
    assert x["pnl"] == pytest.approx((x["price"] - e["price"]) * 100) and not x["forced"]
    assert summary["totalPnl"] == pytest.approx(sum(t["pnl"] for t in trades if t["kind"] == "exit"))
    assert summary["pnlTicks"] == pytest.approx(summary["totalPnl"] / 100)  # ref tick 1 yen
    assert cfg["latency_ms"] == 200 and cfg["episode_s"] == 60


def test_no_trigger_means_no_trades(monkeypatch):
    monkeypatch.setattr(br, "load_detector", lambda: be.Detector(_Const(), 0.95, {}))
    trades, summary, episodes, _ = br.simulate_burst_trades(
        make_raw(), KEY, prev_close=995.0, avg_volume_20d=1e6, is_topix100=False, policy=buy_then_sell)
    assert trades == [] and episodes == [] and summary["tradeCount"] == 0


def test_real_checkpoints_load_with_the_shared_network():
    files = br.model_files()
    if not files:
        pytest.skip("models/burst not built")
    m = br.load_model(files[0])
    obs = np.zeros((2, be.OBS_DIM), np.float32)
    mask = np.ones((2, be.N_ACT), bool)
    mask[1, be.A_BUY] = False
    a = br.greedy_policy(m)(obs, mask, None)
    assert a.shape == (2,) and a[1] != be.A_BUY
    assert "smoke" not in " ".join(p.name for p in files)


# --------------------------------------------------------------------------- #
# app endpoints
# --------------------------------------------------------------------------- #
from test_ai_replay_api import (  # noqa: E402,F401
    _info,
    client,
    isolate_local_authoritative_setting,
)


def test_models_listing_includes_burst_models(client, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(br, "model_files", lambda: [Path("ppo_burst_slip1_s0.pt"), Path("ppo_burst_plain_s1.pt")])
    rows = [m for m in client.get("/api/ai/models").json() if m["kind"] == "burst"]
    assert [m["file"] for m in rows] == ["burst/ppo_burst_slip1_s0.pt", "burst/ppo_burst_plain_s1.pt"]
    assert all(m["label"].startswith("激しい瞬間AI") and m["default"] is False for m in rows)


def test_replay_endpoint_runs_burst_model(client, fake_detector, monkeypatch):  # noqa: F811
    from trading_rl import scalp_replay

    monkeypatch.setattr(br, "parse_key", lambda key: Path("x.pt") if key == KEY else None)
    monkeypatch.setattr(br, "is_available", lambda: True)
    monkeypatch.setattr(br, "load_model", lambda path: None)
    monkeypatch.setattr(br, "greedy_policy", lambda model: buy_then_sell)
    monkeypatch.setattr(scalp_replay, "lookup_session_info", lambda stem, date: _info())
    data = client.get(f"/api/ai/replay-trades?stem=7203&date=2026-08-14&model={KEY}").json()
    assert data["available"] is True, data.get("reason")
    assert data["model"] == KEY and data["model_kind"] == "burst"
    assert data["period"] == "val2" and data["orders"] == []
    assert data["env_config"]["latency_ms"] == 200 and data["env_config"]["warmup_s"] == 10
    assert data["burst_episodes"] and data["trades"][0]["side"] == "buy"


def test_replay_endpoint_burst_model_missing(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(br, "parse_key", lambda key: None)
    data = client.get("/api/ai/replay-trades?stem=7203&date=2026-08-14&model=burst/ppo_burst_zz_s9.pt").json()
    assert data["available"] is False and "burst" in data["reason"]


# --------------------------------------------------------------------------- #
# v3 (arm B, TOPIX500 tick table)
# --------------------------------------------------------------------------- #
from trading_rl import tick_rule  # noqa: E402

KEY_V3 = "burst/v3/ppo_v3B_s2.pt"


@pytest.fixture
def v3_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    d = tmp_path / "models" / "burst_v3"
    d.mkdir(parents=True)
    (d / "detector_v3.pkl").write_bytes(b"x")
    for k in range(5):
        (d / f"ppo_v3B_s{k}.pt").write_bytes(b"x")
    (d / "ppo_v3A_s0.pt").write_bytes(b"x")  # arm A is not offered
    return d


def test_v3_keys_labels_and_files(v3_dir):
    files = br.model_files_v3()
    assert [p.name for p in files] == [f"ppo_v3B_s{k}.pt" for k in range(5)]
    assert all(br.is_v3(p) for p in files)
    assert br.model_key(files[2]) == KEY_V3
    assert br.parse_key(KEY_V3) == files[2]
    assert br.parse_key("burst/v3/ppo_v3A_s0.pt") is None
    assert br.parse_key("burst/v3/../burst/ppo_burst_slip1_s0.pt") is None
    assert br.model_label(files[2]).startswith("激しい瞬間AI v3・腕B・s2")
    (v3_dir / "detector_v3.pkl").unlink()
    assert br.model_files_v3() == [] and br.parse_key(KEY_V3) is None


def test_tick_rule_topix500_table_and_price_grid():
    # Mid400 at 9,000 yen: 1 yen (the vendored TOPIX100-only rule said 10)
    assert tick_rule.tick_size(9000, True) == 1 and tick_rule.tick_size(9000, False) == 10
    assert tick_rule.tick_size(3000, True) == 0.5 and tick_rule.tick_size(3001, True) == 1
    assert tick_rule.fine_rule("TOPIX Mid400", None) is True
    assert tick_rule.fine_rule("TOPIX Small 1", None) is False
    fine_prices = 6700 + np.arange(40) * 1.0  # 1-yen grid at 6,700 -> only the fine table
    general_prices = 6700 + np.arange(40) * 10.0
    assert tick_rule.measured_table(fine_prices) == "fine"
    assert tick_rule.measured_table(general_prices) == "general"
    assert tick_rule.measured_table([6700.0]) == "unknown"
    # the day's grid wins over the size category
    assert tick_rule.fine_rule("TOPIX Mid400", "general") is False
    assert tick_rule.fine_rule(None, "fine") is True


def test_fine_tick_v3_sources(monkeypatch):
    monkeypatch.setattr(br, "lookup_scale_category", lambda stem, date: ("TOPIX Mid400", "universe"))
    r = br.fine_tick_v3("4062", "2026-08-14", np.array([6700.0]))
    assert r == {"fine": True, "scale_category": "TOPIX Mid400", "measured": "unknown",
                 "source": "scale_universe"}
    r = br.fine_tick_v3("4062", "2026-08-14", 6700 + np.arange(40) * 10.0)
    assert r["fine"] is False and r["source"] == "price_grid"
    monkeypatch.setattr(br, "lookup_scale_category", lambda stem, date: (None, "none"))
    assert br.fine_tick_v3("x", "2026-08-14", np.array([6700.0]))["source"] == "default_general"


def test_v3_replay_uses_the_v3_detector(monkeypatch):
    calls = []
    det = be.Detector(_Const(), 0.5, {})
    monkeypatch.setattr(br, "load_detector", lambda: calls.append("v1") or det)
    monkeypatch.setattr(br, "load_detector_v3", lambda: calls.append("v3") or det)
    _, summary, _, cfg = br.simulate_burst_trades(
        make_raw(), KEY_V3, prev_close=995.0, avg_volume_20d=1e6, is_topix100=True, policy=buy_then_sell)
    assert calls == ["v3"] and cfg["tick_rule"] == "topix500 (v3)" and summary["tradeCount"] > 0


def test_models_listing_default_is_only_the_five_v3_policies(client, v3_dir, monkeypatch):  # noqa: F811
    from tickreplay import server

    monkeypatch.delenv(server.AI_MODELS_ENV, raising=False)
    rows = client.get("/api/ai/models").json()
    assert [m["file"] for m in rows] == [f"burst/v3/ppo_v3B_s{k}.pt" for k in range(5)]
    assert [m["default"] for m in rows] == [True, False, False, False, False]
    assert all(m["kind"] == "burst" for m in rows)
    monkeypatch.setenv(server.AI_MODELS_ENV, "all")
    files = {m["file"] for m in client.get("/api/ai/models").json()}
    assert {f"burst/v3/ppo_v3B_s{k}.pt" for k in range(5)} <= files


def test_replay_endpoint_v3_passes_the_fine_flag(client, monkeypatch):  # noqa: F811
    from tickreplay import server
    from trading_rl import scalp_replay

    monkeypatch.delenv(server.AI_MODELS_ENV, raising=False)
    seen = {}
    real = br.simulate_burst_trades

    def spy(raw, key, **kw):
        seen.update(key=key, t100=kw["is_topix100"])
        return real(raw, key, **kw, policy=buy_then_sell)

    det = be.Detector(_Const(), 0.5, {})
    monkeypatch.setattr(br, "load_detector_v3", lambda: det)
    monkeypatch.setattr(br, "simulate_burst_trades", spy)
    monkeypatch.setattr(br, "model_files_v3", lambda: [Path("models/burst_v3/ppo_v3B_s0.pt")])
    monkeypatch.setattr(br, "parse_key", lambda key: Path("models/burst_v3/ppo_v3B_s0.pt")
                        if key == "burst/v3/ppo_v3B_s0.pt" else None)
    monkeypatch.setattr(br, "lookup_scale_category", lambda stem, date: ("TOPIX Mid400", "universe"))
    monkeypatch.setattr(scalp_replay, "lookup_session_info", lambda stem, date: _info())
    # no model param -> the first v3 policy
    data = client.get("/api/ai/replay-trades?stem=7203&date=2026-08-14").json()
    assert data["available"] is True, data.get("reason")
    assert data["model"] == "burst/v3/ppo_v3B_s0.pt" and seen["key"] == data["model"]
    assert data["fine_tick"]["scale_category"] == "TOPIX Mid400"
    assert data["is_topix100"] == seen["t100"] == data["fine_tick"]["fine"]
    assert data["env_config"]["latency_ms"] == 200 and data["burst_episodes"]

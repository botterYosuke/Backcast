"""Stage-2 predictor replay (trading_rl.predictor_replay) and its app endpoints."""

from __future__ import annotations

import numpy as np
import pytest

from trading_rl import fill_model as fm
from trading_rl import predictor_replay as pr
from trading_rl.data import OpeningSessionData
from trading_rl.scalp_env import US

T_OPEN = int(np.datetime64("2026-08-14T09:00:00", "us").astype(np.int64))
KEY = "predictor/h10_q0.01_O3"


def make_raw(ticks) -> OpeningSessionData:
    epoch = np.array([T_OPEN + round(t * US) for t, _ in ticks], dtype=np.int64)
    price = np.array([p for _, p in ticks], dtype=np.float64)
    return OpeningSessionData(
        stem="1234", code="12340", date="2026-08-14", t_open_us=T_OPEN,
        p_open=float(price[0]), v_open=1000, epoch_us=epoch, price=price,
        qty=np.full(len(price), 100, dtype=np.int64),
        trade_type=np.full(len(price), "", dtype=object),
    )


class _Const:
    def __init__(self, v):
        self.v = v

    def predict_proba(self, R):
        return np.column_stack([1 - np.full(len(R), self.v), np.full(len(R), self.v)])

    def predict(self, R):
        return np.full(len(R), self.v, dtype=np.float64)


@pytest.fixture
def fake_models(monkeypatch):
    """Constant predictor (signal set per test) + fill model with marketable limits."""
    state = {"signal": 5.0}

    class _Pred:
        def predict(self, X):
            assert X.shape[1] == 28 + 11
            return np.full(len(X), state["signal"])

    preds = {"horizons": (10,), "models": {10: _Pred()}, "thr": {10: {0.01: 1.0}}}
    # p_mkt = 1: every limit fills at activation; q_hat irrelevant (log1p(0) -> 0)
    fill = fm.FillModel(_Const(1.0), _Const(0.0), shrink=1.0)
    monkeypatch.setattr(pr, "load_predictors", lambda: preds)
    monkeypatch.setattr(pr, "load_fill_model", lambda: fill)
    return state


TICKS = [(0.0, 100.0)] + [(float(t), 100.0 + (t // 30) % 3) for t in range(5, 1200, 5)]


def run(signal_state, signal):
    signal_state["signal"] = signal
    return pr.simulate_predictor_trades(
        make_raw(TICKS), KEY, prev_close=100.0, avg_volume_20d=1e6, is_topix100=False,
    )


def test_parse_key_round_trips_presets():
    for p in pr.PRESETS:
        assert pr.parse_key(p.key) == p
        assert p.label.startswith("予測モデル")
    assert pr.parse_key("ppo_scalp_top50_open10m.pt") is None
    assert pr.parse_key("predictor/h10_q0.01_O9") is None
    assert pr.parse_key(None) is None


def test_long_signal_enters_buy_and_exits_after_horizon(fake_models):
    trades, summary, orders, cfg = run(fake_models, +5.0)
    assert trades and trades[0]["kind"] == "entry" and trades[0]["side"] == "buy"
    assert trades[0]["order"] == "limit"
    t_in = (trades[0]["us"] - T_OPEN) / US
    assert 10.0 <= t_in < 11.0
    ex = trades[1]
    assert ex["kind"] == "exit" and ex["side"] == "sell"
    assert 10.0 <= (ex["us"] - trades[0]["us"]) / US < 10.0 + pr.EXIT_WAIT_S + 2
    assert summary["tradeCount"] == sum(t["kind"] == "exit" for t in trades) > 1
    assert summary["signalSeconds"] == cfg["window_seconds"] - 10
    assert {o["action"] for o in orders} >= {"place", "fill"}
    # marketable limits fill inside ScalpEnv._place: the recorder still logs the place
    # first, and the fill carries that order's id (not a stale one)
    placed = {}
    for o in orders:
        if o["action"] == "place":
            placed[o["id"]] = o
        elif o["action"] == "fill":
            assert o["id"] in placed and placed[o["id"]]["side"] == o["side"]
            assert placed[o["id"]]["us"] <= o["us"]
    assert cfg["window_seconds"] == 8940  # 09:00:00 -> 11:29:00
    assert cfg["fill_rule"] == "queue_est" and cfg["latency_ms"] == 200
    assert (cfg["horizon_s"], cfg["top_q"], cfg["variant"]) == (10, 0.01, "O3")
    # everything is flat by the end of the morning window
    assert all((t["us"] - T_OPEN) <= cfg["window_seconds"] * US for t in trades)


def test_short_signal_sells_first_and_weak_signal_never_trades(fake_models):
    trades, *_ = run(fake_models, -5.0)
    assert trades[0]["side"] == "sell" and trades[0]["kind"] == "entry"
    trades, summary, orders, _ = run(fake_models, 0.5)  # below the 1.0-tick threshold
    assert trades == [] and orders == [] and summary["signalSeconds"] == 0


def test_unknown_preset_is_rejected(fake_models):
    with pytest.raises(ValueError):
        pr.simulate_predictor_trades(make_raw(TICKS), "predictor/h5_q0.01_O3",
                                     prev_close=100.0, avg_volume_20d=1e6, is_topix100=False)


# --------------------------------------------------------------------------- #
# app endpoints
# --------------------------------------------------------------------------- #
from test_ai_replay_api import _info, client, isolate_local_authoritative_setting  # noqa: E402,F401


def test_models_listing_includes_presets_when_built(client, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(pr, "is_available", lambda: True)
    rows = [m for m in client.get("/api/ai/models").json() if m["kind"] == "predictor"]
    assert [m["file"] for m in rows] == [p.key for p in pr.PRESETS]
    assert all(m["label"].startswith("予測モデル") and m["default"] is False for m in rows)
    monkeypatch.setattr(pr, "is_available", lambda: False)
    assert not [m for m in client.get("/api/ai/models").json() if m["kind"] == "predictor"]


def test_replay_endpoint_runs_predictor(client, fake_models, monkeypatch):  # noqa: F811
    from trading_rl import scalp_replay

    monkeypatch.setattr(pr, "is_available", lambda: True)
    monkeypatch.setattr(scalp_replay, "lookup_session_info", lambda stem, date: _info())
    data = client.get(f"/api/ai/replay-trades?stem=7203&date=2026-08-14&model={KEY}").json()
    assert data["available"] is True, data.get("reason")
    assert data["model"] == KEY and data["model_kind"] == "predictor"
    assert data["env_config"]["fill_rule"] == "queue_est"
    assert data["env_config"]["latency_ms"] == 200 and data["env_config"]["warmup_s"] == 10
    assert data["predictor"]["horizon_s"] == 10
    assert data["trades"] and data["trades"][0]["side"] == "buy"


def test_replay_endpoint_predictor_not_built(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(pr, "is_available", lambda: False)
    data = client.get(f"/api/ai/replay-trades?stem=7203&date=2026-08-14&model={KEY}").json()
    assert data["available"] is False and "predictor" in data["reason"]

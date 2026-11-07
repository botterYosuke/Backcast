"""Unit tests for the AI Replay trades endpoint (GET /api/ai/replay-trades)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tickreplay import server
from tickreplay.config import CACHE_DIR_ENV_VAR, LOCAL_AUTHORITATIVE_ENV_VAR

ROWS_7203 = [
    ("2026-08-14 09:00:00.000000", 2500.0, 500, "1", "72030"),
    ("2026-08-14 09:01:00.000000", 2505.0, 300, "1", "72030"),
    ("2026-08-14 09:02:00.000000", 2510.0, 200, "1", "72030"),
    ("2026-08-14 09:05:00.000000", 2515.0, 400, "1", "72030"),
    ("2026-08-14 09:10:00.000000", 2520.0, 600, "2", "72030"),
    ("2026-08-14 09:14:59.000000", 2525.0, 800, "2", "72030"),
    ("2026-08-14 09:20:00.000000", 2530.0, 1000, "1", "72030"),
]

AFTERNOON_ONLY_ROWS = [
    ("2026-08-14 08:30:00.000000", 2500.0, 100, "1", "72030"),
]


@pytest.fixture(autouse=True)
def isolate_local_authoritative_setting(monkeypatch):
    monkeypatch.setenv(LOCAL_AUTHORITATIVE_ENV_VAR, "false")


@pytest.fixture
def client(cache_dir, http_client, tick_db_factory, monkeypatch):
    tick_db_factory("7203", ROWS_7203)
    tick_db_factory("9999", AFTERNOON_ONLY_ROWS)
    monkeypatch.setenv(CACHE_DIR_ENV_VAR, str(cache_dir))
    monkeypatch.setattr(server, "build_http_client", lambda config: http_client)
    server.reset_for_tests()
    with TestClient(server.app) as test_client:
        server.get_repository().symbol_info("7203")
        server.get_repository().symbol_info("9999")
        yield test_client
    server.reset_for_tests()


def test_ai_replay_trades_missing_model(client):
    """Returns available=False when model file does not exist."""
    res = client.get(
        "/api/ai/replay-trades?stem=7203&date=2026-08-14&model=nonexistent_model.pt"
    )
    assert res.status_code == 200
    data = res.json()
    assert data["available"] is False
    assert data["stem"] == "7203"
    assert data["date"] == "2026-08-14"
    assert data["trades"] == []
    assert data["summary"] is None


def test_ai_replay_trades_unknown_symbol(client):
    """Returns available=False when symbol is unknown or not in database."""
    res = client.get("/api/ai/replay-trades?stem=0000&date=2026-08-14")
    assert res.status_code == 200
    data = res.json()
    assert data["available"] is False
    assert data["trades"] == []
    assert data["summary"] is None


def test_ai_replay_trades_no_morning_session(client):
    """Returns available=False when date has no morning executions >= 09:00:00."""
    res = client.get("/api/ai/replay-trades?stem=9999&date=2026-08-14")
    assert res.status_code == 200
    data = res.json()
    assert data["available"] is False
    assert data["trades"] == []
    assert data["summary"] is None


def test_ai_replay_trades_success_with_existing_model(client):
    """Returns available=True and executed trades when model and morning session exist."""
    model_path = Path("models/ppo_7203_opening15m.pt")
    if not model_path.is_file():
        pytest.skip("models/ppo_7203_opening15m.pt not available")

    res = client.get("/api/ai/replay-trades?stem=7203&date=2026-08-14")
    assert res.status_code == 200
    data = res.json()
    assert data["available"] is True
    assert data["stem"] == "7203"
    assert data["date"] == "2026-08-14"
    assert "t_open_us" in data
    assert isinstance(data["trades"], list)
    assert isinstance(data["summary"], dict)

    summary = data["summary"]
    assert "totalPnl" in summary
    assert "tradeCount" in summary
    assert "winRate" in summary
    assert 0.0 <= summary["winRate"] <= 1.0

    # Verify trades schema if agent executed trades
    for trade in data["trades"]:
        assert isinstance(trade["us"], int)
        assert trade["side"] in ("buy", "sell")
        assert trade["qty"] == 100
        assert isinstance(trade["price"], (int, float))
        assert trade["kind"] in ("entry", "exit")
        assert isinstance(trade["pnl"], (int, float))


def _write_buy_checkpoint(path: Path, env_config: dict | None) -> None:
    """Checkpoint whose deterministic policy always chooses BUY."""
    import torch

    from trading_rl.policy import ActorCritic

    policy = ActorCritic(obs_dim=18, action_dim=3, hidden_dim=16)
    with torch.no_grad():
        policy.actor.bias.copy_(torch.tensor([0.0, 20.0, 0.0]))
    torch.save(
        {
            "policy_state_dict": policy.state_dict(),
            "obs_dim": 18,
            "action_dim": 3,
            "hidden_dim": 16,
            "env_config": env_config,
        },
        path,
    )


def test_ai_replay_uses_checkpoint_window_warmup_and_slippage(client, tmp_path):
    """Replay builds the env from the checkpoint's env_config (600s window here)."""
    model = tmp_path / "w600.pt"
    _write_buy_checkpoint(
        model,
        {"window_seconds": 600.0, "opening_warmup_seconds": 15.0, "slippage": 0.5},
    )
    res = client.get(
        "/api/ai/replay-trades", params={"stem": "7203", "date": "2026-08-14", "model": str(model)}
    )
    data = res.json()
    assert data["available"] is True
    t_open = data["t_open_us"]
    trades = data["trades"]
    assert [t["kind"] for t in trades] == ["entry", "exit"]
    # First tradable tick is 09:01:00 (60s); window ends exactly at 09:10:00 (600s)
    assert trades[0]["us"] - t_open == 60_000_000
    assert trades[1]["us"] - t_open == 600_000_000
    assert all(15_000_000 <= t["us"] - t_open <= 600_000_000 for t in trades)
    # slippage 0.5 from the checkpoint: buy 2505.5, forced sell 2520 - 0.5
    assert trades[0]["price"] == 2505.5
    assert trades[1]["price"] == 2519.5
    assert trades[1]["pnl"] == pytest.approx(1400.0)
    assert data["summary"]["tradeCount"] == 1


def test_ai_replay_legacy_checkpoint_without_env_config_uses_900s(client, tmp_path):
    model = tmp_path / "legacy.pt"
    _write_buy_checkpoint(model, None)
    res = client.get(
        "/api/ai/replay-trades", params={"stem": "7203", "date": "2026-08-14", "model": str(model)}
    )
    data = res.json()
    assert data["available"] is True
    trades = data["trades"]
    # Legacy window 900s -> last tick inside is 09:14:59 (899s), slippage 0
    assert trades[-1]["us"] - data["t_open_us"] == 899_000_000
    assert trades[0]["price"] == 2505.0


def test_resolve_model_checkpoint_prefers_opening10m(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    models = tmp_path / "models"
    models.mkdir()
    (models / "ppo_7203_opening15m.pt").write_bytes(b"x")
    assert server._resolve_model_checkpoint("7203") == Path("models/ppo_7203_opening15m.pt")
    (models / "ppo_7203_opening10m.pt").write_bytes(b"x")
    assert server._resolve_model_checkpoint("7203") == Path("models/ppo_7203_opening10m.pt")
    assert server._resolve_model_checkpoint("6758") == Path("models/ppo_7203_opening15m.pt")


# ----------------------------------------------------- limit-order scalp model
def _write_scalp_checkpoint(path: Path, action: int = 2) -> None:
    """Scalp checkpoint whose deterministic policy always picks ``action``."""
    import torch

    from trading_rl.policy import ActorCritic
    from trading_rl.scalp_env import N_ACTIONS, OBS_DIM, ScalpConfig

    policy = ActorCritic(obs_dim=OBS_DIM, action_dim=N_ACTIONS, hidden_dim=8)
    with torch.no_grad():
        policy.actor.weight.zero_()
        policy.actor.bias.zero_()
        policy.actor.bias[action] = 20.0
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


@pytest.fixture
def models_dir(tmp_path, monkeypatch):
    """cwd with a models/ dir holding one scalp and one legacy checkpoint."""
    monkeypatch.chdir(tmp_path)
    d = tmp_path / "models"
    d.mkdir()
    _write_scalp_checkpoint(d / "ppo_scalp_top50_open10m.pt")
    _write_buy_checkpoint(
        d / "ppo_7203_opening10m.pt",
        {"window_seconds": 600.0, "opening_warmup_seconds": 15.0, "slippage": 0.5},
    )
    (d / "broken.pt").write_bytes(b"not a checkpoint")
    return d


def _info(in_universe: bool = True):
    from trading_rl.scalp_replay import ScalpSessionInfo

    return ScalpSessionInfo(
        2500.0, 1_000_000.0, False, in_universe, "universe" if in_universe else "daily_panel",
        "universe" if in_universe else "default_false",
    )


def test_resolve_prefers_scalp_model_for_every_stem(models_dir):
    assert server._resolve_model_checkpoint("7203") == Path("models/ppo_scalp_top50_open10m.pt")
    assert server._resolve_model_checkpoint("6758") == Path("models/ppo_scalp_top50_open10m.pt")
    assert server._resolve_model_checkpoint("7203", "ppo_7203_opening10m.pt") == (
        Path("models") / "ppo_7203_opening10m.pt"
    )
    (models_dir / "ppo_scalp_top50_open10m.pt").unlink()
    assert server._resolve_model_checkpoint("7203") == Path("models/ppo_7203_opening10m.pt")


def test_ai_models_listing(client, models_dir):
    res = client.get("/api/ai/models")
    assert res.status_code == 200
    by_file = {m["file"]: m for m in res.json()}
    assert set(by_file) == {"ppo_scalp_top50_open10m.pt", "ppo_7203_opening10m.pt"}  # broken skipped
    assert by_file["ppo_scalp_top50_open10m.pt"]["kind"] == "scalp"
    assert by_file["ppo_7203_opening10m.pt"]["kind"] == "market"
    assert by_file["ppo_scalp_top50_open10m.pt"]["default"] is True
    assert by_file["ppo_7203_opening10m.pt"]["default"] is False
    assert all(m["label"] for m in by_file.values())


def test_ai_models_legacy_checkpoint_without_metadata_is_market(client, models_dir):
    _write_buy_checkpoint(models_dir / "legacy.pt", None)
    kinds = {m["file"]: m["kind"] for m in client.get("/api/ai/models").json()}
    assert kinds["legacy.pt"] == "market"


def test_replay_defaults_to_scalp_model(client, models_dir, monkeypatch):
    from trading_rl import scalp_replay

    monkeypatch.setattr(scalp_replay, "lookup_session_info", lambda stem, date: _info())
    res = client.get("/api/ai/replay-trades?stem=7203&date=2026-08-14")
    data = res.json()
    assert data["available"] is True
    assert data["model"] == "ppo_scalp_top50_open10m.pt"
    assert data["model_kind"] == "scalp"
    assert data["env_config"] == {
        "window_s": 600, "warmup_s": 10, "fill_rule": "trade_through", "latency_ms": 50,
    }
    assert data["in_universe"] is True
    assert data["period"] == "test"
    assert isinstance(data["is_heldout"], bool)
    assert isinstance(data["orders"], list) and data["orders"]
    assert {"us", "side", "price", "action"} <= set(data["orders"][0])
    s = data["summary"]
    assert {"totalPnl", "tradeCount", "winRate", "limitExits", "forcedExits"} <= set(s)
    t_open = data["t_open_us"]
    for tr in data["trades"]:
        assert tr["order"] in ("limit", "market") and isinstance(tr["forced"], bool)
        assert 10_000_000 <= tr["us"] - t_open <= 600_000_000


def test_replay_scalp_flags_out_of_universe_and_training_period(client, models_dir, monkeypatch):
    from trading_rl import scalp_replay

    monkeypatch.setattr(
        scalp_replay, "lookup_session_info", lambda stem, date: _info(in_universe=False)
    )
    data = client.get("/api/ai/replay-trades?stem=7203&date=2026-08-14").json()
    assert data["available"] is True and data["in_universe"] is False
    monkeypatch.setattr(scalp_replay, "period_of", lambda date: "train")
    assert client.get("/api/ai/replay-trades?stem=7203&date=2026-08-14").json()["period"] == "train"


def test_replay_scalp_unavailable_without_session_inputs(client, models_dir, monkeypatch):
    from trading_rl import scalp_replay

    monkeypatch.setattr(scalp_replay, "lookup_session_info", lambda stem, date: None)
    res = client.get("/api/ai/replay-trades?stem=7203&date=2026-08-14")
    assert res.status_code == 200
    data = res.json()
    assert data["available"] is False and data["trades"] == [] and data["summary"] is None
    assert "prev_close" in data["reason"]


def test_replay_model_param_selects_legacy_path(client, models_dir):
    data = client.get(
        "/api/ai/replay-trades?stem=7203&date=2026-08-14&model=ppo_7203_opening10m.pt"
    ).json()
    assert data["available"] is True
    assert data["model"] == "ppo_7203_opening10m.pt"
    assert data["model_kind"] == "market"
    assert data["orders"] == []
    assert [t["kind"] for t in data["trades"]] == ["entry", "exit"]
    assert "order" not in data["trades"][0]

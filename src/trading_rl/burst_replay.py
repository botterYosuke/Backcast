"""Burst-moment RL (``models/burst/ppo_burst_*.pt``) replayed on one morning for the web UI.

The replay is exactly the evaluation setting of ``scripts/burst/eval_burst.py``: the
detector (``models/burst/detector_v1.pkl``) scans the whole morning, every trigger opens
a 60 s episode of :mod:`trading_rl.burst_env`, the policy acts greedily, market fills at
the last print 200 ms after the decision +/- 0.5 tick.

Model keys in the app are ``burst/ppo_burst_<tag>_s<seed>.pt`` (the path below ``models/``).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import numpy as np

from . import burst_env as be
from .data import OpeningSessionData

KIND_BURST = "burst"
KEY_PREFIX = "burst/"
#: like the other app models, relative to the server's cwd (the repo root under run.py)
MODELS_DIR = Path("models") / "burst"
DETECTOR_NAME = "detector_v1.pkl"
#: replay window to extract from the day's ticks (the session is cut at 11:29:00)
REPLAY_WINDOW_US = 9000 * be.US
MAIN_MODEL = "ppo_burst_slip1_s0.pt"  # pre-registered main policy (rl-burst-results.md)
_NAME_RE = re.compile(r"^ppo_burst_([a-z0-9]+)_s(\d+)\.pt$")
_TAG_JA = {"plain": "基準の約定（ずれ0.5tick）で練習", "slip1": "1tickずれで練習"}


def _torch_module():
    import torch
    from torch import nn

    class ActorCritic(nn.Module):
        """PPO network of scripts/burst/train_burst.py (same state-dict layout)."""

        def __init__(self, obs_dim: int, hidden: int, mean: np.ndarray, std: np.ndarray):
            super().__init__()
            self.register_buffer("mean", torch.as_tensor(mean, dtype=torch.float32))
            self.register_buffer("std", torch.as_tensor(std, dtype=torch.float32))

            def mlp(out: int, gain: float) -> nn.Sequential:
                m = nn.Sequential(nn.Linear(obs_dim, hidden), nn.SiLU(), nn.Linear(hidden, hidden),
                                  nn.SiLU(), nn.Linear(hidden, out))
                for i, layer in enumerate(m):
                    if isinstance(layer, nn.Linear):
                        nn.init.orthogonal_(layer.weight, gain if i == 4 else np.sqrt(2))
                        nn.init.zeros_(layer.bias)
                return m

            self.pi = mlp(be.N_ACT, 0.01)
            self.v = mlp(1, 1.0)

        def forward(self, obs, mask):
            x = ((obs - self.mean) / self.std).clamp(-10, 10)
            logits = self.pi(x).masked_fill(~mask, -1e9)
            return logits, self.v(x).squeeze(-1)

    return torch, ActorCritic


def ActorCritic(obs_dim: int, hidden: int, mean: np.ndarray, std: np.ndarray):  # noqa: N802
    """Build the PPO network (import torch lazily)."""
    _, cls = _torch_module()
    return cls(obs_dim, hidden, mean, std)


def model_files() -> list[Path]:
    """Selectable checkpoints (the smoke-test run is left out)."""
    if not (MODELS_DIR / DETECTOR_NAME).is_file():
        return []
    return [p for p in sorted(MODELS_DIR.glob("ppo_burst_*.pt"))
            if _NAME_RE.match(p.name) and "smoke" not in p.name]


def model_key(path: Path) -> str:
    return KEY_PREFIX + path.name


def model_label(path: Path) -> str:
    m = _NAME_RE.match(path.name)
    tag, seed = (m.group(1), m.group(2)) if m else (path.stem, "?")
    main = "・本命" if path.name == MAIN_MODEL else ""
    return f"激しい瞬間AI（成行・{_TAG_JA.get(tag, tag)}・seed{seed}{main}）"


def parse_key(key: str | None) -> Path | None:
    """``burst/ppo_burst_slip1_s0.pt`` -> its path when it exists, else None."""
    if not key or not key.startswith(KEY_PREFIX):
        return None
    name = key[len(KEY_PREFIX):]
    if not _NAME_RE.match(name):
        return None
    p = MODELS_DIR / name
    return p if p.is_file() else None


def is_available() -> bool:
    return bool(model_files())


_CACHE: dict[str, tuple[int, Any]] = {}


def _cached(path: Path, loader) -> Any:
    mt = path.stat().st_mtime_ns
    hit = _CACHE.get(str(path))
    if hit is None or hit[0] != mt:
        hit = (mt, loader(path))
        _CACHE[str(path)] = hit
    return hit[1]


def load_detector() -> be.Detector:
    return _cached(MODELS_DIR / DETECTOR_NAME, be.Detector.load)


def load_model(path: Path):
    def _load(p: Path):
        torch, cls = _torch_module()
        ck = torch.load(p, weights_only=False)  # local artefact written by train_burst.py
        m = cls(be.OBS_DIM, int(ck["hidden"]), np.zeros(be.OBS_DIM), np.ones(be.OBS_DIM))
        m.load_state_dict(ck["state_dict"])
        m.eval()
        return m

    return _cached(path, _load)


def greedy_policy(model):
    torch, _ = _torch_module()

    def f(obs: np.ndarray, mask: np.ndarray, _bb: Any) -> np.ndarray:
        with torch.no_grad():
            lg, _ = model(torch.from_numpy(obs), torch.from_numpy(mask))
        return lg.argmax(-1).numpy()

    return f


def period_of(date: str) -> str:
    """Burst-study periods: train / val (検証その1) / val2 (検証その2, used once) / final."""
    if date < be.TRAIN[0]:
        return "pre_train"
    if date <= be.TRAIN[1]:
        return "train"
    if date <= be.VAL[1]:
        return "val"
    if date <= be.VAL2[1]:
        return "val2"
    return "final"


def simulate_burst_trades(
    raw: OpeningSessionData,
    key: str,
    *,
    prev_close: float,
    avg_volume_20d: float,
    is_topix100: bool,
    held: bool = False,
    qty: int = 100,
    policy: Any = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """Replay one morning: ``(trades, summary, episodes, env_config)``.

    ``trades`` has the JSON shape of :func:`trading_rl.scalp_replay.format_replay`
    (market orders only); ``episodes`` lists the detector windows
    ``{"start_us", "end_us", "score"}`` (absolute microseconds).  ``raw`` must cover the
    morning (extract it with :data:`REPLAY_WINDOW_US`).  ``policy`` overrides the model
    (tests).
    """
    from .scalp_env import morning_window_s

    if policy is None:
        path = parse_key(key)
        if path is None:
            raise ValueError(f"not a burst model key: {key!r}")
        policy = greedy_policy(load_model(path))
    det = load_detector()
    cfg = be.BurstConfig()
    W = morning_window_s(raw.t_open_us)
    g = be.session_grid(raw, prev_close=prev_close, avg_volume_20d=avg_volume_20d,
                        is_topix100=is_topix100, window_s=W)
    score = det.score(g.X)
    part = be.session_episodes(g, score, det.threshold)
    part.update(date=g.date, stem=g.stem, held=bool(held), t100=bool(is_topix100), tod0=g.tod_open_s)
    t_open = int(raw.t_open_us)
    lat = cfg.latency_ms * 1000
    env_config = {
        "window_seconds": W,
        "opening_warmup_seconds": be.START_S,
        "fill_rule": "market (last ±0.5 tick)",
        "latency_ms": cfg.latency_ms,
        "episode_s": be.EP_S,
        "detector_threshold": round(det.threshold, 4),
    }
    trades: list[dict[str, Any]] = []
    episodes: list[dict[str, Any]] = []
    pnl_ticks = 0.0
    if len(part["s0"]):
        data = be.concat_episodes([part], [(g.date, g.stem, bool(held))])
        bb = be.BurstBatch(data, np.arange(len(data)), cfg)
        while not bb.done:
            bb.step(policy(bb.obs(), bb.mask(), bb))
        T = bb.trip_table()
        b = np.searchsorted(bb.idx, T["ep"])
        for i in range(len(T["pnl"])):
            k, ej, xj, d = int(b[i]), int(T["entry_j"][i]), int(T["exit_j"][i]), int(T["dir"][i])
            s0 = int(data.s0[k])
            ent = float(bb.buy_px[k, ej] if d > 0 else bb.sell_px[k, ej])
            ex = float(bb.sell_px[k, xj] if d > 0 else bb.buy_px[k, xj])
            pnl = d * (ex - ent) * qty
            pnl_ticks += float(T["pnl"][i])
            trades.append({"us": t_open + (s0 + ej) * be.US + lat, "side": "buy" if d > 0 else "sell",
                           "price": round(ent, 4), "qty": qty, "kind": "entry", "pnl": 0.0,
                           "order": "market", "forced": False})
            trades.append({"us": t_open + (s0 + xj) * be.US + lat, "side": "sell" if d > 0 else "buy",
                           "price": round(ex, 4), "qty": qty, "kind": "exit",
                           "pnl": float(round(pnl, 2)), "order": "market",
                           "forced": bool(T["forced"][i])})
        trades.sort(key=lambda t: (t["us"], t["kind"] != "exit"))
        for k in range(len(data)):
            s0 = int(data.s0[k])
            episodes.append({"start_us": t_open + s0 * be.US,
                             "end_us": t_open + (s0 + be.EP_S) * be.US,
                             "score": round(float(data.score[k, 0]), 4)})
    exits = [t for t in trades if t["kind"] == "exit"]
    summary = {
        "totalPnl": float(round(sum(t["pnl"] for t in exits), 2)),
        "tradeCount": len(exits),
        "winRate": float(round(sum(t["pnl"] > 0 for t in exits) / len(exits), 4)) if exits else 0.0,
        "limitExits": 0,
        "forcedExits": sum(1 for t in exits if t["forced"]),
        "marketExits": sum(1 for t in exits if not t["forced"]),
        "burstEpisodes": len(episodes),
        "pnlTicks": round(pnl_ticks, 3),
    }
    return trades, summary, episodes, env_config


__all__ = [
    "KIND_BURST",
    "MAIN_MODEL",
    "REPLAY_WINDOW_US",
    "ActorCritic",
    "is_available",
    "model_files",
    "model_key",
    "model_label",
    "parse_key",
    "period_of",
    "simulate_burst_trades",
]

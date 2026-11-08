"""Stage-2 predictor + rule (plan Q6) replayed on the calibrated ScalpEnv for the web UI.

The predictor (``scripts/probes/stage2_trade.py --fit``) is one HGB regressor per
horizon ``h`` on the 28 gate features + the 11 trades-only fill-model features, all from
the kabu PUSH-like tape (``obs_tape="push20"``).  A *preset* fixes ``(h, q, variant)``:
enter when ``|prediction| >= `` the train top-``q`` threshold, with the rule
:func:`rule_policy` (``O2`` limit in / market out, ``O3`` limit in / limit out), holding
``h`` seconds.  The simulator is the calibrated one of stage 1/2: ``fill_rule="queue_est"``,
200 ms latency, sequential cancel, the whole morning (T_open+10 s .. 11:29:00).

Model keys in the app are ``predictor/h10_q0.01_O3`` etc. (see :data:`PRESETS`).
"""

from __future__ import annotations

import pickle
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from . import fill_model as fm
from .data import OpeningSessionData
from .scalp_env import (
    G_ASK0,
    G_BID0,
    G_CANCEL,
    G_FLATTEN,
    G_KEEP,
    G_MBUY,
    G_MSELL,
    MORNING_CACHE_S,
    ScalpConfig,
    ScalpEnv,
    ScalpSession,
    build_scalp_session,
    morning_window_s,
    push_tape,
    tod_seconds,
)

PRED_MODELS_PATH = (
    Path(__file__).resolve().parents[2] / ".cache" / "trading_rl" / "stage2" / "pred_models.pkl"
)
FILL_MODEL_PATH = fm.DEFAULT_MODEL_PATH
KIND_PREDICTOR = "predictor"
KEY_PREFIX = "predictor/"
#: replay window to extract from the day's ticks (the session is cut at 11:29:00)
REPLAY_WINDOW_US = MORNING_CACHE_S * fm.US
ENTRY_WAIT_S = 3
EXIT_WAIT_S = 5
LATENCY_MS = 200


@dataclass(frozen=True)
class Preset:
    h: int
    q: float
    variant: str

    @property
    def key(self) -> str:
        return f"{KEY_PREFIX}h{self.h}_q{self.q:g}_{self.variant}"

    @property
    def label(self) -> str:
        how = "指値で入り指値で出る" if self.variant == "O3" else "指値で入り成行で出る"
        return f"予測モデル（{self.h}秒先・自信上位{self.q * 100:g}%・{how}）"


#: presets offered in the app (the stage-2 grid is h x q x {O2, O3})
PRESETS: tuple[Preset, ...] = (
    Preset(10, 0.01, "O3"),
    Preset(10, 0.002, "O3"),
    Preset(10, 0.05, "O3"),
    Preset(5, 0.01, "O3"),
    Preset(3, 0.01, "O3"),
    Preset(1, 0.01, "O3"),
    Preset(10, 0.01, "O2"),
)

_KEY_RE = re.compile(r"^predictor/h(\d+)_q([0-9.]+)_(O2|O3)$")


def parse_key(key: str | None) -> Preset | None:
    """``predictor/h10_q0.01_O3`` -> Preset, anything else -> None."""
    m = _KEY_RE.match(key or "")
    if not m:
        return None
    return Preset(int(m.group(1)), float(m.group(2)), m.group(3))


def is_available() -> bool:
    """Both the predictors and the fill model have been built."""
    return PRED_MODELS_PATH.is_file() and FILL_MODEL_PATH.is_file()


_CACHE: dict[str, tuple[int, Any]] = {}


def _load(path: Path, loader) -> Any:
    mt = path.stat().st_mtime_ns
    hit = _CACHE.get(str(path))
    if hit is None or hit[0] != mt:
        hit = (mt, loader(path))
        _CACHE[str(path)] = hit
    return hit[1]


def load_predictors() -> dict[str, Any]:
    return _load(PRED_MODELS_PATH, lambda p: pickle.loads(p.read_bytes()))


def load_fill_model() -> fm.FillModel:
    return _load(FILL_MODEL_PATH, fm.FillModel.load)


def predictor_config() -> ScalpConfig:
    return ScalpConfig(order_mode="both", fill_rule="queue_est", latency_ms=LATENCY_MS,
                       sequential_cancel=True, window="morning", obs_tape="push20")


def predictor_features(raw: OpeningSessionData, sess: ScalpSession) -> np.ndarray:
    """(W - start_s, 28 + 11) features at every decision second, push20 tape."""
    S, W = sess.start_s, sess.window_s
    dec = np.arange(S, W)
    pt = push_tape(raw)
    t_rel = np.asarray(pt.epoch_us, dtype=np.int64) - int(raw.t_open_us)
    keep = (t_rel >= 0) & (t_rel <= W * fm.US)
    Xm = fm.fill_features_at(t_rel[keep], np.asarray(pt.price)[keep], np.asarray(pt.qty)[keep],
                             dec * fm.US, sess.tick[dec], tod_seconds(raw.t_open_us) + dec)
    return np.concatenate([sess.X[: len(dec)], Xm], 1).astype(np.float32)


def rule_policy(env: ScalpEnv, sess: ScalpSession, variant: str, sig: np.ndarray, H: int,
                thr: float, wait: int = ENTRY_WAIT_S, exit_wait: int = EXIT_WAIT_S
                ) -> dict[str, Any]:
    """Run one session with the signal rule; returns the episode summary.

    ``sig[d - start_s]`` is the signal (ticks) at decision second ``d``.  Entry when
    ``|sig| >= thr``:

    * ``O1`` market in, market out after ``H`` s
    * ``O2`` limit in at the last print (re-placed each second while the signal holds,
      up to ``wait`` s), market out ``H`` s after the fill
    * ``O3`` limit in as O2, limit out at the last print ``H`` s after the fill, market
      after ``exit_wait`` s
    """
    env.reset(options={"session": sess})
    W, S = sess.window_s, sess.start_s
    st = {"mode": "flat", "t": 0, "dir": 0}
    done = False
    d = S
    info: dict[str, Any] = {}
    while not done:
        pos = env.position
        sv = env.state_view()
        a = G_KEEP
        s_now = sig[d - S]
        if pos == 0:
            if st["mode"] in ("exit_wait", "holding"):
                st = {"mode": "flat", "t": 0, "dir": 0}
            want = 1 if s_now >= thr else (-1 if s_now <= -thr else 0)
            if variant == "O1":
                if want and W - d > H + 2:
                    a = G_MBUY if want > 0 else G_MSELL
                    st = {"mode": "holding", "t": d, "dir": want}
            else:
                working = sv["bid"] is not None or sv["ask"] is not None
                if st["mode"] == "entry_wait":
                    if want != st["dir"] or d - st["t"] >= wait or W - d <= H + 2:
                        a = G_CANCEL if working else G_KEEP
                        st = {"mode": "flat", "t": 0, "dir": 0}
                    else:
                        # keep the order at k=0 of the current last print
                        px = sv["bid"] if want > 0 else sv["ask"]
                        if px is None or abs(px - sv["last"]) > 1e-9:
                            a = G_BID0 if want > 0 else G_ASK0
                elif want and W - d > H + 2:
                    a = G_BID0 if want > 0 else G_ASK0
                    st = {"mode": "entry_wait", "t": d, "dir": want}
        else:
            if st["mode"] in ("flat", "entry_wait"):
                # entry just filled (limit) -> holding from now
                if sv["bid"] is not None or sv["ask"] is not None:
                    a = G_CANCEL
                st = {"mode": "holding", "t": d, "dir": pos}
            elif st["mode"] == "holding" and d - st["t"] >= H:
                if variant == "O3":
                    a = G_ASK0 if pos > 0 else G_BID0
                    st = {"mode": "exit_wait", "t": d, "dir": pos}
                else:
                    a = G_FLATTEN
            elif st["mode"] == "exit_wait":
                if d - st["t"] >= exit_wait:
                    a = G_FLATTEN
                else:
                    px = sv["ask"] if pos > 0 else sv["bid"]
                    if px is None or abs(px - sv["last"]) > 1e-9:
                        a = G_ASK0 if pos > 0 else G_BID0
        if not env.action_mask()[a]:
            a = G_KEEP
        _, _, done, _, info = env.step(a)
        d += 1
    return info["episode"]


def simulate_predictor_trades(
    opening_session: OpeningSessionData,
    key: str,
    *,
    prev_close: float,
    avg_volume_20d: float,
    is_topix100: bool,
    qty: int = 100,
    seed: int = 0,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """Replay a preset on one morning session: ``(trades, summary, orders, env_config)``.

    Same JSON shapes as :func:`scalp_replay.simulate_scalp_trades`.  ``opening_session``
    must cover the morning (extract it with :data:`REPLAY_WINDOW_US`).
    """
    from .scalp_replay import _RecordingScalpEnv, format_replay

    preset = parse_key(key)
    if preset is None:
        raise ValueError(f"not a predictor key: {key!r}")
    pred = load_predictors()
    if preset.h not in pred["models"] or preset.q not in pred["thr"][preset.h]:
        raise ValueError(f"preset {key!r} is not in {PRED_MODELS_PATH.name}")
    W = morning_window_s(opening_session.t_open_us)
    sess = build_scalp_session(
        opening_session, prev_close=prev_close, avg_volume_20d=avg_volume_20d,
        is_topix100=is_topix100, window_s=W, obs_tape="push20", fill_model=load_fill_model(),
    )
    sig = pred["models"][preset.h].predict(predictor_features(opening_session, sess))
    thr = pred["thr"][preset.h][preset.q]
    env = _RecordingScalpEnv(config=predictor_config(), seed=seed)
    rule_policy(env, sess, preset.variant, sig, preset.h, thr)
    trades, summary, orders = format_replay(env, sess, qty)
    summary["signalSeconds"] = int(np.sum(np.abs(sig) >= thr))
    env_config = {
        "window_seconds": W,
        "opening_warmup_seconds": sess.start_s,
        "fill_rule": "queue_est",
        "latency_ms": LATENCY_MS,
        "horizon_s": preset.h,
        "top_q": preset.q,
        "threshold_ticks": round(float(thr), 4),
        "variant": preset.variant,
    }
    return trades, summary, orders, env_config


__all__ = [
    "KIND_PREDICTOR",
    "PRED_MODELS_PATH",
    "PRESETS",
    "REPLAY_WINDOW_US",
    "Preset",
    "is_available",
    "parse_key",
    "predictor_config",
    "predictor_features",
    "rule_policy",
    "simulate_predictor_trades",
]

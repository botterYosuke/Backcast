"""Burst-moment scalping practice ground ("honest" market-order env).

Episodes are the ``ep_s`` (default 60) seconds that follow a trigger of a
volatility-burst detector (HGB classifier of "the last print moves >= K ticks in
either direction within the next H seconds"), collected over the whole morning
session (``T_open + 10 s`` .. 11:29:00).  The detector and its threshold are fitted
on the train period only (:func:`fit_detector` refuses anything else).

Why a separate env (instead of :class:`trading_rl.scalp_env.ScalpEnv`):
    * Market orders only.  Limit fills cannot be reproduced honestly from prints
      (``queue_est``'s "marketable on arrival" is a coin flip unrelated to the
      later price path, findings rl-burst-task-prompt.md point 4).
    * Episodes are short and fixed-length, so every fill price can be precomputed
      and the env vectorised over thousands of episodes (PPO needs ~10^7 steps).

Conventions
    * Decisions at seconds ``d_j = s0 + j`` (``j = 0 .. ep_s - 1``; ``s0`` = trigger
      second since ``T_open``).  At ``j = ep_s`` any open position is flattened
      (forced).  The detector fires at ``s0`` using prints ``<= T_open + s0`` only.
    * Actions (3): ``0`` wait / hold, ``1`` market buy, ``2`` market sell.  Flat: all
      legal (buy opens long, sell opens short).  Long: {0, 2 = close}.  Short:
      {0, 1 = close}.  Illegal actions are treated as ``0`` and counted.
    * Market fill: an order decided at ``d_j`` fills at the last print at or before
      ``d_j + latency`` (default 200 ms) +/- ``slip_ticks`` (default 0.5) x the tick
      size at that price.  ``fill_price="worst"`` (stress): the reference is the
      worst print in ``[last at d_j] U (d_j, d_j + latency]`` for the order side.
      Round trip with the defaults = 1 tick.
    * PnL is in ticks of the episode reference tick (tick size of the last print at
      the trigger), per 100 shares; ``pnl_ticks`` of a trip is net of slippage.
    * Observation (all causal, prints ``<= T_open + d_j``): the 28 gate features and
      the 11 fill-model micro features on the kabu-PUSH-like tape (``push20``), the
      detector log-odds at ``d_j``, then agent state: position, unrealized PnL
      (ticks, last print at ``d_j`` vs the entry fill, clipped +/-20), seconds held
      / 60, fraction of the episode left.  With ``latency <= 1 s`` the entry fill of
      an order decided at ``d_{j-1}`` happens at or before ``d_j``, so the state is
      causal too.
    * Reward (training only; reported PnL is always raw): the trip PnL on the step
      that closes it (incl. the forced close), minus
      ``unrealized_loss_penalty_per_s * max(0, -unrealized)`` per second a position
      is carried into the next decision, minus ``hold_penalty_per_s`` per such
      second, plus ``entry_bonus_ticks`` per entry.
"""

from __future__ import annotations

import pickle
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import gymnasium as gym
import numpy as np
from gymnasium import spaces

from . import fill_model as fm
from .data import OpeningSessionData
from .gate_features import FEATURE_NAMES as GATE_NAMES
from .universe import tick_sizes

US = 1_000_000
START_S = 10
EP_S = 60
LATENCIES_MS: tuple[int, ...] = (200, 500, 1000)
TRAIN = ("2024-01-04", "2025-06-30")
VAL = ("2025-07-01", "2025-12-31")
VAL2 = ("2026-01-01", "2026-08-19")
SPLITS = {"train": TRAIN, "val": VAL, "val2": VAL2}
LAST_USABLE_DATE = "2026-08-19"  # 2026-08-20+ is the owner's final test, never touched
EVENT_H, EVENT_K = 10, 5.0  # detector event: |move| >= 5 ticks within 10 s
MARKET_NAMES: list[str] = list(GATE_NAMES) + [f"m_{n}" for n in fm.BASE_NAMES]
STATE_NAMES: list[str] = ["det_logit", "pos", "unreal_t", "held_min", "t_left"]
OBS_NAMES: list[str] = MARKET_NAMES + STATE_NAMES
N_MARKET = len(MARKET_NAMES)
OBS_DIM = len(OBS_NAMES)
N_ACT = 3
A_WAIT, A_BUY, A_SELL = 0, 1, 2
DEFAULT_DIR = Path(__file__).resolve().parents[2] / ".cache" / "trading_rl" / "burst"
MODEL_DIR = Path(__file__).resolve().parents[2] / "models" / "burst"


# --------------------------------------------------------------------------- #
# Per-session feature grid
# --------------------------------------------------------------------------- #
@dataclass
class SessionGrid:
    date: str
    stem: str
    is_topix100: bool
    t_open_us: int
    tod_open_s: float
    W: int  # forced flatten second of the session (since T_open)
    secs: np.ndarray  # (n,) decision seconds START_S .. W - 1
    X: np.ndarray  # (n, N_MARKET) float32 causal features at secs
    last: np.ndarray  # (W + 1,) last print at each whole second (every print)
    tick: np.ndarray  # (W + 1,) tick size at last
    t_rel: np.ndarray  # (m,) int64 us since T_open, every print in [0, W]
    price: np.ndarray  # (m,) float64


def session_grid(
    raw: OpeningSessionData,
    *,
    prev_close: float,
    avg_volume_20d: float,
    is_topix100: bool,
    window_s: int,
) -> SessionGrid:
    """Causal features on the 1 s grid (gate features + fill micro, push20 tape)."""
    from .scalp_env import build_scalp_session, push_tape, tod_seconds

    W = int(window_s)
    sess = build_scalp_session(
        raw, prev_close=prev_close, avg_volume_20d=avg_volume_20d,
        is_topix100=is_topix100, window_s=W, obs_tape="push20",
    )
    pt = push_tape(raw)
    t_rel = np.asarray(pt.epoch_us, dtype=np.int64) - int(raw.t_open_us)
    keep = (t_rel >= 0) & (t_rel <= W * US)
    secs = np.arange(START_S, W, dtype=np.int64)
    tod0 = tod_seconds(raw.t_open_us)
    Xm = fm.fill_features_at(
        t_rel[keep], np.asarray(pt.price, dtype=np.float64)[keep],
        np.asarray(pt.qty, dtype=np.float64)[keep], secs * US, sess.tick[secs], tod0 + secs,
    )
    X = np.concatenate([sess.X, Xm.astype(np.float32)], axis=1).astype(np.float32)
    return SessionGrid(
        date=str(raw.date), stem=str(raw.stem), is_topix100=bool(is_topix100),
        t_open_us=int(raw.t_open_us), tod_open_s=float(tod0), W=W, secs=secs, X=X,
        last=sess.last, tick=sess.tick, t_rel=sess.t_rel, price=sess.price,
    )


def burst_labels(g: SessionGrid, h: int = EVENT_H, k: float = EVENT_K) -> np.ndarray:
    """Event at each decision second: max |last[s+1..s+h] - last[s]| >= k ticks
    (uses the FUTURE -> training labels only).  Seconds without h s left -> False."""
    s = g.secs
    ok = s + h <= g.W
    fut = np.stack([g.last[np.minimum(s + j, g.W)] for j in range(1, h + 1)], 1)
    mv = np.abs(fut - g.last[s][:, None]) / g.tick[s][:, None]
    return (mv.max(1) >= k - 1e-9) & ok


# --------------------------------------------------------------------------- #
# Detector (train period only)
# --------------------------------------------------------------------------- #
def _in_period(dates: Any, period: tuple[str, str]) -> bool:
    d = np.asarray([str(x)[:10] for x in np.ravel(np.asarray(dates))])
    return bool(len(d)) and bool((d >= period[0]).all() and (d <= period[1]).all())


@dataclass
class Detector:
    clf: Any
    threshold: float  # score >= threshold -> trigger
    meta: dict[str, Any] = field(default_factory=dict)

    def score(self, X: np.ndarray) -> np.ndarray:
        return self.clf.predict_proba(np.asarray(X, dtype=np.float32))[:, 1].astype(np.float32)

    def save(self, path: Path | str) -> None:
        path = Path(path)
        if path.exists():
            raise FileExistsError(f"{path} exists (models are never overwritten)")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as fh:
            pickle.dump({"clf": self.clf, "threshold": self.threshold, "meta": self.meta}, fh)

    @classmethod
    def load(cls, path: Path | str) -> Detector:
        with open(path, "rb") as fh:
            z = pickle.load(fh)  # noqa: S301 - local artefact written by save()
        return cls(z["clf"], float(z["threshold"]), z.get("meta", {}))


def fit_detector(
    X_fit: np.ndarray,
    y_fit: np.ndarray,
    dates_fit: Any,
    X_thr: np.ndarray,
    dates_thr: Any,
    q: float = 0.995,
    *,
    max_iter: int = 200,
    random_state: int = 0,
) -> Detector:
    """Fit the HGB burst classifier on ``X_fit`` and set the threshold at the ``q``
    quantile of the scores of ``X_thr`` (a disjoint set of TRAIN sessions).  Both row
    sets must lie in the train period, otherwise ``ValueError``."""
    from sklearn.ensemble import HistGradientBoostingClassifier

    for name, d in (("fit", dates_fit), ("threshold", dates_thr)):
        if not _in_period(d, TRAIN):
            raise ValueError(f"detector {name} rows must all be in the train period {TRAIN}")
    clf = HistGradientBoostingClassifier(
        max_iter=max_iter, learning_rate=0.05, max_leaf_nodes=31, min_samples_leaf=200,
        random_state=random_state,
    )
    clf.fit(np.asarray(X_fit, dtype=np.float32), np.asarray(y_fit, dtype=bool))
    det = Detector(clf, 0.0)
    s = det.score(X_thr)
    det.threshold = float(np.quantile(s, q))
    dd = np.asarray([str(x)[:10] for x in np.concatenate([np.ravel(dates_fit), np.ravel(dates_thr)])])
    det.meta = {
        "event": {"h_s": EVENT_H, "k_ticks": EVENT_K, "two_sided": True},
        "q": q,
        "train_period": TRAIN,
        "date_min": str(sorted(dd.tolist())[0]),
        "date_max": str(sorted(dd.tolist())[-1]),
        "n_fit_rows": int(len(y_fit)),
        "fit_pos_rate": float(np.mean(y_fit)),
        "n_thr_rows": int(len(s)),
        "features": MARKET_NAMES,
    }
    return det


# --------------------------------------------------------------------------- #
# Episodes
# --------------------------------------------------------------------------- #
def trigger_seconds(secs: np.ndarray, score: np.ndarray, thr: float, W: int, ep_s: int = EP_S) -> list[int]:
    """Non-overlapping triggers: first second with score >= thr, then the next one only
    after that episode's forced close (``s0 + ep_s``); the episode must end by ``W``."""
    out: list[int] = []
    nxt = -1
    for i in np.flatnonzero(score >= thr):
        s0 = int(secs[i])
        if s0 <= nxt or s0 + ep_s > W:
            continue
        out.append(s0)
        nxt = s0 + ep_s
    return out


def _fill_refs(t_rel: np.ndarray, price: np.ndarray, t_dec: np.ndarray, lat_us: int):
    """Last print at ``t_dec + lat`` and the max / min of {last at t_dec} U prints in
    ``(t_dec, t_dec + lat]``."""
    c0 = np.searchsorted(t_rel, t_dec, side="right")
    c1 = np.searchsorted(t_rel, t_dec + lat_us, side="right")
    px = price[np.maximum(c1 - 1, 0)]
    last0 = price[np.maximum(c0 - 1, 0)]
    hi = last0.copy()
    lo = last0.copy()
    for i in np.flatnonzero(c1 > c0):
        seg = price[c0[i] : c1[i]]
        hi[i] = max(hi[i], seg.max())
        lo[i] = min(lo[i], seg.min())
    return px, hi, lo


def session_episodes(
    g: SessionGrid, score: np.ndarray, thr: float, ep_s: int = EP_S,
    lats_ms: tuple[int, ...] = LATENCIES_MS,
) -> dict[str, np.ndarray]:
    """Episode arrays of one session (keys as in :class:`BurstData`)."""
    trig = trigger_seconds(g.secs, score, thr, g.W, ep_s)
    n = len(trig)
    F = g.X.shape[1]
    out: dict[str, Any] = {
        "X": np.zeros((n, ep_s, F), np.float32),
        "score": np.zeros((n, ep_s), np.float32),
        "last": np.zeros((n, ep_s + 1), np.float64),
        "s0": np.asarray(trig, np.int32),
    }
    for L in lats_ms:
        for k in ("px", "hi", "lo"):
            out[f"{k}{L}"] = np.zeros((n, ep_s + 1), np.float64)
    for e, s0 in enumerate(trig):
        i0 = s0 - START_S
        out["X"][e] = g.X[i0 : i0 + ep_s]
        out["score"][e] = score[i0 : i0 + ep_s]
        d = np.arange(s0, s0 + ep_s + 1, dtype=np.int64)
        out["last"][e] = g.last[d]
        for L in lats_ms:
            px, hi, lo = _fill_refs(g.t_rel, g.price, d * US, L * 1000)
            out[f"px{L}"][e], out[f"hi{L}"][e], out[f"lo{L}"][e] = px, hi, lo
    return out


@dataclass
class BurstData:
    """Concatenated episodes of a split.  Per-episode meta: date, stem, held,
    t100, s0 (trigger second since T_open), tod0 (time of day of T_open, s),
    ref_tick.  ``sessions``: every scanned session (date, stem, held) incl. the ones
    without a trigger, for per-session averages."""

    X: np.ndarray
    score: np.ndarray
    last: np.ndarray
    fills: dict[str, np.ndarray]  # px200 / hi200 / lo200 / px1000 ...
    date: np.ndarray
    stem: np.ndarray
    held: np.ndarray
    t100: np.ndarray
    s0: np.ndarray
    tod0: np.ndarray
    ref_tick: np.ndarray
    sess_date: np.ndarray
    sess_stem: np.ndarray
    sess_held: np.ndarray
    meta: dict[str, Any] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.date)

    @property
    def ep_s(self) -> int:
        return int(self.score.shape[1])

    def subset(self, m: np.ndarray) -> BurstData:
        """Episode subset; the scanned-session table is kept whole."""
        m = np.asarray(m)
        return BurstData(
            X=self.X[m], score=self.score[m], last=self.last[m],
            fills={k: v[m] for k, v in self.fills.items()}, date=self.date[m],
            stem=self.stem[m], held=self.held[m], t100=self.t100[m], s0=self.s0[m],
            tod0=self.tod0[m], ref_tick=self.ref_tick[m],
            sess_date=self.sess_date, sess_stem=self.sess_stem, sess_held=self.sess_held, meta=dict(self.meta),
        )

    def save(self, path: Path | str) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez(
            path, X=self.X, score=self.score, last=self.last, date=self.date, stem=self.stem,
            held=self.held, t100=self.t100, s0=self.s0, tod0=self.tod0, ref_tick=self.ref_tick,
            sess_date=self.sess_date, sess_stem=self.sess_stem, sess_held=self.sess_held,
            meta=np.array(repr(self.meta)), **{f"f_{k}": v for k, v in self.fills.items()},
        )

    @classmethod
    def load(cls, path: Path | str) -> BurstData:
        import ast

        z = np.load(path, allow_pickle=False)
        fills = {k[2:]: z[k] for k in z.files if k.startswith("f_")}
        return cls(
            X=z["X"], score=z["score"], last=z["last"], fills=fills, date=z["date"],
            stem=z["stem"], held=z["held"], t100=z["t100"], s0=z["s0"], tod0=z["tod0"],
            ref_tick=z["ref_tick"], sess_date=z["sess_date"], sess_stem=z["sess_stem"],
            sess_held=z["sess_held"], meta=ast.literal_eval(str(z["meta"])),
        )


def concat_episodes(parts: list[dict[str, Any]], sessions: list[tuple[str, str, bool]],
                    meta: dict[str, Any] | None = None) -> BurstData:
    """Join :func:`session_episodes` outputs; each part also carries ``date``,
    ``stem``, ``held``, ``t100``, ``tod0`` scalars."""
    parts = [p for p in parts if len(p["s0"])]
    if not parts:
        raise ValueError("no episodes")
    cat = lambda k: np.concatenate([p[k] for p in parts])  # noqa: E731
    rep = lambda k, dt: np.concatenate([np.full(len(p["s0"]), p[k], dtype=dt) for p in parts])  # noqa: E731
    t100 = rep("t100", bool)
    last = cat("last")
    ref = tick_sizes(last[:, 0], t100)
    fills = {k: cat(k) for k in parts[0] if k[:2] in ("px", "hi", "lo")}
    sd = np.array([s[0] for s in sessions])
    return BurstData(
        X=cat("X"), score=cat("score"), last=last, fills=fills,
        date=rep("date", "<U10"), stem=rep("stem", "<U12"), held=rep("held", bool), t100=t100,
        s0=cat("s0"), tod0=rep("tod0", np.float64), ref_tick=ref.astype(np.float64),
        sess_date=sd.astype("<U10"), sess_stem=np.array([s[1] for s in sessions], dtype="<U12"),
        sess_held=np.array([s[2] for s in sessions], dtype=bool), meta=dict(meta or {}),
    )


# --------------------------------------------------------------------------- #
# Vectorised simulator
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class BurstConfig:
    latency_ms: int = 200
    slip_ticks: float = 0.5
    fill_price: str = "last"  # "last" | "worst"
    unrealized_loss_penalty_per_s: float = 0.0
    hold_penalty_per_s: float = 0.0
    entry_bonus_ticks: float = 0.0

    def __post_init__(self) -> None:
        if self.latency_ms not in LATENCIES_MS:
            raise ValueError(f"latency_ms must be one of {LATENCIES_MS}")
        if self.fill_price not in ("last", "worst"):
            raise ValueError("fill_price must be 'last' or 'worst'")
        if self.slip_ticks < 0 or min(self.unrealized_loss_penalty_per_s,
                                      self.hold_penalty_per_s, self.entry_bonus_ticks) < 0:
            raise ValueError("costs / penalties must be >= 0")


def logit(p: np.ndarray) -> np.ndarray:
    p = np.clip(np.asarray(p, np.float64), 1e-6, 1 - 1e-6)
    return np.log(p / (1 - p)).astype(np.float32)


class BurstBatch:
    """``B`` episodes stepped in lockstep (all have the same length ``ep_s``)."""

    def __init__(self, data: BurstData, idx: np.ndarray, cfg: BurstConfig) -> None:
        self.data, self.cfg = data, cfg
        self.idx = np.asarray(idx, dtype=np.int64)
        B = len(self.idx)
        L = cfg.latency_ms
        t100 = data.t100[self.idx][:, None]
        if cfg.fill_price == "worst":
            bref, sref = data.fills[f"hi{L}"][self.idx], data.fills[f"lo{L}"][self.idx]
        else:
            bref = sref = data.fills[f"px{L}"][self.idx]
        self.buy_px = bref + cfg.slip_ticks * tick_sizes(bref, np.broadcast_to(t100, bref.shape))
        self.sell_px = sref - cfg.slip_ticks * tick_sizes(sref, np.broadcast_to(t100, sref.shape))
        self.last = data.last[self.idx]
        self.ref = data.ref_tick[self.idx]
        self.det = logit(data.score[self.idx])
        self.ep_s = data.ep_s
        self.j = 0
        self.pos = np.zeros(B, np.int8)
        self.entry = np.zeros(B)
        self.entry_j = np.zeros(B, np.int32)
        self.n_invalid = 0
        self.n_entries = 0
        self.trips: list[tuple[np.ndarray, ...]] = []  # (b, entry_j, exit_j, dir, pnl, forced)
        self.done = False

    @property
    def B(self) -> int:
        return len(self.idx)

    def obs(self) -> np.ndarray:
        j = min(self.j, self.ep_s - 1)
        o = np.empty((self.B, OBS_DIM), np.float32)
        o[:, :N_MARKET] = self.data.X[self.idx, j]
        o[:, N_MARKET] = self.det[:, j]
        p = self.pos.astype(np.float32)
        o[:, N_MARKET + 1] = p
        un = np.where(self.pos != 0, p * (self.last[:, j] - self.entry) / self.ref, 0.0)
        o[:, N_MARKET + 2] = np.clip(un, -20, 20)
        o[:, N_MARKET + 3] = np.where(self.pos != 0, (j - self.entry_j) / 60.0, 0.0)
        o[:, N_MARKET + 4] = (self.ep_s - j) / self.ep_s
        return o

    def mask(self) -> np.ndarray:
        m = np.ones((self.B, N_ACT), bool)
        m[self.pos == 1, A_BUY] = False
        m[self.pos == -1, A_SELL] = False
        return m

    def _close(self, sel: np.ndarray, j: int, forced: bool) -> np.ndarray:
        """Close positions in ``sel`` at the fills of decision ``j``; PnL per episode."""
        pnl = np.zeros(self.B)
        if not sel.any():
            return pnl
        ex = np.where(self.pos == 1, self.sell_px[:, j], self.buy_px[:, j])
        v = self.pos * (ex - self.entry) / self.ref
        pnl[sel] = v[sel]
        b = np.flatnonzero(sel)
        self.trips.append((b, self.entry_j[b].copy(), np.full(len(b), j, np.int32),
                           self.pos[b].copy(), v[b].copy(), np.full(len(b), forced)))
        self.pos[sel] = 0
        self.entry[sel] = 0.0
        return pnl

    def step(self, a: np.ndarray) -> tuple[np.ndarray, bool]:
        if self.done:
            raise RuntimeError("episode batch is done")
        cfg, j = self.cfg, self.j
        a = np.asarray(a, dtype=np.int64)
        ok = self.mask()[np.arange(self.B), a]
        self.n_invalid += int((~ok).sum())
        a = np.where(ok, a, A_WAIT)
        flat = self.pos == 0
        r = self._close(((self.pos == 1) & (a == A_SELL)) | ((self.pos == -1) & (a == A_BUY)), j, False)
        el, es = flat & (a == A_BUY), flat & (a == A_SELL)
        self.entry = np.where(el, self.buy_px[:, j], np.where(es, self.sell_px[:, j], self.entry))
        self.pos[el], self.pos[es] = 1, -1
        self.entry_j[el | es] = j
        ne = el | es
        self.n_entries += int(ne.sum())
        r += cfg.entry_bonus_ticks * ne
        self.j = j + 1
        if self.j >= self.ep_s:
            r += self._close(self.pos != 0, self.ep_s, True)
            self.done = True
        else:
            hold = self.pos != 0
            un = self.pos * (self.last[:, self.j] - self.entry) / self.ref
            r -= hold * (cfg.unrealized_loss_penalty_per_s * np.maximum(0.0, -un)
                         + cfg.hold_penalty_per_s)
        return r.astype(np.float32), self.done

    def trip_table(self) -> dict[str, np.ndarray]:
        """Trips with episode meta (date, stem, held, tod bucket ...)."""
        if not self.trips:
            zi, zf = np.zeros(0, np.int64), np.zeros(0)
            return {"ep": zi, "entry_j": zi, "exit_j": zi, "dir": zi, "pnl": zf,
                    "forced": np.zeros(0, bool), "hold_s": zf, "since_open": zf, "tod": zf}
        b, ej, xj, d, v, f = (np.concatenate(c) for c in zip(*self.trips, strict=True))
        ep = self.idx[b]
        so = self.data.s0[ep] + ej
        return {"ep": ep, "entry_j": ej, "exit_j": xj, "dir": d, "pnl": v, "forced": f,
                "hold_s": (xj - ej).astype(np.float64), "since_open": so.astype(np.float64),
                "tod": self.data.tod0[ep] + so}


def run_policy(
    data: BurstData,
    policy: Callable[[np.ndarray, np.ndarray, BurstBatch], np.ndarray],
    cfg: BurstConfig,
    idx: np.ndarray | None = None,
    batch: int = 8192,
) -> dict[str, Any]:
    """Run ``policy(obs, mask, batch_obj) -> actions`` on every episode; trip table."""
    idx = np.arange(len(data)) if idx is None else np.asarray(idx)
    tabs, n_inv, n_ent = [], 0, 0
    for i in range(0, len(idx), batch):
        bb = BurstBatch(data, idx[i : i + batch], cfg)
        while not bb.done:
            bb.step(policy(bb.obs(), bb.mask(), bb))
        tabs.append(bb.trip_table())
        n_inv += bb.n_invalid
        n_ent += bb.n_entries
    trips = {k: np.concatenate([t[k] for t in tabs]) for k in tabs[0]}
    return {"trips": trips, "n_invalid": n_inv, "n_entries": n_ent, "n_episodes": len(idx)}


# --------------------------------------------------------------------------- #
# Gymnasium wrapper (single episode)
# --------------------------------------------------------------------------- #
class BurstEnv(gym.Env):
    """One burst episode per ``reset`` (see module docstring)."""

    metadata = {"render_modes": []}

    def __init__(self, data: BurstData, config: BurstConfig | None = None, seed: int | None = None):
        super().__init__()
        self.data = data
        self.config = config or BurstConfig()
        self.action_space = spaces.Discrete(N_ACT)
        self.observation_space = spaces.Box(-np.inf, np.inf, shape=(OBS_DIM,), dtype=np.float32)
        self._rng = np.random.default_rng(seed)
        self._bb: BurstBatch | None = None

    def reset(self, *, seed: int | None = None, options: dict[str, Any] | None = None):
        super().reset(seed=seed)
        if seed is not None:
            self._rng = np.random.default_rng(seed)
        options = options or {}
        i = int(options["index"]) if "index" in options else int(self._rng.integers(len(self.data)))
        self._bb = BurstBatch(self.data, np.array([i]), self.config)
        return self._bb.obs()[0], {"episode": i}

    def action_mask(self) -> np.ndarray:
        assert self._bb is not None
        return self._bb.mask()[0]

    def step(self, action: int):
        assert self._bb is not None
        r, done = self._bb.step(np.array([int(action)]))
        info: dict[str, Any] = {"n_invalid": self._bb.n_invalid}
        if done:
            info["trips"] = self._bb.trip_table()
        return self._bb.obs()[0], float(r[0]), bool(done), False, info


# --------------------------------------------------------------------------- #
# Reporting helpers
# --------------------------------------------------------------------------- #
TOD_BUCKETS = ("寄り後10分", "9時台", "10時以降")


def tod_bucket(since_open: np.ndarray, tod: np.ndarray) -> np.ndarray:
    return np.where(since_open < 600, 0, np.where(tod < 10 * 3600, 1, 2))


def summarize(data: BurstData, res: dict[str, Any], mask_fn: Callable[[dict], np.ndarray] | None = None,
              sess_mask: np.ndarray | None = None, n_boot: int = 1000, seed: int = 0) -> dict[str, Any]:
    """Metrics with 95% day-bootstrap CIs.  ``mask_fn(trips)`` selects trips;
    ``sess_mask`` the scanned sessions forming the per-session denominator."""
    T = res["trips"]
    m = np.ones(len(T["pnl"]), bool) if mask_fn is None else mask_fn(T)
    sm = np.ones(len(data.sess_date), bool) if sess_mask is None else sess_mask
    days = np.unique(data.sess_date[sm])
    di = {d: i for i, d in enumerate(days)}
    nd = len(days)
    tdate = data.date[T["ep"][m].astype(np.int64)]
    ti = np.array([di.get(d, -1) for d in tdate], dtype=np.int64)
    ok = ti >= 0
    pnl, win, ti = T["pnl"][m][ok], (T["pnl"][m][ok] > 0).astype(float), ti[ok]
    sp = np.bincount(ti, pnl, nd)
    sn = np.bincount(ti, None, nd).astype(float)
    sw = np.bincount(ti, win, nd)
    ss = np.bincount(np.array([di[d] for d in data.sess_date[sm]]), None, nd).astype(float)
    rng = np.random.default_rng(seed)
    bi = rng.integers(0, nd, size=(n_boot, nd)) if nd else np.zeros((n_boot, 0), int)

    def ratio(num, den):
        if den.sum() <= 0:
            return (float("nan"),) * 3
        r = num[bi].sum(1) / np.maximum(den[bi].sum(1), 1e-12)
        return (float(num.sum() / den.sum()), float(np.percentile(r, 2.5)), float(np.percentile(r, 97.5)))

    hold = T["hold_s"][m][ok]
    return {
        "n_trips": int(len(pnl)), "n_days": nd, "n_sessions": int(ss.sum()),
        "tick_trip": ratio(sp, sn), "tick_sess": ratio(sp, ss), "trips_sess": ratio(sn, ss),
        "win": ratio(sw, sn), "hold_med": float(np.median(hold)) if len(hold) else float("nan"),
        "forced_rate": float(T["forced"][m][ok].mean()) if len(pnl) else float("nan"),
    }

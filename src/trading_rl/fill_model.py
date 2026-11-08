"""Trades-only limit-fill model for ScalpEnv ``fill_rule="queue_est"``.

Calibrated against the kabu push board (``scripts/probes/fill_calib.py`` /
``fill_rule_fit.py``, findings ``.agents/docs/research/scalp-fill-calibration.md``).
For a 100-share limit order placed ``k`` ticks inside the last print (``bid_k`` =
``last - k * tick``, ``ask_k`` = ``last + k * tick``) the model gives, at a decision
time, from prints only:

* ``p_mkt``: probability that the order is marketable on arrival (the opposite best
  is already at / through its price) -> it fills at once;
* ``q_hat``: shares queued ahead of it at its price otherwise.  The env then runs a
  virtual FIFO queue: fill once prints at exactly the order price exceed
  ``q_hat + 100`` shares, or on a print through the price.

This module is the single source of the model's features (training recomputes them
with :func:`fill_features_at`, the env with :func:`session_fill_arrays`).
"""

from __future__ import annotations

import pickle
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

US = 1_000_000
FEATURE_VERSION = 1
BASE_NAMES: tuple[str, ...] = (
    "ret1_t", "ret5_t", "ret30_t", "tickdir", "pos5", "n10", "vol10_rel",
    "log_medq", "tick_bps", "bounce_t", "tod_h",
)
ROW_NAMES: tuple[str, ...] = (
    "k", "side", "s_ret1", "s_ret5", "s_ret30", "s_tickdir", "s_pos5", "n10",
    "vol10_rel", "log_medq", "tick_bps", "bounce_t", "tod_h",
)
SIDES = (1, -1)  # index 0 = buy (bid_k), 1 = sell (ask_k)
KS = (0, 1, 2)
MEDQ_WINDOW = 64  # prints
BOUNCE_WINDOW = 50  # non-zero price changes
DEFAULT_MODEL_PATH = (
    Path(__file__).resolve().parents[2] / ".cache" / "trading_rl" / "fillcal" / "fill_model.pkl"
)


class _RangeMax:
    """Sparse table for O(1) range max / min over a fixed array."""

    def __init__(self, a: np.ndarray) -> None:
        n = len(a)
        self.mx = [a.copy()]
        self.mn = [a.copy()]
        j = 1
        while (1 << j) <= n:
            h = 1 << (j - 1)
            pm, pn = self.mx[-1], self.mn[-1]
            self.mx.append(np.maximum(pm[:-h], pm[h:]))
            self.mn.append(np.minimum(pn[:-h], pn[h:]))
            j += 1

    def query(self, lo: np.ndarray, hi: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Max and min over ``a[lo:hi]`` (hi exclusive, requires hi > lo)."""
        length = hi - lo
        j = np.floor(np.log2(np.maximum(length, 1))).astype(np.int64)
        mx = np.empty(len(lo))
        mn = np.empty(len(lo))
        for jj in np.unique(j):
            m = j == jj
            a, b = self.mx[jj], self.mn[jj]
            l0, r0 = lo[m], hi[m] - (1 << jj)
            mx[m] = np.maximum(a[l0], a[r0])
            mn[m] = np.minimum(b[l0], b[r0])
        return mx, mn


def _sliding_median(x: np.ndarray, w: int) -> np.ndarray:
    """``out[i]`` = median of ``x[max(0, i-w+1) : i+1]``."""
    if len(x) == 0:
        return x.astype(np.float64)
    pad = np.concatenate([np.full(w - 1, np.nan), x.astype(np.float64)])
    win = np.lib.stride_tricks.sliding_window_view(pad, w)
    return np.nanmedian(win, axis=1)


def fill_features_at(
    t_rel: np.ndarray,
    price: np.ndarray,
    qty: np.ndarray,
    t0: np.ndarray,
    tick: np.ndarray,
    tod_s: np.ndarray,
) -> np.ndarray:
    """Side-agnostic causal features ``(len(t0), len(BASE_NAMES))``.

    ``t_rel``/``price``/``qty``: the session prints (``t_rel`` sorted, any origin);
    ``t0``: decision times on the same clock; ``tick``: tick size at the last print
    of each decision; ``tod_s``: seconds since midnight (JST) of each decision.
    Only prints with ``t_rel <= t0`` are used.
    """
    t_rel = np.asarray(t_rel, dtype=np.int64)
    price = np.asarray(price, dtype=np.float64)
    qty = np.asarray(qty, dtype=np.float64)
    t0 = np.asarray(t0, dtype=np.int64)
    n = len(price)
    if n == 0:
        raise ValueError("no prints")
    c0 = np.searchsorted(t_rel, t0, side="right")
    il = np.maximum(c0 - 1, 0)
    last = price[il]

    def px_back(h: float) -> np.ndarray:
        c = np.searchsorted(t_rel, t0 - int(h * US), side="right")
        return price[np.maximum(c - 1, 0)]

    tk = np.where(np.asarray(tick, dtype=np.float64) > 0, tick, 1.0)
    ret1 = (last - px_back(1)) / tk
    ret5 = (last - px_back(5)) / tk
    ret30 = (last - px_back(30)) / tk

    dp = np.diff(price, prepend=price[0])
    sg = np.sign(dp)
    idx = np.where(sg != 0, np.arange(n), 0)
    np.maximum.accumulate(idx, out=idx)
    tickdir = sg[idx][il]

    i5 = np.searchsorted(t_rel, t0 - 5 * US, side="right")
    lo_i = np.minimum(i5, il)  # empty window -> just the last print
    rm = _RangeMax(price)
    hi5, lo5 = rm.query(lo_i, il + 1)
    empty = i5 > il
    hi5 = np.where(empty, last, hi5)
    lo5 = np.where(empty, last, lo5)
    pos5 = (last - lo5) / np.maximum(tk, hi5 - lo5)

    i10 = np.searchsorted(t_rel, t0 - 10 * US, side="right")
    n10 = (c0 - i10).astype(np.float64)
    cq = np.concatenate([[0.0], np.cumsum(qty)])
    vol10 = cq[c0] - cq[i10]
    medq = _sliding_median(qty, MEDQ_WINDOW)[il]
    vol10_rel = vol10 / np.maximum(medq, 1.0)

    nz_idx = np.nonzero(dp)[0]
    nz_abs = np.abs(dp[nz_idx])
    if len(nz_idx):
        med_nz = _sliding_median(nz_abs, BOUNCE_WINDOW)
        m = np.searchsorted(nz_idx, il, side="right")  # non-zero changes up to il
        bounce = np.where(m > 0, med_nz[np.maximum(m - 1, 0)], 0.0) / tk
    else:
        bounce = np.zeros(len(t0))
    tick_bps = tk / last * 1e4
    tod_h = np.asarray(tod_s, dtype=np.float64) / 3600.0
    F = np.column_stack(
        [ret1, ret5, ret30, tickdir, pos5, n10, vol10_rel, np.log1p(medq), tick_bps,
         bounce, tod_h]
    )
    return np.nan_to_num(F, nan=0.0, posinf=0.0, neginf=0.0)


def expand_rows(F: np.ndarray, side: np.ndarray, k: np.ndarray) -> np.ndarray:
    """Per-order model rows from base features (side +1 buy / -1 sell, k ticks)."""
    s = np.asarray(side, dtype=np.float64)
    return np.column_stack(
        [
            np.asarray(k, dtype=np.float64), s,
            s * F[:, 0], s * F[:, 1], s * F[:, 2], s * F[:, 3],
            np.where(s > 0, F[:, 4], 1.0 - F[:, 4]),
            F[:, 5], F[:, 6], F[:, 7], F[:, 8], F[:, 9], F[:, 10],
        ]
    )


@dataclass
class FillModel:
    clf: Any  # P(marketable)
    reg: Any  # log1p(queue ahead, shares)
    shrink: float = 1.0
    meta: dict[str, Any] | None = None

    def predict_rows(self, R: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        p = self.clf.predict_proba(R)[:, 1]
        q = np.expm1(self.reg.predict(R)).clip(0.0, None) * self.shrink
        return p.astype(np.float32), q.astype(np.float32)

    def save(self, path: Path | str = DEFAULT_MODEL_PATH) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as fh:
            pickle.dump(
                {"version": FEATURE_VERSION, "clf": self.clf, "reg": self.reg,
                 "shrink": self.shrink, "meta": self.meta or {}},
                fh,
            )

    @classmethod
    def load(cls, path: Path | str = DEFAULT_MODEL_PATH) -> FillModel:
        with open(path, "rb") as fh:
            z = pickle.load(fh)  # noqa: S301 - local artefact written by save()
        if z.get("version") != FEATURE_VERSION:
            raise ValueError(f"fill model version {z.get('version')} != {FEATURE_VERSION}")
        return cls(z["clf"], z["reg"], float(z["shrink"]), z.get("meta"))


def session_fill_arrays(
    model: FillModel,
    t_rel: np.ndarray,
    price: np.ndarray,
    qty: np.ndarray,
    dec_s: np.ndarray,
    tick_at_dec: np.ndarray,
    tod_open_s: float,
) -> tuple[np.ndarray, np.ndarray]:
    """``(p_mkt, q_hat)`` of shape ``(len(dec_s), 2, 3)`` [side (buy, sell), k]."""
    dec_s = np.asarray(dec_s, dtype=np.int64)
    F = fill_features_at(t_rel, price, qty, dec_s * US, tick_at_dec, tod_open_s + dec_s)
    n = len(dec_s)
    rows, sides, ks = [], [], []
    for si, s in enumerate(SIDES):
        for k in KS:
            rows.append(F)
            sides.append(np.full(n, s))
            ks.append(np.full(n, k))
    R = expand_rows(np.concatenate(rows), np.concatenate(sides), np.concatenate(ks))
    p, q = model.predict_rows(R)
    p = p.reshape(len(SIDES), len(KS), n).transpose(2, 0, 1)
    q = q.reshape(len(SIDES), len(KS), n).transpose(2, 0, 1)
    return np.ascontiguousarray(p), np.ascontiguousarray(q)

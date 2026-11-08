"""Baseline policies for the burst practice ground (obs-based, vectorised).

RulePolicy: within the first ``ew`` seconds of the episode, at the first decision whose
detector log-odds >= ``smin`` and whose ``m``-second move (fill micro ``ret{m}_t``,
ticks) is non-zero, enter at market in the direction ``mode * sign(move)``
(mode +1 = follow, -1 = fade).  Exit at market when the unrealized PnL (last print vs
entry fill, reference ticks) >= ``tp`` or <= ``-sl`` or after ``T`` seconds.  One trade
per episode (the episode end flattens anyway).
"""

from __future__ import annotations

import sys

import numpy as np

sys.path.insert(0, "src")
from trading_rl import burst_env as be  # noqa: E402

I_DET, I_POS, I_UN, I_HELD = be.N_MARKET, be.N_MARKET + 1, be.N_MARKET + 2, be.N_MARKET + 3
INF = 1e9


class RulePolicy:
    def __init__(self, m: int = 5, mode: int = 1, tp: float = INF, sl: float = INF, T: int = 60,
                 smin: float = -INF, ew: int = 5, random_dir: bool = False, seed: int = 0):
        self.m, self.mode, self.tp, self.sl, self.T = int(m), int(mode), float(tp), float(sl), int(T)
        self.smin, self.ew, self.random_dir = float(smin), int(ew), bool(random_dir)
        self.col = be.OBS_NAMES.index(f"m_ret{self.m}_t")
        self.rng = np.random.default_rng(seed)

    def __call__(self, obs: np.ndarray, mask: np.ndarray, bb: be.BurstBatch) -> np.ndarray:
        if bb.j == 0 or not hasattr(bb, "_rule_done"):
            bb._rule_done = np.zeros(bb.B, bool)  # type: ignore[attr-defined]
        done = bb._rule_done  # type: ignore[attr-defined]
        a = np.zeros(bb.B, np.int64)
        pos = obs[:, I_POS]
        mv = obs[:, self.col]
        if self.random_dir:
            sgn = self.rng.choice([-1.0, 1.0], bb.B) * (mv != 0)
        else:
            sgn = np.sign(mv) * self.mode
        enter = (pos == 0) & ~done & (bb.j < self.ew) & (obs[:, I_DET] >= self.smin) & (sgn != 0)
        a[enter & (sgn > 0)] = be.A_BUY
        a[enter & (sgn < 0)] = be.A_SELL
        done |= enter
        un, held = obs[:, I_UN], obs[:, I_HELD] * 60.0
        ex = (pos != 0) & ((un >= self.tp - 1e-9) | (un <= -self.sl + 1e-9) | (held >= self.T - 1e-3))
        a[ex & (pos > 0)] = be.A_SELL
        a[ex & (pos < 0)] = be.A_BUY
        return a


class UniformRandom:
    def __init__(self, seed: int = 0):
        self.rng = np.random.default_rng(seed)

    def __call__(self, obs, mask, bb):
        u = self.rng.random(mask.shape) * mask
        return u.argmax(1)


def nothing(obs, mask, bb):
    return np.zeros(len(obs), np.int64)


def fill_prices(data: be.BurstData, cfg: be.BurstConfig, idx: np.ndarray):
    """(buy_px, sell_px) per decision exactly as BurstBatch computes them."""
    bb = be.BurstBatch(data, idx, cfg)
    return bb.buy_px, bb.sell_px


def rule_trips_fast(data: be.BurstData, cfg: be.BurstConfig, m: int, mode: int, smin: float,
                    exits: list[tuple[float, float, int]], ew: int = 5) -> list[dict[str, np.ndarray]]:
    """Trip tables of RulePolicy(m, mode, tp, sl, T, smin, ew) for every (tp, sl, T) in
    ``exits`` (same semantics, vectorised over episodes; checked in the tests)."""
    col = be.OBS_NAMES.index(f"m_ret{m}_t")
    E = data.ep_s
    mv = data.X[:, :ew, col]
    ok = (be.logit(data.score[:, :ew]) >= smin) & (mv != 0)
    ep = np.flatnonzero(ok.any(1))
    je = ok[ep].argmax(1)
    d = (np.sign(mv[ep, je]) * mode).astype(np.int8)
    buy, sell = fill_prices(data, cfg, ep)
    r = np.arange(len(ep))
    entry = np.where(d > 0, buy[r, je], sell[r, je])
    ref = data.ref_tick[ep]
    last = data.last[ep][:, :E]
    un = np.clip(d[:, None] * (last - entry[:, None]) / ref[:, None], -20, 20).astype(np.float32)
    jj = np.arange(E)[None, :]
    after = jj > je[:, None]
    held = jj - je[:, None]
    exit_buy = np.where(d[:, None] > 0, sell, buy)  # price of the closing order at each decision
    so = data.s0[ep] + je
    outs = []
    for tp, sl, T in exits:
        c = after & ((un >= tp - 1e-9) | (un <= -sl + 1e-9) | (held >= T))
        has = c.any(1)
        xj = np.where(has, c.argmax(1), E)
        px = exit_buy[r, xj]
        pnl = d * (px - entry) / ref
        outs.append({"ep": ep, "entry_j": je, "exit_j": xj, "dir": d, "pnl": pnl, "forced": ~has,
                     "hold_s": (xj - je).astype(np.float64), "since_open": so.astype(np.float64),
                     "tod": data.tod0[ep] + so})
    return outs

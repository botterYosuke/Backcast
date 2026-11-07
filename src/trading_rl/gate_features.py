"""1-second decision-grid features and forward targets for the P2 predictability gate.

For one cached opening session (ticks in ``[T_open, T_open + 600 s]``) the module
builds, for every grid second ``s`` (``T_open + s``), a symbol-agnostic feature
vector computed **only from ticks with timestamp <= T_open + s** plus universe
columns known before the open (``prev_close``, ``avg_volume_20d``,
``is_topix100``), and the forward targets for each horizon ``h``.

Conventions
    * Price at a grid time = last trade at or before it.
    * Windows ``(s - w, s]`` are clamped at ``T_open`` (nothing before the open
      exists in the cache), so early rows see shorter windows; ``elapsed`` tells
      the model how much history is real.
    * Tick-rule side: sign of the price change vs the previous trade; an
      unchanged price inherits the previous sign (0 until the first change).
    * Features are clipped to sane ranges and NaN/inf-sanitised (-> 0).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .data import OpeningSessionData
from .universe import tick_sizes

HORIZONS: tuple[int, ...] = (30, 60, 300)
WINDOW_S: int = 600
START_S: int = 10  # owner rule: no entries before T_open + 10 s

RET_WINDOWS: tuple[int, ...] = (1, 5, 15, 30, 60)
IMB_WINDOWS: tuple[int, ...] = (5, 30, 60)
CNT_WINDOWS: tuple[int, ...] = (5, 30)
RV_WINDOWS: tuple[int, ...] = (30, 60)

FEATURE_NAMES: list[str] = (
    [f"ret_bps_{k}s" for k in RET_WINDOWS]
    + [f"ret_tk_{k}s" for k in RET_WINDOWS]
    + [
        "ret_open_bps",
        "gap_bps",
        "range_pos",
        "vwap_dev_bps",
    ]
    + [f"imb_{w}s" for w in IMB_WINDOWS]
    + [f"log_ntrades_{w}s" for w in CNT_WINDOWS]
    + [f"log_vol_ratio_{w}s" for w in CNT_WINDOWS]
    + [f"rv_bps_{w}s" for w in RV_WINDOWS]
    + [
        "elapsed_s",
        "log_open_vol_ratio",
        "log_time_since_trade",
        "is_topix100",
        "log_price_ticks",
    ]
)
N_FEATURES = len(FEATURE_NAMES)

_CLIP_DEFAULT_BPS = 500.0
_CLIP_TICKS = 200.0
_CLIP_GAP_BPS = 2000.0
_VOL_EPS = 1e-6


def default_grid(horizons: tuple[int, ...] = HORIZONS) -> np.ndarray:
    """Grid seconds ``START_S .. WINDOW_S - min(horizons)`` (561 rows by default)."""
    return np.arange(START_S, WINDOW_S - min(horizons) + 1, dtype=np.int64)


@dataclass(frozen=True)
class SessionGrid:
    """Features and targets of one session on the 1 s decision grid."""

    s: np.ndarray  # (n,) int64 grid second since T_open
    X: np.ndarray  # (n, N_FEATURES) float32
    price: np.ndarray  # (n,) last-trade price at the grid time
    tick: np.ndarray  # (n,) tick size at that price
    fwd_price: np.ndarray  # (n, H) price at s + h (NaN if s + h > WINDOW_S)
    fwd_bps: np.ndarray  # (n, H) forward return in bps (NaN if invalid)
    fwd_ticks: np.ndarray  # (n, H) forward return in tick units (NaN if invalid)
    fwd_sign: np.ndarray  # (n, H) int8 sign of the forward return (0 = tie/invalid)
    valid: np.ndarray  # (n, H) bool, s + h <= WINDOW_S
    horizons: tuple[int, ...]


def _window_sum(cum: np.ndarray, cnt: np.ndarray, s: np.ndarray, w: int) -> np.ndarray:
    """Sum over ticks in ``(s - w, s]`` using prefix sums ``cum`` (leading 0)."""
    lo = cnt[np.maximum(s - w, 0)]
    return cum[cnt[s]] - cum[lo]


def compute_session_grid(
    session: OpeningSessionData,
    *,
    prev_close: float,
    avg_volume_20d: float,
    is_topix100: bool,
    horizons: tuple[int, ...] = HORIZONS,
    s_grid: np.ndarray | None = None,
) -> SessionGrid:
    """Causal features at every grid second plus forward targets."""
    s = default_grid(horizons) if s_grid is None else np.asarray(s_grid, dtype=np.int64)
    t_rel = np.asarray(session.epoch_us, dtype=np.int64) - int(session.t_open_us)
    price = np.asarray(session.price, dtype=np.float64)
    qty = np.asarray(session.qty, dtype=np.float64)
    n_ticks = len(price)
    if n_ticks == 0:
        raise ValueError("empty session")

    secs = np.arange(0, WINDOW_S + 1, dtype=np.int64)
    cnt = np.searchsorted(t_rel, secs * 1_000_000, side="right").astype(np.int64)
    last = np.maximum(cnt - 1, 0)
    P = price[last]  # last trade price at each second (opening print before any trade)

    # tick-rule side with carry-forward
    dp = np.diff(price, prepend=price[0])
    sg = np.sign(dp)
    idx = np.where(sg != 0, np.arange(n_ticks), 0)
    np.maximum.accumulate(idx, out=idx)
    side = sg[idx]

    zero = np.zeros(1)
    cum_q = np.concatenate([zero, np.cumsum(qty)])
    cum_sv = np.concatenate([zero, np.cumsum(side * qty)])
    cum_pq = np.concatenate([zero, np.cumsum(price * qty)])
    cum_n = np.arange(n_ticks + 1, dtype=np.float64)
    run_hi = np.maximum.accumulate(price)
    run_lo = np.minimum.accumulate(price)

    flag = np.full(len(s), bool(is_topix100))
    p_s = P[s]
    tk = tick_sizes(p_s, flag)

    cols: list[np.ndarray] = []
    for k in RET_WINDOWS:
        p_k = P[np.maximum(s - k, 0)]
        cols.append(np.clip((p_s / p_k - 1.0) * 1e4, -_CLIP_DEFAULT_BPS, _CLIP_DEFAULT_BPS))
    for k in RET_WINDOWS:
        p_k = P[np.maximum(s - k, 0)]
        cols.append(np.clip((p_s - p_k) / tk, -_CLIP_TICKS, _CLIP_TICKS))

    p_open = float(session.p_open)
    cols.append(np.clip((p_s / p_open - 1.0) * 1e4, -2 * _CLIP_DEFAULT_BPS, 2 * _CLIP_DEFAULT_BPS))
    gap = (p_open / prev_close - 1.0) * 1e4 if prev_close and prev_close > 0 else 0.0
    cols.append(np.full(len(s), np.clip(gap, -_CLIP_GAP_BPS, _CLIP_GAP_BPS)))

    hi = run_hi[last[s]]
    lo = run_lo[last[s]]
    rng_ = hi - lo
    cols.append(np.where(rng_ > 0, (p_s - lo) / np.where(rng_ > 0, rng_, 1.0), 0.5))

    q_cum = cum_q[cnt[s]]
    vwap = np.where(q_cum > 0, cum_pq[cnt[s]] / np.where(q_cum > 0, q_cum, 1.0), p_s)
    cols.append(np.clip((p_s / vwap - 1.0) * 1e4, -_CLIP_DEFAULT_BPS, _CLIP_DEFAULT_BPS))

    for w in IMB_WINDOWS:
        sv = _window_sum(cum_sv, cnt, s, w)
        v = _window_sum(cum_q, cnt, s, w)
        cols.append(np.where(v > 0, sv / np.where(v > 0, v, 1.0), 0.0))

    avg_v = max(float(avg_volume_20d), 1.0) if np.isfinite(avg_volume_20d) else 1.0
    for w in CNT_WINDOWS:
        cols.append(np.log1p(_window_sum(cum_n, cnt, s, w)))
    for w in CNT_WINDOWS:
        cols.append(np.log10(_window_sum(cum_q, cnt, s, w) / avg_v + _VOL_EPS))

    r1 = np.zeros(len(P))
    r1[1:] = np.log(P[1:] / P[:-1])
    cs = np.cumsum(r1 * r1)
    for w in RV_WINDOWS:
        cols.append(
            np.clip(
                np.sqrt(np.maximum(cs[s] - cs[np.maximum(s - w, 0)], 0.0)) * 1e4,
                0.0,
                _CLIP_DEFAULT_BPS,
            )
        )

    cols.append(s.astype(np.float64))
    cols.append(np.full(len(s), np.log10(float(session.v_open) / avg_v + _VOL_EPS)))
    since = s - t_rel[last[s]] / 1e6
    cols.append(np.log1p(np.maximum(since, 0.0)))
    cols.append(np.full(len(s), 1.0 if is_topix100 else 0.0))
    cols.append(np.log(p_s / tk))

    X = np.stack(cols, axis=1)
    X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32)

    h_arr = np.asarray(horizons, dtype=np.int64)
    s_fwd = s[:, None] + h_arr[None, :]
    valid = s_fwd <= WINDOW_S
    fwd_price = np.where(valid, P[np.minimum(s_fwd, WINDOW_S)], np.nan)
    fwd_bps = (fwd_price / p_s[:, None] - 1.0) * 1e4
    fwd_ticks = (fwd_price - p_s[:, None]) / tk[:, None]
    fwd_sign = np.where(valid, np.sign(fwd_price - p_s[:, None]), 0).astype(np.int8)
    return SessionGrid(
        s=s,
        X=X,
        price=p_s,
        tick=tk,
        fwd_price=fwd_price,
        fwd_bps=fwd_bps,
        fwd_ticks=fwd_ticks,
        fwd_sign=fwd_sign,
        valid=valid,
        horizons=tuple(horizons),
    )

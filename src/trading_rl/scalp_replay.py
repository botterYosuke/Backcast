"""Deterministic replay of the limit-order scalping policy (ScalpEnv) for the web UI.

The tick-replay app shows a trained checkpoint's fills on its charts.  The
original market-order models are replayed by :func:`evaluator.simulate_ai_trades`;
this module does the same for checkpoints trained on :class:`ScalpEnv`:

* :func:`checkpoint_kind` tells the two apart from checkpoint metadata.
* :func:`lookup_session_info` supplies the per-session inputs ScalpEnv needs
  (``prev_close``, ``avg_volume_20d``, ``is_topix100``) from the universe cache or,
  point-in-time, from the daily panel.
* :func:`simulate_scalp_trades` drives the *unmodified* ScalpEnv with the argmax
  policy and returns fills (same JSON shape as the old replay plus ``order`` /
  ``forced``), a summary and the limit-order lifecycle events.  ScalpEnv is the single
  source of fills; the subclass here only records order events.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import duckdb

from .data import OpeningSessionData
from .policy import ActorCritic, load_policy_from_checkpoint
from .scalp_env import (
    G_CANCEL,
    G_FLATTEN,
    G_MBUY,
    G_MSELL,
    US,
    WINDOW_S,
    ScalpConfig,
    ScalpEnv,
    build_scalp_session,
    is_heldout,
    obs_dim,
)
from .scalp_features import ContextStore, default_store, parse_feature_spec
from .universe import DEFAULT_CACHE_DIR, MIN_CALENDAR_BREADTH, PANEL_FILE, UNIVERSE_FILE

# Training / validation / test split of train_scalp.py (inclusive date bounds).
TRAIN_START, TRAIN_END = "2024-01-04", "2025-06-30"
VAL_START, VAL_END = "2025-07-01", "2025-12-31"
TEST_START = "2026-01-01"

KIND_SCALP = "scalp"
KIND_MARKET = "market"


# --------------------------------------------------------------------------- #
# Checkpoint metadata
# --------------------------------------------------------------------------- #
_CKPT_CACHE: dict[tuple[str, int], tuple[ActorCritic, dict[str, Any]]] = {}


def load_checkpoint_cached(
    model_path: Path | str,
) -> tuple[ActorCritic, dict[str, Any]]:
    """``load_policy_from_checkpoint`` memoised on (path, mtime)."""
    path = Path(model_path)
    key = (str(path.resolve()), path.stat().st_mtime_ns)
    hit = _CKPT_CACHE.get(key)
    if hit is None:
        for stale in [k for k in _CKPT_CACHE if k[0] == key[0]]:
            del _CKPT_CACHE[stale]
        hit = load_policy_from_checkpoint(path)
        _CKPT_CACHE[key] = hit
    return hit


def checkpoint_kind(checkpoint: dict[str, Any]) -> str:
    """``"scalp"`` for ScalpEnv checkpoints, ``"market"`` for the legacy env.

    ScalpEnv checkpoints carry ``fill_rule`` / ``action_names`` in ``env_config``;
    legacy ones have neither (or no ``env_config`` at all).
    """
    cfg = checkpoint.get("env_config") or {}
    if "fill_rule" in cfg or "action_names" in cfg:
        return KIND_SCALP
    return KIND_MARKET


def scalp_config_from_checkpoint(checkpoint: dict[str, Any]) -> ScalpConfig:
    cfg = checkpoint.get("env_config") or {}
    if int(round(float(cfg.get("window_seconds", WINDOW_S)))) != WINDOW_S:
        raise ValueError(f"unsupported window_seconds {cfg.get('window_seconds')!r}")
    # Old checkpoints have no ``order_mode`` (= the 9-action limit set).  Reward
    # shaping (training only) is deliberately not restored: replay shows raw fills.
    return ScalpConfig(
        latency_ms=int(cfg.get("latency_ms", 50)),
        fill_rule=str(cfg.get("fill_rule", "trade_through")),
        commission_yen=float(cfg.get("commission_yen_per_unit_per_fill", 0.0)),
        order_mode=str(cfg.get("order_mode", "limit")),
        # Old checkpoints have no ``features`` (= the 28-column base observation).
        features=parse_feature_spec(cfg.get("features", "base")).canonical,
    )


def uses_action_mask(checkpoint: dict[str, Any]) -> bool:
    """Was the checkpoint trained with legal-action masks (``action_masking``)?"""
    return bool((checkpoint.get("env_config") or {}).get("action_masking", False))


def period_of(date: str) -> str:
    """Which part of the model's lifetime ``date`` falls into.

    ``train`` / ``val`` (checkpoint selection) / ``test`` (never touched);
    ``pre_train`` for dates before the training window (also unseen).
    """
    if date < TRAIN_START:
        return "pre_train"
    if date <= TRAIN_END:
        return "train"
    if date <= VAL_END:
        return "val"
    return "test"


# --------------------------------------------------------------------------- #
# Per-session inputs (prev_close, avg_volume_20d, is_topix100)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ScalpSessionInfo:
    prev_close: float
    avg_volume_20d: float
    is_topix100: bool
    in_universe: bool
    source: str  # "universe" | "daily_panel"
    topix100_source: str  # "universe" | "universe_nearest" | "default_false"


def _sql_str(value: object) -> str:
    return "'" + str(value).replace("'", "''") + "'"


@lru_cache(maxsize=2)
def _calendar(panel: str, mtime_ns: int) -> list[str]:
    """Trading calendar of the panel (dates with enough breadth), ascending."""
    con = duckdb.connect(":memory:")
    try:
        rows = con.execute(
            f"SELECT CAST(d AS VARCHAR) FROM read_parquet({_sql_str(Path(panel).as_posix())}) "
            f"WHERE close IS NOT NULL GROUP BY d HAVING count(*) >= {MIN_CALENDAR_BREADTH} "
            f"ORDER BY d"
        ).fetchall()
    finally:
        con.close()
    return [r[0] for r in rows]


def lookup_session_info(
    stem: str, date: str, cache_dir: Path | str = DEFAULT_CACHE_DIR
) -> ScalpSessionInfo | None:
    """ScalpEnv inputs for ``(stem, date)`` or ``None`` when they cannot be derived.

    Order of preference:

    1. the point-in-time universe row of that date (exactly what training used);
    2. the daily panel, point-in-time: ``prev_close`` = the stem's close on the last
       calendar day strictly before ``date``; ``avg_volume_20d`` = mean volume of the
       stem over the 20 calendar days ending there (same definition as the universe).
       ``is_topix100`` then comes from the stem's nearest universe row (the TOPIX100
       tick table barely changes), else ``False``.
    """
    cache_dir = Path(cache_dir)
    stem_l = stem.lower()
    uni = cache_dir / UNIVERSE_FILE
    panel = cache_dir / PANEL_FILE

    con = duckdb.connect(":memory:")
    try:
        if uni.is_file():
            row = con.execute(
                f"SELECT prev_close, avg_volume_20d, is_topix100 FROM "
                f"read_parquet({_sql_str(uni.as_posix())}) "
                f"WHERE date = CAST(? AS DATE) AND lower(stem) = ? LIMIT 1",
                [date, stem_l],
            ).fetchone()
            if row is not None and row[0] and row[1] is not None:
                return ScalpSessionInfo(
                    float(row[0]), float(row[1] or 0.0), bool(row[2]), True,
                    "universe", "universe",
                )
        if not panel.is_file():
            return None

        cal = _calendar(str(panel), panel.stat().st_mtime_ns)
        j = bisect.bisect_left(cal, date)  # first calendar day >= date
        if j == 0:
            return None
        prev_d = cal[j - 1]
        window_start = cal[max(0, j - 20)]
        rows = con.execute(
            f"SELECT CAST(d AS VARCHAR), close, volume FROM "
            f"read_parquet({_sql_str(panel.as_posix())}) "
            f"WHERE lower(stem) = ? AND d >= ? AND d <= ? ORDER BY d",
            [stem_l, window_start, prev_d],
        ).fetchall()
        prev = next((r for r in rows if r[0] == prev_d), None)
        if prev is None or not prev[1]:
            return None  # no close on P(D): halted / not listed -> do not guess
        vols = [float(r[2]) for r in rows if r[2] is not None]
        avg_vol = sum(vols) / len(vols) if vols else 0.0

        t100, t100_src = False, "default_false"
        if uni.is_file():
            near = con.execute(
                f"SELECT is_topix100 FROM read_parquet({_sql_str(uni.as_posix())}) "
                f"WHERE lower(stem) = ? "
                f"ORDER BY abs(date_diff('day', date, CAST(? AS DATE))) LIMIT 1",
                [stem_l, date],
            ).fetchone()
            if near is not None:
                t100, t100_src = bool(near[0]), "universe_nearest"
        return ScalpSessionInfo(
            float(prev[1]), avg_vol, t100, False, "daily_panel", t100_src
        )
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# Simulation
# --------------------------------------------------------------------------- #
class _RecordingScalpEnv(ScalpEnv):
    """ScalpEnv that additionally records the lifecycle of limit orders.

    It only observes: fill / order logic is entirely the parent's.  Times are
    microseconds since T_open (callers add ``t_open_us``).
    """

    def reset(self, *, seed=None, options=None):  # type: ignore[override]
        out = super().reset(seed=seed, options=options)
        self.events: list[dict[str, Any]] = []
        self.exit_pnl: dict[int, float] = {}  # fill index -> yen per 100 shares
        self._live: dict[int, tuple[int, float]] = {}  # side -> (order id, price)
        self._next_id = 0
        return out

    def _ev(self, t_us: int, side: int, price: float, action: str, oid: int) -> None:
        self.events.append(
            {"t": int(t_us), "side": side, "price": float(price), "action": action, "id": oid}
        )

    def _apply_action(self, a: int, t_act: int = 0) -> None:  # ``a``: global action id
        if a == G_CANCEL:
            t = t_act if t_act else self._d * US + self._lat_us
            for side in (+1, -1):
                live = self._live.pop(side, None)
                if live is not None:
                    self._ev(t, side, live[1], "cancel", live[0])
        super()._apply_action(a, t_act)

    def _place(self, side: int, k: int, px: float, t_act: int) -> bool:
        # Every limit placement passes through here (also the queue_est marketable
        # draw that fills inside the parent's ``_place``), so the replaced order's
        # cancel and the new order's place are recorded before any fill.
        t = t_act if t_act else self._d * US + self._lat_us
        live = self._live.pop(side, None)
        if live is not None:
            # sequential_cancel: the old order went away one latency before the new one
            t_cancel = t - self._lat_us if self._seq else t
            self._ev(t_cancel, side, live[1], "cancel", live[0])
        oid = self._next_id
        self._next_id += 1
        self._live[side] = (oid, float(px))
        self._ev(t, side, float(px), "place", oid)
        return super()._place(side, k, px, t_act)

    def _fill(self, side: int, price: float, t_us: int, order: str, forced: bool) -> None:
        n_trips = len(self._trips)
        super()._fill(side, price, t_us, order, forced)
        if len(self._trips) > n_trips:
            self.exit_pnl[len(self._fills) - 1] = float(self._trips[-1]["pnl_yen100"])
        if order == "limit":
            live = self._live.pop(side, None)
            oid = live[0] if live is not None else -1
            self._ev(t_us, side, price, "fill", oid)

    def step(self, action: int):  # type: ignore[override]
        g, d = self._ids[int(action)], self._d
        out = super().step(action)
        # orders dropped without an explicit cancel: any market order / end of window
        for side in list(self._live):
            if (self._bid if side > 0 else self._ask) is None:
                oid, px = self._live.pop(side)
                t = (
                    d * US + self._lat_us
                    if g in (G_FLATTEN, G_MBUY, G_MSELL)
                    else self._W * US
                )
                self._ev(t, side, px, "cancel", oid)
        return out


def _side(s: int) -> str:
    return "buy" if s > 0 else "sell"


def simulate_scalp_trades(
    opening_session: OpeningSessionData,
    model_path: Path | str,
    *,
    prev_close: float,
    avg_volume_20d: float,
    is_topix100: bool,
    qty: int = 100,
    store: ContextStore | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    """Run the checkpoint's argmax policy on ScalpEnv for one opening session.

    Returns ``(trades, summary, orders)``.  ``trades`` items:
    ``{us, side, price, qty, kind: entry|exit, pnl, order: limit|market, forced}``
    with ``us`` absolute epoch microseconds and ``pnl`` yen for ``qty`` shares on
    exits (0 on entries).  ``orders`` items: ``{us, side, price, action:
    place|cancel|fill, id}`` (``place``/``cancel`` carry the effective time, i.e.
    decision time + latency; ``fill`` is limit fills only).

    Checkpoints trained with extra feature groups (``env_config["features"]``) get the
    same daily / cross-sectional context at replay time: it is read from ``store``
    (default: the process-wide store of the default cache dir, i.e. that date's
    universe sessions from the tick cache), so no extra caller input is needed.
    """
    policy, checkpoint = load_checkpoint_cached(model_path)
    policy.eval()
    config = scalp_config_from_checkpoint(checkpoint)
    ck_dim = getattr(policy, "obs_dim", None)
    if ck_dim is not None and ck_dim != obs_dim(config.features):
        raise ValueError(
            f"checkpoint obs_dim {ck_dim} does not match features "
            f"{config.features!r} ({obs_dim(config.features)})"
        )
    spec = parse_feature_spec(config.features)
    sess = build_scalp_session(
        opening_session,
        prev_close=prev_close,
        avg_volume_20d=avg_volume_20d,
        is_topix100=is_topix100,
        features=spec.canonical,
        store=(store or default_store()) if spec.groups else None,
    )
    env = _RecordingScalpEnv([sess], config)
    obs, _ = env.reset(options={"index": 0})
    done = False
    masked = uses_action_mask(checkpoint)
    while not done:
        if masked:
            a = policy.predict(obs, deterministic=True, mask=env.action_mask())
        else:
            a = policy.predict(obs, deterministic=True)
        obs, _r, terminated, truncated, _info = env.step(a)
        done = terminated or truncated
    return format_replay(env, sess, qty)


def format_replay(
    env: _RecordingScalpEnv, sess: Any, qty: int = 100
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    """``(trades, summary, orders)`` of a finished :class:`_RecordingScalpEnv` episode."""
    t_open = int(sess.t_open_us)
    scale = qty / 100.0
    trades: list[dict[str, Any]] = []
    for i, f in enumerate(env.fills):
        pnl = env.exit_pnl.get(i, 0.0) * scale
        trades.append(
            {
                "us": int(f["abs_us"]),
                "side": _side(f["side"]),
                "price": float(round(f["price"], 4)),
                "qty": qty,
                "kind": f["kind"],
                "pnl": float(round(pnl, 2)),
                "order": f["order"],
                "forced": bool(f["forced"]),
            }
        )

    exits = [t for t in trades if t["kind"] == "exit"]
    wins = [t for t in exits if t["pnl"] > 0]
    summary = {
        "totalPnl": float(round(sum(t["pnl"] for t in exits), 2)),
        "tradeCount": len(exits),
        "winRate": float(round(len(wins) / len(exits), 4)) if exits else 0.0,
        "limitExits": sum(1 for t in exits if t["order"] == "limit"),
        "forcedExits": sum(1 for t in exits if t["forced"]),
        "marketExits": sum(1 for t in exits if t["order"] == "market" and not t["forced"]),
    }
    orders = [
        {
            "us": t_open + e["t"],
            "side": _side(e["side"]),
            "price": float(round(e["price"], 4)),
            "action": e["action"],
            "id": e["id"],
        }
        for e in sorted(env.events, key=lambda e: e["t"])  # stable: keeps logical order
    ]
    return trades, summary, orders


__all__ = [
    "KIND_MARKET",
    "KIND_SCALP",
    "ScalpSessionInfo",
    "checkpoint_kind",
    "format_replay",
    "is_heldout",
    "load_checkpoint_cached",
    "lookup_session_info",
    "period_of",
    "scalp_config_from_checkpoint",
    "simulate_scalp_trades",
    "uses_action_mask",
]

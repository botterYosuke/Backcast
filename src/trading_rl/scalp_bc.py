"""Behaviour-cloning warm start for the scalping PPO (E4).

PPO from scratch collapsed to "enter once, hold to the forced close".  A warm start
copies a sensible behaviour first so exploration starts from active trading:

* ``rule``: actions of the tuned :class:`~trading_rl.scalp_baselines.RuleScalper`
  on training sessions (valid in the chosen ``order_mode`` by construction).
* ``csv:<path>``: the owner's real trades, mapped onto the env's action set.

Trade CSV format (header required, UTF-8, one row per fill)::

    date,stem,us,side,price,qty,kind[,order]
    2026-03-02,7203,1772000000000000,buy,3105.0,100,entry
    2026-03-02,7203,1772000030000000,sell,3106.0,100,exit,limit

* ``date``  session date ``YYYY-MM-DD``; ``stem`` the symbol (case-insensitive).
* ``us``    microseconds: absolute epoch (as in the replay JSON) when >= 1e13, else
  microseconds since the session's T_open.
* ``side``  ``buy``/``sell`` (also ``b``/``s``/``1``/``-1``).  ``qty`` is read but
  only 100-share units are modelled.
* ``kind``  ``entry`` or ``exit``.
* ``order`` (optional) ``limit`` or ``market``; when absent a fill within
  1e-6 yen of ``last +/- slippage`` is a market order, anything else a limit order.

Mapping (a stub, documented approximations): a fill at time ``t`` becomes the
action of the decision second ``floor((t - latency) / 1 s)`` (clipped to the
trading window).  The unknown order-placement time of a limit fill is assumed to be
that same second; every other second is labelled ``keep``.  Limit fills map to
``bid_k``/``ask_k`` with ``k = round(|last - price| / tick)`` clipped to 0..2;
market fills to ``market_buy`` / ``market_sell`` / ``market_flatten`` (modes
without a limit menu map every fill to the market actions, modes without market
entries map a market entry to ``bid_k0`` / ``ask_k0``).  Two fills in one second:
the first wins.  The env is then replayed with the labelled actions to obtain the
observation / legal-action mask of each step; steps whose label is illegal in that
state are dropped.
"""

from __future__ import annotations

import csv
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import torch

from .policy import ActorCritic, apply_action_mask
from .scalp_env import (
    ACTION_INDEX,
    G_ASK0,
    G_BID0,
    G_FLATTEN,
    G_KEEP,
    G_MBUY,
    G_MSELL,
    START_S,
    US,
    WINDOW_S,
    ScalpConfig,
    ScalpEnv,
    ScalpSession,
    n_actions,
    obs_dim,
)

CSV_COLUMNS: tuple[str, ...] = ("date", "stem", "us", "side", "price", "qty", "kind")
ABSOLUTE_US_MIN = 10**13


# --------------------------------------------------------------------------- #
# Dataset container
# --------------------------------------------------------------------------- #
@dataclass
class BCDataset:
    obs: np.ndarray  # (N, obs_dim(features)) float32
    actions: np.ndarray  # (N,) int64, indices of the mode's action set
    masks: np.ndarray  # (N, A) bool
    returns: np.ndarray  # (N,) float32 discounted shaped MC return (ticks)
    n_sessions: int = 0

    def __len__(self) -> int:
        return len(self.actions)

    def action_counts(self) -> np.ndarray:
        return np.bincount(self.actions, minlength=self.masks.shape[1])


def _discounted(rewards: Sequence[float], gamma: float) -> np.ndarray:
    out = np.zeros(len(rewards), dtype=np.float64)
    acc = 0.0
    for i in range(len(rewards) - 1, -1, -1):
        acc = rewards[i] + gamma * acc
        out[i] = acc
    return out


def _concat(parts: list[BCDataset], a: int, dim: int) -> BCDataset:
    if not parts:
        return BCDataset(
            np.zeros((0, dim), np.float32),
            np.zeros(0, np.int64),
            np.zeros((0, a), bool),
            np.zeros(0, np.float32),
            0,
        )
    return BCDataset(
        np.concatenate([p.obs for p in parts]),
        np.concatenate([p.actions for p in parts]),
        np.concatenate([p.masks for p in parts]),
        np.concatenate([p.returns for p in parts]),
        sum(p.n_sessions for p in parts),
    )


def _play(
    env: ScalpEnv,
    session: ScalpSession,
    pick: Any,
    gamma: float,
) -> BCDataset:
    """Run one session; ``pick(env, obs, d) -> action index`` labels each step.

    A label that is illegal in the current state is replaced by keep and the step
    is dropped from the dataset.
    """
    obs, _ = env.reset(options={"session": session})
    o_l, a_l, m_l, r_l, keep_l = [], [], [], [], []
    done = False
    while not done:
        d = env.seconds
        mask = env.action_mask()
        a = int(pick(env, obs, d))
        ok = bool(mask[a])
        o_l.append(obs.copy())
        a_l.append(a if ok else 0)
        m_l.append(mask)
        keep_l.append(ok)
        obs, r, done, _, _ = env.step(a if ok else 0)
        r_l.append(r)
    rets = _discounted(r_l, gamma)
    k = np.array(keep_l, dtype=bool)
    return BCDataset(
        np.asarray(o_l, dtype=np.float32)[k],
        np.asarray(a_l, dtype=np.int64)[k],
        np.asarray(m_l, dtype=bool)[k],
        rets.astype(np.float32)[k],
        1,
    )


# --------------------------------------------------------------------------- #
# Rule teacher
# --------------------------------------------------------------------------- #
def collect_rule_dataset(
    sessions: Sequence[ScalpSession],
    rule: Any,
    config: ScalpConfig,
    gamma: float = 0.997,
) -> BCDataset:
    """(obs, action, mask, MC return) from ``rule`` played on ``sessions``.

    ``rule`` is any baseline with ``reset()`` / ``act(env, obs)`` (RuleScalper).
    The env uses ``config`` as given, so returns include the training shaping.
    """
    env = ScalpEnv(None, config)
    parts = []
    for s in sessions:
        rule.reset()
        parts.append(_play(env, s, lambda e, o, d: rule.act(e, o), gamma))
    return _concat(parts, n_actions(config.order_mode), obs_dim(config.features))


# --------------------------------------------------------------------------- #
# CSV of real trades
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TradeRow:
    date: str
    stem: str
    us: int
    side: int  # +1 buy / -1 sell
    price: float
    qty: int
    kind: str  # "entry" | "exit"
    order: str | None = None  # "limit" | "market" | None (infer)


def _parse_side(x: str) -> int:
    v = x.strip().lower()
    if v in ("buy", "b", "1", "+1", "long"):
        return 1
    if v in ("sell", "s", "-1", "short"):
        return -1
    raise ValueError(f"unknown side {x!r}")


def load_trades_csv(path: str | Path) -> dict[tuple[str, str], list[TradeRow]]:
    """Read the trade CSV (see module docstring) grouped by ``(date, stem.lower())``."""
    out: dict[tuple[str, str], list[TradeRow]] = {}
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rd = csv.DictReader(fh)
        missing = [c for c in CSV_COLUMNS if c not in (rd.fieldnames or [])]
        if missing:
            raise ValueError(f"trade CSV is missing columns {missing}")
        for i, row in enumerate(rd, start=2):
            try:
                kind = row["kind"].strip().lower()
                if kind not in ("entry", "exit"):
                    raise ValueError(f"unknown kind {row['kind']!r}")
                order = (row.get("order") or "").strip().lower() or None
                if order not in (None, "limit", "market"):
                    raise ValueError(f"unknown order {order!r}")
                tr = TradeRow(
                    date=row["date"].strip(),
                    stem=row["stem"].strip().lower(),
                    us=int(float(row["us"])),
                    side=_parse_side(row["side"]),
                    price=float(row["price"]),
                    qty=int(float(row["qty"])),
                    kind=kind,
                    order=order,
                )
            except (ValueError, KeyError) as e:
                raise ValueError(f"{path}:{i}: {e}") from e
            out.setdefault((tr.date, tr.stem), []).append(tr)
    return out


def plan_actions_from_trades(
    session: ScalpSession,
    trades: Sequence[TradeRow],
    config: ScalpConfig,
) -> dict[int, int]:
    """``{decision second: action index}`` for the trades of one session."""
    mode = config.order_mode
    ix = ACTION_INDEX[mode]
    lat = config.latency_ms * 1000
    plan: dict[int, int] = {}

    def rel_us(t: TradeRow) -> int:
        return t.us - session.t_open_us if t.us >= ABSOLUTE_US_MIN else t.us

    for t in sorted(trades, key=rel_us):
        t_rel = rel_us(t)
        d = int(np.floor((t_rel - lat) / US))
        d = min(max(d, START_S), WINDOW_S - 1)
        if d in plan:
            continue
        sec = min(max(int(t_rel // US), 0), WINDOW_S)
        last = float(session.last[sec])
        tk = float(session.tick[sec])
        order = t.order
        if order is None:
            slip = config.market_slip_ticks * tk
            px_mkt = last + slip if t.side > 0 else last - slip
            order = "market" if abs(t.price - px_mkt) < 1e-6 else "limit"
        if mode == "market":
            g = G_FLATTEN if t.kind == "exit" else (G_MBUY if t.side > 0 else G_MSELL)
        elif order == "market":
            if t.kind == "exit":
                g = G_FLATTEN
            elif G_MBUY in ix:
                g = G_MBUY if t.side > 0 else G_MSELL
            else:
                g = G_BID0 if t.side > 0 else G_ASK0
        else:
            dist = (last - t.price) if t.side > 0 else (t.price - last)
            k = int(np.clip(round(dist / tk), 0, 2))
            g = (G_BID0 if t.side > 0 else G_ASK0) + k
        plan[d] = ix[g]
    return plan


def collect_csv_dataset(
    sessions: Sequence[ScalpSession],
    trades: dict[tuple[str, str], list[TradeRow]],
    config: ScalpConfig,
    gamma: float = 0.997,
) -> BCDataset:
    """Replay each CSV session under its labelled action plan (see module doc)."""
    env = ScalpEnv(None, config)
    parts = []
    keep = ACTION_INDEX[config.order_mode][G_KEEP]
    for s in sessions:
        rows = trades.get((s.date, s.stem.lower()))
        if not rows:
            continue
        plan = plan_actions_from_trades(s, rows, config)
        parts.append(_play(env, s, lambda e, o, d, plan=plan: plan.get(d, keep), gamma))
    return _concat(parts, n_actions(config.order_mode), obs_dim(config.features))


# --------------------------------------------------------------------------- #
# Pretraining
# --------------------------------------------------------------------------- #
def masked_cross_entropy(
    logits: torch.Tensor,
    actions: torch.Tensor,
    masks: torch.Tensor,
    label_smoothing: float = 0.0,
) -> torch.Tensor:
    """Cross-entropy over the legal actions (smoothing spreads mass over legal ones)."""
    logp = torch.log_softmax(apply_action_mask(logits, masks), dim=-1)
    nll = -logp.gather(1, actions.unsqueeze(1)).squeeze(1)
    if label_smoothing > 0:
        m = masks.to(logp.dtype)
        uniform = -(logp * m).sum(dim=1) / m.sum(dim=1).clamp(min=1)
        nll = (1 - label_smoothing) * nll + label_smoothing * uniform
    return nll.mean()


def bc_pretrain(
    policy: ActorCritic,
    data: BCDataset,
    *,
    steps: int,
    batch_size: int = 256,
    lr: float = 1e-3,
    label_smoothing: float = 0.1,
    use_masks: bool = True,
    fit_critic: bool = False,
    seed: int = 0,
) -> dict[str, float]:
    """Cross-entropy pretraining of the actor on ``data`` (optionally the critic).

    The critic regresses the MC returns divided by their std (the scale PPO's reward
    normaliser will use), so it starts in roughly the right units.  Returns loss /
    accuracy before and after (mean over the first / last 10% of steps, plus a full
    pass accuracy under argmax).
    """
    if len(data) == 0:
        raise ValueError("empty behaviour-cloning dataset")
    if steps <= 0:
        raise ValueError("steps must be > 0")
    gen = torch.Generator().manual_seed(seed)
    obs = torch.as_tensor(data.obs)
    act = torch.as_tensor(data.actions)
    msk = torch.as_tensor(data.masks) if use_masks else torch.ones_like(
        torch.as_tensor(data.masks)
    )
    ret_scale = float(max(np.std(data.returns), 1e-6))
    ret = torch.as_tensor(data.returns / ret_scale)
    params = list(policy.parameters())
    opt = torch.optim.Adam(params, lr=lr, eps=1e-5)
    policy.train()
    losses = []
    for _ in range(steps):
        idx = torch.randint(0, len(data), (min(batch_size, len(data)),), generator=gen)
        logits, value = policy(obs[idx])
        loss = masked_cross_entropy(logits, act[idx], msk[idx], label_smoothing)
        if fit_critic:
            loss = loss + 0.5 * ((value - ret[idx]) ** 2).mean()
        opt.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm_(params, 1.0)
        opt.step()
        losses.append(float(loss.item()))
    policy.eval()
    with torch.no_grad():
        logits, _ = policy(obs)
        pred = apply_action_mask(logits, msk).argmax(dim=-1)
        acc = float((pred == act).float().mean())
    k = max(1, steps // 10)
    return {
        "loss_start": float(np.mean(losses[:k])),
        "loss_end": float(np.mean(losses[-k:])),
        "accuracy": acc,
        "n_samples": float(len(data)),
        "return_scale": ret_scale,
    }

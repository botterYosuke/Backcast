#!/usr/bin/env python3
"""P3 scalping: tune baselines, train PPO on the limit-order env, evaluate, report.

    uv run python scripts/train_scalp.py --smoke
    uv run python scripts/train_scalp.py --train-steps 10000000

v2 options: ``--order-mode {limit,market,both}``, legal-action masking (default on,
``--no-action-mask`` to ablate), reward shaping ``--inventory-penalty`` /
``--idle-penalty`` (ticks per second, training only; reported PnL is always raw),
``--fill-curriculum p0,frac``, ``--ent-start/--ent-end``, ``--bc-from rule|csv:PATH``
and ``--skip-baselines`` / ``--reuse-baselines JSON`` (baseline results cached per
order mode so several runs do not repeat the tuning).  Validation / test always use
fixed trade_through (main) and touch (reference) fills and no shaping.

Splits / symbol holdout follow ``scripts/predictability_gate.py`` (train
2024-01-04..2025-06-30, val 2025-07-01..2025-12-31, test 2026-01-01..2026-08-19;
~20% of stems held out by hash and excluded from train / val selection).  The
RuleScalper grid and the PPO checkpoint are chosen on validation (seen stems,
trade-through fills) only; test is touched once, after both are frozen.  The
report is generated from measured values only.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import json
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from trading_rl.scalp_baselines import (  # noqa: E402
    RuleScalperParams,
    eval_baseline_rows,
    momentum_grid,
    rule_scalper_grid,
    tune_specs_on_rows,
)
from trading_rl.scalp_env import (  # noqa: E402
    ORDER_MODES,
    STATE_MEAN,
    STATE_STD,
    ScalpConfig,
    SessionRow,
    action_names,
    is_heldout,
    load_scalp_sessions,
    n_actions,
    obs_dim,
    universe_session_rows,
)
from trading_rl.scalp_features import (  # noqa: E402
    default_store,
    market_names,
    parse_feature_spec,
)

SPLITS = {
    "train": ("2024-01-04", "2025-06-30"),
    "val": ("2025-07-01", "2025-12-31"),
    "test": ("2026-01-01", "2026-08-19"),
}
# Per-run defaults are derived from --order-mode (see ``default_paths``); the v1
# model / report are never default targets so they cannot be overwritten by accident.
SCRATCH = Path(
    "C:/Users/sasai/AppData/Local/Temp/claude/e40016ce9a690696/"
    "dfe7ccbe-d763-474e-aa6b-13ac077b02b5/scratchpad"
)
N_BOOT = 2000
RULES = ("trade_through", "touch")


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# Statistics
# --------------------------------------------------------------------------- #
def boot_ci(x: np.ndarray, rng: np.random.Generator) -> tuple[float, float, float]:
    """Mean and 95% bootstrap CI over the entries of ``x`` (days)."""
    if len(x) == 0:
        return float("nan"), float("nan"), float("nan")
    idx = rng.integers(0, len(x), size=(N_BOOT, len(x)))
    means = x[idx].mean(axis=1)
    return (
        float(x.mean()),
        float(np.percentile(means, 2.5)),
        float(np.percentile(means, 97.5)),
    )


def boot_ratio_ci(
    num: np.ndarray, den: np.ndarray, rng: np.random.Generator
) -> tuple[float, float, float]:
    """Pooled ``sum(num) / sum(den)`` and its 95% day-bootstrap CI (per-session mean)."""
    if len(num) == 0 or den.sum() <= 0:
        return float("nan"), float("nan"), float("nan")
    idx = rng.integers(0, len(num), size=(N_BOOT, len(num)))
    ratios = num[idx].sum(axis=1) / np.maximum(den[idx].sum(axis=1), 1e-12)
    return (
        float(num.sum() / den.sum()),
        float(np.percentile(ratios, 2.5)),
        float(np.percentile(ratios, 97.5)),
    )


def summarize(eps: list[dict[str, Any]], seed: int = 0) -> dict[str, Any]:
    """Aggregate episode summaries into the report metrics."""
    rng = np.random.default_rng(seed)
    if not eps:
        return {"n_sessions": 0}
    dates = sorted({e["date"] for e in eps})
    di = {d: i for i, d in enumerate(dates)}
    yen = np.zeros(len(dates))
    ticks = np.zeros(len(dates))
    trips = np.zeros(len(dates))
    cnt = np.zeros(len(dates))
    for e in eps:
        i = di[e["date"]]
        yen[i] += e["pnl_yen100"]
        ticks[i] += e["pnl_ticks"]
        trips[i] += e["n_trips"]
        cnt[i] += 1

    def tot(k: str) -> float:
        return float(sum(e[k] for e in eps))

    n_trips = tot("n_trips")
    gl = tot("gross_loss_yen")
    cum = np.cumsum(yen)
    peak = np.maximum.accumulate(np.concatenate([[0.0], cum]))[1:]
    max_dd = float((peak - cum).max()) if len(cum) else 0.0
    n_sess = len(eps)
    acts = np.sum([e["actions"] for e in eps], axis=0) if "actions" in eps[0] else None
    return {
        "n_sessions": n_sess,
        "n_days": len(dates),
        "yen_day": boot_ci(yen, rng),
        "ticks_day": boot_ci(ticks, rng),
        "yen_sess": float(np.mean([e["pnl_yen100"] for e in eps])),
        "yen_sess_ci": boot_ratio_ci(yen, cnt, rng),
        "ticks_sess": float(np.mean([e["pnl_ticks"] for e in eps])),
        "trips_day": float(trips.mean()),
        "trips_sess": n_trips / n_sess,
        "win_rate": tot("n_wins") / n_trips if n_trips else float("nan"),
        "pf": tot("gross_win_yen") / gl if gl > 0 else float("nan"),
        "max_dd_yen": max_dd,
        "entry_limit": tot("n_entry_limit"),
        "entry_market": tot("n_entry_market"),
        "exit_limit": tot("n_exit_limit"),
        "exit_market": tot("n_exit_market"),
        "forced": tot("n_forced"),
        "hold_s": tot("hold_s_sum") / n_trips if n_trips else float("nan"),
        # mean round-trip PnL before the market slippage (limit fills pay none)
        "gross_ticks_trip": (
            tot("gross_ticks_sum") / n_trips if n_trips else float("nan")
        ),
        "orders_sess": tot("n_orders") / n_sess,
        "invalid_sess": tot("n_invalid_actions") / n_sess,
        "latency_noops_sess": tot("n_latency_noops") / n_sess,
        "no_trade_frac": float(np.mean([e["n_trips"] == 0 for e in eps])),
        "actions": acts,
    }


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #
def split_rows(
    name: str, limit_days: int | None = None
) -> tuple[list[SessionRow], list[SessionRow]]:
    """``(seen_rows, heldout_rows)`` for a split; train keeps only seen stems."""
    start, end = SPLITS[name]
    rows = universe_session_rows(start, end)
    if limit_days:
        days = sorted({r[0] for r in rows})[:limit_days]
        keep = set(days)
        rows = [r for r in rows if r[0] in keep]
    seen = [r for r in rows if not is_heldout(r[1])]
    held = [r for r in rows if is_heldout(r[1])]
    return seen, (held if name != "train" else [])


def load_exclude_sessions(path: Path) -> set[tuple[str, str]]:
    """``(date, stem_lower)`` pairs from a CSV with ``date,stem`` columns."""
    with open(path, encoding="utf-8-sig", newline="") as f:
        return {
            (str(r["date"]).strip()[:10], str(r["stem"]).strip().lower())
            for r in csv.DictReader(f)
        }


def drop_sessions(
    rows: list[SessionRow], exclude: set[tuple[str, str]]
) -> list[SessionRow]:
    return [r for r in rows if (str(r[0])[:10], str(r[1]).lower()) not in exclude]


def sample_rows(rows: list[SessionRow], n: int, seed: int) -> list[SessionRow]:
    if len(rows) <= n:
        return list(rows)
    rng = np.random.default_rng(seed)
    idx = np.sort(rng.choice(len(rows), size=n, replace=False))
    return [rows[i] for i in idx]


def chunked(seq: list[Any], n: int) -> list[list[Any]]:
    return [seq[i : i + n] for i in range(0, len(seq), n)]


def fit_normalizer(sessions: list[Any], seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    pick = rng.choice(len(sessions), size=min(3000, len(sessions)), replace=False)
    X = np.concatenate([sessions[i].X[::7] for i in pick]).astype(np.float64)
    mean = np.concatenate([X.mean(0), STATE_MEAN])
    std = np.concatenate([X.std(0), STATE_STD])
    std = np.where(std < 1e-8, 1.0, std)
    return mean.astype(np.float32), std.astype(np.float32)


# --------------------------------------------------------------------------- #
# Baseline tuning / evaluation (multiprocessing)
# --------------------------------------------------------------------------- #
def tune_grid(
    specs: list[dict[str, Any]],
    rows: list[SessionRow],
    n_stage1: int,
    n_stage2: int,
    top_k: int,
    workers: int,
    order_mode: str,
    label: str,
) -> tuple[int, list[dict[str, Any]]]:
    """Two-stage validation search over ``specs``; returns (best index, table)."""
    cfg = ScalpConfig(order_mode=order_mode).__dict__.copy()
    top_k = min(top_k, len(specs))
    sub1 = sample_rows(rows, n_stage1, seed=101)
    log(f"{label} stage 1: {len(specs)} configs x {len(sub1)} val sessions")
    groups = [list(range(i, len(specs), workers)) for i in range(workers)]
    res1: dict[int, dict[str, float]] = {}
    with cf.ProcessPoolExecutor(workers) as ex:
        futs = {
            ex.submit(tune_specs_on_rows, [specs[i] for i in g], sub1, cfg): g
            for g in groups
            if g
        }
        for fut in cf.as_completed(futs):
            for i, r in zip(futs[fut], fut.result(), strict=True):
                res1[i] = r
    order = sorted(res1, key=lambda i: -res1[i]["pnl_yen100"])
    top = order[:top_k]
    log(
        f"{label} stage 1 best: "
        + ", ".join(f"#{i} {res1[i]['pnl_yen100']:.0f}" for i in top[:3])
        + f"; median {np.median([r['pnl_yen100'] for r in res1.values()]):.0f}"
    )
    sub2 = sample_rows(rows, n_stage2, seed=202)
    log(f"{label} stage 2: top {len(top)} x {len(sub2)} val sessions")
    top_specs = [specs[i] for i in top]
    parts = [list(range(i, len(top), workers)) for i in range(workers)]
    res2: dict[int, dict[str, float]] = {}
    with cf.ProcessPoolExecutor(workers) as ex:
        futs = {
            ex.submit(tune_specs_on_rows, [top_specs[j] for j in p], sub2, cfg): p
            for p in parts
            if p
        }
        for fut in cf.as_completed(futs):
            for j, r in zip(futs[fut], fut.result(), strict=True):
                res2[j] = r
    best_j = max(res2, key=lambda j: res2[j]["pnl_yen100"])
    table = [
        {
            "params": specs[top[j]].get("params", {}),
            "stage1_yen": res1[top[j]]["pnl_yen100"],
            "stage2_yen": res2[j]["pnl_yen100"],
            "stage2_ticks": res2[j]["pnl_ticks"],
            "stage2_trips": res2[j]["n_trips"],
            "n1": len(sub1),
            "n2": len(sub2),
            "chosen": j == best_j,
        }
        for j in sorted(res2, key=lambda j: -res2[j]["pnl_yen100"])
    ]
    n_pos = sum(1 for r in res1.values() if r["pnl_yen100"] > 0)
    table.append(
        {
            "meta": {
                "n_configs": len(specs),
                "n_positive_stage1": n_pos,
                "best_stage1": max(r["pnl_yen100"] for r in res1.values()),
                "median_stage1": float(
                    np.median([r["pnl_yen100"] for r in res1.values()])
                ),
            }
        }
    )
    return top[best_j], table


def tune_rule_scalper(
    rows: list[SessionRow],
    n_stage1: int,
    n_stage2: int,
    top_k: int,
    workers: int,
    order_mode: str = "limit",
) -> tuple[RuleScalperParams, list[dict[str, Any]]]:
    grid = rule_scalper_grid(order_mode=order_mode)
    specs = [{"name": "RuleScalper", "params": g.as_dict()} for g in grid]
    best, table = tune_grid(
        specs, rows, n_stage1, n_stage2, top_k, workers, order_mode, "RuleScalper"
    )
    return grid[best], table


def tune_momentum(
    rows: list[SessionRow],
    n_stage1: int,
    n_stage2: int,
    top_k: int,
    workers: int,
    order_mode: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    grid = momentum_grid()
    specs = [{"name": "Momentum", "params": g.as_dict()} for g in grid]
    best, table = tune_grid(
        specs, rows, n_stage1, n_stage2, top_k, workers, order_mode, "Momentum"
    )
    return grid[best].as_dict(), table


def eval_baseline_pool(
    spec: dict[str, Any],
    rows: list[SessionRow],
    rule: str,
    workers: int,
    order_mode: str = "limit",
) -> list[dict[str, Any]]:
    cfg = ScalpConfig(fill_rule=rule, order_mode=order_mode).__dict__.copy()
    parts = chunked(rows, max(1, -(-len(rows) // (workers * 4))))
    out: list[dict[str, Any]] = []
    with cf.ProcessPoolExecutor(workers) as ex:
        futs = [ex.submit(eval_baseline_rows, spec, p, cfg, 7) for p in parts]
        for f in futs:
            out.extend(f.result())
    return out


def eval_ppo_rows(
    policy: Any,
    rows: list[SessionRow],
    rule: str,
    chunk: int = 1500,
    order_mode: str = "limit",
    use_masks: bool = False,
    features: str = "base",
) -> list[dict[str, Any]]:
    from trading_rl.vec_rollout import evaluate_ppo_policy

    # raw PnL, no shaping
    cfg = ScalpConfig(fill_rule=rule, order_mode=order_mode, features=features)
    out: list[dict[str, Any]] = []
    for part in chunked(rows, chunk):
        sess = load_scalp_sessions(part, features=features)
        out.extend(evaluate_ppo_policy(policy, sess, cfg, use_masks=use_masks))
        del sess
    return out


# --------------------------------------------------------------------------- #
# Options helpers
# --------------------------------------------------------------------------- #
def parse_fill_curriculum(text: str | None) -> tuple[float, float] | None:
    """``"p0,frac"`` -> (p0, frac): P(trade_through) rises p0 -> 1 over frac of steps."""
    if not text:
        return None
    try:
        p0, frac = (float(x) for x in text.split(","))
    except ValueError as e:
        raise SystemExit(f"--fill-curriculum expects 'p0,frac', got {text!r}") from e
    if not (0.0 <= p0 <= 1.0 and 0.0 <= frac <= 1.0):
        raise SystemExit("--fill-curriculum: p0 and frac must be in [0, 1]")
    return p0, frac


def entropy_schedule(args: argparse.Namespace) -> tuple[float, float]:
    """(start, end) entropy coefficient; both = ``--entropy`` when no schedule given."""
    if args.ent_start is None and args.ent_end is None:
        return args.entropy, args.entropy
    start = args.entropy if args.ent_start is None else args.ent_start
    end = args.entropy if args.ent_end is None else args.ent_end
    return start, end


def train_config(args: argparse.Namespace) -> ScalpConfig:
    """Training env config (with reward shaping).  Evaluation never uses it."""
    return ScalpConfig(
        order_mode=args.order_mode,
        inventory_penalty_ticks_per_s=args.inventory_penalty,
        idle_penalty_ticks_per_s=args.idle_penalty,
        keep_penalty_ticks=args.keep_penalty,
        action_bonus_ticks=args.action_bonus,
        reward_mode=args.reward_mode,
        unrealized_loss_penalty_per_s=args.unrealized_loss_penalty,
        features=args.features,
    )


def default_paths(order_mode: str) -> tuple[Path, Path]:
    return (
        REPO_ROOT / f"models/ppo_scalp_top50_open10m_{order_mode}_v2.pt",
        REPO_ROOT / f".agents/docs/research/rl-top50-scalp-v2-{order_mode}.md",
    )


def rule_params_from_json(text: str) -> RuleScalperParams:
    d = json.loads(text)
    if d.get("sl") == "inf":
        d["sl"] = float("inf")
    return RuleScalperParams(**d)


def build_bc_dataset(
    args: argparse.Namespace,
    train_sessions: list[Any],
    rule_params: RuleScalperParams | None,
    cfg: ScalpConfig,
):
    """Behaviour-cloning data: tuned RuleScalper actions, or the owner's trade CSV."""
    from trading_rl.scalp_baselines import RuleScalper
    from trading_rl.scalp_bc import (
        collect_csv_dataset,
        collect_rule_dataset,
        load_trades_csv,
    )

    if args.bc_from == "rule":
        params = rule_params or RuleScalperParams()
        pick = sample_rows(list(range(len(train_sessions))), args.bc_sessions, seed=404)
        subset = [train_sessions[i] for i in pick]
        log(f"BC: RuleScalper {params.as_dict()} on {len(subset)} train sessions")
        data = collect_rule_dataset(subset, RuleScalper(params), cfg, gamma=args.gamma)
        return data, params.as_dict()
    if args.bc_from.startswith("csv:"):
        trades = load_trades_csv(args.bc_from[4:])
        keys = set(trades)
        dates = sorted({k[0] for k in keys})
        rows = [
            r
            for r in universe_session_rows(dates[0], dates[-1])
            if (r[0], r[1].lower()) in keys
        ]
        log(f"BC: csv {len(keys)} sessions in file, {len(rows)} found in the cache")
        sess = load_scalp_sessions(rows, features=args.features)
        data = collect_csv_dataset(sess, trades, cfg, gamma=args.gamma)
        return data, {"csv": args.bc_from[4:]}
    raise SystemExit(f"--bc-from must be 'rule' or 'csv:<path>', got {args.bc_from!r}")


# --------------------------------------------------------------------------- #
# Training
# --------------------------------------------------------------------------- #
def train_ppo(
    args: argparse.Namespace,
    train_sessions: list[Any],
    val_sessions: list[Any],
    rule_params: RuleScalperParams | None = None,
):
    import torch

    from trading_rl.policy import ActorCritic
    from trading_rl.scalp_bc import bc_pretrain
    from trading_rl.trainer import PPOConfig
    from trading_rl.vec_rollout import (
        VecPPOTrainer,
        VecScalpEnv,
        evaluate_ppo_policy,
        fill_curriculum_p,
        linear_schedule,
    )

    torch.set_num_threads(args.threads)
    cfg = train_config(args)  # shaped reward
    # raw PnL, trade_through
    eval_cfg = ScalpConfig(order_mode=args.order_mode, features=args.features)
    curriculum = parse_fill_curriculum(args.fill_curriculum)
    ent0, ent1 = entropy_schedule(args)
    total = args.train_steps
    p_tt0 = fill_curriculum_p(0, total, *curriculum) if curriculum else None
    venv = VecScalpEnv(
        train_sessions, args.n_envs, cfg, seed=args.seed, p_trade_through=p_tt0
    )
    with torch.random.fork_rng(devices=[]):
        torch.manual_seed(args.seed)
        policy = ActorCritic(
            obs_dim(args.features), n_actions(args.order_mode), hidden_dim=args.hidden
        )
    mean, std = fit_normalizer(train_sessions, args.seed)
    policy.set_obs_normalizer(mean, std)

    bc_info: dict[str, Any] = {"bc_from": args.bc_from}
    if args.bc_from:
        data, bc_src = build_bc_dataset(args, train_sessions, rule_params, cfg)
        counts = data.action_counts()
        names = action_names(args.order_mode)
        log(
            f"BC data: {len(data):,} samples from {data.n_sessions} sessions; actions "
            + ", ".join(f"{n}={c}" for n, c in zip(names, counts, strict=True))
        )
        res = bc_pretrain(
            policy,
            data,
            steps=args.bc_steps,
            lr=args.bc_lr,
            label_smoothing=args.bc_label_smoothing,
            use_masks=args.mask,
            fit_critic=args.bc_critic,
            seed=args.seed,
        )
        log(
            f"BC: loss {res['loss_start']:.3f} -> {res['loss_end']:.3f}, "
            f"argmax accuracy {100 * res['accuracy']:.1f}%"
        )
        bc_info.update(
            bc_source=bc_src,
            bc_steps=args.bc_steps,
            bc_sessions=data.n_sessions,
            bc_samples=len(data),
            bc_critic=args.bc_critic,
            bc_label_smoothing=args.bc_label_smoothing,
            bc_loss_start=res["loss_start"],
            bc_loss_end=res["loss_end"],
            bc_accuracy=res["accuracy"],
            bc_action_counts=[int(c) for c in counts],
        )
        del data

    ppo_cfg = PPOConfig(
        learning_rate=args.lr,
        gamma=args.gamma,
        gae_lambda=args.gae_lambda,
        clip_ratio=0.2,
        value_coef=0.5,
        entropy_coef=ent0,
        max_grad_norm=0.5,
        update_epochs=4,
        batch_size=args.batch,
        anneal_lr=True,
    )
    trainer = VecPPOTrainer(
        venv,
        policy,
        ppo_cfg,
        steps_per_env=args.steps_per_env,
        seed=args.seed,
        use_masks=args.mask,
    )
    meta_base = cfg.metadata()
    meta_base.update(
        {
            "train_split": SPLITS["train"],
            "val_split": SPLITS["val"],
            "symbol_holdout": "md5(stem)[0] % 5 == 0",
            "obs_dim": obs_dim(args.features),
            "hidden_dim": args.hidden,
            "seed": args.seed,
            "n_envs": args.n_envs,
            "steps_per_env": args.steps_per_env,
            "gamma": args.gamma,
            "entropy_coef": args.entropy,
            "entropy_start": ent0,
            "entropy_end": ent1,
            "action_masking": bool(args.mask),
            "fill_curriculum": list(curriculum) if curriculum else None,
            "train_steps": total,
            "reward_note": (
                "training reward is shaped (inventory/idle penalty); "
                "pnl_ticks / pnl_yen100 are always raw"
            ),
            **bc_info,
        }
    )
    curve: list[dict[str, Any]] = []
    best = {"val_yen": -1e18}
    next_eval = args.eval_every
    t_start = time.time()
    recent: list[dict[str, Any]] = []
    last_metrics: dict[str, float] = {}
    eval_wall = 0.0

    def mean_of(key: str) -> float:
        return float(np.mean([e[key] for e in recent])) if recent else float("nan")

    def do_eval() -> None:
        nonlocal best, eval_wall
        t_e = time.time()
        eps = evaluate_ppo_policy(policy, val_sessions, eval_cfg, use_masks=args.mask)
        s = summarize(eps)
        p_tt = venv.p_trade_through
        rec = {
            "steps": trainer.total_timesteps,
            "wall_s": time.time() - t_start,
            "train_ep_ticks": mean_of("pnl_ticks"),
            "train_ep_yen": mean_of("pnl_yen100"),
            "train_shaped_ticks": mean_of("shaped_return_ticks"),
            "entropy": last_metrics.get("entropy", float("nan")),
            "ent_coef": trainer.config.entropy_coef,
            "p_tt": float("nan") if p_tt is None else p_tt,
            "val_yen": s["yen_sess"],
            "val_ticks": s["ticks_sess"],
            "val_trips": s["trips_sess"],
            "val_orders": s["orders_sess"],
            "val_invalid": s["invalid_sess"],
        }
        curve.append(rec)
        star = ""
        if rec["val_yen"] > best["val_yen"]:
            best = dict(rec)
            trainer.save(
                args.model,
                {
                    **meta_base,
                    "selected_at_steps": rec["steps"],
                    "val_session_mean_yen100": rec["val_yen"],
                    "val_session_mean_ticks": rec["val_ticks"],
                },
            )
            star = " *best*"
        log(
            f"eval @{rec['steps']:,}: val {rec['val_yen']:+.1f} yen/sess "
            f"{rec['val_ticks']:+.2f} tk/sess (raw), trips/sess {rec['val_trips']:.2f}, "
            f"invalid/sess {rec['val_invalid']:.1f}, "
            f"train {rec['train_ep_ticks']:+.1f} tk/ep raw "
            f"({rec['train_shaped_ticks']:+.1f} shaped), H={rec['entropy']:.3f}{star}"
        )
        eval_wall += time.time() - t_e

    t0 = time.time()
    if args.bc_from:
        do_eval()  # step 0: the behaviour-cloned policy
    it = 0
    start_steps = trainer.total_timesteps
    while trainer.total_timesteps < total:
        done_steps = trainer.total_timesteps - start_steps
        progress = done_steps / max(1, total - start_steps)
        trainer.config.entropy_coef = linear_schedule(ent0, ent1, progress)
        if curriculum:
            venv.set_p_trade_through(fill_curriculum_p(done_steps, total, *curriculum))
        m = trainer.train_iteration(progress=progress)
        last_metrics = m
        recent.extend(trainer.episode_log)
        trainer.episode_log.clear()
        recent = recent[-400:]
        it += 1
        if it % 20 == 0:
            el = time.time() - t0 - eval_wall
            log(
                f"it {it} steps {trainer.total_timesteps:,} "
                f"({(trainer.total_timesteps - start_steps) / el:,.0f}/s) "
                f"ep_ticks {m['mean_ep_return']:+.1f} H={m['entropy']:.3f} "
                f"kl={m['approx_kl']:.4f} ent_coef={trainer.config.entropy_coef:.4f}"
            )
        if trainer.total_timesteps >= next_eval:
            do_eval()
            next_eval += args.eval_every
    if not curve or curve[-1]["steps"] != trainer.total_timesteps:
        do_eval()
    train_wall = time.time() - t0
    last_path = SCRATCH / f"{args.model.stem}_last.pt"
    last_path.parent.mkdir(parents=True, exist_ok=True)
    trainer.save(last_path, {**meta_base, "note": "last iterate (not selected)"})
    n_steps = trainer.total_timesteps - start_steps
    return {
        "curve": curve,
        "best": best,
        "wall_s": train_wall,
        "steps": trainer.total_timesteps,
        "throughput": n_steps / max(1e-9, train_wall - eval_wall),
        "throughput_incl_eval": n_steps / max(1e-9, train_wall),
        "ppo_cfg": ppo_cfg,
        "iters": it,
        "bc": bc_info,
        "curriculum": curriculum,
        "entropy": (ent0, ent1),
    }


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
def f_ci(t: tuple[float, float, float], digits: int = 0) -> str:
    m, lo, hi = t
    return f"{m:,.{digits}f} [{lo:,.{digits}f}, {hi:,.{digits}f}]"


def f_num(x: float, d: int = 2) -> str:
    return "n/a" if x != x else f"{x:,.{d}f}"


def metrics_table(results: dict[str, dict[str, dict[str, Any]]]) -> list[str]:
    lines = [
        "| policy | fill rule | sessions/days | yen/100sh per day [95% CI] | "
        "ticks per day [95% CI] | yen/sess | trips/day | trips/sess | win % | PF | "
        "max DD (yen) | entries L/M | exits L/M/forced | limit-exit share | hold s | "
        "gross ticks/trip | invalid/sess |",
        "|" + "---|" * 17,
    ]
    for pol, by_rule in results.items():
        for rule in RULES:
            s = by_rule.get(rule)
            if not s or not s.get("n_sessions"):
                continue
            n_exit = s["exit_limit"] + s["exit_market"] + s["forced"]
            share = s["exit_limit"] / n_exit if n_exit else float("nan")
            lines.append(
                f"| {pol} | {rule} | {s['n_sessions']}/{s['n_days']} | "
                f"{f_ci(s['yen_day'])} | {f_ci(s['ticks_day'], 1)} | "
                f"{s['yen_sess']:+.1f} | {s['trips_day']:.0f} | {s['trips_sess']:.2f} | "
                f"{100 * s['win_rate']:.1f} | {f_num(s['pf'])} | "
                f"{s['max_dd_yen']:,.0f} | "
                f"{s['entry_limit']:.0f}/{s['entry_market']:.0f} | "
                f"{s['exit_limit']:.0f}/{s['exit_market']:.0f}/{s['forced']:.0f} | "
                f"{f_num(100 * share, 1)}% | {f_num(s['hold_s'], 1)} | "
                f"{f_num(s.get('gross_ticks_trip', float('nan')), 3)} | "
                f"{s['invalid_sess']:.2f} |"
            )
    return lines


def action_table(s: dict[str, Any], order_mode: str = "limit") -> list[str]:
    acts = s["actions"]
    tot = float(acts.sum())
    lines = ["| action | share of decisions |", "|---|---|"]
    for name, c in zip(action_names(order_mode), acts, strict=True):
        lines.append(f"| {name} | {100 * c / tot:.2f}% |")
    return lines


def auto_notes(all_res: dict[str, dict[str, dict[str, dict[str, Any]]]]) -> list[str]:
    notes: list[str] = []
    for group, res in all_res.items():
        ppo = res.get("PPO", {}).get("trade_through")
        if not ppo or not ppo.get("n_sessions"):
            continue
        if ppo["trips_sess"] < 0.5:
            notes.append(
                f"- [{group}] PPO makes {ppo['trips_sess']:.2f} round trips per "
                "session: it essentially does not trade."
            )
        n_ent = ppo["entry_limit"] + ppo["entry_market"]
        if n_ent and ppo["entry_market"] / n_ent > 0.5:
            notes.append(f"- [{group}] PPO enters mostly with market orders.")
        n_exit = ppo["exit_limit"] + ppo["exit_market"] + ppo["forced"]
        if n_exit and ppo["exit_limit"] / n_exit < 0.2:
            notes.append(
                f"- [{group}] PPO exits mostly by market/forced close "
                f"(limit-exit share {100 * ppo['exit_limit'] / n_exit:.0f}%)."
            )
        if n_exit and ppo["forced"] / n_exit > 0.3:
            notes.append(
                f"- [{group}] {100 * ppo['forced'] / n_exit:.0f}% of PPO exits are the "
                "forced 600 s close (it holds to the end)."
            )
        acts = ppo["actions"]
        if acts is not None and acts.sum() > 0 and acts[0] / acts.sum() > 0.95:
            notes.append(
                f"- [{group}] PPO plays 'keep' on {100 * acts[0] / acts.sum():.1f}% of decisions."
            )
        for base in ("DoNothing", "Random", "RuleScalper(tuned)"):
            b = res.get(base, {}).get("trade_through")
            if (
                b
                and b.get("n_sessions")
                and abs(b["yen_sess"] - ppo["yen_sess"]) < 1e-9
            ):
                notes.append(f"- [{group}] PPO result is identical to {base}.")
        lo, hi = ppo["yen_day"][1], ppo["yen_day"][2]
        verdict = (
            "positive"
            if lo > 0
            else "negative"
            if hi < 0
            else "not distinguishable from 0"
        )
        notes.append(
            f"- [{group}] PPO trade-through daily PnL is {verdict} "
            f"({f_ci(ppo['yen_day'])} yen/100sh per day)."
        )
        t = res.get("PPO", {}).get("touch")
        if t and t.get("n_sessions"):
            notes.append(
                f"- [{group}] PPO touch-minus-trade-through gap: "
                f"{t['yen_day'][0] - ppo['yen_day'][0]:+,.0f} yen/100sh per day."
            )
    return notes


def write_report(path: Path, ctx: dict[str, Any]) -> None:
    L: list[str] = []
    cfg = ScalpConfig()
    a = ctx["args"]
    mode = a.order_mode
    L.append(
        f"# P3 開場スキャルピング v2 結果 (order_mode={mode}): 売買代金上位50銘柄 × 寄り付き10分"
    )
    L.append("")
    L.append(
        f"- 生成: {time.strftime('%Y-%m-%d %H:%M')} / `scripts/train_scalp.py` (実測値のみ)"
    )
    L.append("")
    L.append("## 1. 設定")
    L.append(
        "- 環境 `trading_rl.scalp_env.ScalpEnv`: 1銘柄×1日=1エピソード。判断は T_open+10s から"
        "1秒ごと (590回)、T_open+600s に全キャンセル+成行で強制決済して終了。"
    )
    L.append(
        "- 建玉: -1/0/+1 単位 (1単位=100株)。指値は買い・売り各1本まで。フラットは両建て注文可、"
        "ロング中は売り指値のみ、ショート中は買い指値のみ (違反は no-op として計数)。"
    )
    L.append(
        f"- order_mode = **{mode}**, 行動 (Discrete {n_actions(mode)}): "
        + ", ".join(f"{i}={n}" for i, n in enumerate(action_names(mode)))
        + "。bid_kN = last-N呼値、ask_kN = last+N呼値 (その時点の価格帯の呼値)。"
        "market_buy = flat->long / short->flat、market_sell = flat->short / long->flat "
        "(last ± 0.5呼値、全成行は全指値をキャンセル)。"
    )
    L.append(
        "- 合法手マスク (action_mask): ロング中は買い指値/成行買い不可、ショート中は売り側不可、"
        "注文なしで cancel 不可、フラットで flatten 不可。"
        + (
            "PPO は学習・評価・再生ともマスク付き (非合法手は確率0、log-prob/エントロピー/argmax もマスク後)。"
            if a.mask
            else "このrunはマスクなし (--no-action-mask)。"
        )
        + "マスク時の無効アクション数 invalid/sess は判断時点の違反のみ。"
        "判断後レイテンシ中に指値が約定して無効化した分は別カウント (n_latency_noops)。"
    )
    L.append(
        "- 約定ルール (主): **trade_through** = 買い指値 P は P より安い約定が出たとき、売り指値 P は"
        "P より高い約定が出たときに全量 P で約定 (P ちょうどの約定では約定しない)。"
        "参考: touch = P ちょうどでも約定。待ち行列順位は未モデル、marketable 指値の即時約定も未モデル。"
    )
    L.append(
        f"- レイテンシ {cfg.latency_ms} ms: 全アクション (発注・訂正・取消・成行) は判断時刻+{cfg.latency_ms}ms に"
        "有効化。新規注文は有効化時刻より厳密に後の約定のみで約定。"
    )
    L.append(
        f"- 成行: last ± {cfg.market_slip_ticks} 呼値 (買いは+、売りは-)。手数料 {cfg.commission_yen} 円/約定 (パラメータ)。"
        "窓外 (T_open+10s 前・T_open+600s 後) の約定はテストで禁止を確認。"
    )
    L.append(
        "- 報酬 = 1ステップの含み損益 (最終約定値で時価評価) の差分を、セッション基準呼値 (寄り付き価格の呼値) "
        "で割った値。ペナルティなしなら合計は最終損益 (呼値) に厳密に一致。PPO 学習時のみ割引リターンの"
        "走行標準偏差で報酬を割る (スケールのみ)。"
    )
    L.append(
        f"- **学習報酬 (シェイピング) と生損益の区別**: 学習時のみ 在庫ペナルティ {a.inventory_penalty} 呼値/秒 × |建玉| × 1秒、"
        f"アイドルペナルティ {a.idle_penalty} 呼値/秒 (フラットかつ注文なし; 取引自体を目的化するため非推奨、比較用)、"
        f"行動ベース (建玉・注文の状態は見ない): keep 判断ごとの罰 {a.keep_penalty} 呼値、"
        f"実際に効いた keep 以外の行動ごとの報酬 {a.action_bonus} 呼値。"
        f"報酬の土台 reward_mode={a.reward_mode} "
        "(mtm = 含み損益込みの毎秒の損益変化 / realized = 決済で確定した損益のみ、決済した秒に支払い)。"
        f"含み損ペナルティ {a.unrealized_loss_penalty} × 含み損(呼値) / 秒 (建玉中のみ、含み益には報酬なし)。"
        "このレポートの全ての PnL (学習曲線の val/train 列、結果表) は **生損益** (ペナルティ控除前)。"
        "ペナルティ込みの学習報酬は学習曲線の「train shaped」列のみ。検証・テストはペナルティなし・固定約定ルール。"
    )
    spec = parse_feature_spec(a.features)
    n_mkt = len(market_names(spec))
    L.append(
        f"- 観測 ({obs_dim(a.features)}次元): 特徴量スペック **`{spec.canonical}`** = 市場特徴量 {n_mkt} 個 "
        "(gate_features 28個から -J で削った残り + 追加グループ A-I、全て因果的) + 自己状態 10 個 "
        "(建玉, 含み損益[呼値], 買い注文の有無/last からの距離, 売り注文の有無/距離, エントリーからの秒/60, "
        "各注文発注からの秒/60, 残り時間割合)。正規化は ActorCritic の obs_mean/obs_std に学習セッションで fit "
        "(学習セッションのみ。検証/テストの統計は使わない)。"
        + (
            "**`--no-test`: テスト分割は一切読み込まず、評価もしていない (スクリーニング用)。**"
            if a.no_test
            else ""
        )
    )
    L.append(
        "- 日次PnL = その日の全セッション (≤50銘柄) の PnL 合計。CI は日単位ブートストラップ "
        f"({N_BOOT}回)。yen/sess は1セッション平均。PF・勝率はラウンドトリップ単位。"
        "max DD は日次累積 PnL (円/100株) の最大ドローダウン。"
    )
    L.append("")
    L.append("## 2. データ")
    L.append("| split | 期間 | 既知銘柄 sessions | 学習外銘柄 sessions | 備考 |")
    L.append("|---|---|---|---|---|")
    for name, (d0, d1) in SPLITS.items():
        if name == "test" and a.no_test:
            continue
        c = ctx["counts"].get(name, (0, 0))
        note = "学習外銘柄は除外" if name == "train" else ""
        L.append(f"| {name} | {d0} .. {d1} | {c[0]:,} | {c[1]:,} | {note} |")
    L.append("")
    L.append(
        "`ok` 状態のキャッシュ済みセッションのみ。学習外銘柄 = md5(stem)[0] % 5 == 0 (約20%)。"
    )
    if ctx.get("exclude_path"):
        L.append(
            f"除外セッション (`{ctx['exclude_path']}`, 行動模倣の教師が売買した日・銘柄、"
            "リーク防止): "
            + ", ".join(f"{k} {v:,}" for k, v in ctx["excluded"].items())
            + " 件を上表の件数から除外済み。"
        )
    L.append("")
    L.append("## 3. 学習")
    tr = ctx["train"]
    pc = tr["ppo_cfg"]
    L.append(
        f"- PPO: hidden {a.hidden}, lr {pc.learning_rate} (線形減衰), gamma {pc.gamma}, GAE λ {pc.gae_lambda}, "
        f"clip {pc.clip_ratio}, entropy {tr['entropy'][0]} -> {tr['entropy'][1]} (線形), value_coef {pc.value_coef}, "
        f"{pc.update_epochs} epochs, minibatch {pc.batch_size}, {a.n_envs} envs × {a.steps_per_env} steps/iter, seed {a.seed}。"
        "gamma=0.997 は約590ステップのエピソードに合わせた (有効ホライズン約330秒)。"
    )
    cur = tr["curriculum"]
    L.append(
        "- 約定ルール・カリキュラム (学習のみ): "
        + (
            f"新エピソードごとに trade_through の確率が {cur[0]} から 1.0 へ最初の {100 * cur[1]:.0f}% のステップで線形に上昇 (それ以外は touch)。"
            if cur
            else "なし (常に trade_through)。"
        )
        + " 検証/テストは常に trade_through (主) と touch (参考)。"
    )
    bc = tr["bc"]
    L.append(
        "- 行動模倣ウォームスタート: "
        + (
            f"{bc['bc_from']} / {bc['bc_steps']} steps / {bc['bc_sessions']} sessions / {bc['bc_samples']:,} samples, "
            f"CE {bc['bc_loss_start']:.3f} → {bc['bc_loss_end']:.3f}, argmax一致率 {100 * bc['bc_accuracy']:.1f}%, "
            f"label smoothing {bc['bc_label_smoothing']}, critic {'あり' if bc['bc_critic'] else 'なし'}, 教師 `{bc['bc_source']}`"
            if bc.get("bc_steps")
            else "なし"
        )
        + "。"
    )
    L.append(
        f"- 実行: {tr['steps']:,} env steps, {tr['iters']} iterations, 学習壁時計 {tr['wall_s'] / 60:.1f} 分, "
        f"**スループット {tr['throughput']:,.0f} steps/s** (更新込み・評価除く、{a.threads} CPU スレッド、"
        f"マスク{'あり' if a.mask else 'なし'}; 評価込み {tr['throughput_incl_eval']:,.0f} steps/s)。"
    )
    L.append(
        f"- チェックポイント選択: 検証 (既知銘柄の固定 {a.val_eval_sessions} セッション, trade_through, 決定論的) の"
        f"セッション平均 yen/100sh 最大。選択 = {tr['best']['steps']:,} steps、保存先 `{a.model}`。"
    )
    L.append("")
    L.append("### 学習曲線 (検証は固定サブセット・決定論的)")
    L.append(
        "| steps | wall min | train ep ticks (raw) | train shaped | entropy | ent coef | p(trade_through) | "
        "val yen/sess (raw) | val ticks/sess | val trips/sess | val orders/sess | val invalid/sess |"
    )
    L.append("|" + "---|" * 12)
    for r in tr["curve"]:
        L.append(
            f"| {r['steps']:,} | {r['wall_s'] / 60:.1f} | {r['train_ep_ticks']:+.1f} | {r['train_shaped_ticks']:+.1f} | "
            f"{r['entropy']:.3f} | {r['ent_coef']:.4f} | {f_num(r['p_tt'])} | "
            f"{r['val_yen']:+.1f} | {r['val_ticks']:+.2f} | {r['val_trips']:.2f} | {r['val_orders']:.1f} | {r['val_invalid']:.2f} |"
        )
    L.append("")
    L.append("## 4. RuleScalper のチューニング (検証・既知銘柄・trade_through)")
    if ctx.get("baselines_cached"):
        L.append(
            f"- ベースライン (チューニング・評価) はキャッシュ `{ctx['baselines_cached']}` から再利用 (同一 order_mode/設定)。"
        )
    if not ctx["tune"]:
        L.append("- スキップ (--skip-baselines): ベースライン結果なし。")
    else:
        meta = ctx["tune"][-1]["meta"]
        L.append(
            f"- グリッド {meta['n_configs']} 通り (mode, k_entry, tp, sl, timeout, requote)。第1段 {ctx['tune'][0]['n1']} セッションで全探索、"
            f"上位 {len(ctx['tune']) - 1} を {ctx['tune'][0]['n2']} セッションで再評価して凍結。"
            f"第1段で yen/sess>0 の設定: {meta['n_positive_stage1']}/{meta['n_configs']}、最良 {meta['best_stage1']:+.1f}、中央値 {meta['median_stage1']:+.1f}。"
        )
        L.append(
            "| chosen | params | stage1 yen/sess | stage2 yen/sess | stage2 ticks/sess | trips/sess |"
        )
        L.append("|---|---|---|---|---|---|")
        for t in ctx["tune"][:-1]:
            L.append(
                f"| {'*' if t['chosen'] else ''} | `{t['params']}` | {t['stage1_yen']:+.1f} | "
                f"{t['stage2_yen']:+.1f} | {t['stage2_ticks']:+.2f} | {t['stage2_trips']:.2f} |"
            )
    if ctx.get("tune_momentum"):
        meta = ctx["tune_momentum"][-1]["meta"]
        L.append("")
        L.append("### Momentum のチューニング (検証・既知銘柄・trade_through)")
        L.append(
            f"- グリッド {meta['n_configs']} 通り (lookback 30/60/120s × 閾値 0/1/2 呼値 × 保有 60/180/590s × "
            f"最早エントリー 10/30/60s)。第1段 {ctx['tune_momentum'][0]['n1']} セッション全探索、"
            f"上位 {len(ctx['tune_momentum']) - 1} を {ctx['tune_momentum'][0]['n2']} セッションで再評価して凍結。"
            f"第1段で yen/sess>0: {meta['n_positive_stage1']}/{meta['n_configs']}、最良 {meta['best_stage1']:+.1f}、"
            f"中央値 {meta['median_stage1']:+.1f}。"
        )
        L.append(
            "| chosen | params | stage1 yen/sess | stage2 yen/sess | stage2 ticks/sess | trips/sess |"
        )
        L.append("|---|---|---|---|---|---|")
        for t in ctx["tune_momentum"][:-1]:
            L.append(
                f"| {'*' if t['chosen'] else ''} | `{t['params']}` | {t['stage1_yen']:+.1f} | "
                f"{t['stage2_yen']:+.1f} | {t['stage2_ticks']:+.2f} | {t['stage2_trips']:.2f} |"
            )
    L.append("")
    L.append("## 5. 結果")
    for group, label in ctx["group_labels"].items():
        L.append(f"### {label}")
        L.extend(metrics_table(ctx["results"][group]))
        L.append("")
    L.append(
        "### trade_through と touch の差 (touch - trade_through, 日次 yen/100sh 平均)"
    )
    L.append("| group | " + " | ".join(ctx["policies"]) + " |")
    L.append("|---|" + "---|" * len(ctx["policies"]))
    for group, label in ctx["group_labels"].items():
        cells = []
        for pol in ctx["policies"]:
            r = ctx["results"][group].get(pol, {})
            if r.get("trade_through", {}).get("n_sessions") and r.get("touch", {}).get(
                "n_sessions"
            ):
                cells.append(
                    f"{r['touch']['yen_day'][0] - r['trade_through']['yen_day'][0]:+,.0f}"
                )
            else:
                cells.append("n/a")
        L.append(f"| {label} | " + " | ".join(cells) + " |")
    L.append("")
    L.append(
        "## 6. PPO の挙動 ("
        + ("検証" if a.no_test else "テスト")
        + "・既知銘柄・trade_through)"
    )
    key = ctx["behaviour_group"]
    s = ctx["results"][key].get("PPO", {}).get("trade_through")
    if s and s.get("n_sessions"):
        L.append(
            f"- 1セッションあたり: 発注 {s['orders_sess']:.1f} 回, ラウンドトリップ {s['trips_sess']:.2f}, "
            f"平均保有 {f_num(s['hold_s'], 1)} 秒, 取引ゼロのセッション {100 * s['no_trade_frac']:.1f}%, "
            f"無効アクション {s['invalid_sess']:.2f} (レイテンシ中の失効 {s['latency_noops_sess']:.2f})。"
        )
        L.extend(action_table(s, mode))
    L.append("")
    L.append("## 7. 自動所見 (事実のみ)")
    L.extend(auto_notes(ctx["results"]) or ["- (該当なし)"])
    L.append("")
    L.append("## 8. 再現コマンド")
    L.append("`" + ctx["cmd"] + "`")
    L.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(L), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Baseline cache (per order mode / tuning setup)
# --------------------------------------------------------------------------- #
def _jsonable(x: Any) -> Any:
    if isinstance(x, np.ndarray):
        return {"__nd__": x.tolist()}
    if isinstance(x, tuple | list):
        return [_jsonable(v) for v in x]
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, np.integer):
        return int(x)
    if isinstance(x, np.floating):
        return float(x)
    return x


def _unjson(x: Any) -> Any:
    if isinstance(x, dict):
        if set(x) == {"__nd__"}:
            return np.asarray(x["__nd__"])
        return {k: _unjson(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_unjson(v) for v in x]
    return x


def _restore_summary(s: dict[str, Any]) -> dict[str, Any]:
    for k in ("yen_day", "ticks_day"):
        if k in s:
            s[k] = tuple(s[k])
    return s


def baseline_key(args: argparse.Namespace) -> str:
    return "|".join(
        str(v)
        for v in (
            args.order_mode,
            args.limit_days,
            args.tune_stage1,
            args.tune_stage2,
            args.tune_top,
            "momentum-v1",  # baseline set changed: Momentum added (old entries differ)
            "no_test" if args.no_test else "with_test",
        )
    ) + (f"|exclude={args.exclude_sessions.name}" if args.exclude_sessions else "")


def load_baseline_cache(path: Path, key: str) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    entry = json.loads(path.read_text(encoding="utf-8")).get("entries", {}).get(key)
    if entry is None:
        return None
    entry = _unjson(entry)
    for by_pol in entry["results"].values():
        for by_rule in by_pol.values():
            for s in by_rule.values():
                _restore_summary(s)
    return entry


def save_baseline_cache(path: Path, key: str, entry: dict[str, Any]) -> None:
    data: dict[str, Any] = {"version": 1, "entries": {}}
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("entries", {})[key] = _jsonable(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def run_baselines(
    args: argparse.Namespace,
    va_seen: list[SessionRow],
    groups: dict[str, tuple[str, list[SessionRow]]],
    counts: dict[str, tuple[int, int]],
) -> dict[str, Any]:
    """Tune RuleScalper on validation, then evaluate DoNothing / Random / tuned rule."""
    best_params, tune_table = tune_rule_scalper(
        va_seen,
        args.tune_stage1,
        args.tune_stage2,
        args.tune_top,
        args.workers,
        args.order_mode,
    )
    log(f"RuleScalper frozen: {best_params}")
    specs = {
        "DoNothing": {"name": "DoNothing"},
        "Random": {"name": "Random"},
        "RuleScalper(tuned)": {"name": "RuleScalper", "params": best_params.as_dict()},
    }
    best_mom: dict[str, Any] | None = None
    tune_mom: list[dict[str, Any]] = []
    if args.order_mode in ("market", "both"):  # Momentum needs market orders
        best_mom, tune_mom = tune_momentum(
            va_seen,
            args.tune_stage1,
            args.tune_stage2,
            args.tune_top,
            args.workers,
            args.order_mode,
        )
        log(f"Momentum frozen: {best_mom}")
        specs["Momentum(tuned)"] = {"name": "Momentum", "params": best_mom}
    results: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
    for gkey, (_label, rows) in groups.items():
        results[gkey] = {}
        if not rows:
            continue
        for pol_name, spec in specs.items():
            results[gkey][pol_name] = {}
            for rule in RULES:
                t0 = time.time()
                eps = eval_baseline_pool(
                    spec, rows, rule, args.workers, args.order_mode
                )
                results[gkey][pol_name][rule] = summarize(eps)
                log(
                    f"{gkey} {pol_name} {rule}: {len(eps)} sess, {time.time() - t0:.0f}s, "
                    f"{results[gkey][pol_name][rule]['yen_sess']:+.1f} yen/sess"
                )
    return {
        "best_params": best_params.as_dict(),
        "best_momentum": best_mom,
        "tune_momentum": tune_mom,
        "tune": tune_table,
        "results": results,
        "counts": counts,
    }


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--train-steps", type=int, default=10_000_000)
    ap.add_argument("--n-envs", type=int, default=32)
    ap.add_argument("--steps-per-env", type=int, default=256)
    ap.add_argument("--batch", type=int, default=1024)
    ap.add_argument("--hidden", type=int, default=128)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--gamma", type=float, default=0.997)
    ap.add_argument(
        "--gae-lambda",
        type=float,
        default=0.95,
        help="GAE lambda; 1.0 = Monte-Carlo returns (a realized PnL reaches the "
        "entry decision discounted only by gamma)",
    )
    ap.add_argument("--entropy", type=float, default=0.02)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--eval-every", type=int, default=500_000)
    ap.add_argument("--val-eval-sessions", type=int, default=1000)
    ap.add_argument("--tune-stage1", type=int, default=300)
    ap.add_argument("--tune-stage2", type=int, default=1500)
    ap.add_argument("--tune-top", type=int, default=10)
    ap.add_argument("--limit-days", type=int, default=0, help="smoke: days per split")
    ap.add_argument(
        "--features",
        default="base",
        metavar="SPEC",
        help="observation feature spec: base, or base+A+C-J (groups A-I add columns, "
        "-J drops tick-activity / imbalance features); see trading_rl.scalp_features",
    )
    ap.add_argument(
        "--no-test",
        action="store_true",
        help="screening mode: never load or evaluate the test split (baselines "
        "included) and omit test sections from the report",
    )
    # --- v2: order mode, masking, shaping, curriculum, entropy, behaviour cloning
    ap.add_argument("--order-mode", choices=ORDER_MODES, default="limit")
    ap.add_argument(
        "--no-action-mask",
        dest="mask",
        action="store_false",
        help="disable legal-action masking (ablation / legacy behaviour)",
    )
    ap.add_argument(
        "--inventory-penalty",
        type=float,
        default=0.0,
        help="training-only reward penalty, ticks per second per unit held",
    )
    ap.add_argument(
        "--idle-penalty",
        type=float,
        default=0.0,
        help="training-only penalty, ticks/s while flat with no working order "
        "(comparison only; NOT recommended: rewards trading for its own sake)",
    )
    ap.add_argument(
        "--reward-mode",
        choices=("mtm", "realized"),
        default="mtm",
        help="training reward base: mtm = per-second change of mark-to-market PnL; "
        "realized = only PnL locked in when a position is closed",
    )
    ap.add_argument(
        "--unrealized-loss-penalty",
        type=float,
        default=0.0,
        help="training-only penalty per second = rate x unrealized loss (ticks) while "
        "a position is open; unrealized gains are not rewarded",
    )
    ap.add_argument(
        "--keep-penalty",
        type=float,
        default=0.0,
        help="training-only penalty, ticks per 'keep' decision (action-based, "
        "ignores position / working orders)",
    )
    ap.add_argument(
        "--action-bonus",
        type=float,
        default=0.0,
        help="training-only reward, ticks per non-keep action that took effect "
        "(action-based, ignores position / working orders)",
    )
    ap.add_argument(
        "--fill-curriculum",
        default=None,
        metavar="P0,FRAC",
        help="P(trade_through) of a new training episode rises P0 -> 1 over the "
        "first FRAC of the steps (others use touch), e.g. 0.0,0.5",
    )
    ap.add_argument("--ent-start", type=float, default=None)
    ap.add_argument("--ent-end", type=float, default=None)
    ap.add_argument(
        "--bc-from",
        default=None,
        metavar="rule|csv:PATH",
        help="behaviour-cloning warm start from the tuned RuleScalper or a trade CSV",
    )
    ap.add_argument("--bc-steps", type=int, default=2000)
    ap.add_argument("--bc-sessions", type=int, default=300)
    ap.add_argument("--bc-lr", type=float, default=1e-3)
    ap.add_argument("--bc-label-smoothing", type=float, default=0.1)
    ap.add_argument("--bc-critic", action="store_true", help="also fit V on MC returns")
    ap.add_argument(
        "--bc-rule-params",
        default=None,
        help='RuleScalperParams as JSON, overrides the tuned rule, e.g. \'{"mode":"long"}\'',
    )
    # --- baselines
    ap.add_argument("--skip-baselines", action="store_true")
    ap.add_argument(
        "--reuse-baselines",
        type=Path,
        default=None,
        metavar="JSON",
        help="baseline cache: reuse the entry for this order mode/setup when present, "
        "else compute and add it (default cache: <report>.baselines.json)",
    )
    ap.add_argument(
        "--exclude-sessions",
        type=Path,
        default=None,
        metavar="CSV",
        help="date,stem pairs removed from every split's evaluation and training rows "
        "(leakage guard, e.g. sessions a BC teacher traded)",
    )
    ap.add_argument("--report", type=Path, default=None)
    ap.add_argument("--model", type=Path, default=None)
    ap.add_argument(
        "--overwrite-model",
        action="store_true",
        help="allow replacing an existing --model",
    )
    return ap


def main() -> None:
    ap = build_parser()
    args = ap.parse_args()
    model_default, report_default = default_paths(args.order_mode)
    if args.smoke:
        args.train_steps = min(args.train_steps, 200_000)
        args.eval_every = min(args.eval_every, 100_000)
        args.val_eval_sessions = min(args.val_eval_sessions, 100)
        args.tune_stage1, args.tune_stage2, args.tune_top = 20, 40, 3
        args.limit_days = args.limit_days or 12
        model_default = SCRATCH / f"smoke-{args.order_mode}-model.pt"
        report_default = SCRATCH / f"smoke-{args.order_mode}-report.md"
    args.report = args.report or report_default
    args.model = args.model or model_default
    if args.model.exists() and not args.overwrite_model:
        raise SystemExit(f"{args.model} exists; pass --overwrite-model to replace it")
    if args.bc_from and args.bc_from != "rule" and not args.bc_from.startswith("csv:"):
        raise SystemExit("--bc-from must be 'rule' or 'csv:<path>'")
    if args.skip_baselines and args.reuse_baselines:
        raise SystemExit("--skip-baselines and --reuse-baselines are exclusive")
    parse_fill_curriculum(args.fill_curriculum)  # validate early
    try:
        args.features = parse_feature_spec(args.features).canonical
    except ValueError as e:
        raise SystemExit(f"--features: {e}") from e

    t_all = time.time()
    ld = args.limit_days or None
    log("building split row lists")
    tr_seen, _ = split_rows("train", ld)
    va_seen, va_held = split_rows("val", ld)
    if args.no_test:  # screening: the test rows are never even listed
        te_seen, te_held = [], []
    else:
        te_seen, te_held = split_rows("test", ld)
    excluded: dict[str, int] = {}
    if args.exclude_sessions:
        ex = load_exclude_sessions(args.exclude_sessions)
        before = {
            "train": len(tr_seen),
            "val": len(va_seen) + len(va_held),
            "test": len(te_seen) + len(te_held),
        }
        tr_seen = drop_sessions(tr_seen, ex)
        va_seen, va_held = drop_sessions(va_seen, ex), drop_sessions(va_held, ex)
        te_seen, te_held = drop_sessions(te_seen, ex), drop_sessions(te_held, ex)
        after = {
            "train": len(tr_seen),
            "val": len(va_seen) + len(va_held),
            "test": len(te_seen) + len(te_held),
        }
        excluded = {k: before[k] - after[k] for k in before}
        log(f"excluded sessions ({args.exclude_sessions}): {excluded}")
    counts = {
        "train": (len(tr_seen), 0),
        "val": (len(va_seen), len(va_held)),
        "test": (len(te_seen), len(te_held)),
    }
    log(f"sessions: {counts}")
    groups = {
        "val_seen": ("検証 (既知銘柄)", va_seen),
        "val_held": ("検証 (学習外銘柄)", va_held),
    }
    if not args.no_test:
        groups["test_seen"] = ("テスト (既知銘柄)", te_seen)
        groups["test_held"] = ("テスト (学習外銘柄)", te_held)
    spec = parse_feature_spec(args.features)
    if any(g in spec.groups for g in "CD"):  # per-date cross-section, cached on disk
        dates = sorted({r[0] for r in tr_seen + va_seen + va_held + te_seen + te_held})
        t0 = time.time()
        store = default_store()
        store.precompute(dates, progress_every=100)
        log(f"context for {len(dates)} dates ready in {time.time() - t0:.0f}s")
    if spec.groups and default_store().warnings:
        for w in default_store().warnings:
            log(f"WARNING feature context: {w}")

    # 1. tune + evaluate the baselines (before touching PPO or test); cached per mode
    base: dict[str, Any] | None = None
    cache_path = args.reuse_baselines or args.report.with_suffix(".baselines.json")
    key = baseline_key(args)
    cached_from = None
    if not args.skip_baselines:
        if args.reuse_baselines:
            base = load_baseline_cache(args.reuse_baselines, key)
            if base is not None:
                cached_from = str(args.reuse_baselines)
                log(f"baselines: reusing cache entry '{key}' from {cached_from}")
        if base is None:
            base = run_baselines(args, va_seen, groups, counts)
            save_baseline_cache(cache_path, key, base)
            log(f"baselines: cached entry '{key}' -> {cache_path}")
    if args.bc_rule_params:
        rule_params: RuleScalperParams | None = rule_params_from_json(
            args.bc_rule_params
        )
    elif base is not None:
        rule_params = rule_params_from_json(json.dumps(base["best_params"]))
    else:
        rule_params = None
        if args.bc_from == "rule":
            log(
                "WARNING: no tuned rule available (--skip-baselines); using default params"
            )

    # 2. PPO
    log(f"loading {len(tr_seen)} train sessions")
    t0 = time.time()
    train_sessions = load_scalp_sessions(
        tr_seen, progress_every=3000, features=args.features
    )
    log(f"loaded {len(train_sessions)} train sessions in {time.time() - t0:.0f}s")
    val_sub = sample_rows(va_seen, args.val_eval_sessions, seed=303)
    val_sessions = load_scalp_sessions(val_sub, features=args.features)
    train = train_ppo(args, train_sessions, val_sessions, rule_params)
    del train_sessions

    # 3. frozen evaluation of PPO (raw PnL, no shaping, masks as trained)
    import torch

    from trading_rl.policy import load_policy_from_checkpoint

    policy, _ckpt = load_policy_from_checkpoint(args.model)
    torch.set_num_threads(args.threads)
    results: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
    for gkey, (_label, rows) in groups.items():
        results[gkey] = dict(base["results"].get(gkey, {})) if base else {}
        if not rows:
            continue
        results[gkey]["PPO"] = {}
        for rule in RULES:
            t0 = time.time()
            eps = eval_ppo_rows(
                policy,
                rows,
                rule,
                order_mode=args.order_mode,
                use_masks=args.mask,
                features=args.features,
            )
            results[gkey]["PPO"][rule] = summarize(eps)
            log(
                f"{gkey} PPO {rule}: {len(eps)} sess, {time.time() - t0:.0f}s, "
                f"{results[gkey]['PPO'][rule]['yen_sess']:+.1f} yen/sess"
            )

    ctx = {
        "counts": counts,
        "excluded": excluded,
        "exclude_path": str(args.exclude_sessions) if args.exclude_sessions else None,
        "train": train,
        "tune": base["tune"] if base else [],
        "tune_momentum": (base.get("tune_momentum") or []) if base else [],
        "results": results,
        "group_labels": {k: v[0] for k, v in groups.items() if results.get(k)},
        "policies": (
            [
                p
                for p in ("DoNothing", "Random", "RuleScalper(tuned)", "Momentum(tuned)")
                if any(p in results[g] for g in results)
            ]
            + ["PPO"]
            if base
            else ["PPO"]
        ),
        "behaviour_group": "val_seen" if args.no_test else "test_seen",
        "args": args,
        "baselines_cached": cached_from,
        "cmd": "uv run python scripts/train_scalp.py " + " ".join(sys.argv[1:]),
    }
    write_report(args.report, ctx)
    summary_path = args.report.with_suffix(".summary.json")
    summary_path.write_text(
        json.dumps(
            _jsonable(
                {
                    "features": args.features,
                    "order_mode": args.order_mode,
                    "obs_dim": obs_dim(args.features),
                    "no_test": bool(args.no_test),
                    "model": str(args.model),
                    "train_steps": train["steps"],
                    "best_step": train["best"].get("steps"),
                    "results": {
                        g: {
                            pol: {
                                rule: {
                                    k: v
                                    for k, v in sm.items()
                                    if k != "actions"
                                }
                                for rule, sm in by_rule.items()
                            }
                            for pol, by_rule in by_pol.items()
                        }
                        for g, by_pol in results.items()
                    },
                }
            )
        ),
        encoding="utf-8",
    )
    curve_path = args.report.with_name(args.report.stem + "-curve.csv")
    with open(curve_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(train["curve"][0].keys()))
        w.writeheader()
        w.writerows(train["curve"])
    log(
        f"report -> {args.report}; curve -> {curve_path}; "
        f"total {(time.time() - t_all) / 60:.1f} min"
    )


if __name__ == "__main__":
    main()

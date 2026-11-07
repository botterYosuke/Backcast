#!/usr/bin/env python3
"""P2 predictability gate: can simple models beat 1 tick of cost out of sample?

    uv run python scripts/predictability_gate.py                 # full range
    uv run python scripts/predictability_gate.py --dry-run       # tiny range

Features are causal 1-second-grid features (``trading_rl.gate_features``); models
are logistic regression and a small gradient-boosting classifier per horizon;
the economic test enters at most one position per (stem, day), holds exactly h
seconds and pays 0.5 tick on each side.  Threshold and model are chosen on the
validation period (seen stems) only and frozen for test (seen and held-out
stems).  Output: console summary + a markdown report generated from measured
values only.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

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

from sklearn.ensemble import HistGradientBoostingClassifier  # noqa: E402
from sklearn.inspection import permutation_importance  # noqa: E402
from sklearn.linear_model import LogisticRegression  # noqa: E402
from sklearn.metrics import brier_score_loss, roc_auc_score  # noqa: E402
from sklearn.pipeline import make_pipeline  # noqa: E402
from sklearn.preprocessing import StandardScaler  # noqa: E402

from trading_rl.cache import CachedSessionUnavailable, load_cached_session  # noqa: E402
from trading_rl.gate_features import (  # noqa: E402
    FEATURE_NAMES,
    HORIZONS,
    START_S,
    WINDOW_S,
    compute_session_grid,
    default_grid,
)
from trading_rl.universe import (  # noqa: E402
    DEFAULT_CACHE_DIR,
    load_universe,
    tick_sizes,
)

DEFAULT_REPORT = REPO_ROOT / ".agents/docs/research/rl-top50-predictability-gate.md"
THETAS = (0.0, 0.01, 0.02, 0.03, 0.04, 0.05, 0.075, 0.10, 0.15, 0.20, 0.30)
MIN_VAL_TRADES = 200
N_BOOT = 2000
MODELS = ("logreg", "gbm")
T100_COL = FEATURE_NAMES.index("is_topix100")
GRID = default_grid()
ROWS_PER_SESSION = len(GRID)


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def is_heldout(stem: str) -> bool:
    """Deterministic ~20% symbol holdout from a hash of the stem."""
    return hashlib.md5(stem.lower().encode()).digest()[0] % 5 == 0


# --------------------------------------------------------------------------- #
# Dataset
# --------------------------------------------------------------------------- #
@dataclass
class Block:
    """Concatenated grid rows of many sessions (``stride=1`` blocks are regular)."""

    X: np.ndarray
    price: np.ndarray
    tick: np.ndarray
    fwd_price: np.ndarray  # (n, H)
    fwd_sign: np.ndarray  # (n, H) int8
    valid: np.ndarray  # (n, H) bool
    sess: np.ndarray  # (n,) int32 session index
    n_sess: int
    sess_day: np.ndarray  # (n_sess,) int32 day index (within block)
    sess_held: np.ndarray  # (n_sess,) bool
    sess_stem: list[str] = field(default_factory=list)
    sess_date: list[str] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.price)


@dataclass
class SplitInfo:
    name: str
    start: str
    end: str
    universe_rows: int = 0
    excluded_heldout: int = 0
    status: Counter = field(default_factory=Counter)
    n_ok: int = 0
    n_ok_heldout: int = 0
    days: set = field(default_factory=set)
    stems: set = field(default_factory=set)
    stems_heldout: set = field(default_factory=set)
    rows: int = 0


def _status_of(exc: CachedSessionUnavailable) -> str:
    m = str(exc)
    if "status=" in m:
        return m.split("status=", 1)[1].split()[0]
    return "not_cached"


def build_block(
    info: SplitInfo,
    *,
    include_heldout: bool,
    stride: int,
    rng: np.random.Generator,
    cache_dir: Path,
    day_frac: float = 1.0,
) -> Block:
    rows = load_universe(cache_dir, info.start, info.end)
    if day_frac < 1.0:
        days = sorted({str(r["date"]) for r in rows})
        keep = set(rng.choice(days, size=max(1, int(len(days) * day_frac)), replace=False))
        rows = [r for r in rows if str(r["date"]) in keep]
    parts: dict[str, list[np.ndarray]] = {
        k: [] for k in ("X", "price", "tick", "fwd_price", "fwd_sign", "valid")
    }
    sess_idx: list[np.ndarray] = []
    sess_day: list[int] = []
    sess_held: list[bool] = []
    sess_stem: list[str] = []
    sess_date: list[str] = []
    day_index: dict[str, int] = {}
    t0 = time.time()
    for i, r in enumerate(rows):
        stem = str(r["stem"])
        held = is_heldout(stem)
        if held and not include_heldout:
            info.excluded_heldout += 1
            continue
        info.universe_rows += 1
        date = str(r["date"])
        try:
            sess = load_cached_session(date, stem, cache_dir)
        except CachedSessionUnavailable as exc:
            info.status[_status_of(exc)] += 1
            continue
        if stride > 1:
            off = int(rng.integers(0, stride))
            s_grid = GRID[off::stride]
        else:
            s_grid = GRID
        try:
            g = compute_session_grid(
                sess,
                prev_close=float(r["prev_close"] or 0.0),
                avg_volume_20d=float(r["avg_volume_20d"] or 0.0),
                is_topix100=bool(r["is_topix100"]),
                s_grid=s_grid,
            )
        except Exception:  # noqa: BLE001
            info.status["compute_error"] += 1
            continue
        k = len(sess_day)
        parts["X"].append(g.X)
        parts["price"].append(g.price)
        parts["tick"].append(g.tick)
        parts["fwd_price"].append(g.fwd_price)
        parts["fwd_sign"].append(g.fwd_sign)
        parts["valid"].append(g.valid)
        sess_idx.append(np.full(len(g.s), k, dtype=np.int32))
        sess_day.append(day_index.setdefault(date, len(day_index)))
        sess_held.append(held)
        sess_stem.append(stem)
        sess_date.append(date)
        info.n_ok += 1
        info.n_ok_heldout += int(held)
        info.days.add(date)
        info.stems.add(stem)
        if held:
            info.stems_heldout.add(stem)
        if (i + 1) % 2000 == 0:
            log(f"  {info.name}: {i + 1}/{len(rows)} rows, {info.n_ok} ok, {time.time() - t0:.0f}s")
    if not sess_day:
        raise SystemExit(f"split {info.name}: no usable sessions in {info.start}..{info.end}")
    blk = Block(
        X=np.concatenate(parts["X"]),
        price=np.concatenate(parts["price"]),
        tick=np.concatenate(parts["tick"]),
        fwd_price=np.concatenate(parts["fwd_price"]),
        fwd_sign=np.concatenate(parts["fwd_sign"]),
        valid=np.concatenate(parts["valid"]),
        sess=np.concatenate(sess_idx),
        n_sess=len(sess_day),
        sess_day=np.asarray(sess_day, dtype=np.int32),
        sess_held=np.asarray(sess_held, dtype=bool),
        sess_stem=sess_stem,
        sess_date=sess_date,
    )
    info.rows = blk.n
    log(f"{info.name}: {info.n_ok} sessions, {blk.n:,} rows, {time.time() - t0:.0f}s")
    return blk


# --------------------------------------------------------------------------- #
# Models and classification metrics
# --------------------------------------------------------------------------- #
def make_model(name: str):
    if name == "logreg":
        return make_pipeline(StandardScaler(), LogisticRegression(C=1.0, max_iter=300))
    return HistGradientBoostingClassifier(
        max_iter=200,
        learning_rate=0.05,
        max_leaf_nodes=31,
        min_samples_leaf=500,
        l2_regularization=1.0,
        early_stopping=False,
        random_state=0,
    )


def fit_model(name: str, blk: Block, j: int, max_rows: int, rng: np.random.Generator):
    m = blk.valid[:, j] & (blk.fwd_sign[:, j] != 0)
    idx = np.flatnonzero(m)
    if len(idx) > max_rows:
        idx = np.sort(rng.choice(idx, size=max_rows, replace=False))
    model = make_model(name)
    model.fit(blk.X[idx], (blk.fwd_sign[idx, j] > 0).astype(np.int8))
    return model, len(idx)


def predict_up(model, blk: Block, j: int) -> np.ndarray:
    p = np.full(blk.n, np.nan, dtype=np.float32)
    m = blk.valid[:, j]
    p[m] = model.predict_proba(blk.X[m])[:, 1]
    return p


def cls_metrics(blk: Block, p: np.ndarray, j: int, sess_mask: np.ndarray) -> dict:
    row_mask = sess_mask[blk.sess] & blk.valid[:, j] & (blk.fwd_sign[:, j] != 0)
    y = (blk.fwd_sign[row_mask, j] > 0).astype(np.int8)
    q = p[row_mask].astype(np.float64)
    n_all = int((sess_mask[blk.sess] & blk.valid[:, j]).sum())
    if len(y) == 0 or y.min() == y.max():
        return {"n": len(y), "tie_frac": float("nan")}
    base = float(y.mean())
    return {
        "n": int(len(y)),
        "tie_frac": 1.0 - len(y) / max(n_all, 1),
        "auc": float(roc_auc_score(y, q)),
        "acc": float(((q > 0.5) == (y == 1)).mean()),
        "base": max(base, 1 - base),
        "up_rate": base,
        "brier": float(brier_score_loss(y, q)),
        "brier_const": float(brier_score_loss(y, np.full_like(q, base))),
    }


# --------------------------------------------------------------------------- #
# Economic simulation
# --------------------------------------------------------------------------- #
@dataclass
class Trades:
    sess: np.ndarray
    net_bps: np.ndarray
    net_ticks: np.ndarray
    entry_row: np.ndarray
    direction: np.ndarray


def schedule_trades(
    blk: Block, j: int, h: int, p: np.ndarray, theta: float, sess_mask: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Greedy one-position-at-a-time entries; returns (entry_rows, direction)."""
    n = blk.n
    assert n == blk.n_sess * ROWS_PER_SESSION, "schedule needs full-grid blocks"
    sig = np.zeros(n, dtype=np.int8)
    with np.errstate(invalid="ignore"):
        sig[p > 0.5 + theta] = 1
        sig[p < 0.5 - theta] = -1
    sig[~blk.valid[:, j]] = 0
    sig[~sess_mask[blk.sess]] = 0
    cand = np.where(sig != 0, np.arange(n), n)
    nxt = np.concatenate([np.minimum.accumulate(cand[::-1])[::-1], np.full(h + 2, n)])
    n_valid = WINDOW_S - h - START_S + 1
    cur = np.flatnonzero(sess_mask) * ROWS_PER_SESSION
    end = cur + n_valid
    out: list[np.ndarray] = []
    while cur.size:
        e = nxt[cur]
        ok = e < end
        e = e[ok]
        out.append(e)
        cur = e + h
        end = end[ok]
    rows = np.concatenate(out) if out else np.zeros(0, dtype=np.int64)
    rows = np.sort(rows)
    return rows, sig[rows]


def pnl_of(blk: Block, j: int, rows: np.ndarray, direction: np.ndarray) -> Trades:
    pe = blk.price[rows]
    px = blk.fwd_price[rows, j]
    tk_e = blk.tick[rows]
    tk_x = tick_sizes(px, blk.X[rows, T100_COL] > 0.5)
    net = direction * (px - pe) - 0.5 * tk_e - 0.5 * tk_x
    return Trades(
        sess=blk.sess[rows],
        net_bps=net / pe * 1e4,
        net_ticks=net / tk_e,
        entry_row=rows,
        direction=direction,
    )


def summarize(
    blk: Block, tr: Trades, sess_mask: np.ndarray, rng: np.random.Generator
) -> dict:
    days_of = blk.sess_day[sess_mask]
    uniq, inv = np.unique(days_of, return_inverse=True)
    remap = np.full(blk.n_sess, -1, dtype=np.int64)
    remap[np.flatnonzero(sess_mask)] = inv
    nd = len(uniq)
    d_idx = remap[tr.sess]
    daily = np.bincount(d_idx, weights=tr.net_bps, minlength=nd)
    daily_n = np.bincount(d_idx, minlength=nd).astype(np.float64)
    boot = rng.integers(0, nd, size=(N_BOOT, nd))
    bm = daily[boot].mean(axis=1)
    sn = daily_n[boot].sum(axis=1)
    per_trade = np.where(sn > 0, daily[boot].sum(axis=1) / np.maximum(sn, 1), 0.0)
    nt = len(tr.net_bps)
    return {
        "n_days": nd,
        "n_trades": nt,
        "trades_per_day": nt / nd,
        "bps_per_trade": float(tr.net_bps.mean()) if nt else 0.0,
        "ticks_per_trade": float(tr.net_ticks.mean()) if nt else 0.0,
        "win_rate": float((tr.net_ticks > 0).mean()) if nt else 0.0,
        "daily_mean_bps": float(daily.mean()),
        "daily_ci": (float(np.percentile(bm, 2.5)), float(np.percentile(bm, 97.5))),
        "per_trade_ci": (
            float(np.percentile(per_trade, 2.5)),
            float(np.percentile(per_trade, 97.5)),
        ),
        "daily_mean_ticks": float(
            np.bincount(d_idx, weights=tr.net_ticks, minlength=nd).mean()
        ),
    }


def eval_schedule(
    blk: Block,
    j: int,
    h: int,
    p: np.ndarray,
    theta: float,
    sess_mask: np.ndarray,
    rng: np.random.Generator,
    baselines: bool = False,
) -> dict:
    rows, d = schedule_trades(blk, j, h, p, theta, sess_mask)
    res = summarize(blk, pnl_of(blk, j, rows, d), sess_mask, rng)
    if baselines:
        res["always_long"] = summarize(
            blk, pnl_of(blk, j, rows, np.ones_like(d)), sess_mask, rng
        )
        rd = rng.choice(np.array([-1, 1], dtype=np.int8), size=len(rows))
        res["random_dir"] = summarize(blk, pnl_of(blk, j, rows, rd), sess_mask, rng)
    return res


# --------------------------------------------------------------------------- #
# Main study
# --------------------------------------------------------------------------- #
def run_study(args: argparse.Namespace) -> dict:
    rng = np.random.default_rng(args.seed)
    cache_dir = Path(args.cache_dir)
    t_start = time.time()
    infos = {
        "train": SplitInfo("train", args.train_start, args.train_end),
        "val": SplitInfo("val", args.val_start, args.val_end),
        "test": SplitInfo("test", args.test_start, args.test_end),
    }
    log("building train block (seen stems, strided grid)")
    train = build_block(
        infos["train"], include_heldout=False, stride=args.train_stride, rng=rng,
        cache_dir=cache_dir, day_frac=args.train_day_frac,
    )
    log("building validation block (seen stems, full grid)")
    val = build_block(
        infos["val"], include_heldout=False, stride=1, rng=rng, cache_dir=cache_dir
    )
    log("building test block (all stems, full grid)")
    test = build_block(
        infos["test"], include_heldout=True, stride=1, rng=rng, cache_dir=cache_dir
    )
    t_data = time.time() - t_start

    val_all = np.ones(val.n_sess, dtype=bool)
    test_seen = ~test.sess_held
    test_held = test.sess_held.copy()

    results: dict = {"per_h": {}, "t_data": t_data}
    for j, h in enumerate(HORIZONS):
        t_h = time.time()
        log(f"horizon {h}s: fitting")
        per_model: dict = {}
        for name in MODELS:
            t_m = time.time()
            model, n_fit = fit_model(name, train, j, args.max_train_rows, rng)
            pv = predict_up(model, val, j)
            pt = predict_up(model, test, j)
            entry: dict = {"n_fit": n_fit, "model": model}
            entry["cls"] = {
                "val_seen": cls_metrics(val, pv, j, val_all),
                "test_seen": cls_metrics(test, pt, j, test_seen),
                "test_heldout": cls_metrics(test, pt, j, test_held),
            }
            val_curve = {}
            for th in THETAS:
                val_curve[th] = eval_schedule(val, j, h, pv, th, val_all, rng)
            entry["val_curve"] = val_curve
            eligible = [th for th in THETAS if val_curve[th]["n_trades"] >= MIN_VAL_TRADES]
            pool = eligible or list(THETAS)
            best_th = max(pool, key=lambda th: val_curve[th]["daily_mean_bps"])
            entry["theta"] = best_th
            entry["val"] = val_curve[best_th]
            entry["test_seen"] = eval_schedule(
                test, j, h, pt, best_th, test_seen, rng, baselines=True
            )
            entry["test_heldout"] = eval_schedule(
                test, j, h, pt, best_th, test_held, rng, baselines=True
            )
            per_model[name] = entry
            log(
                f"  {name}: n_fit={n_fit:,} theta={best_th} "
                f"val {entry['val']['daily_mean_bps']:+.2f} bps/day "
                f"test seen {entry['test_seen']['daily_mean_bps']:+.2f} "
                f"held {entry['test_heldout']['daily_mean_bps']:+.2f} "
                f"({time.time() - t_m:.0f}s)"
            )
        chosen = max(MODELS, key=lambda m: per_model[m]["val"]["daily_mean_bps"])
        ts, th_ = per_model[chosen]["test_seen"], per_model[chosen]["test_heldout"]
        passed = ts["daily_ci"][0] > 0 and th_["daily_ci"][0] > 0
        # GBM permutation importance on validation (small sample)
        vm = np.flatnonzero(val.valid[:, j] & (val.fwd_sign[:, j] != 0))
        sub = np.sort(rng.choice(vm, size=min(args.perm_rows, len(vm)), replace=False))
        pi = permutation_importance(
            per_model["gbm"]["model"],
            val.X[sub],
            (val.fwd_sign[sub, j] > 0).astype(np.int8),
            scoring="roc_auc",
            n_repeats=3,
            random_state=0,
        )
        order = np.argsort(-pi.importances_mean)
        importance = [
            (FEATURE_NAMES[i], float(pi.importances_mean[i]), float(pi.importances_std[i]))
            for i in order
        ]
        # bid-ask-bounce diagnostic on test (seen): short-term reversal sign
        m = test_seen[test.sess] & test.valid[:, j]
        sel = np.flatnonzero(m)
        sel = sel[:: max(1, len(sel) // 500_000)]
        fwd_t = (test.fwd_price[sel, j] - test.price[sel]) / test.tick[sel]
        k1 = FEATURE_NAMES.index("ret_tk_1s")
        k5 = FEATURE_NAMES.index("ret_tk_5s")
        bounce = {
            "corr_ret1s_fwd": float(np.corrcoef(test.X[sel, k1], fwd_t)[0, 1]),
            "corr_ret5s_fwd": float(np.corrcoef(test.X[sel, k5], fwd_t)[0, 1]),
        }
        results["per_h"][h] = {
            "models": per_model,
            "chosen": chosen,
            "pass": bool(passed),
            "importance": importance,
            "bounce": bounce,
        }
        log(f"horizon {h}s: chosen={chosen} verdict={'PASS' if passed else 'FAIL'} ({time.time() - t_h:.0f}s)")
    results["infos"] = infos
    results["train_rows"] = train.n
    results["train_sessions"] = train.n_sess
    results["runtime_s"] = time.time() - t_start
    results["n_test_days"] = len(infos["test"].days)
    return results


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
def _ci(ci: tuple[float, float]) -> str:
    return f"[{ci[0]:+.1f}, {ci[1]:+.1f}]"


def write_report(res: dict, args: argparse.Namespace, path: Path) -> str:
    L: list[str] = []
    w = L.append
    infos: dict[str, SplitInfo] = res["infos"]
    mode = "DRY RUN (reduced date range, not the gate result)" if args.dry_run else "full run"
    overall = all(res["per_h"][h]["pass"] for h in HORIZONS)
    w("# Top-50 opening-window predictability gate (P2)")
    w("")
    w(f"- Run: {time.strftime('%Y-%m-%d %H:%M')} / mode: **{mode}** / runtime {res['runtime_s'] / 60:.1f} min "
      f"(data+features {res['t_data'] / 60:.1f} min)")
    w(f"- Command args: seed={args.seed}, train stride={args.train_stride} s, "
      f"max train rows={args.max_train_rows:,}, train day fraction={args.train_day_frac}, "
      f"bootstrap reps={N_BOOT}")
    w("")
    w("## Verdict")
    w("")
    for h in HORIZONS:
        r = res["per_h"][h]
        c = r["models"][r["chosen"]]
        w(f"- h={h}s: **{'PASS' if r['pass'] else 'FAIL'}** (model {r['chosen']}, theta={c['theta']}; "
          f"test seen daily mean {c['test_seen']['daily_mean_bps']:+.1f} bps CI {_ci(c['test_seen']['daily_ci'])}, "
          f"held-out {c['test_heldout']['daily_mean_bps']:+.1f} bps CI {_ci(c['test_heldout']['daily_ci'])})")
    w("")
    flags = ", ".join(f"{h}s={'P' if res['per_h'][h]['pass'] else 'F'}" for h in HORIZONS)
    w(f"Overall: **{'PASS on all horizons' if overall else 'not all horizons pass'}** ({flags}). "
      "PASS rule: test mean daily PnL (bps, 1-tick round-trip cost deducted) 95% day-bootstrap CI "
      "lower bound > 0 for BOTH seen and held-out stems, with the model and theta chosen on validation only.")
    w("")
    w("## Data coverage")
    w("")
    w("| split | range | universe rows used | ok sessions | days | stems | skipped by status |")
    w("|---|---|---|---|---|---|---|")
    for k in ("train", "val", "test"):
        i = infos[k]
        skipped = ", ".join(f"{s}={n}" for s, n in sorted(i.status.items())) or "none"
        extra = f" (+{i.excluded_heldout} held-out-stem rows excluded)" if i.excluded_heldout else ""
        w(f"| {k} | {i.start}..{i.end} | {i.universe_rows}{extra} | {i.n_ok} | {len(i.days)} | "
          f"{len(i.stems)} | {skipped} |")
    t = infos["test"]
    w("")
    w(f"- Held-out stems (md5(stem)[0] % 5 == 0, excluded from train/val): "
      f"{len(t.stems_heldout)} of {len(t.stems)} stems seen in test; test sessions: "
      f"seen={t.n_ok - t.n_ok_heldout}, held-out={t.n_ok_heldout}.")
    w(f"- Train grid: {res['train_sessions']} sessions subsampled every {args.train_stride} s "
      f"-> {res['train_rows']:,} rows (models fit on at most {args.max_train_rows:,} non-tie rows per horizon). "
      "Validation and test use the full 1 s grid.")
    w("")
    w("## Features (all causal; ticks <= t plus pre-open universe columns)")
    w("")
    w(", ".join(f"`{n}`" for n in FEATURE_NAMES) + f" ({len(FEATURE_NAMES)} features; decision grid 1 s from "
      f"T_open+{START_S}s to T_open+{WINDOW_S}s-h; targets: last-trade forward return over h in bps/ticks and its sign. "
      "Tie (zero-return) rows are dropped from classifier training and AUC/accuracy/Brier but "
      "kept in the economic test).")
    w("")
    w("## Results per horizon and model")
    w("")
    w("Classification on non-tie rows (test, seen / held-out stems):")
    w("")
    w("| h | model | AUC seen | AUC held | acc seen | base seen | acc held | base held | Brier seen (const) | tie frac |")
    w("|---|---|---|---|---|---|---|---|---|---|")
    for h in HORIZONS:
        for m in MODELS:
            c = res["per_h"][h]["models"][m]["cls"]
            s_, o_ = c["test_seen"], c["test_heldout"]
            w(f"| {h} | {m} | {s_.get('auc', float('nan')):.4f} | {o_.get('auc', float('nan')):.4f} | "
              f"{s_.get('acc', float('nan')):.4f} | {s_.get('base', float('nan')):.4f} | "
              f"{o_.get('acc', float('nan')):.4f} | {o_.get('base', float('nan')):.4f} | "
              f"{s_.get('brier', float('nan')):.4f} ({s_.get('brier_const', float('nan')):.4f}) | "
              f"{s_.get('tie_frac', float('nan')):.3f} |")
    w("")
    w("Economics (one position per stem-day, hold h s, 1-tick round trip; theta from validation; daily = sum over "
      "that day's trades of the group, mean over test days, 95% CI by day bootstrap):")
    w("")
    w("| h | model | theta | group | trades/day | bps/trade | ticks/trade | win rate | daily mean bps | CI | "
      "always-long daily bps (CI) | random-dir daily bps (CI) |")
    w("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for h in HORIZONS:
        for m in MODELS:
            e = res["per_h"][h]["models"][m]
            star = "*" if m == res["per_h"][h]["chosen"] else ""
            for grp in ("test_seen", "test_heldout"):
                r = e[grp]
                w(f"| {h} | {m}{star} | {e['theta']} | {grp.replace('test_', '')} | {r['trades_per_day']:.1f} | "
                  f"{r['bps_per_trade']:+.2f} | {r['ticks_per_trade']:+.3f} | {r['win_rate']:.3f} | "
                  f"{r['daily_mean_bps']:+.1f} | {_ci(r['daily_ci'])} | "
                  f"{r['always_long']['daily_mean_bps']:+.1f} {_ci(r['always_long']['daily_ci'])} | "
                  f"{r['random_dir']['daily_mean_bps']:+.1f} {_ci(r['random_dir']['daily_ci'])} |")
    w("")
    w("`*` = model chosen on validation daily-mean bps (seen stems). Validation figures at the chosen theta:")
    w("")
    w("| h | model | theta | val trades/day | val bps/trade | val daily mean bps | val AUC | val acc (base) |")
    w("|---|---|---|---|---|---|---|---|")
    for h in HORIZONS:
        for m in MODELS:
            e = res["per_h"][h]["models"][m]
            v = e["val"]
            vc = e["cls"]["val_seen"]
            w(f"| {h} | {m} | {e['theta']} | {v['trades_per_day']:.1f} | {v['bps_per_trade']:+.2f} | "
              f"{v['daily_mean_bps']:+.1f} | {vc.get('auc', float('nan')):.4f} | "
              f"{vc.get('acc', float('nan')):.4f} ({vc.get('base', float('nan')):.4f}) |")
    w("")
    w("Per-trade bps 95% CI (day bootstrap, chosen model):")
    w("")
    for h in HORIZONS:
        r = res["per_h"][h]
        e = r["models"][r["chosen"]]
        w(f"- h={h}s {r['chosen']}: seen {e['test_seen']['bps_per_trade']:+.2f} bps/trade "
          f"{_ci(e['test_seen']['per_trade_ci'])}, held-out {e['test_heldout']['bps_per_trade']:+.2f} "
          f"{_ci(e['test_heldout']['per_trade_ci'])}; test days={e['test_seen']['n_days']}")
    w("")
    w("## GBM permutation importance (validation sample, AUC drop, top 10)")
    w("")
    for h in HORIZONS:
        imp = res["per_h"][h]["importance"][:10]
        w(f"- h={h}s: " + ", ".join(f"`{n}` {v:.4f}" for n, v, _ in imp))
    w("")
    w("## Bid-ask bounce caveat")
    w("")
    w("Targets are last-trade prices, not mid quotes, so they include bid-ask bounce: a trade at the ask "
      "followed by one at the bid shows up as a one-tick reversal. Short-lag reversal features "
      "(`ret_*_1s`, `ret_*_5s`, imbalance) can therefore look more predictive than a real, executable "
      "strategy would be. The 1-tick round-trip cost (0.5 tick per side) removes the average bounce on "
      "the trades taken but does not remove the selection effect (signals fire right after a bounce), "
      "and entries are assumed to fill at the last trade price +/- 0.5 tick with no queue or partial-fill "
      "risk. Measured test (seen) correlation between past return in ticks and forward return in ticks: "
      + "; ".join(
          f"h={h}s: 1s-lag {res['per_h'][h]['bounce']['corr_ret1s_fwd']:+.4f}, "
          f"5s-lag {res['per_h'][h]['bounce']['corr_ret5s_fwd']:+.4f}" for h in HORIZONS
      ) + " (negative = reversal).")
    w("")
    text = "\n".join(L) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return text


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    ap.add_argument("--train-start", default="2024-01-04")
    ap.add_argument("--train-end", default="2025-06-30")
    ap.add_argument("--val-start", default="2025-07-01")
    ap.add_argument("--val-end", default="2025-12-31")
    ap.add_argument("--test-start", default="2026-01-01")
    ap.add_argument("--test-end", default="2026-08-19")
    ap.add_argument("--train-stride", type=int, default=5)
    ap.add_argument("--train-day-frac", type=float, default=1.0)
    ap.add_argument("--max-train-rows", type=int, default=2_500_000)
    ap.add_argument("--perm-rows", type=int, default=30_000)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true",
                    help="2026-07-01..2026-08-19 only, split 07-01..07-17 / 07-18..07-31 / 08-03..08-19")
    ap.add_argument("--report", default=None)
    args = ap.parse_args()
    if args.dry_run:
        args.train_start, args.train_end = "2026-07-01", "2026-07-17"
        args.val_start, args.val_end = "2026-07-18", "2026-07-31"
        args.test_start, args.test_end = "2026-08-03", "2026-08-19"
        args.train_stride = min(args.train_stride, 5)
    report = Path(args.report) if args.report else (
        DEFAULT_REPORT.with_name("rl-top50-predictability-gate-dryrun.md") if args.dry_run else DEFAULT_REPORT
    )
    res = run_study(args)
    text = write_report(res, args, report)
    print(text)
    log(f"report written: {report}  runtime {res['runtime_s'] / 60:.1f} min")


if __name__ == "__main__":
    main()

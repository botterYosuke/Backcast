#!/usr/bin/env python3
"""Coverage (val) and dead-column sanity (train) report for feature groups K-Q.

    uv run python scripts/ext_feature_report.py --out report.json

Coverage: per group, share of VAL sessions with the ``*_known`` flag set (at the first
and the last decision second).  Sanity: on TRAIN sessions only, per new column the
share of non-zero values, the std, and the Pearson correlation with the stock's next
60 s return at a few decision seconds -- only to catch dead / constant / broken
columns, NOT for feature selection.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from trading_rl.scalp_env import load_scalp_session  # noqa: E402
from trading_rl.scalp_features import EXTRA_NAMES, market_names  # noqa: E402
from trading_rl.universe import DEFAULT_CACHE_DIR, load_universe  # noqa: E402

SPEC = "base+K+L+M+N+O+P+Q"
SPLITS = {
    "train": ("2024-01-04", "2025-06-30"),
    "val": ("2025-07-01", "2025-12-31"),
}
KNOWN = {
    "K": ["k_known_1570", "k_known_1321", "k_known_1306", "k_beta_known"],
    "L": ["l_known"],
    "M": ["m_covered", "m_rev_known", "m_surprise_known"],
    "N": ["n_known", "n_beta_known"],
    "O": ["o_known"],
    "P": ["p_known"],
}
NEW = [n for g in "KLMNOPQ" for n in EXTRA_NAMES[g]]


def sessions(split: str, every: int):
    rows = load_universe(DEFAULT_CACHE_DIR, *SPLITS[split])
    dates = sorted({str(r["date"]) for r in rows})
    keep = set(dates[::every])
    for r in rows:
        if str(r["date"]) in keep:
            yield (str(r["date"]), str(r["stem"]), float(r["prev_close"]),
                   float(r["avg_volume_20d"]), bool(r["is_topix100"]))  # fmt: skip


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--val-every", type=int, default=1)
    ap.add_argument("--train-every", type=int, default=4)
    args = ap.parse_args()
    names = market_names(SPEC)
    idx = {n: names.index(n) for n in NEW}
    res: dict = {"spec": SPEC, "n_market": len(names)}

    # coverage on val
    cov: dict[str, list[float]] = {k: [0, 0, 0] for g in KNOWN.values() for k in g}
    nval = 0
    extra = {"m_any_disc": 0, "m_earn_disc": 0, "m_earn_due_today": 0}
    for row in sessions("val", args.val_every):
        try:
            s = load_scalp_session(row, DEFAULT_CACHE_DIR, SPEC)
        except Exception:  # noqa: BLE001
            continue
        nval += 1
        for k in cov:
            col = s.X[:, idx[k]]
            cov[k][0] += col[0] > 0
            cov[k][1] += col[-1] > 0
            cov[k][2] += (col > 0).any()
        extra["m_any_disc"] += s.X[0, idx["m_log1p_n_disc"]] > 0
        extra["m_earn_disc"] += s.X[0, idx["m_earn_disc"]] > 0
        extra["m_earn_due_today"] += s.X[0, idx["m_earn_due_today"]] > 0
    res["val_sessions"] = nval
    res["coverage"] = {k: {"first": v[0] / nval, "last": v[1] / nval, "ever": v[2] / nval}
                       for k, v in cov.items()}  # fmt: skip
    res["m_rates"] = {k: v / nval for k, v in extra.items()}

    # sanity on train
    sec = (30, 120, 300)
    xs = {s: [] for s in sec}
    ys = {s: [] for s in sec}
    nz = np.zeros(len(NEW))
    mx = np.zeros(len(NEW))
    mn = np.zeros(len(NEW))
    ntr = 0
    for row in sessions("train", args.train_every):
        try:
            s = load_scalp_session(row, DEFAULT_CACHE_DIR, SPEC)
        except Exception:  # noqa: BLE001
            continue
        ntr += 1
        X = s.X[:, [idx[n] for n in NEW]]
        nz += (X != 0).mean(axis=0)
        mx = np.maximum(mx, X.max(axis=0))
        mn = np.minimum(mn, X.min(axis=0))
        for sc in sec:
            xs[sc].append(X[sc - 10])
            ys[sc].append(s.last[sc + 60] / s.last[sc] - 1.0)
    res["train_sessions"] = ntr
    cols = {}
    for j, n in enumerate(NEW):
        d = {"nonzero_share": float(nz[j] / ntr), "min": float(mn[j]), "max": float(mx[j])}
        for sc in sec:
            x = np.array([r[j] for r in xs[sc]], dtype=np.float64)
            y = np.array(ys[sc], dtype=np.float64)
            ok = np.isfinite(x) & np.isfinite(y)
            d[f"std@{sc}"] = float(x[ok].std()) if ok.any() else 0.0
            d[f"corr@{sc}"] = (
                float(np.corrcoef(x[ok], y[ok])[0, 1]) if ok.any() and x[ok].std() > 0 and y[ok].std() > 0 else None
            )
        cols[n] = d
    res["columns"] = cols
    Path(args.out).write_text(json.dumps(res, indent=1), encoding="utf-8")
    print("wrote", args.out, "val", nval, "train", ntr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

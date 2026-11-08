"""Stage 2 (plan Q6): short-horizon predictability on the whole morning session.

Features the agent could see live (kabu PUSH-like tape, ``obs_tape="push20"``):
  base  = the 28 gate features (trading_rl.gate_features) on the push tape
  micro = the 11 trades-only fill-model features (trading_rl.fill_model) on the push tape
Targets: (last[s + h] - last[s]) / tick[s] from every print (the true price path).

Train: known symbols, train split (2024-01..2025-06), N_TRAIN sessions, every STRIDE_TRAIN s.
Val: val split (2025-07..12) incl. held-out symbols, N_VAL sessions, every STRIDE_VAL s.
IC = mean over days of the Pearson correlation of prediction and target pooled over
that day's (symbol, second) rows (same definition as the opening-window probes).

Run: uv run python -W ignore scripts/probes/predict_am.py [--procs 2]
Data cache: .cache/trading_rl/stage2/{train,val}.npz
"""

from __future__ import annotations

import argparse
import sys
import time
from multiprocessing import Pool
from pathlib import Path

import numpy as np
from sklearn.ensemble import HistGradientBoostingRegressor

sys.path.insert(0, "src")
from trading_rl import fill_model as fm  # noqa: E402
from trading_rl.cache import load_cached_session  # noqa: E402
from trading_rl.scalp_env import (  # noqa: E402
    MORNING_CACHE_DIR,
    START_S,
    build_scalp_session,
    is_heldout,
    morning_window_s,
    push_tape,
    tod_seconds,
    universe_session_rows,
)

OUT = Path(".cache/trading_rl/stage2")
HORIZONS = (1, 3, 5, 10)
SPLITS = {"train": ("2024-01-04", "2025-06-30"), "val": ("2025-07-01", "2025-12-31")}


def one(args):
    row, stride, seed = args
    date, stem, prev_close, avg_vol, t100 = row
    try:
        raw = load_cached_session(date, stem, MORNING_CACHE_DIR)
        W = morning_window_s(raw.t_open_us)
        sess = build_scalp_session(raw, prev_close=prev_close, avg_volume_20d=avg_vol,
                                   is_topix100=t100, window_s=W, obs_tape="push20")
    except Exception as exc:  # noqa: BLE001
        return None, f"{date} {stem}: {exc}"
    rng = np.random.default_rng(seed)
    off = int(rng.integers(stride))
    dec = np.arange(START_S + off, W - max(HORIZONS), stride)
    Xb = sess.X[dec - START_S]
    pt = push_tape(raw)
    t_rel = np.asarray(pt.epoch_us, dtype=np.int64) - int(raw.t_open_us)
    keep = (t_rel >= 0) & (t_rel <= W * fm.US)
    Xm = fm.fill_features_at(t_rel[keep], np.asarray(pt.price)[keep], np.asarray(pt.qty)[keep],
                             dec * fm.US, sess.tick[dec], tod_seconds(raw.t_open_us) + dec)
    Y = np.stack([(sess.last[dec + h] - sess.last[dec]) / sess.tick[dec] for h in HORIZONS], 1)
    n = len(dec)
    return {
        "Xb": Xb.astype(np.float32), "Xm": Xm.astype(np.float32), "Y": Y.astype(np.float32),
        "day": np.full(n, date), "stem": np.full(n, stem),
        "held": np.full(n, is_heldout(stem)), "sec": dec.astype(np.int32),
    }, None


def build(split, n, stride, procs, seed):
    path = OUT / f"{split}.npz"
    if path.exists():
        z = np.load(path, allow_pickle=True)
        return {k: z[k] for k in z.files}
    lo, hi = SPLITS[split]
    rows = universe_session_rows(lo, hi, MORNING_CACHE_DIR)
    if split == "train":
        rows = [r for r in rows if not is_heldout(r[1])]
    rng = np.random.default_rng(seed)
    rows = [rows[i] for i in sorted(rng.choice(len(rows), min(n, len(rows)), replace=False))]
    print(f"{split}: {len(rows)} sessions, stride {stride}s", flush=True)
    parts, errs, t0 = [], [], time.time()
    with Pool(procs) as pool:
        for i, (r, e) in enumerate(pool.imap_unordered(one, [(r, stride, j) for j, r in enumerate(rows)], chunksize=4)):
            if r:
                parts.append(r)
            if e:
                errs.append(e)
            if (i + 1) % 200 == 0:
                print(f"  {i+1}/{len(rows)} {time.time()-t0:.0f}s", flush=True)
    if errs:
        print(f"  {len(errs)} failed, e.g. {errs[:2]}")
    out = {k: np.concatenate([p[k] for p in parts]) for k in parts[0]}
    OUT.mkdir(parents=True, exist_ok=True)
    np.savez(path, **out)
    return out


def daily_ic(pred, y, day):
    ics = []
    for d in np.unique(day):
        m = day == d
        if m.sum() > 50 and np.std(pred[m]) > 0:
            ics.append(np.corrcoef(pred[m], y[m])[0, 1])
    ics = np.array(ics)
    return ics.mean(), ics.mean() / (ics.std(ddof=1) / np.sqrt(len(ics))), len(ics)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-train", type=int, default=2000)
    ap.add_argument("--n-val", type=int, default=600)
    ap.add_argument("--stride-train", type=int, default=4)
    ap.add_argument("--stride-val", type=int, default=2)
    ap.add_argument("--procs", type=int, default=2)
    ap.add_argument("--max-train-rows", type=int, default=3_000_000)
    a = ap.parse_args()
    tr = build("train", a.n_train, a.stride_train, a.procs, 1)
    va = build("val", a.n_val, a.stride_val, a.procs, 2)
    print(f"rows train {len(tr['Y'])} val {len(va['Y'])}", flush=True)
    rng = np.random.default_rng(0)
    idx = np.sort(rng.choice(len(tr["Y"]), min(a.max_train_rows, len(tr["Y"])), replace=False))
    tr = {k: tr[k][idx] for k in ("Xb", "Xm", "Y")}  # subsample first to bound memory
    idx = np.arange(len(tr["Y"]))
    sets = {"base": lambda d: d["Xb"], "base+micro": lambda d: np.concatenate([d["Xb"], d["Xm"]], 1)}
    print("\nIC = mean daily corr (t-stat, days); top q = mean target (ticks, signed by prediction) "
          "among the q most confident val rows")
    for name, fx in sets.items():
        Xtr, Xva = fx(tr)[idx], fx(va)
        for hi, h in enumerate(HORIZONS):
            y = tr["Y"][idx, hi]
            ycl = np.clip(y, -20, 20)
            m = HistGradientBoostingRegressor(max_iter=300, learning_rate=0.05, max_leaf_nodes=63,
                                              min_samples_leaf=200, random_state=0)
            t0 = time.time()
            m.fit(Xtr, ycl)
            p = m.predict(Xva)
            yv = va["Y"][:, hi]
            out = []
            for lab, mask in (("all", np.ones(len(yv), bool)), ("known", ~va["held"]), ("heldout", va["held"])):
                ic, t, nd = daily_ic(p[mask], yv[mask], va["day"][mask])
                out.append(f"{lab} IC {ic:+.3f} (t {t:+.1f}, {nd}d)")
            ap_ = np.abs(p)
            tops = []
            for q in (0.05, 0.01):
                thr = np.quantile(ap_, 1 - q)
                sel = ap_ >= thr
                tops.append(f"top{int(q*100)}% {np.mean(np.sign(p[sel]) * yv[sel]):+.2f}t")
            print(f"{name:10s} h={h:2d}s  " + " | ".join(out) + "  | " + " ".join(tops)
                  + f"  [{time.time()-t0:.0f}s]", flush=True)


if __name__ == "__main__":
    main()

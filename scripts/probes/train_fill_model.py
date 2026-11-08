"""Train / validate / save the trades-only fill model (trading_rl.fill_model).

Features are recomputed with ``fill_model.fill_features_at`` (the env's definition) on
the session prints (from T_open = first print >= 09:00:00, like the cache), for every
env-type order (last_k0..k2) in ``.cache/trading_rl/fillcal/orders.npz``.

  1. train on the first 5 board days, validate on the last 4 (vs queue_cap truth)
  2. refit on all days and save to ``fill_model.DEFAULT_MODEL_PATH`` (with --save)

Run: uv run python -W ignore scripts/probes/train_fill_model.py [--save]
"""

from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
from sklearn.metrics import roc_auc_score

sys.path.insert(0, "scripts/probes")
sys.path.insert(0, "src")
import fill_calib as fc  # noqa: E402

from trading_rl import fill_model as fm  # noqa: E402

BAD_DAYS = {"2026-07-10"}
KMAP = {"last_k0": 0, "last_k1": 1, "last_k2": 2}


def feats_for(day: str, code: str, t_sec: np.ndarray, side: np.ndarray, k: np.ndarray,
              tick: np.ndarray) -> np.ndarray | None:
    tr = fc.load_trades(code, day)
    if tr is None:
        return None
    tt, tp, tq = tr
    day0 = np.datetime64(f"{day}T00:00:00", "ns").astype(np.int64)
    i_open = np.searchsorted(tt, day0 + 9 * 3600 * fc.NS, side="left")
    if i_open >= len(tt):
        return None
    t_us = (tt[i_open:] - day0) // 1000  # us since midnight
    F = fm.fill_features_at(t_us, tp[i_open:], tq[i_open:], (t_sec * fm.US).astype(np.int64),
                            tick, t_sec)
    return fm.expand_rows(F, side, k)


def build_rows(d: dict) -> np.ndarray:
    n = len(d["day"])
    R = np.full((n, len(fm.ROW_NAMES)), np.nan)
    keys = sorted(set(zip(d["day"], d["code"])))
    side = np.where(d["side"] == "buy", 1.0, -1.0)
    k = np.array([KMAP[x] for x in d["type"]], dtype=float)
    t_sec = d["t_sec"].astype(float)
    tick = d["tick"].astype(float)
    grp: dict = {}
    for i, key in enumerate(zip(d["day"], d["code"])):
        grp.setdefault(key, []).append(i)

    def job(key):
        idx = np.array(grp[key])
        return idx, feats_for(key[0], key[1], t_sec[idx], side[idx], k[idx], tick[idx])

    done = 0
    with ThreadPoolExecutor(4) as pool:
        for idx, rows in pool.map(job, keys):
            if rows is not None:
                R[idx] = rows
            done += 1
            if done % 100 == 0:
                print(f"  features {done}/{len(keys)} code-days", flush=True)
    return R


def fit(R, taker, q0, m):
    clf = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.05, random_state=0)
    clf.fit(R[m], taker[m])
    rm = m & (taker == 0) & np.isfinite(q0)
    reg = HistGradientBoostingRegressor(max_iter=300, learning_rate=0.05, random_state=0)
    reg.fit(R[rm], np.log1p(q0[rm]))
    return clf, reg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--save", action="store_true")
    ap.add_argument("--orders", default=".cache/trading_rl/fillcal/orders.npz")
    a = ap.parse_args()
    z = dict(np.load(a.orders, allow_pickle=True))
    keep = np.array([(x not in BAD_DAYS) and (t in KMAP) for x, t in zip(z["day"], z["type"])])
    d = {kk: v[keep] for kk, v in z.items()}
    print(f"{len(d['day'])} env-type orders over {len(set(d['day']))} days", flush=True)
    rows_path = fc.OUT_DIR / "fill_rows_v1.npy"
    if rows_path.exists():
        R = np.load(rows_path)
    else:
        R = build_rows(d)
        np.save(rows_path, R)
    ok = np.isfinite(R).all(1)
    taker = d["taker"].astype(int)
    q0 = d["q0"].astype(float)
    days = sorted(set(d["day"]))
    hold = np.isin(d["day"], days[-4:])
    tr, te = ok & ~hold, ok & hold
    clf, reg = fit(R, taker, q0, tr)
    p = clf.predict_proba(R[ok])[:, 1]
    P = np.full(len(R), np.nan); P[ok] = p
    Q = np.full(len(R), np.nan); Q[ok] = np.expm1(reg.predict(R[ok])).clip(0, None)
    print(f"marketable AUC holdout {roc_auc_score(taker[te], P[te]):.3f}")
    rt = te & (taker == 0) & np.isfinite(q0)
    print(f"queue log-corr holdout {np.corrcoef(np.log1p(q0[rt]), np.log1p(Q[rt]))[0, 1]:.3f}")

    G = np.array(fc.Q_GRID, dtype=float)
    vf = np.column_stack([d[f"v{i}_fill_s"].astype(float) for i in range(len(G))])
    vmo = {h: np.column_stack([d[f"v{i}_mo{int(h)}"].astype(float) for i in range(len(G))])
           for h in fc.VMARK}
    rng = np.random.default_rng(0)
    U = rng.random(len(R))

    def env_rule(c):
        gi = np.abs(np.log1p(G[None, :-1]) - np.log1p(np.nan_to_num(c * Q)[:, None])).argmin(1)
        rr = np.arange(len(R))
        fs = vf[rr, gi].copy()
        mos = {h: vmo[h][rr, gi].copy() for h in fc.VMARK}
        mk = U < np.nan_to_num(P)
        fs[mk] = 0.0
        for h in fc.VMARK:
            mos[h][mk] = vmo[h][mk, 0]
        return fs, mos

    def stats(fs, mos, m):
        return (np.mean(fs[m] <= 10) * 100, np.mean(np.isfinite(fs[m])) * 100,
                [np.nanmean(mos[h][m]) for h in fc.VMARK])

    truth = {r: (d[f"{r}_fill_s"].astype(float), {h: d[f"{r}_mo{int(h)}"].astype(float) for h in fc.VMARK})
             for r in ("touch", "trade_through", "queue_cap")}
    best = None
    for c in (0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0):
        a_ = stats(*env_rule(c), tr)
        b_ = stats(*truth["queue_cap"], tr)
        err = abs(a_[0] - b_[0]) + abs(a_[1] - b_[1]) + 20 * abs(a_[2][1] - b_[2][1])
        print(f"  c={c}: fill10 {a_[0]:.1f}/{b_[0]:.1f} fill60 {a_[1]:.1f}/{b_[1]:.1f} "
              f"mo5 {a_[2][1]:+.2f}/{b_[2][1]:+.2f} err {err:.2f}")
        if best is None or err < best[1]:
            best = (c, err)
    c = best[0]
    print(f"shrink c = {c}")
    cand = {"env_rule": env_rule(c), **truth}
    t = d["t_sec"].astype(float)
    groups = [(f"type {ty}", te & (d["type"] == ty)) for ty in KMAP] + [("all", te)] + [
        (f"band {lab}", te & (t >= b0) & (t < b1))
        for b0, b1, lab in ((32400, 33000, "0900-0910"), (33000, 36000, "0910-1000"),
                            (36000, 43200, "1000-1130"))
    ]
    for name, m in groups:
        print(f"\n## holdout {name} (n={m.sum()})   fill%10s fill%60s   mo1   mo5  mo30")
        for r in ("touch", "env_rule", "queue_cap", "trade_through"):
            f10, f60, mo = stats(*cand[r], m)
            print(f"  {r:14s} {f10:6.1f}  {f60:6.1f}   " + " ".join(f"{x:+5.2f}" for x in mo))
    if a.save:
        clf, reg = fit(R, taker, q0, ok)
        model = fm.FillModel(clf, reg, shrink=c, meta={
            "days": days, "n_orders": int(ok.sum()), "source": "fill_calib queue_cap",
            "row_names": list(fm.ROW_NAMES)})
        model.save()
        print(f"saved {fm.DEFAULT_MODEL_PATH}")


if __name__ == "__main__":
    main()

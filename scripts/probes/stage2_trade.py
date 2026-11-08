"""Stage 2 (plan Q6): a real predictor + simple rule trading in the calibrated simulator.

Predictor: HGB on base (28 gate features) + micro (11 fill-model features), all computed
from the kabu PUSH-like tape (push20), predicting the h-second move of the last print in
ticks (see predict_am.py).  The policy is oracle_upper.run_policy (O2 / O3) with the
prediction as the signal, holding H = h seconds.  Entry thresholds are fixed |prediction|
quantiles of a held-aside slice of the TRAIN predictions (top q fraction), so nothing is
fitted on the evaluated split.

  --fit   train one model per horizon on .cache/trading_rl/stage2/train.npz -> pred_models.pkl
  (default) evaluate on --split sessions (known + held-out symbols)

Reported per (h, q, variant): ticks/session with day-bootstrap 95% CI, trips/session,
net/gross ticks per trip, median hold, held-out-symbol ticks/session, and ticks/trip by
entry time band (open 10 min / 9 o'clock / 10 o'clock+).

Run: uv run python -W ignore scripts/probes/stage2_trade.py --fit
     uv run python -W ignore scripts/probes/stage2_trade.py --n 300 --procs 2
"""

from __future__ import annotations

import argparse
import pickle
import sys
import time
from multiprocessing import Pool
from pathlib import Path

import numpy as np
from sklearn.ensemble import HistGradientBoostingRegressor

sys.path.insert(0, "src")
sys.path.insert(0, "scripts/probes")
from oracle_upper import boot_ci, run_policy  # noqa: E402

from trading_rl import fill_model as fm  # noqa: E402
from trading_rl.cache import load_cached_session  # noqa: E402
from trading_rl.scalp_env import (  # noqa: E402
    MORNING_CACHE_DIR,
    ScalpConfig,
    ScalpEnv,
    build_scalp_session,
    is_heldout,
    morning_window_s,
    push_tape,
    tod_seconds,
    universe_session_rows,
)

OUT = Path(".cache/trading_rl/stage2")
MODELS = OUT / "pred_models.pkl"
HORIZONS = (1, 3, 5, 10)
QS = (0.05, 0.02, 0.01, 0.005, 0.002)
SPLITS = {"val": ("2025-07-01", "2025-12-31"), "val2": ("2026-01-01", "2026-08-19")}
BANDS = (("open10", 0, 600), ("9h", 600, None), ("10h+", None, None))


def fit(max_rows):
    z = np.load(OUT / "train.npz", allow_pickle=True)
    n = len(z["Y"])
    rng = np.random.default_rng(0)
    idx = np.sort(rng.choice(n, min(max_rows + 300_000, n), replace=False))
    X = np.concatenate([z["Xb"][idx], z["Xm"][idx]], 1)
    Y = z["Y"][idx]
    del z
    fit_i, cal_i = np.arange(len(idx) - 300_000), np.arange(len(idx) - 300_000, len(idx))
    rng.shuffle(fit_i)
    out = {"horizons": HORIZONS, "models": {}, "thr": {}}
    for hi, h in enumerate(HORIZONS):
        t0 = time.time()
        m = HistGradientBoostingRegressor(max_iter=300, learning_rate=0.05, max_leaf_nodes=63,
                                          min_samples_leaf=200, random_state=0)
        m.fit(X[fit_i], np.clip(Y[fit_i, hi], -20, 20))
        ap = np.abs(m.predict(X[cal_i]))
        out["models"][h] = m
        out["thr"][h] = {q: float(np.quantile(ap, 1 - q)) for q in QS}
        print(f"h={h}s fitted {time.time()-t0:.0f}s thr " +
              " ".join(f"top{q*100:g}%={v:.2f}t" for q, v in out["thr"][h].items()), flush=True)
    MODELS.write_bytes(pickle.dumps(out))
    print(f"saved {MODELS}")


_CACHE = {}


def _get():
    if not _CACHE:
        _CACHE["fill"] = fm.FillModel.load()
        _CACHE["pred"] = pickle.loads(MODELS.read_bytes())
    return _CACHE["fill"], _CACHE["pred"]


def job(args):
    row, hs, qs, variants, wait, exit_wait, seed = args
    date, stem, prev_close, avg_vol, t100 = row
    fill, pred = _get()
    try:
        raw = load_cached_session(date, stem, MORNING_CACHE_DIR)
        W = morning_window_s(raw.t_open_us)
        sess = build_scalp_session(raw, prev_close=prev_close, avg_volume_20d=avg_vol,
                                   is_topix100=t100, window_s=W, obs_tape="push20",
                                   fill_model=fill)
    except Exception as exc:  # noqa: BLE001
        return None, f"{date} {stem}: {type(exc).__name__}: {exc}"
    S = sess.start_s
    dec = np.arange(S, W)
    pt = push_tape(raw)
    t_rel = np.asarray(pt.epoch_us, dtype=np.int64) - int(raw.t_open_us)
    keep = (t_rel >= 0) & (t_rel <= W * fm.US)
    Xm = fm.fill_features_at(t_rel[keep], np.asarray(pt.price)[keep], np.asarray(pt.qty)[keep],
                             dec * fm.US, sess.tick[dec], tod_seconds(raw.t_open_us) + dec)
    X = np.concatenate([sess.X[: len(dec)], Xm], 1).astype(np.float32)
    tod_open = tod_seconds(raw.t_open_us)
    cfg = ScalpConfig(order_mode="both", fill_rule="queue_est", latency_ms=200,
                      sequential_cancel=True, window="morning", obs_tape="push20")
    out = []
    for h in hs:
        sig = pred["models"][h].predict(X)
        for q in qs:
            thr = pred["thr"][h][q]
            for v in variants:
                env = ScalpEnv(config=cfg, seed=seed)
                ep = run_policy(env, sess, v, sig, h, thr, wait, exit_wait)
                ent = [f for f in env.fills if f["kind"] == "entry"]
                trips = env._trips  # noqa: SLF001
                band = np.zeros(3)
                nb = np.zeros(3)
                for f, t in zip(ent, trips, strict=False):
                    tod = tod_open + f["us"] / fm.US
                    b = 0 if f["us"] < 600 * fm.US else (1 if tod < 10 * 3600 else 2)
                    band[b] += t["pnl_ticks"]
                    nb[b] += 1
                out.append({
                    "day": date, "held": is_heldout(stem), "key": (h, q, v),
                    "pnl": ep["pnl_ticks"], "trips": ep["n_trips"], "gross": ep["gross_ticks_sum"],
                    "holds": [t["hold_s"] for t in trips], "band": band, "nb": nb,
                })
    return out, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fit", action="store_true")
    ap.add_argument("--max-train-rows", type=int, default=1_500_000)
    ap.add_argument("--split", default="val")
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--h", default="1,3,5,10")
    ap.add_argument("--q", default=",".join(str(q) for q in QS))
    ap.add_argument("--variants", default="O2,O3")
    ap.add_argument("--wait", type=int, default=3)
    ap.add_argument("--exit-wait", type=int, default=5)
    ap.add_argument("--procs", type=int, default=2)
    a = ap.parse_args()
    if a.fit:
        fit(a.max_train_rows)
        return
    lo, hi = SPLITS[a.split]
    rows = universe_session_rows(lo, hi, MORNING_CACHE_DIR)
    rng = np.random.default_rng(3)
    rows = [rows[i] for i in sorted(rng.choice(len(rows), min(a.n, len(rows)), replace=False))]
    hs = [int(x) for x in a.h.split(",")]
    qs = [float(x) for x in a.q.split(",")]
    variants = a.variants.split(",")
    print(f"{len(rows)} {a.split} sessions ({sum(is_heldout(r[1]) for r in rows)} held-out), "
          f"h={hs} q={qs} {variants}", flush=True)
    jobs = [(r, hs, qs, variants, a.wait, a.exit_wait, i) for i, r in enumerate(rows)]
    res, errs, t0 = [], [], time.time()
    with Pool(a.procs) as pool:
        for i, (out, err) in enumerate(pool.imap_unordered(job, jobs)):
            if out:
                res += out
            if err:
                errs.append(err)
            if (i + 1) % 25 == 0:
                print(f"  {i+1}/{len(jobs)} {time.time()-t0:.0f}s", flush=True)
    if errs:
        print(f"{len(errs)} failed, e.g. {errs[:3]}")
    print("\n h     q   var  ticks/sess [95% CI]        trips/sess  net t/trip gross t/trip "
          "med hold  heldout t/sess   t/trip open10 | 9h | 10h+")
    for key in sorted({r["key"] for r in res}):
        rs = [r for r in res if r["key"] == key]
        day = np.array([r["day"] for r in rs])
        pnl = np.array([r["pnl"] for r in rs])
        held = np.array([r["held"] for r in rs])
        trips = np.array([r["trips"] for r in rs])
        gross = sum(r["gross"] for r in rs)
        holds = np.concatenate([r["holds"] for r in rs]) if trips.sum() else np.array([np.nan])
        band = sum(r["band"] for r in rs)
        nb = sum(r["nb"] for r in rs)
        ci = boot_ci(day, pnl)
        nt = max(trips.sum(), 1)
        hp = pnl[held].mean() if held.any() else float("nan")
        bt = " | ".join(f"{band[i]/max(nb[i],1):+.2f}({int(nb[i])})" for i in range(3))
        print(f"{key[0]:2d} {key[1]:5.3f} {key[2]}  {pnl.mean():+7.2f} [{ci[0]:+6.2f}, {ci[1]:+6.2f}]"
              f"  {trips.mean():8.2f}   {pnl.sum()/nt:+7.3f}   {gross/nt:+7.3f}   {np.median(holds):6.1f}"
              f"   {hp:+8.2f}       {bt}", flush=True)


if __name__ == "__main__":
    main()

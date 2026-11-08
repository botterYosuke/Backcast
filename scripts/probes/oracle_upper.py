"""Stage-1 upper bound (plan Q6): a (noisy) oracle trading in the calibrated simulator.

ScalpEnv with fill_rule="queue_est", latency 200 ms, sequential_cancel, morning
window (T_open+10 s .. 11:29:00), order_mode="both".  The oracle signal at decision
second d is the future move  s_d = (last[d + H] - last[d]) / tick[d]  (ticks), optionally
degraded to correlation rho with the truth:  s~ = s + sigma * sqrt(1/rho^2 - 1) * eps
(sigma = per-session std of s).  Policies (entry when |s~| >= THR ticks):

  O1  market in, market out after H s            (no fill-model dependence)
  O2  limit in (k=0, re-placed each second while the signal holds, up to WAIT s),
      market out H s after the fill
  O3  limit in as O2, limit out (k=0 on the exit side) H s after the fill, market
      after EXIT_WAIT s

Per-session PnL in ticks of the session reference tick (raw, no shaping), trips,
gross ticks per trip.  Day-bootstrap CI of the per-session mean.

Run: uv run python -W ignore scripts/probes/oracle_upper.py --n 400 [--split val]
"""

from __future__ import annotations

import argparse
import sys
import time
from multiprocessing import Pool

import numpy as np

sys.path.insert(0, "src")
from trading_rl import fill_model as fm  # noqa: E402
from trading_rl.predictor_replay import rule_policy as run_policy  # noqa: E402
from trading_rl.scalp_env import (  # noqa: E402
    MORNING_CACHE_DIR,
    ScalpConfig,
    ScalpEnv,
    is_heldout,
    load_scalp_session,
    universe_session_rows,
)

SPLITS = {"train": ("2024-01-04", "2025-06-30"), "val": ("2025-07-01", "2025-12-31")}
_MODEL = None


def _model():
    global _MODEL
    if _MODEL is None:
        _MODEL = fm.FillModel.load()
    return _MODEL


def job(args):
    row, H, thr, rhos, variants, wait, exit_wait, seed, zs = args
    try:
        sess = load_scalp_session(row, MORNING_CACHE_DIR, window="morning", fill_model=_model())
    except Exception as exc:  # noqa: BLE001
        return None, f"{row[0]} {row[1]}: {type(exc).__name__}: {exc}"
    S, W = sess.start_s, sess.window_s
    dec = np.arange(S, W)
    fut = sess.last[np.minimum(dec + H, W)]
    true = (fut - sess.last[dec]) / sess.tick[dec]
    sd = float(np.std(true)) or 1.0
    rng = np.random.default_rng(seed)
    eps = rng.standard_normal(len(true))
    cfg = ScalpConfig(order_mode="both", fill_rule="queue_est", latency_ms=200,
                      sequential_cancel=True, window="morning")
    out = []
    for rho in rhos:
        sig = true if rho >= 1 else true + sd * np.sqrt(1 / rho**2 - 1) * eps
        # z thresholds are in units of the noisy signal's std (sd / rho by construction)
        sd_sig = sd / rho if rho < 1 else sd
        for z in zs or [None]:
            th = thr if z is None else z * sd_sig
            for v in variants:
                env = ScalpEnv(config=cfg, seed=seed)
                ep = run_policy(env, sess, v, sig, H, th, wait, exit_wait)
                out.append((row[0], row[1], (rho, z), v, ep["pnl_ticks"], ep["n_trips"],
                            ep["gross_ticks_sum"], ep["hold_s_sum"], ep["n_limit_marketable"],
                            ep["n_entry_limit"], ep["n_entry_market"]))
    return out, None


def boot_ci(day, x, n=1000, seed=0):
    days = np.unique(day)
    idx = {dd: np.where(day == dd)[0] for dd in days}
    rng = np.random.default_rng(seed)
    means = []
    for _ in range(n):
        pick = rng.choice(days, len(days))
        ii = np.concatenate([idx[dd] for dd in pick])
        means.append(x[ii].mean())
    return np.percentile(means, [2.5, 97.5])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="val")
    ap.add_argument("--n", type=int, default=400)
    ap.add_argument("--H", type=int, default=10)
    ap.add_argument("--thr", type=float, default=2.0)
    ap.add_argument("--rhos", default="1.0,0.5,0.3,0.2,0.1")
    ap.add_argument("--variants", default="O1,O2,O3")
    ap.add_argument("--wait", type=int, default=3)
    ap.add_argument("--exit-wait", type=int, default=5)
    ap.add_argument("--procs", type=int, default=4)
    ap.add_argument("--z", default="", help="entry thresholds in signal-std units (overrides --thr)")
    a = ap.parse_args()
    zs = [float(x) for x in a.z.split(",")] if a.z else []
    lo, hi = SPLITS[a.split]
    rows = [r for r in universe_session_rows(lo, hi, MORNING_CACHE_DIR) if not is_heldout(r[1])]
    rng = np.random.default_rng(0)
    rows = [rows[i] for i in sorted(rng.choice(len(rows), min(a.n, len(rows)), replace=False))]
    rhos = [float(x) for x in a.rhos.split(",")]
    variants = a.variants.split(",")
    print(f"{len(rows)} {a.split} sessions, H={a.H}s thr={a.thr}t rhos={rhos} {variants}", flush=True)
    jobs = [(r, a.H, a.thr, rhos, variants, a.wait, a.exit_wait, i, zs) for i, r in enumerate(rows)]
    res, errs = [], []
    t0 = time.time()
    with Pool(a.procs) as pool:
        for i, (out, err) in enumerate(pool.imap_unordered(job, jobs)):
            if out:
                res += out
            if err:
                errs.append(err)
            if (i + 1) % 50 == 0:
                print(f"  {i+1}/{len(jobs)} sessions {time.time()-t0:.0f}s", flush=True)
    if errs:
        print(f"{len(errs)} sessions failed, e.g. {errs[:3]}")
    day = np.array([r[0] for r in res])
    key_a = [r[2] for r in res]; var_a = np.array([r[3] for r in res])
    pnl = np.array([r[4] for r in res]); trips = np.array([r[5] for r in res])
    gross = np.array([r[6] for r in res]); hold = np.array([r[7] for r in res])
    mk = np.array([r[8] for r in res]); el = np.array([r[9] for r in res]); em = np.array([r[10] for r in res])
    print("\nrho   z    var  ticks/sess [95% CI]          trips/sess  net t/trip  gross t/trip  hold s/trip  mkt-fill share of limit entries")
    for key in sorted(set(key_a), key=lambda k: (-k[0], k[1] or 0)):
        rho, z = key
        for v in variants:
            m = np.array([k == key for k in key_a]) & (var_a == v)
            if not m.any():
                continue
            ci = boot_ci(day[m], pnl[m])
            nt = trips[m].sum()
            zl = "-" if z is None else f"{z:g}"
            print(f"{rho:4.2f} {zl:>4} {v}  {pnl[m].mean():+8.2f} [{ci[0]:+7.2f}, {ci[1]:+7.2f}]   {trips[m].mean():8.2f}"
                  f"   {pnl[m].sum()/max(nt,1):+8.3f}   {gross[m].sum()/max(nt,1):+8.3f}    {hold[m].sum()/max(nt,1):7.1f}"
                  f"   {mk[m].sum()/max(el[m].sum(),1):.2f}")


if __name__ == "__main__":
    main()

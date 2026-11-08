"""Summarise .cache/trading_rl/fillcal/orders*.npz (see fill_calib.py)."""
import sys
import numpy as np

RULES = ("touch", "trade_through", "queue_fifo", "queue_cap")
f = sys.argv[1] if len(sys.argv) > 1 else ".cache/trading_rl/fillcal/orders.npz"
d = dict(np.load(f, allow_pickle=True))
keep = np.array([x != "2026-07-10" for x in d["day"]])  # board volume never matches prints
d = {k: v[keep] for k, v in d.items()}
n = len(d["day"])
t = d["t_sec"].astype(float)
band = np.where(t < 9 * 3600 + 600, "0900-0910", np.where(t < 10 * 3600, "0910-1000", "1000-1130"))
print(f"{f}: {n} orders, {len(set(d['day']))} days, {len(set(d['code']))} codes")


def tab(mask, title):
    print(f"\n## {title}  (n={mask.sum()})")
    print("rule           fill%10s fill%60s  med_s  mo1   mo5   mo10  mo30   (markout ticks vs mid, fills only)")
    for r in RULES:
        fs = d[f"{r}_fill_s"][mask].astype(float)
        f10 = np.mean(fs <= 10) * 100
        f60 = np.mean(np.isfinite(fs)) * 100
        med = np.nanmedian(fs) if np.isfinite(fs).any() else np.nan
        mos = [np.nanmean(d[f"{r}_mo{h}"][mask].astype(float)) for h in (1, 5, 10, 30)]
        print(f"{r:14s} {f10:6.1f}   {f60:6.1f}   {med:5.1f} " + " ".join(f"{m:+5.2f}" for m in mos))


typ = d["type"]; tk = d["taker"].astype(int)
for ty in ("join", "last_k0", "last_k1", "last_k2"):
    m = typ == ty
    print(f"\n# type={ty}: taker(marketable) {np.mean(tk[m])*100:.1f}%  "
          f"pos vs best (ticks; +=better than best) median {np.median(d['px_vs_best_t'][m].astype(float)):+.1f}  "
          f"q0 median {np.nanmedian(d['q0'][m & (tk==0)].astype(float)):.0f}")
    tab(m & (tk == 0), f"{ty} resting (non-marketable)")
for b in ("0900-0910", "0910-1000", "1000-1130"):
    tab((typ == "join") & (band == b) & (tk == 0), f"join resting, band {b}")
sp = d["spread_t"].astype(float)
tab((typ == "join") & (sp == 1), "join resting, spread=1 tick")
tab((typ == "join") & (sp >= 2), "join resting, spread>=2 ticks")

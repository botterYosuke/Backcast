"""Supervised predictor -> simple trading rule, net of the 1-tick market round trip.
Train HGB on train (target: 120 s forward move), thresholds from TRAIN prediction quantiles,
then on ALL val sessions: flat & |pred|>thr -> market entry in pred direction at second s
(price last[s] +/- 0.5 tick), exit at s+120 (or 600) at last -/+ 0.5 tick. Day-bootstrap CI."""
import sys, time
sys.path.insert(0, "scripts"); sys.path.insert(0, "src")
import numpy as np
from train_scalp import split_rows, sample_rows
from trading_rl.scalp_env import load_scalp_sessions
from trading_rl.scalp_features import market_names
from sklearn.ensemble import HistGradientBoostingRegressor

FULL = "base+D+E+F+K+N+P"
names = market_names(FULL); H = 120
tr = load_scalp_sessions(sample_rows(split_rows("train")[0], 3000, 7), features=FULL)
vs_rows, vh_rows = split_rows("val")
t0 = time.time()
va = load_scalp_sessions(vs_rows + vh_rows, features=FULL); held = set((r[0], r[1]) for r in vh_rows)
print(f"val {len(va)} sessions {time.time()-t0:.0f}s", flush=True)
def xy(ss, step):
    X, Y = [], []
    for s in ss:
        d = np.arange(0, 590, step); sec = 10 + d
        X.append(s.X[d]); Y.append((s.last[np.minimum(sec + H, 600)] - s.last[sec]) / s.tick[sec])
    return np.vstack(X), np.clip(np.concatenate(Y), -40, 40)
Xtr, ytr = xy(tr, 5)
rng = np.random.default_rng(0)
for spec in ["base", "base+K", "base+N", "base+E+F+K+N+P", "base+D+E+F+K+N+P"]:
    c = [names.index(n) for n in market_names(spec)]
    m = HistGradientBoostingRegressor(max_iter=200, learning_rate=0.05, max_leaf_nodes=31, min_samples_leaf=200, random_state=0).fit(Xtr[:, c], ytr)
    ptr = np.abs(m.predict(Xtr[:, c]))
    for q in (0.95, 0.99, 0.999):
        thr = np.quantile(ptr, q)
        per_day, per_day_h, trips, nets = {}, {}, 0, []
        for s in va:
            p = m.predict(s.X[:, c]); d = 0; pnl = 0.0
            while d < 590:
                if abs(p[d]) > thr:
                    s0 = 10 + d; s1 = min(s0 + H, 600); dirn = np.sign(p[d])
                    net = dirn * (s.last[s1] - s.last[s0]) / s.tick[s0] - 1.0  # 0.5 tick each side
                    pnl += net; nets.append(net); trips += 1; d = s1 - 10 + 1
                else:
                    d += 1
            key = s.date; tgt = per_day_h if (s.date, s.stem) in held else per_day
            tgt.setdefault(key, []).append(pnl)
        def ci(dd):
            v = np.array([np.mean(x) for x in dd.values()]); n = np.array([len(x) for x in dd.values()])
            tot = np.array([np.sum(x) for x in dd.values()])
            bs = []
            for _ in range(1000):
                i = rng.integers(0, len(v), len(v)); bs.append(tot[i].sum() / n[i].sum())
            return tot.sum() / n.sum(), np.percentile(bs, 2.5), np.percentile(bs, 97.5)
        a, lo, hi = ci(per_day); ah, loh, hih = ci(per_day_h)
        print(f"{spec:20s} q{q}: trips/sess {trips/len(va):.2f} net/trip {np.mean(nets) if nets else 0:+.2f}tk | seen ticks/sess {a:+.3f} [{lo:+.3f},{hi:+.3f}] | held {ah:+.3f} [{loh:+.3f},{hih:+.3f}]", flush=True)

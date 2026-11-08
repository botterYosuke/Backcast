"""Predictability at short horizons (1,2,5,10,20 s): HGB trained on train, scored on val."""
import sys, time
sys.path.insert(0, "scripts"); sys.path.insert(0, "src")
import numpy as np
from train_scalp import split_rows, sample_rows
from trading_rl.scalp_env import load_scalp_sessions
from trading_rl.scalp_features import market_names
from sklearn.ensemble import HistGradientBoostingRegressor

HS = (1, 2, 5, 10, 20)
FULL = "base+A+B+C+D+E+F+G+H+I+K+L+M+N+O+P+Q"
names = market_names(FULL); STEP = 5
def build(rows):
    ss = load_scalp_sessions(rows, features=FULL)
    X, Y, DT = [], {h: [] for h in HS}, []
    for s in ss:
        d = np.arange(0, 590, STEP); sec = 10 + d
        for h in HS: Y[h].append((s.last[np.minimum(sec + h, 600)] - s.last[sec]) / s.tick[sec])
        X.append(s.X[d]); DT += [s.date] * len(d)
    return np.vstack(X), {h: np.concatenate(v) for h, v in Y.items()}, np.array(DT)
Xtr, Ytr, _ = build(sample_rows(split_rows("train")[0], 3000, 7))
vs, vh = split_rows("val"); Xva, Yva, DTva = build(sample_rows(vs + vh, 2000, 7))
for h in HS: print(f"h={h}s: share of zero moves {np.mean(Yva[h]==0):.2f}, mean |move| {np.mean(np.abs(Yva[h])):.2f} tk", flush=True)
rng = np.random.default_rng(0); ud = np.unique(DTva); idx = {u: DTva == u for u in ud}
def score(pred, y):
    ics = np.array([np.corrcoef(pred[m], y[m])[0, 1] for m in idx.values() if m.sum() > 20 and pred[m].std() > 0])
    bs = [rng.choice(ics, len(ics)).mean() for _ in range(300)]
    e = {}
    for q in (0.95, 0.99):
        t = np.quantile(np.abs(pred), q); m = np.abs(pred) >= t; e[q] = (np.sign(pred[m]) * y[m]).mean()
    return ics.mean(), np.percentile(bs, 2.5), np.percentile(bs, 97.5), e[0.95], e[0.99]
specs = ["base", "base+C", "base+H", "base+I", "base+K", "base+L", "base+O", "base+N", "base+E+F+K+N+P", FULL]
print("spec | " + " | ".join(f"h{h}: IC [CI] edge5% edge1%" for h in HS))
for sp in specs:
    c = [names.index(n) for n in market_names(sp)]; row = []
    for h in HS:
        m = HistGradientBoostingRegressor(max_iter=200, learning_rate=0.05, max_leaf_nodes=31, min_samples_leaf=200, random_state=0)
        m.fit(Xtr[:, c], np.clip(Ytr[h], -15, 15))
        ic, lo, hi, e5, e1 = score(m.predict(Xva[:, c]), Yva[h])
        row.append(f"{ic:+.3f} [{lo:+.3f},{hi:+.3f}] {e5:+.2f} {e1:+.2f}")
    print(f"{sp if sp != FULL else 'ALL(A-Q)'} | " + " | ".join(row), flush=True)

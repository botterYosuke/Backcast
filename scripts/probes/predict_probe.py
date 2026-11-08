"""Supervised predictability probe: can each feature group predict the next 30/60 s move?
Train HistGradientBoosting on train sessions, score on val (seen+held). No RL noise."""
import sys, time
sys.path.insert(0, "scripts"); sys.path.insert(0, "src")
import numpy as np
from train_scalp import split_rows, sample_rows
from trading_rl.scalp_env import load_scalp_sessions
from trading_rl.scalp_features import market_names
from sklearn.ensemble import HistGradientBoostingRegressor

FULL = "base+A+B+C+D+E+F+G+H+I+K+L+M+N+O+P+Q"
names = market_names(FULL)
STEP = 5
def build(rows):
    t0 = time.time(); ss = load_scalp_sessions(rows, features=FULL)
    X, Y30, Y60, D, DATE = [], [], [], [], []
    for s in ss:
        d = np.arange(0, 590, STEP); sec = 10 + d
        for h, Y in ((30, Y30), (60, Y60)):
            Y.append((s.last[np.minimum(sec + h, 600)] - s.last[sec]) / s.tick[sec])
        X.append(s.X[d]); D.append(d); DATE += [s.date] * len(d)
    print(f"  {len(ss)} sessions in {time.time()-t0:.0f}s", flush=True)
    return np.vstack(X), np.concatenate(Y30), np.concatenate(Y60), np.concatenate(D), np.array(DATE)

tr_rows = sample_rows(split_rows("train")[0], 3000, 7)
vs, vh = split_rows("val"); va_rows = sample_rows(vs + vh, 2000, 7)
print("load train"); Xtr, y30tr, y60tr, _, _ = build(tr_rows)
print("load val"); Xva, y30va, y60va, Dva, DTva = build(va_rows)
y30tr, y60tr = np.clip(y30tr, -20, 20), np.clip(y60tr, -20, 20)

def cols(spec):
    want = market_names(spec); return [names.index(n) for n in want]

def score(pred, y, dates, rng):
    ud = np.unique(dates); ics = []
    for u in ud:
        m = dates == u
        if m.sum() > 20 and pred[m].std() > 0: ics.append(np.corrcoef(pred[m], y[m])[0, 1])
    ics = np.array(ics); bs = [rng.choice(ics, len(ics)).mean() for _ in range(500)]
    q = np.quantile(np.abs(pred), 0.95); m = np.abs(pred) >= q
    edge = (np.sign(pred[m]) * y[m]).mean()   # ticks captured before cost, top 5% confident
    return ics.mean(), np.percentile(bs, 2.5), np.percentile(bs, 97.5), edge

specs = ["base"] + [f"base+{g}" for g in "ABCDEFGHIKLMNOPQ"] + ["base+K+L+M+N+O+P+Q", FULL]
rng = np.random.default_rng(0)
print(f"{'spec':24s} {'IC60':>7s} {'[95%CI]':>17s} {'top5%edge60':>11s} | {'IC30':>7s} {'top5%edge30':>11s}")
for sp in specs:
    c = cols(sp); out = []
    for ytr, yva in ((y60tr, y60va), (y30tr, y30va)):
        m = HistGradientBoostingRegressor(max_iter=200, learning_rate=0.05, max_leaf_nodes=31, min_samples_leaf=200, random_state=0)
        m.fit(Xtr[:, c], ytr); out.append(score(m.predict(Xva[:, c]), yva, DTva, rng))
    (ic, lo, hi, e), (ic3, _, _, e3) = out
    print(f"{sp:24s} {ic:+7.4f} [{lo:+.4f},{hi:+.4f}] {e:+11.3f} | {ic3:+7.4f} {e3:+11.3f}", flush=True)

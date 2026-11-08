"""Can we catch the moments right before a big up move a few seconds later?
Event(h,k): max(last[s+1..s+h]) - last[s] >= k ticks (per-second last prints).
HGB classifier trained on train, threshold from TRAIN score quantiles, scored on val (seen+held).
Tradeable check: market buy at last[s]+0.5tk, sell at s+h at last-0.5tk (net = move - 1 tick);
also 'hit the event' rate = precision.  Day-bootstrap CI on net/trade."""
import sys
sys.path.insert(0, "scripts"); sys.path.insert(0, "src")
import numpy as np
from train_scalp import split_rows, sample_rows
from trading_rl.scalp_env import load_scalp_sessions
from trading_rl.scalp_features import market_names
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import roc_auc_score

T, DN = {}, {}
FULL = "base+A+B+C+D+E+F+G+H+I+K+L+M+N+O+P+Q"; names = market_names(FULL); STEP = 2
EV = [(5, 3), (5, 5), (10, 5)]
def build(rows):
    ss = load_scalp_sessions(rows, features=FULL)
    X, DT, E, R = [], [], {e: [] for e in EV}, {e: [] for e in EV}; T.clear(); DN.clear(); T.update({e: [] for e in EV}); DN.update({e: [] for e in EV})
    for s in ss:
        d = np.arange(0, 590, STEP); sec = 10 + d; tk = s.tick[sec]
        for h, k in EV:
            fut = np.stack([s.last[np.minimum(sec + j, 600)] for j in range(1, h + 1)], 1)
            mv = (fut - s.last[sec][:, None]) / tk[:, None]
            E[(h, k)].append(mv.max(1) >= k - 1e-9); DN[(h, k)].append(mv.min(1) <= -k + 1e-9)
            hit = mv >= k - 1e-9; first = hit.argmax(1); has = hit.any(1)
            T[(h, k)].append(np.where(has, mv[np.arange(len(mv)), first], mv[:, -1]) - 1.0)
            R[(h, k)].append((s.last[np.minimum(sec + h, 600)] - s.last[sec]) / tk)
        X.append(s.X[d]); DT += [s.date] * len(d)
    return np.vstack(X), {e: np.concatenate(v) for e, v in E.items()}, {e: np.concatenate(v) for e, v in R.items()}, np.array(DT)
Xtr, Etr, _, _ = build(sample_rows(split_rows("train")[0], 3000, 7))
vs, vh = split_rows("val"); Xva, Eva, Rva, DTva = build(sample_rows(vs + vh, 2500, 7)); Tva = {e: np.concatenate(v) for e, v in T.items()}; DNva = {e: np.concatenate(v) for e, v in DN.items()}
rng = np.random.default_rng(0); ud, inv = np.unique(DTva, return_inverse=True)
def boot(net, days):
    u, iv = np.unique(days, return_inverse=True); tot = np.bincount(iv, net); n = np.bincount(iv)
    bs = [(tot[i].sum() / n[i].sum()) for i in (rng.integers(0, len(u), len(u)) for _ in range(1000))]
    return np.percentile(bs, 2.5), np.percentile(bs, 97.5), len(u)
print(f"{'spec':10s} {'event':10s} {'base%':>6s} {'AUC':>6s} | top-q (q from train): n, precision, net/trade [95%CI] days")
for sp in ["base", "base+O", FULL]:
    c = [names.index(n) for n in market_names(sp)]
    for ev in EV:
        m = HistGradientBoostingClassifier(max_iter=200, learning_rate=0.05, max_leaf_nodes=31, min_samples_leaf=200, random_state=0)
        m.fit(Xtr[:, c], Etr[ev]); ptr = m.predict_proba(Xtr[:, c])[:, 1]; pva = m.predict_proba(Xva[:, c])[:, 1]
        auc = roc_auc_score(Eva[ev], pva); out = []
        for q in (0.99, 0.999):
            sel = pva >= np.quantile(ptr, q)
            if sel.sum() < 5: out.append(f"q{q}: n={sel.sum()}"); continue
            net = Rva[ev][sel] - 1.0; lo, hi, nd = boot(net, DTva[sel])
            tp = Tva[ev][sel]; tlo, thi, _ = boot(tp, DTva[sel])
            out.append(f"q{q}: n={sel.sum()} up={Eva[ev][sel].mean():.2f} down={DNva[ev][sel].mean():.2f} hold={net.mean():+.2f} [{lo:+.2f},{hi:+.2f}] TP={tp.mean():+.2f} [{tlo:+.2f},{thi:+.2f}] d={nd}")
        lab = "ALL" if sp == FULL else sp
        print(f"{lab:10s} h{ev[0]}s>={ev[1]}tk {100*Eva[ev].mean():5.1f}% {auc:.3f} | " + " | ".join(out), flush=True)

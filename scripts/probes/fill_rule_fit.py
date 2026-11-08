"""Fit + validate a trades-only limit-fill rule against the board-calibrated queue model.

Input: .cache/trading_rl/fillcal/orders.npz from fill_calib.py.
Truth = ``queue_cap`` (board-displayed queue, cancels ahead inferred when the level shrinks).

The trades-only rule (what ScalpEnv can run without a board):
  1. p_mkt  = classifier(features)            -> with prob p_mkt the order is marketable
                                                 (filled at once at P)
  2. q_hat  = c * exp(regressor(features))-1  -> virtual FIFO queue ahead (shares); fill once
                                                 prints at P exceed q_hat + 100, or on a
                                                 print through P
  c is a single shrink factor (cancels ahead are invisible without a board) fitted on the
  train days so the fill rate matches queue_cap.

Validation on held-out days (last 4 board days): fill% within 10 s / 60 s and mean
markouts (ticks vs mid; fills only), per order type, next to touch / trade_through /
queue_cap computed on the same orders.

Run: uv run python -W ignore scripts/probes/fill_rule_fit.py
"""

from __future__ import annotations

import sys

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor

sys.path.insert(0, "scripts/probes")
from fill_calib import FEATS, Q_GRID, VMARK  # noqa: E402

F = sys.argv[1] if len(sys.argv) > 1 else ".cache/trading_rl/fillcal/orders.npz"
d = dict(np.load(F, allow_pickle=True))
BAD_DAYS = {"2026-07-10"}  # board volume never matches the prints (fill_calib day_offsets)
keep = np.array([x not in BAD_DAYS for x in d["day"]])
d = {kk: v[keep] for kk, v in d.items()}
n = len(d["day"])
days = sorted(set(d["day"]))
hold_days = set(days[-4:])
hold = np.array([x in hold_days for x in d["day"]])
print(f"{n} orders; train days {days[:-4]}; holdout {sorted(hold_days)}")

sg = np.where(d["side"] == "buy", 1.0, -1.0)
kmap = {"join": -1, "last_k0": 0, "last_k1": 1, "last_k2": 2}
k = np.array([kmap[x] for x in d["type"]], dtype=float)
envmask = k >= 0  # the env only has last-relative prices; join needs the board
f = {c: d[c].astype(float) for c in FEATS}
X = np.column_stack([
    k, sg,
    sg * f["f_ret1"], sg * f["f_ret5"], sg * f["f_ret30"], sg * f["f_tickdir"],
    np.where(sg > 0, f["f_pos5"], 1 - f["f_pos5"]),
    f["f_n10"], f["f_vol10_rel"], np.log1p(f["f_medq"]), f["f_tickbps"], f["f_bounce"],
    d["t_sec"].astype(float) / 3600.0,
])
taker = d["taker"].astype(int)
q0 = d["q0"].astype(float)

tr = envmask & ~hold
te = envmask & hold
clf = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.05, random_state=0)
clf.fit(X[tr], taker[tr])
p_mkt = clf.predict_proba(X)[:, 1]
rm = tr & (taker == 0) & np.isfinite(q0)
reg = HistGradientBoostingRegressor(max_iter=300, learning_rate=0.05, random_state=0)
reg.fit(X[rm], np.log1p(q0[rm]))
qhat_raw = np.expm1(reg.predict(X)).clip(0, None)

from sklearn.metrics import roc_auc_score  # noqa: E402

print(f"marketable AUC (holdout) {roc_auc_score(taker[te], p_mkt[te]):.3f}; "
      f"rate train {taker[tr].mean():.3f} hold {taker[te].mean():.3f}")
rt = te & (taker == 0) & np.isfinite(q0)
r = np.corrcoef(np.log1p(q0[rt]), np.log1p(qhat_raw[rt]))[0, 1]
print(f"queue log-corr (holdout resting) {r:.3f}; median q0 {np.median(q0[rt]):.0f} vs q_hat {np.median(qhat_raw[rt]):.0f}")

G = np.array(Q_GRID, dtype=float)
vf = np.column_stack([d[f"v{i}_fill_s"].astype(float) for i in range(len(G))])
vmo = {h: np.column_stack([d[f"v{i}_mo{int(h)}"].astype(float) for i in range(len(G))]) for h in VMARK}


def virtual(qhat, rng_seed=0):
    """Virtual-FIFO outcome at the grid point nearest (log) to q_hat, plus the marketable draw."""
    gi = np.abs(np.log1p(G[None, :-1]) - np.log1p(qhat[:, None])).argmin(1)
    rows = np.arange(len(qhat))
    fs = vf[rows, gi].copy()
    mos = {h: vmo[h][rows, gi].copy() for h in VMARK}
    rng = np.random.default_rng(rng_seed)
    mk = rng.random(len(qhat)) < p_mkt
    fs[mk] = 0.0
    # marketable at P: env fills at P -> markout vs mid measured from P (same as v-grid at q=0 timing ~ now)
    for h in VMARK:
        mos[h][mk] = np.nan  # filled below from truth-free proxy
    return fs, mos, mk


# markout for a marketable env fill at P: use the truth taker markout when the order really
# was marketable, else the q=0 virtual markout (closest proxy for "filled immediately at P")
def stats(fs, mos, m):
    f10 = np.mean(fs[m] <= 10) * 100
    f60 = np.mean(np.isfinite(fs[m])) * 100
    return f10, f60, [np.nanmean(mos[h][m]) for h in VMARK]


truth = {}
for rule in ("touch", "trade_through", "queue_cap"):
    truth[rule] = (d[f"{rule}_fill_s"].astype(float),
                   {h: d[f"{rule}_mo{int(h)}"].astype(float) for h in VMARK})

# fit shrink factor c on train days: match queue_cap fill-within-60s and 10s
best = None
for c in (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 1.0):
    fs, mos, mk = virtual(c * qhat_raw)
    for h in VMARK:
        mos[h][mk] = vmo[h][mk, 0]
    a = stats(fs, mos, tr)
    b = stats(*truth["queue_cap"], tr)
    err = abs(a[0] - b[0]) + abs(a[1] - b[1]) + 20 * abs(a[2][1] - b[2][1])
    print(f"  c={c:.1f}: fill10 {a[0]:5.1f} vs {b[0]:5.1f}  fill60 {a[1]:5.1f} vs {b[1]:5.1f}  mo5 {a[2][1]:+.2f} vs {b[2][1]:+.2f}  err {err:.2f}")
    if best is None or err < best[1]:
        best = (c, err)
c = best[0]
print(f"chosen shrink c = {c}")
fs, mos, mk = virtual(c * qhat_raw)
for h in VMARK:
    mos[h][mk] = vmo[h][mk, 0]
cand = {"env_rule": (fs, mos), **truth}

for ty in ("last_k0", "last_k1", "last_k2", "all"):
    m = te & ((d["type"] == ty) if ty != "all" else True)
    print(f"\n## holdout {ty} (n={m.sum()})   fill%10s fill%60s   mo1   mo5  mo30")
    for name in ("touch", "env_rule", "queue_cap", "trade_through"):
        f10, f60, mo = stats(*cand[name], m)
        print(f"  {name:14s} {f10:6.1f}  {f60:6.1f}   " + " ".join(f"{x:+5.2f}" for x in mo))
for b0, b1, lab in ((9 * 3600, 9 * 3600 + 600, "0900-0910"), (9 * 3600 + 600, 10 * 3600, "0910-1000"),
                    (10 * 3600, 12 * 3600, "1000-1130")):
    t = d["t_sec"].astype(float)
    m = te & (t >= b0) & (t < b1)
    print(f"\n## holdout band {lab} (n={m.sum()})   fill%10s fill%60s   mo1   mo5  mo30")
    for name in ("touch", "env_rule", "queue_cap", "trade_through"):
        f10, f60, mo = stats(*cand[name], m)
        print(f"  {name:14s} {f10:6.1f}  {f60:6.1f}   " + " ".join(f"{x:+5.2f}" for x in mo))

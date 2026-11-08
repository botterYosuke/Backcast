"""Re-exit the stage-2 predictor's actual entry fills with other exit rules (stop / target / time).

Entries are kept exactly as the calibrated simulator filled them (stage2_trends.py trip logs,
so the adverse selection of the entry stays in); only the exit is replayed on the full tape:
  - entry price = the limit price = last print at the decision second of the order that filled
    (decision second = floor(place time); place time = fill time - fill_wait)
  - stop:   first print with dir*(p - E) <= -k ticks  -> market at the last print before
            trigger + 200 ms, -0.5 tick slippage (same as ScalpEnv._market_fill)
  - target: limit at E + dir*m ticks, filled only when a print trades THROUGH it (strictly beyond)
  - time:   market after T s (last print before entry + T + 200 ms, -0.5 tick)
  stops / targets in ticks, or scaled by vol60 (std of 1 s changes over the last 60 s, ticks).
PnL in session reference ticks (pnl_t units) and bp.  Also prints MFE / MAE (best / worst
excursion within 10 s) and win / loss sizes by volatility quintile.

Run: uv run python -W ignore scripts/probes/stage2_exits.py --split val   (-> exits_<split>.npz)
     uv run python -W ignore scripts/probes/stage2_exits.py --report
"""

from __future__ import annotations

import argparse
import itertools
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, "src")
sys.path.insert(0, "scripts/probes")
import stage2_enrich as se  # noqa: E402
from stage2_trends import boot  # noqa: E402

from trading_rl.cache import load_cached_session  # noqa: E402
from trading_rl.scalp_env import MORNING_CACHE_DIR, morning_window_s, tick_size  # noqa: E402

OUT = Path(".cache/trading_rl/stage2")
US = 1_000_000
LAT = 200_000
TS = (10, 30, 60, 120)
STOPS = (None, 1, 2, 3, 5, "0.5v", "1v", "2v")
TARGETS = (None, 2, 4, 8, "1v", "2v", "4v")
RULES = list(itertools.product(TS, STOPS, TARGETS))


def _thr(x, vol):
    if x is None:
        return np.inf
    if isinstance(x, str):
        return float(x[:-1]) * max(vol, 0.25)
    return float(x)


def replay_trip(t, p, fill_us, E, d, tk, vol, W_us):
    """PnL (in ticks of tk) of every rule in RULES + (mfe10, mae10)."""
    i0 = int(np.searchsorted(t, fill_us, side="right"))
    t_end = min(fill_us + max(TS) * US + LAT, W_us)
    i1 = int(np.searchsorted(t, t_end, side="right"))
    pt, pp = t[i0:i1], p[i0:i1]
    f = d * (pp - E) / tk  # favourable excursion per print, ticks
    if len(f) == 0:
        f = np.zeros(1)
        pt = np.array([fill_us])
        pp = np.array([E])
    cmin = np.minimum.accumulate(f)
    cmax = np.maximum.accumulate(f)
    i10 = int(np.searchsorted(pt, fill_us + 10 * US, side="right"))
    mfe = float(cmax[i10 - 1]) if i10 > 0 else 0.0
    mae = float(cmin[i10 - 1]) if i10 > 0 else 0.0

    def mkt_at(t_trig):
        j = int(np.searchsorted(t, t_trig + LAT, side="right")) - 1
        return d * (p[max(j, 0)] - E) / tk - 0.5

    out = np.empty(len(RULES))
    cache_time = {T: mkt_at(min(fill_us + T * US, W_us - LAT)) for T in TS}
    for r, (T, s, m) in enumerate(RULES):
        tT = min(fill_us + T * US, W_us - LAT)
        nT = int(np.searchsorted(pt, tT, side="right"))  # prints usable before the time exit
        ks = _thr(s, vol)
        km = float(np.ceil(_thr(m, vol) - 1e-9))  # the limit sits on a whole tick
        # stop: first print with f <= -ks  (-cmin is non-decreasing)
        js = int(np.searchsorted(-cmin, ks - 1e-9, side="left")) if np.isfinite(ks) else len(f)
        # target: first print strictly beyond the limit (trade-through), cmax non-decreasing
        jm = int(np.searchsorted(cmax, km + 1e-9, side="left")) if np.isfinite(km) else len(f)
        js = js if js < nT else len(f)
        jm = jm if jm < nT else len(f)
        if js >= len(f) and jm >= len(f):
            out[r] = cache_time[T]
        elif jm < js:
            out[r] = km
        else:
            out[r] = mkt_at(pt[js])
    return out, mfe, mae


def run(split):
    D = se.merged(split)
    n = len(D["pnl_t"])
    P = np.full((n, len(RULES)), np.nan, np.float32)
    mfe, mae, base_chk = np.full(n, np.nan), np.full(n, np.nan), np.full(n, np.nan)
    groups = defaultdict(list)
    for i in range(n):
        groups[(str(D["date"][i]), str(D["stem"][i]))].append(i)
    for g, (date, stem) in enumerate(sorted(groups)):
        ix = groups[(date, stem)]
        try:
            raw = load_cached_session(date, stem, MORNING_CACHE_DIR)
        except Exception as exc:  # noqa: BLE001
            print("skip", date, stem, exc)
            continue
        t = np.asarray(raw.epoch_us, np.int64) - int(raw.t_open_us)
        p = np.asarray(raw.price, np.float64)
        W_us = morning_window_s(raw.t_open_us) * US
        for i in ix:
            fill_us = int(round(D["since_open"][i] * US))
            wait = D["fill_wait"][i]
            place_us = fill_us - int(round((0.0 if not np.isfinite(wait) else wait) * US))
            dsec = place_us // US
            jd = int(np.searchsorted(t, dsec * US, side="right")) - 1
            E = p[max(jd, 0)]
            d = int(D["dir"][i])
            ref = float(D["tick_rel"][i] * D["px"][i])
            tk = tick_size(E, bool(D["t100"][i] == 1))
            vol = float(D["vol60"][i]) * ref / tk  # vol60 is in reference ticks
            r, a, b = replay_trip(t, p, fill_us, E, d, tk, vol, W_us)
            P[i] = r * tk / ref  # back to reference ticks
            mfe[i], mae[i] = a * tk / ref, b * tk / ref
        if (g + 1) % 300 == 0:
            print(f"  {g+1}/{len(groups)}", flush=True)
    np.savez(OUT / f"exits_{split}.npz", P=P, mfe=mfe, mae=mae)
    print(f"saved exits_{split}.npz")


def rule_name(r):
    T, s, m = r
    s_ = "なし" if s is None else (f"{s}t" if not isinstance(s, str) else f"{s[:-1]}σ")
    m_ = "なし" if m is None else (f"{m}t" if not isinstance(m, str) else f"{m[:-1]}σ")
    return f"最長{T}s・損切{s_}・利確{m_}"


def report():
    data = {}
    for s in ("val", "val2"):
        f = OUT / f"exits_{s}.npz"
        if f.exists():
            D = se.merged(s)
            z = np.load(f)
            D["P"], D["mfe"], D["mae"] = z["P"], z["mfe"], z["mae"]
            data[s] = D
    V = data["val"]
    base = RULES.index((10, None, None))
    for pi in (0, 1):
        print(f"\n## preset {pi} ({'上位1%' if pi == 0 else '上位5%'})")
        m0 = V["preset"] == pi
        e = np.quantile(V["vol60"][m0], [0.2, 0.4, 0.6, 0.8])
        for s, D in data.items():
            D["vq"] = np.digitize(D["vol60"], e)
        print("\n### 激しさ別: 勝ち負けの大きさと、10秒以内の最大の含み益・含み損（実際の売買, tick）")
        print("| 激しさ | split | 回数 | 勝率 | 勝ちの平均 | 負けの平均 | 10秒内の最大含み益 平均 | 最大含み損 平均 | 実際 tick/回 |")
        print("|---|---|---|---|---|---|---|---|---|")
        for q in range(5):
            for s, D in data.items():
                m = (D["preset"] == pi) & (D["vq"] == q)
                x = D["pnl_t"][m]
                print(f"| Q{q+1} | {s} | {m.sum()} | {np.mean(x > 0)*100:.0f}% | {x[x > 0].mean():+.2f} | "
                      f"{x[x < 0].mean():+.2f} | {np.nanmean(D['mfe'][m]):+.2f} | {np.nanmean(D['mae'][m]):+.2f} | {x.mean():+.2f} |")
        # rule table: overall and by vol quintile; choose best on val
        print("\n### 決済ルール別 tick/回（val）— 上位10ルールと基準（10秒で成行）")
        for scope, fn in [("全体", lambda D: np.ones(len(D["pnl_t"]), bool))] + [
                (f"激しさQ{q+1}", (lambda q: lambda D: D["vq"] == q)(q)) for q in range(5)]:
            m = (V["preset"] == pi) & fn(V)
            mu = np.nanmean(V["P"][m], 0)
            order = np.argsort(-mu)[:10]
            print(f"\n#### {scope}（val n={m.sum()}）")
            print("| ルール | val tick/回 [CI] | val 勝率 | val2 tick/回 [CI] |")
            print("|---|---|---|---|")
            for r in [base, *order]:
                cells = []
                for s, D in data.items():
                    mm = (D["preset"] == pi) & fn(D)
                    x = D["P"][mm, r].astype(float)
                    ok = np.isfinite(x)
                    lo, hi = boot(D["date"][mm][ok], x[ok], n=200)
                    cells.append((f"{x[ok].mean():+.3f} [{lo:+.2f}, {hi:+.2f}]", f"{np.mean(x[ok] > 0)*100:.0f}%"))
                v2 = cells[1][0] if len(cells) > 1 else "–"
                tag = "（基準）" if r == base else ""
                print(f"| {rule_name(RULES[r])}{tag} | {cells[0][0]} | {cells[0][1]} | {v2} |")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="val")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    if a.report:
        report()
    else:
        run(a.split)


if __name__ == "__main__":
    main()

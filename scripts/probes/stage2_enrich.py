"""Extra slicing attributes for the stage-2 predictor trip logs (stage2_trends.py).

Reads .cache/trading_rl/stage2/trips_<split>.npz (does not modify it), recomputes causal
context at each entry fill from the cached morning tape and the ETF grids, and writes
.cache/trading_rl/stage2/enrich_<split>.npz (same row order).  `--report` prints
PnL tables by attribute for val and val2 side by side; bin edges come from val only
(find on val, confirm on val2).

Run: uv run python -W ignore scripts/probes/stage2_enrich.py --split val
     uv run python -W ignore scripts/probes/stage2_enrich.py --report
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, "src")
sys.path.insert(0, "scripts/probes")
from stage2_trends import PRESETS, boot, load, nikkei_days  # noqa: E402

from trading_rl.cache import load_cached_session  # noqa: E402
from trading_rl.scalp_env import MORNING_CACHE_DIR, universe_session_rows  # noqa: E402

OUT = Path(".cache/trading_rl/stage2")
SPLITS = {"val": ("2025-07-01", "2025-12-31"), "val2": ("2026-01-01", "2026-08-19")}
US = 1_000_000
ETF_DIR = Path(".cache/trading_rl/ext/etf")


def _px_back(t, p, t0, h):
    c = np.searchsorted(t, t0 - int(h * US), side="right")
    return p[np.maximum(c - 1, 0)]


def session_attrs(date, stem, since_open, dirs, tick):
    raw = load_cached_session(date, stem, MORNING_CACHE_DIR)
    t = np.asarray(raw.epoch_us, np.int64) - int(raw.t_open_us)
    p = np.asarray(raw.price, np.float64)
    q = np.asarray(raw.qty, np.float64)
    t0 = (since_open * US).astype(np.int64)
    c0 = np.searchsorted(t, t0, side="right")
    il = np.maximum(c0 - 1, 0)
    last = p[il]
    tk = np.where(tick > 0, tick, 1.0)
    out = {}
    # volatility: std of 1 s changes of the last print over the past 60 s, ticks
    grid = np.arange(-60, 1)[None, :] * US + t0[:, None]
    g = p[np.maximum(np.searchsorted(t, grid, side="right") - 1, 0)]
    out["vol60"] = np.diff(g, axis=1).std(1) / tk
    out["range60"] = (g.max(1) - g.min(1)) / tk
    # activity: prints in the last 10 s vs. the session average rate so far
    n10 = c0 - np.searchsorted(t, t0 - 10 * US, side="right")
    rate = c0 / np.maximum(since_open, 1.0) * 10
    out["n10"] = n10.astype(float)
    out["act_rel"] = n10 / np.maximum(rate, 1e-9)
    # stock move since open (%), direction-aligned 5 min / 30 s moves (ticks)
    out["ret_open"] = (last / raw.p_open - 1) * 100
    out["ret_open_dir"] = out["ret_open"] * dirs
    out["mom300_dir"] = (last - _px_back(t, p, t0, 300)) / tk * dirs
    out["mom30_dir"] = (last - _px_back(t, p, t0, 30)) / tk * dirs
    # position inside the day's range so far, VWAP distance (direction aligned, ticks)
    cmax = np.maximum.accumulate(p)
    cmin = np.minimum.accumulate(p)
    hi, lo = cmax[il], cmin[il]
    out["range_pos"] = np.where(hi > lo, (last - lo) / np.maximum(hi - lo, 1e-9), 0.5)
    cpq, cq = np.cumsum(p * q), np.cumsum(q)
    vwap = cpq[il] / np.maximum(cq[il], 1)
    out["vwap_dir"] = (last - vwap) / tk * dirs
    out["day_range_t"] = (hi - lo) / tk
    return out, raw.t_open_us


def nikkei_attrs(date, tod, dirs):
    f = ETF_DIR / f"{date}.npz"
    n = len(tod)
    nan = np.full(n, np.nan)
    if not f.is_file():
        return {"nk_so": nan, "nk300_dir": nan, "nk60_dir": nan}
    z = np.load(f)
    stems = [str(s) for s in z["stems"]]
    grid = z["grid"][stems.index("1321")].astype(np.float64)
    fin = np.flatnonzero(np.isfinite(grid))
    if not len(fin):
        return {"nk_so": nan, "nk300_dir": nan, "nk60_dir": nan}
    first = fin[0]
    gi = np.clip(np.floor(tod - 9 * 3600).astype(int), 0, len(grid) - 1)

    def at(k):
        return grid[np.clip(np.maximum(k, first), 0, len(grid) - 1)]

    cur = at(gi)
    return {
        "nk_so": (cur / grid[first] - 1) * 1e4,  # bps since 1321's first print
        "nk300_dir": (cur / at(gi - 300) - 1) * 1e4 * dirs,
        "nk60_dir": (cur / at(gi - 60) - 1) * 1e4 * dirs,
    }


def enrich(split):
    D = load(split)
    n = len(D["pnl_t"])
    lo, hi = SPLITS[split]
    meta = {(r[0], r[1]): r for r in universe_session_rows(lo, hi, MORNING_CACHE_DIR)}
    keys = ("vol60", "range60", "n10", "act_rel", "ret_open", "ret_open_dir", "mom300_dir",
            "mom30_dir", "range_pos", "vwap_dir", "day_range_t", "nk_so", "nk300_dir",
            "nk60_dir", "avg_vol", "t100", "trip_no", "prev_win", "gap_since_prev")
    E = {k: np.full(n, np.nan) for k in keys}
    groups = defaultdict(list)
    for i in range(n):
        groups[(str(D["date"][i]), str(D["stem"][i]))].append(i)
    for gk, (date, stem) in enumerate(sorted(groups)):
        ix = np.array(groups[(date, stem)])
        so = D["since_open"][ix].astype(float)
        dirs = D["dir"][ix].astype(float)
        tick = D["tick_rel"][ix].astype(float) * D["px"][ix].astype(float)
        try:
            a, _ = session_attrs(date, stem, so, dirs, tick)
        except Exception as exc:  # noqa: BLE001
            print(f"skip {date} {stem}: {exc}")
            continue
        a.update(nikkei_attrs(date, D["tod"][ix].astype(float), dirs))
        r = meta.get((date, stem))
        a["avg_vol"] = np.full(len(ix), r[3] * r[2] if r else np.nan)  # avg shares x prev close
        a["t100"] = np.full(len(ix), float(r[4]) if r else np.nan)
        for k, v in a.items():
            E[k][ix] = v
        # order within the session, per preset (rows are in fill order per preset)
        for pi in np.unique(D["preset"][ix]):
            jj = ix[D["preset"][ix] == pi]
            jj = jj[np.argsort(D["since_open"][jj], kind="stable")]
            E["trip_no"][jj] = np.arange(1, len(jj) + 1)
            pw = np.r_[np.nan, (D["pnl_t"][jj][:-1] > 0).astype(float)]
            pw[1:][D["pnl_t"][jj][:-1] == 0] = 0.5
            E["prev_win"][jj] = pw
            ends = D["since_open"][jj] + D["hold"][jj]
            E["gap_since_prev"][jj] = np.r_[np.nan, D["since_open"][jj][1:] - ends[:-1]]
        if (gk + 1) % 200 == 0:
            print(f"  {gk+1}/{len(groups)}", flush=True)
    np.savez(OUT / f"enrich_{split}.npz", **E)
    print(f"saved enrich_{split}.npz ({n} rows)")


# ------------------------------------------------------------------- report
def merged(split):
    f = OUT / f"enrich_{split}.npz"
    if not f.exists() or not (OUT / f"trips_{split}.npz").exists():
        return None
    D = load(split)
    z = np.load(f)
    D.update({k: z[k] for k in z.files})
    nk = nikkei_days()
    arr = np.array([nk.get(str(d), (np.nan,) * 3) for d in D["date"]])
    D["nk_gap"], D["nk_day"] = arr[:, 0], arr[:, 2]
    D["month"] = np.array([int(str(d)[5:7]) for d in D["date"]])
    D["weekday"] = np.array([np.datetime64(str(d)).astype("datetime64[D]").item().weekday()
                             for d in D["date"]])
    return D


def cell(D, m):
    n = int(m.sum())
    if n < 30:
        return f"{n} | – | –", None
    p = D["pnl_t"][m]
    lo, hi = boot(D["date"][m], p, n=300)
    return f"{n} | {np.mean(p > 0) * 100:.0f}% | {p.mean():+.2f} [{lo:+.2f}, {hi:+.2f}]", (p.mean(), lo, hi)


def qtable(title, key, data, pi, nq=5, transform=None, summary=None):
    v0 = data["val"][key][data["val"]["preset"] == pi]
    v0 = v0[np.isfinite(v0)]
    edges = np.unique(np.quantile(v0, np.linspace(0, 1, nq + 1)[1:-1]))
    bins = [(-np.inf, edges[0])] + list(zip(edges[:-1], edges[1:], strict=True)) + [(edges[-1], np.inf)]
    groups = [(f"{a:.3g}〜{b:.3g}" if np.isfinite(a) and np.isfinite(b) else
               (f"< {b:.3g}" if not np.isfinite(a) else f"≥ {a:.3g}"),
               (lambda a, b: lambda D: (D[key] >= a) & (D[key] < b))(a, b)) for a, b in bins]
    gtable(title, groups, data, pi, summary)


def gtable(title, groups, data, pi, summary=None):
    print(f"\n### {title}")
    print("| 区分 | " + " | ".join(f"{s}: 回数 | {s}: 勝率 | {s}: tick/回 [95%CI]" for s in data) + " |")
    print("|---|" + "---|---|---|" * len(data))
    for name, fn in groups:
        cells, stats = [], {}
        for s, D in data.items():
            c, st = cell(D, (D["preset"] == pi) & fn(D))
            cells.append(c)
            stats[s] = st
        print(f"| {name} | " + " | ".join(cells) + " |")
        if summary is not None:
            summary.append((title, name, stats))


def report():
    data = {s: D for s in SPLITS if (D := merged(s)) is not None}
    if not data or "val" not in data:
        print("need enrich_val.npz")
        return
    for pi, p in enumerate(PRESETS):
        print(f"\n## {p.label}")
        S = []
        gtable("全体", [("全部", lambda D: np.ones(len(D["pnl_t"]), bool))], data, pi, S)
        qtable("直近60秒の値動きの激しさ（1秒ごとの変化の標準偏差, tick）", "vol60", data, pi, summary=S)
        qtable("直近60秒の高安の幅（tick）", "range60", data, pi, summary=S)
        qtable("直近10秒の約定件数", "n10", data, pi, summary=S)
        qtable("混み具合（直近10秒の約定件数 ÷ その日のそこまでの平均）", "act_rel", data, pi, summary=S)
        qtable("銘柄の寄りからの騰落（%、符号なし）", "ret_open", data, pi, summary=S)
        qtable("寄りからの騰落 × 売買の向き（+ = 寄りからの流れに乗る向き, %）", "ret_open_dir", data, pi, summary=S)
        qtable("直近5分の値動き × 売買の向き（+ = 順張り, tick）", "mom300_dir", data, pi, summary=S)
        qtable("直近30秒の値動き × 売買の向き（+ = 順張り, tick）", "mom30_dir", data, pi, summary=S)
        qtable("当日の高安の中での位置（0 = 安値, 1 = 高値）", "range_pos", data, pi, summary=S)
        qtable("VWAP からの乖離 × 売買の向き（+ = 有利側から離れて買う/売る, tick）", "vwap_dir", data, pi, summary=S)
        qtable("その日のここまでの値幅（tick）", "day_range_t", data, pi, summary=S)
        qtable("日経（1321）のその時点までの騰落（bps、寄りから）", "nk_so", data, pi, summary=S)
        qtable("日経の直近5分 × 売買の向き（+ = 日経と同じ向き, bps）", "nk300_dir", data, pi, summary=S)
        qtable("日経の直近60秒 × 売買の向き（bps）", "nk60_dir", data, pi, summary=S)
        qtable("売買代金の目安（20日平均出来高 × 前日終値, 円）", "avg_vol", data, pi, summary=S)
        gtable("TOPIX100 か", [("TOPIX100", lambda D: D["t100"] == 1), ("それ以外", lambda D: D["t100"] == 0)],
               data, pi, S)
        gtable("その銘柄・その日で何回目の売買か", [
            ("1回目", lambda D: D["trip_no"] == 1), ("2〜3回目", lambda D: (D["trip_no"] >= 2) & (D["trip_no"] <= 3)),
            ("4〜10回目", lambda D: (D["trip_no"] >= 4) & (D["trip_no"] <= 10)),
            ("11回目以降", lambda D: D["trip_no"] > 10)], data, pi, S)
        gtable("直前の売買の結果", [
            ("直前が勝ち", lambda D: D["prev_win"] == 1), ("直前が負け", lambda D: D["prev_win"] == 0),
            ("直前が引き分け", lambda D: D["prev_win"] == 0.5)], data, pi, S)
        gtable("直前の決済からの間隔", [
            ("5秒以内", lambda D: D["gap_since_prev"] <= 5),
            ("5〜60秒", lambda D: (D["gap_since_prev"] > 5) & (D["gap_since_prev"] <= 60)),
            ("60秒超", lambda D: D["gap_since_prev"] > 60)], data, pi, S)
        qtable("予測の強さ（向きを揃えた予測値, tick）", "sig_signed", data, pi, summary=S)
        qtable("保有時間（秒）", "hold", data, pi, summary=S)
        qtable("株価（円）", "px", data, pi, summary=S)
        gtable("月", [(f"{m}月", (lambda m: lambda D: D["month"] == m)(m)) for m in range(1, 13)], data, pi, S)
        # cells that are positive with CI > 0 on val, and how they look on val2
        print("\n### val で 95%CI の下端が 0 を超えた区分 → val2 での値")
        print("| 切り口 | 区分 | val tick/回 [CI] | val2 tick/回 [CI] |")
        print("|---|---|---|---|")
        for title, name, st in S:
            v = st.get("val")
            if v and v[1] > 0:
                w = st.get("val2")
                ws = f"{w[0]:+.2f} [{w[1]:+.2f}, {w[2]:+.2f}]" if w else "–"
                print(f"| {title} | {name} | {v[0]:+.2f} [{v[1]:+.2f}, {v[2]:+.2f}] | {ws} |")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="val")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    if a.report:
        report()
    else:
        enrich(a.split)


if __name__ == "__main__":
    main()

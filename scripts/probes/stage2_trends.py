"""Stage-2 predictor trades: per-trip log for trend hunting (time of day, Nikkei day, ...).

Generates one row per round trip of the stage-2 predictor presets on the calibrated
simulator (trading_rl.predictor_replay) for a split, and caches it:
  .cache/trading_rl/stage2/trips_<split>.npz
Then `--report` prints PnL / win-rate tables by attribute for val and val2 side by side
(find on val, confirm on val2).

Run: uv run python -W ignore scripts/probes/stage2_trends.py --split val --n 2500 --procs 3
     uv run python -W ignore scripts/probes/stage2_trends.py --report
"""

from __future__ import annotations

import argparse
import sys
import time
from multiprocessing import Pool
from pathlib import Path

import duckdb
import numpy as np

sys.path.insert(0, "src")
from trading_rl import fill_model as fm  # noqa: E402
from trading_rl import predictor_replay as pr  # noqa: E402
from trading_rl.cache import load_cached_session  # noqa: E402
from trading_rl.scalp_env import (  # noqa: E402
    MORNING_CACHE_DIR,
    build_scalp_session,
    is_heldout,
    morning_window_s,
    tod_seconds,
    universe_session_rows,
)
from trading_rl.scalp_replay import _RecordingScalpEnv  # noqa: E402

OUT = Path(".cache/trading_rl/stage2")
SPLITS = {"val": ("2025-07-01", "2025-12-31"), "val2": ("2026-01-01", "2026-08-19")}
PRESETS = (pr.Preset(10, 0.01, "O3"), pr.Preset(10, 0.05, "O3"))


def job(args):
    row, seed = args
    date, stem, prev_close, avg_vol, t100 = row
    try:
        raw = load_cached_session(date, stem, MORNING_CACHE_DIR)
        W = morning_window_s(raw.t_open_us)
        sess = build_scalp_session(raw, prev_close=prev_close, avg_volume_20d=avg_vol,
                                   is_topix100=t100, window_s=W, obs_tape="push20",
                                   fill_model=pr.load_fill_model())
    except Exception as exc:  # noqa: BLE001
        return None, f"{date} {stem}: {type(exc).__name__}: {exc}"
    pred = pr.load_predictors()
    X = pr.predictor_features(raw, sess)
    tod0 = tod_seconds(raw.t_open_us)
    S = sess.start_s
    ref = sess.tick[S]
    out = []
    for pi, p in enumerate(PRESETS):
        sig = pred["models"][p.h].predict(X)
        thr = pred["thr"][p.h][p.q]
        env = _RecordingScalpEnv(config=pr.predictor_config(), seed=seed)
        pr.rule_policy(env, sess, p.variant, sig, p.h, thr)
        ent = [f for f in env.fills if f["kind"] == "entry"]
        places = sorted(e["t"] for e in env.events if e["action"] == "place")
        for f, t in zip(ent, env._trips, strict=False):  # noqa: SLF001
            us = f["us"]
            d = min(max(int(us // fm.US), S), W - 1)
            j = int(np.searchsorted(places, us, "right")) - 1
            wait = (us - places[j]) / fm.US if j >= 0 else np.nan
            out.append((pi, date, stem, is_heldout(stem), tod0 + us / fm.US, us / fm.US,
                        t["dir"], t["pnl_ticks"], t["pnl_yen100"], t["hold_s"],
                        t["exit_order"] == "limit", t["forced"], float(sig[d - S]) * t["dir"],
                        wait, ref / sess.price[0], raw.p_open / prev_close - 1, sess.price[0]))
    return out, None


COLS = ("preset", "date", "stem", "held", "tod", "since_open", "dir", "pnl_t", "pnl_yen",
        "hold", "exit_limit", "forced", "sig_signed", "fill_wait", "tick_rel", "gap", "px")


def generate(split, n, procs):
    # one writer per split: another session may be producing the same trips file
    lock = OUT / f"trips_{split}.lock"
    if lock.exists():
        print(f"{lock} exists ({lock.read_text(encoding='utf-8').strip()}); not generating {split}")
        return
    lo, hi = SPLITS[split]
    rows = universe_session_rows(lo, hi, MORNING_CACHE_DIR)
    rng = np.random.default_rng(11)
    rows = [rows[i] for i in sorted(rng.choice(len(rows), min(n, len(rows)), replace=False))]
    print(f"{split}: {len(rows)} sessions", flush=True)
    res, errs, t0 = [], [], time.time()
    with Pool(procs) as pool:
        for i, (out, err) in enumerate(pool.imap_unordered(job, [(r, k) for k, r in enumerate(rows)],
                                                           chunksize=4)):
            res += out or []
            if err:
                errs.append(err)
            if (i + 1) % 250 == 0:
                print(f"  {i+1}/{len(rows)} {time.time()-t0:.0f}s", flush=True)
    if errs:
        print(f"  {len(errs)} failed, e.g. {errs[:2]}")
    cols = list(zip(*res, strict=True))
    data = {c: np.asarray(v) for c, v in zip(COLS, cols, strict=True)}
    data["n_sessions"] = np.array(len(rows))
    np.savez(OUT / f"trips_{split}.npz", **data)
    print(f"saved {len(res)} trips")


# --------------------------------------------------------------------------- report
def nikkei_days():
    """date -> (gap, intraday, day) returns of ETF 1321 (Nikkei 225), percent."""
    r = duckdb.sql(
        "SELECT CAST(d AS VARCHAR), o, c, lag(c) OVER (ORDER BY d) FROM "
        "read_parquet('.cache/trading_rl/ext/daily_adj.parquet') WHERE stem='1321' ORDER BY d"
    ).fetchall()
    return {d: ((o / pc - 1) * 100, (c / o - 1) * 100, (c / pc - 1) * 100)
            for d, o, c, pc in r if pc}


def load(split):
    z = np.load(OUT / f"trips_{split}.npz", allow_pickle=True)
    return {k: z[k] for k in z.files}


def boot(day, x, n=500, seed=0):
    days = np.unique(day)
    if len(days) < 5:
        return np.nan, np.nan
    idx = {d: np.where(day == d)[0] for d in days}
    rng = np.random.default_rng(seed)
    m = [x[np.concatenate([idx[d] for d in rng.choice(days, len(days))])].mean() for _ in range(n)]
    return np.percentile(m, [2.5, 97.5])


def table(title, groups, data_by_split, preset):
    print(f"\n### {title}")
    print("| 区分 | " + " | ".join(
        f"{s}: 回数 | {s}: 勝率 | {s}: tick/回 [95%CI]" for s in data_by_split) + " |")
    print("|---|" + "---|---|---|" * len(data_by_split))
    for name, fn in groups:
        cells = []
        for _s, D in data_by_split.items():
            m = (D["preset"] == preset) & fn(D)
            n = int(m.sum())
            if n < 30:
                cells.append(f"{n} | – | –")
                continue
            p = D["pnl_t"][m]
            lo, hi = boot(D["date"][m], p)
            cells.append(f"{n} | {np.mean(p > 0) * 100:.0f}% | {p.mean():+.2f} [{lo:+.2f}, {hi:+.2f}]")
        print(f"| {name} | " + " | ".join(cells) + " |")


def report():
    nk = nikkei_days()
    data = {}
    for s in SPLITS:
        f = OUT / f"trips_{s}.npz"
        if f.exists():
            D = load(s)
            arr = np.array([nk.get(str(d), (np.nan,) * 3) for d in D["date"]])
            D["nk_gap"], D["nk_intra"], D["nk_day"] = arr[:, 0], arr[:, 1], arr[:, 2]
            data[s] = D
    for s, D in data.items():
        print(f"{s}: {int(D['n_sessions'])} sessions, trips per preset "
              + ", ".join(f"{p.key}={int((D['preset'] == i).sum())}" for i, p in enumerate(PRESETS)))
    for pi, p in enumerate(PRESETS):
        print(f"\n## {p.label}")
        H = 3600
        table("全体", [("全部", lambda D: np.ones(len(D["pnl_t"]), bool))], data, pi)
        table("時間帯（約定した時刻）", [
            ("寄り後1分", lambda D: D["since_open"] < 60),
            ("寄り後1〜5分", lambda D: (D["since_open"] >= 60) & (D["since_open"] < 300)),
            ("寄り後5〜10分", lambda D: (D["since_open"] >= 300) & (D["since_open"] < 600)),
            ("9:10〜9:30", lambda D: (D["since_open"] >= 600) & (D["tod"] < 9.5 * H)),
            ("9:30〜10:00", lambda D: (D["since_open"] >= 600) & (D["tod"] >= 9.5 * H) & (D["tod"] < 10 * H)),
            ("10:00〜10:30", lambda D: (D["tod"] >= 10 * H) & (D["tod"] < 10.5 * H)),
            ("10:30〜11:00", lambda D: (D["tod"] >= 10.5 * H) & (D["tod"] < 11 * H)),
            ("11:00〜11:29", lambda D: D["tod"] >= 11 * H),
        ], data, pi)
        table("日経（1321）のその日の騰落（前日終値→当日終値）", [
            ("−1%未満", lambda D: D["nk_day"] < -1),
            ("−1〜0%", lambda D: (D["nk_day"] >= -1) & (D["nk_day"] < 0)),
            ("0〜+1%", lambda D: (D["nk_day"] >= 0) & (D["nk_day"] < 1)),
            ("+1%以上", lambda D: D["nk_day"] >= 1),
        ], data, pi)
        table("日経が上がった日・下がった日 × 買い／売り", [
            ("上げ日・買い", lambda D: (D["nk_day"] >= 0) & (D["dir"] > 0)),
            ("上げ日・売り", lambda D: (D["nk_day"] >= 0) & (D["dir"] < 0)),
            ("下げ日・買い", lambda D: (D["nk_day"] < 0) & (D["dir"] > 0)),
            ("下げ日・売り", lambda D: (D["nk_day"] < 0) & (D["dir"] < 0)),
        ], data, pi)
        table("日経の寄り付きギャップ（寄りの時点で分かる）", [
            ("−0.5%未満", lambda D: D["nk_gap"] < -0.5),
            ("−0.5〜+0.5%", lambda D: np.abs(D["nk_gap"]) <= 0.5),
            ("+0.5%超", lambda D: D["nk_gap"] > 0.5),
        ], data, pi)
        table("買いか売りか", [
            ("買い", lambda D: D["dir"] > 0), ("売り（空売り）", lambda D: D["dir"] < 0),
        ], data, pi)
        table("銘柄自身の寄り付きギャップ", [
            ("−2%未満", lambda D: D["gap"] < -0.02),
            ("−2〜0%", lambda D: (D["gap"] >= -0.02) & (D["gap"] < 0)),
            ("0〜+2%", lambda D: (D["gap"] >= 0) & (D["gap"] < 0.02)),
            ("+2%以上", lambda D: D["gap"] >= 0.02),
        ], data, pi)
        table("呼値の粗さ（1 tick が株価の何%か）", [
            ("0.02%未満（細かい）", lambda D: D["tick_rel"] < 2e-4),
            ("0.02〜0.05%", lambda D: (D["tick_rel"] >= 2e-4) & (D["tick_rel"] < 5e-4)),
            ("0.05%以上（粗い）", lambda D: D["tick_rel"] >= 5e-4),
        ], data, pi)
        table("約定までの待ち（その場で約定したか）", [
            ("0.05秒以内（その場）", lambda D: D["fill_wait"] <= 0.05),
            ("0.05〜1秒", lambda D: (D["fill_wait"] > 0.05) & (D["fill_wait"] <= 1)),
            ("1秒超", lambda D: D["fill_wait"] > 1),
        ], data, pi)
        table("決済のしかた", [
            ("指値で決済", lambda D: D["exit_limit"] & ~D["forced"]),
            ("5秒待って成行", lambda D: ~D["exit_limit"] & ~D["forced"]),
        ], data, pi)
        table("曜日", [(n, (lambda k: lambda D: np.array(
            [np.datetime64(str(d)).astype("datetime64[D]").item().weekday() == k for d in D["date"]]))(k))
            for k, n in enumerate(("月", "火", "水", "木", "金"))], data, pi)
        table("学習に使った銘柄か", [
            ("既知の銘柄", lambda D: ~D["held"]), ("学習外の銘柄", lambda D: D["held"]),
        ], data, pi)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="val")
    ap.add_argument("--n", type=int, default=2500)
    ap.add_argument("--procs", type=int, default=3)
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    if a.report:
        report()
    else:
        generate(a.split, a.n, a.procs)


if __name__ == "__main__":
    main()

"""Limit-order fill calibration against the kabu push board (plan Q2, rl-scalp-limit-plan.md).

For each (day, code) where both the kabu push board (S:/jp/stocks_board_kabu_push) and
J-Quants tick trades (S:/jp/stocks_trades) exist, place virtual 100-share limit orders
every STEP_S seconds in the morning session and judge their fills four ways on the SAME
orders:

* touch          -- fill on the first print at <= P (buy) / >= P (sell)   [env reference]
* trade_through  -- fill on the first print strictly through P             [env main rule]
* queue_fifo     -- our order joins the back of the displayed queue at P; fill once
                    prints at P exceed (queue ahead + 100), no cancellations ahead
* queue_cap      -- as queue_fifo, but the queue ahead can never exceed the qty the board
                    currently displays at P (cancels ahead inferred when the level shrinks)
                    ->  main estimate of the "true" fill

Board <-> trades sync uses the board's cumulative ``volume`` (exact: which prints a
snapshot already contains); the clock offset (board ts_local vs exchange ts) is only used
to time activation (decision + LATENCY_MS) and mid-price markouts.

Read-only on S:.  Output: .cache/trading_rl/fillcal/orders.npz + a printed summary.
Run:  uv run python -W ignore scripts/probes/fill_calib.py [--step 15] [--latency-ms 200]
"""

from __future__ import annotations

import argparse
import glob
import os
from multiprocessing import Pool
from pathlib import Path

import duckdb
import numpy as np

BOARD_DIR = "S:/jp/stocks_board_kabu_push"
TRADES_DIR = "S:/jp/stocks_trades"
OUT_DIR = Path(".cache/trading_rl/fillcal")
H_S = 60.0  # order lifetime (s)
MARKOUTS = (1.0, 5.0, 10.0, 30.0)
TYPES = ("join", "last_k0", "last_k1", "last_k2")
RULES = ("touch", "trade_through", "queue_fifo", "queue_cap")
# virtual FIFO queue sizes (shares ahead) for the trades-only rule calibration; 1e12 == trade_through
Q_GRID = (0, 100, 200, 500, 1000, 2000, 5000, 10000, 20000, 50000, 1e12)
VMARK = (1.0, 5.0, 30.0)
FEATS = ("f_ret1", "f_ret5", "f_ret30", "f_tickdir", "f_pos5", "f_n10", "f_vol10_rel",
         "f_medq", "f_tickbps", "f_bounce")
NS = 1_000_000_000


def _connect(path: str):
    con = duckdb.connect(path, read_only=True)
    con.execute("SET enable_progress_bar = false")
    return con


def _ns(col: str) -> str:
    return f"epoch_ns(cast({col} as timestamp))"


def trades_path(code: str) -> str | None:
    for c in (code, code.lower(), code.upper()):
        p = f"{TRADES_DIR}/{c}.duckdb"
        if os.path.exists(p):
            return p
    return None


def load_trades(code: str, day: str):
    p = trades_path(code)
    if p is None:
        return None
    con = _connect(p)
    r = con.execute(
        f"select {_ns('Timestamp')} as t, Price, Qty from stocks_board "
        f"where Timestamp >= '{day} 08:00' and Timestamp < '{day} 16:00' order by Timestamp"
    ).fetchnumpy()
    con.close()
    if len(r["t"]) == 0:
        return None
    return r["t"].astype(np.int64), r["Price"].astype(np.float64), r["Qty"].astype(np.int64)


BCOLS = (
    [f"bid_px_{i}" for i in range(1, 11)]
    + [f"bid_qty_{i}" for i in range(1, 11)]
    + [f"ask_px_{i}" for i in range(1, 11)]
    + [f"ask_qty_{i}" for i in range(1, 11)]
)


def load_board(con, code: str):
    r = con.execute(
        f"select {_ns('ts_local')} as t, volume, {', '.join(BCOLS)} from board_push "
        f"where code = '{code}' order by ts_local"
    ).fetchnumpy()
    if len(r["t"]) == 0:
        return None
    bt = r["t"].astype(np.int64)
    vol = np.nan_to_num(r["volume"].astype(np.float64)).astype(np.int64)
    bp = np.stack([r[f"bid_px_{i}"] for i in range(1, 11)], 1).astype(np.float64)
    bq = np.stack([r[f"bid_qty_{i}"] for i in range(1, 11)], 1).astype(np.float64)
    ap = np.stack([r[f"ask_px_{i}"] for i in range(1, 11)], 1).astype(np.float64)
    aq = np.stack([r[f"ask_qty_{i}"] for i in range(1, 11)], 1).astype(np.float64)
    return bt, vol, np.nan_to_num(bp), np.nan_to_num(bq), np.nan_to_num(ap), np.nan_to_num(aq)


def clock_offset_ns(tt, tq, bt, vol) -> tuple[int, float]:
    """Offset d (ns) s.t. board row at local bt contains exactly the prints with ts <= bt - d."""
    tv = np.cumsum(tq)

    def score(d):
        idx = np.searchsorted(tt, bt - d, side="right") - 1
        ok = idx >= 0
        if ok.sum() == 0:
            return 0.0
        return float(np.mean(vol[ok] == tv[idx[ok]]))

    grid = [(d, score(d * 1_000_000)) for d in range(-5000, 5001, 20)]
    d0 = max(grid, key=lambda x: x[1])[0]
    fine = [(d, score(d * 1_000_000)) for d in range(d0 - 30, d0 + 31, 2)]
    d, m = max(fine, key=lambda x: x[1])
    return d * 1_000_000, m


def infer_tick(bp: np.ndarray) -> float:
    d = bp[:, 0] - bp[:, 1]
    d = d[(bp[:, 1] > 0) & (d > 0)]
    if len(d) == 0:
        return np.nan
    v, c = np.unique(np.round(d, 4), return_counts=True)
    return float(v[c.argmax()])


def level_qty(px: np.ndarray, qty: np.ndarray, P: float, side: str) -> np.ndarray:
    """Displayed qty at price P on our side for each board row.

    0 if P is better than the best level (level empty / inside spread),
    inf if P is beyond the 10th displayed level (unknown, no constraint)."""
    hit = np.isclose(px, P)
    q = np.where(hit, qty, 0.0).sum(1)
    best = px[:, 0]
    worst = np.where(px > 0, px, np.nan)
    if side == "buy":
        worst = np.nanmin(worst, 1)
        better = P > best + 1e-9
        beyond = P < worst - 1e-9
    else:
        worst = np.nanmax(worst, 1)
        better = P < best - 1e-9
        beyond = P > worst + 1e-9
    q = np.where(hit.any(1), q, np.where(better, 0.0, np.inf))
    q = np.where(beyond & ~hit.any(1), np.inf, q)
    q = np.where(best <= 0, np.inf, q)
    return q


def trade_feats(tt, tp, tq, il, t0, tick):
    """Causal trades-only features at decision time t0 (prints [0..il])."""
    last = tp[il]

    def px_at(h):
        j = np.searchsorted(tt, t0 - int(h * NS), side="right") - 1
        return tp[j] if j >= 0 else last

    lo = np.searchsorted(tt, t0 - 300 * NS, side="right")
    i5 = np.searchsorted(tt, t0 - 5 * NS, side="right")
    i10 = np.searchsorted(tt, t0 - 10 * NS, side="right")
    w = tp[i5 : il + 1]
    hi5, lo5 = (w.max(), w.min()) if len(w) else (last, last)
    seg = tp[max(0, il - 200) : il + 1]
    dp = np.diff(seg)
    nz = dp[dp != 0]
    tickdir = float(np.sign(nz[-1])) if len(nz) else 0.0
    bounce = float(np.median(np.abs(nz[-50:])) / tick) if len(nz) else 0.0
    medq = float(np.median(tq[lo : il + 1])) if il + 1 > lo else 100.0
    vol10 = float(tq[i10 : il + 1].sum())
    return [
        (last - px_at(1)) / tick, (last - px_at(5)) / tick, (last - px_at(30)) / tick,
        tickdir, (last - lo5) / max(tick, hi5 - lo5), float(il + 1 - i10),
        vol10 / max(medq, 1.0), medq, tick / last * 1e4, bounce,
    ]


def run_code_day(args):
    day, code, off_ns, step_s, lat_ms = args
    tr = load_trades(code, day)
    if tr is None:
        return None
    tt, tp, tq = tr
    con = _connect(f"{BOARD_DIR}/{day}.duckdb")
    bd = load_board(con, code)
    con.close()
    if bd is None:
        return None
    bt, vol, bp, bq, ap, aq = bd
    be = bt - off_ns  # board rows on the exchange clock
    tick = infer_tick(bp)
    if not np.isfinite(tick) or tick <= 0:
        return None
    tv = np.cumsum(tq)
    kb = np.searchsorted(tv, vol, side="right")  # prints contained in each board row
    mid = np.where((bp[:, 0] > 0) & (ap[:, 0] > 0), (bp[:, 0] + ap[:, 0]) / 2, np.nan)

    day0 = np.datetime64(f"{day}T00:00:00", "ns").astype(np.int64)
    # trades/board timestamps are naive JST -> same base
    t_start = day0 + int((9 * 3600 + 30) * NS)
    t_end = day0 + int((11 * 3600 + 29 * 60) * NS)
    lat = int(lat_ms * 1_000_000)
    H = int(H_S * NS)
    recs = []
    n_stale = 0
    for t0 in range(t_start, t_end, int(step_s * NS)):
        a = t0 + lat
        r0 = np.searchsorted(be, a, side="right") - 1  # board state at activation
        if r0 < 0 or bp[r0, 0] <= 0 or ap[r0, 0] <= 0 or ap[r0, 0] <= bp[r0, 0]:
            continue
        # stale board guard: the snapshot must already contain every print older than
        # 2 s, and be at most 30 s old (recorder gaps, e.g. a late start after the open)
        if a - be[r0] > 30 * NS or kb[r0] < np.searchsorted(tt, a - 2 * NS, side="right"):
            n_stale += 1
            continue
        il = np.searchsorted(tt, t0, side="right") - 1
        if il < 0:
            continue
        last = tp[il]
        feats = trade_feats(tt, tp, tq, il, t0, tick)
        ka = np.searchsorted(tt, a, side="right")  # prints strictly after activation start at ka
        kh = np.searchsorted(tt, a + H, side="right")
        r1 = np.searchsorted(be, a + H, side="right")
        bid1, ask1 = bp[r0, 0], ap[r0, 0]
        spread_t = round((ask1 - bid1) / tick)
        fp, fq, ft = tp[ka:kh], tq[ka:kh], tt[ka:kh]
        rows = slice(r0, r1)
        for side in ("buy", "sell"):
            sg = 1.0 if side == "buy" else -1.0
            for typ in TYPES:
                if typ == "join":
                    P = bid1 if side == "buy" else ask1
                else:
                    k = int(typ[-1])
                    P = last - sg * k * tick
                rec = [day, code, (t0 - day0) / NS, side, typ, tick, spread_t,
                       (P - (bid1 if side == "buy" else ask1)) * sg / tick] + feats
                # trades-only virtual FIFO at fixed queue sizes (what the env could do)
                atv = np.isclose(fp, P)
                thv = (fp < P - 1e-9) if side == "buy" else (fp > P + 1e-9)
                Xv = np.cumsum(np.where(atv, fq, 0))
                vrec = []
                for qg in Q_GRID:
                    ok = (Xv >= qg + 100) | thv
                    i_v = np.argmax(ok) if ok.any() else -1
                    if i_v < 0:
                        vrec += [np.nan] * (1 + len(VMARK))
                        continue
                    vrec.append((ft[i_v] - a) / NS)
                    for h in VMARK:
                        j = np.searchsorted(be, ft[i_v] + int(h * NS), side="right") - 1
                        vrec.append(sg * ((mid[j] if j >= 0 else np.nan) - P) / tick)
                # marketable at activation -> taker fill at the opposite best
                opp = ask1 if side == "buy" else bid1
                if (side == "buy" and P >= opp - 1e-9) or (side == "sell" and P <= opp + 1e-9):
                    fpx = opp
                    fills = {r: (a, fpx) for r in RULES}
                    q0 = 0.0
                    taker = 1
                else:
                    taker = 0
                    if side == "buy":
                        at = np.isclose(fp, P)
                        thr = fp < P - 1e-9
                        tch = fp <= P + 1e-9
                    else:
                        at = np.isclose(fp, P)
                        thr = fp > P + 1e-9
                        tch = fp >= P - 1e-9
                    fills = {}
                    i_t = np.argmax(tch) if tch.any() else -1
                    i_x = np.argmax(thr) if thr.any() else -1
                    fills["touch"] = (ft[i_t], P) if i_t >= 0 else None
                    fills["trade_through"] = (ft[i_x], P) if i_x >= 0 else None
                    # queue models
                    L = level_qty(bp[rows] if side == "buy" else ap[rows],
                                  bq[rows] if side == "buy" else aq[rows], P, side)
                    q0 = L[0]
                    if not np.isfinite(q0):
                        q0 = np.nan
                    X = np.cumsum(np.where(at, fq, 0))  # prints at P after activation
                    # X at each board row (sync by contained prints)
                    cnt = np.clip(kb[rows] - ka, 0, len(fq))
                    Xs = np.concatenate([[0], X])[cnt]
                    for rule in ("queue_fifo", "queue_cap"):
                        if not np.isfinite(q0):
                            fills[rule] = fills["trade_through"]
                            continue
                        if rule == "queue_fifo":
                            Mt = np.full(len(fq), q0)
                        else:
                            cand = np.where(np.isfinite(L), L + Xs, np.inf)
                            cand[0] = min(cand[0], q0)
                            Mrow = np.minimum.accumulate(cand)
                            # M at print i = Mrow at the last board row that contains no
                            # print after i (sync by contained prints, not by clock)
                            ri = np.searchsorted(cnt, np.arange(1, len(fq) + 1), side="right") - 1
                            Mt = np.where(ri >= 0, Mrow[np.clip(ri, 0, None)], q0)
                        ok = (X >= Mt + 100) | thr
                        i_q = np.argmax(ok) if ok.any() else -1
                        fills[rule] = (ft[i_q], P) if i_q >= 0 else None
                rec += [taker, q0]
                for rule in RULES:
                    f = fills[rule]
                    if f is None:
                        rec += [np.nan] + [np.nan] * len(MARKOUTS)
                        continue
                    tf, px = f
                    rec.append((tf - a) / NS)
                    for h in MARKOUTS:
                        j = np.searchsorted(be, tf + int(h * NS), side="right") - 1
                        m = mid[j] if j >= 0 else np.nan
                        rec.append(sg * (m - px) / tick)
                recs.append(rec + vrec)
    if n_stale:
        print(f"  {day} {code}: skipped {n_stale} stale placements", flush=True)
    return recs


def day_offsets(days):
    out = {}
    for day in days:
        con = _connect(f"{BOARD_DIR}/{day}.duckdb")
        offs = []
        for code in ("7203", "8306", "6758", "9984"):
            tr = load_trades(code, day)
            bd = load_board(con, code)
            if tr is None or bd is None:
                continue
            d, m = clock_offset_ns(tr[0], tr[2], bd[0], bd[1])
            offs.append((d, m))
        con.close()
        if offs and np.median([o[1] for o in offs]) < 0.6:
            print(f"{day} SKIP: board volume does not match the prints {[round(o[1], 3) for o in offs]}")
            continue
        if offs:
            d = int(np.median([o[0] for o in offs]))
            out[day] = (d, [round(o[1], 3) for o in offs])
            print(f"{day} offset {d/1e6:+.0f} ms  match {out[day][1]}", flush=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--step", type=float, default=15.0)
    ap.add_argument("--latency-ms", type=float, default=200.0)
    ap.add_argument("--procs", type=int, default=4)
    ap.add_argument("--days", default="")
    ap.add_argument("--tag", default="")
    a = ap.parse_args()
    days = sorted(Path(p).stem for p in glob.glob(f"{BOARD_DIR}/*.duckdb"))
    days = [d for d in days if d <= "2026-08-19"]
    if a.days:
        days = [d for d in days if d in a.days.split(",")]
    offs = day_offsets(days)
    jobs = []
    for day, (d, _) in offs.items():
        con = _connect(f"{BOARD_DIR}/{day}.duckdb")
        codes = [r[0] for r in con.execute("select distinct code from board_push").fetchall()]
        con.close()
        jobs += [(day, c, d, a.step, a.latency_ms) for c in sorted(codes)]
    print(f"{len(jobs)} code-days", flush=True)
    recs = []
    with Pool(a.procs) as pool:
        for i, r in enumerate(pool.imap_unordered(run_code_day, jobs, chunksize=2)):
            if r:
                recs += r
            if (i + 1) % 50 == 0:
                print(f"  {i+1}/{len(jobs)} code-days, {len(recs)} orders", flush=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cols = ["day", "code", "t_sec", "side", "type", "tick", "spread_t", "px_vs_best_t", *FEATS,
            "taker", "q0"]
    for rule in RULES:
        cols += [f"{rule}_fill_s"] + [f"{rule}_mo{int(h)}" for h in MARKOUTS]
    for gi in range(len(Q_GRID)):
        cols += [f"v{gi}_fill_s"] + [f"v{gi}_mo{int(h)}" for h in VMARK]
    arr = {c: np.array([r[i] for r in recs]) for i, c in enumerate(cols)}
    out = OUT_DIR / f"orders{a.tag}.npz"
    np.savez_compressed(out, **arr)
    print(f"saved {out} ({len(recs)} orders)")


if __name__ == "__main__":
    main()

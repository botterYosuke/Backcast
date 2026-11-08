"""Build the burst detector (train only) and the burst-episode datasets.

  uv run python -W ignore scripts/burst/build_burst.py detector
  uv run python -W ignore scripts/burst/build_burst.py episodes --split train
  uv run python -W ignore scripts/burst/build_burst.py episodes --split val
  uv run python -W ignore scripts/burst/build_burst.py episodes --split val2   # once, at the end

Detector: HGB on N_FIT train sessions (known stems, every STRIDE s), threshold = the
Q quantile of the scores of N_THR *other* train sessions (every second).
Episodes: train = N_TRAIN_EP known-stem train sessions not used to fit the detector;
val / val2 = every session (known + held-out stems).
Outputs: models/burst/detector_v1.pkl, .cache/trading_rl/burst/episodes_<split>.npz
"""

from __future__ import annotations

import argparse
import sys
import time
import zlib
from multiprocessing import Pool

import numpy as np

sys.path.insert(0, "src")
from trading_rl import burst_env as be  # noqa: E402
from trading_rl.cache import _get_shard, load_cached_session  # noqa: E402
from trading_rl.scalp_env import (  # noqa: E402
    MORNING_CACHE_DIR,
    is_heldout,
    morning_window_s,
)
from trading_rl.universe import load_universe  # noqa: E402

DET_PATH = be.MODEL_DIR / "detector_v1.pkl"
N_FIT, STRIDE, N_THR, N_TRAIN_EP, Q = 1200, 4, 600, 100000, 0.995  # train episodes: every non-fit session
PROCS = 3


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def session_rows(lo: str, hi: str) -> list[tuple]:
    if hi > be.LAST_USABLE_DATE:
        raise ValueError("dates after 2026-08-19 are reserved for the owner's final test")
    out = []
    for r in load_universe(MORNING_CACHE_DIR, lo, hi):
        date, stem = str(r["date"]), str(r["stem"]).lower()
        try:
            x = _get_shard(MORNING_CACHE_DIR, stem).rows.get(date)
        except Exception:  # noqa: BLE001
            continue
        if x and x[0] == "ok":
            out.append((date, stem, float(r["prev_close"] or 0.0),
                        float(r["avg_volume_20d"] or 0.0), bool(r["is_topix100"])))
    return out


def grid_of(row) -> be.SessionGrid:
    date, stem, pc, av, t100 = row
    raw = load_cached_session(date, stem, MORNING_CACHE_DIR)
    return be.session_grid(raw, prev_close=pc, avg_volume_20d=av, is_topix100=t100,
                           window_s=morning_window_s(raw.t_open_us))


def train_split_rows():
    rows = [r for r in session_rows(*be.TRAIN) if not is_heldout(r[1])]
    rng = np.random.default_rng(20261108)
    perm = rng.permutation(len(rows))
    fit = [rows[i] for i in sorted(perm[:N_FIT])]
    thr = [rows[i] for i in sorted(perm[N_FIT : N_FIT + N_THR])]
    ep = [rows[i] for i in sorted(perm[N_FIT : N_FIT + N_TRAIN_EP])]  # thr sessions included
    return rows, fit, thr, ep


# ---------------------------------------------------------------- workers
def w_fit(row):
    try:
        g = grid_of(row)
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)
    y = be.burst_labels(g)
    off = zlib.crc32(f"{row[0]}{row[1]}".encode()) % STRIDE
    i = np.arange(off, len(g.secs), STRIDE)
    i = i[g.secs[i] + be.EVENT_H <= g.W]
    return (g.X[i], y[i], np.full(len(i), g.date)), None


_DET: be.Detector | None = None


def w_init():
    global _DET
    _DET = be.Detector.load(DET_PATH)


def w_score(row):
    try:
        g = grid_of(row)
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)
    s = _DET.score(g.X)
    y = be.burst_labels(g)
    m = g.secs + be.EVENT_H <= g.W
    return (s, y, m, g.secs < 600), None


def w_episodes(row):
    try:
        g = grid_of(row)
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)
    s = _DET.score(g.X)
    e = be.session_episodes(g, s, _DET.threshold)
    e.update(date=g.date, stem=g.stem, held=is_heldout(g.stem), t100=g.is_topix100, tod0=g.tod_open_s)
    return e, None


# ---------------------------------------------------------------- stages
def stage_detector():
    if DET_PATH.exists():
        raise SystemExit(f"{DET_PATH} exists; refusing to overwrite")
    rows, fit, thr, _ = train_split_rows()
    log(f"train rows {len(rows)}; fit {len(fit)} thr {len(thr)}")
    X, y, D, bad = [], [], [], 0
    with Pool(PROCS) as pool:
        for k, (r, _err) in enumerate(pool.imap(w_fit, fit, chunksize=4)):
            if r is None:
                bad += 1
                continue
            X.append(r[0])
            y.append(r[1])
            D.append(r[2])
            if (k + 1) % 200 == 0:
                log(f"  fit features {k+1}/{len(fit)}")
    X, y, D = np.concatenate(X), np.concatenate(y), np.concatenate(D)
    log(f"fit rows {len(y)} pos {y.mean():.3f} (bad {bad}); fitting HGB")
    det = be.fit_detector(X, y, D, X[:1000], D[:1000], q=Q)  # threshold reset below
    del X
    tmp = DET_PATH.with_suffix(".tmp.pkl")
    if tmp.exists():
        tmp.unlink()
    det.save(tmp)
    S, Y, M, O = [], [], [], []
    with Pool(PROCS, initializer=_init_tmp, initargs=(str(tmp),)) as pool:
        for r, _err in pool.imap(w_score, thr, chunksize=4):
            if r is None:
                continue
            S.append(r[0]); Y.append(r[1]); M.append(r[2]); O.append(r[3])  # noqa: E702
    tmp.unlink()
    S, Y, M, O = map(np.concatenate, (S, Y, M, O))
    from sklearn.metrics import roc_auc_score

    det.threshold = float(np.quantile(S, Q))
    auc = float(roc_auc_score(Y[M], S[M]))
    sel = S >= det.threshold
    det.meta.update(
        n_thr_rows=int(len(S)), thr_sessions=len(thr), auc_thr_sessions=auc,
        thr_dates=(min(r[0] for r in thr), max(r[0] for r in thr)),
        date_max=max(det.meta["date_max"], max(r[0] for r in thr)),
        precision_at_thr=float(Y[sel & M].mean()), base_rate=float(Y[M].mean()),
        share_trigger_secs_opening10m=float(O[sel].mean()),
    )
    assert det.meta["date_max"] <= be.TRAIN[1]
    det.save(DET_PATH)
    log(f"saved {DET_PATH}: thr={det.threshold:.4f} AUC(train thr sessions)={auc:.3f} "
        f"precision@thr={det.meta['precision_at_thr']:.3f} base={det.meta['base_rate']:.3f} "
        f"opening10m share={det.meta['share_trigger_secs_opening10m']:.2f}")


def _init_tmp(path):
    global _DET
    _DET = be.Detector.load(path)


def stage_episodes(split: str):
    out = be.DEFAULT_DIR / f"episodes_{split}.npz"
    if out.exists():
        raise SystemExit(f"{out} exists")
    if split == "train":
        _, _, _, rows = train_split_rows()
    else:
        rows = session_rows(*be.SPLITS[split])
    log(f"{split}: {len(rows)} sessions")
    parts, sessions, bad = [], [], 0
    with Pool(PROCS, initializer=w_init) as pool:
        for k, (r, _err) in enumerate(pool.imap(w_episodes, rows, chunksize=4)):
            if r is None:
                bad += 1
                continue
            parts.append(r)
            sessions.append((r["date"], r["stem"], bool(r["held"])))
            if (k + 1) % 500 == 0:
                log(f"  {k+1}/{len(rows)} episodes so far {sum(len(p['s0']) for p in parts)}")
    det = be.Detector.load(DET_PATH)
    data = be.concat_episodes(parts, sessions, meta={
        "split": split, "detector": str(DET_PATH), "threshold": det.threshold,
        "ep_s": be.EP_S, "bad_sessions": bad})
    data.save(out)
    log(f"saved {out}: {len(data)} episodes from {len(sessions)} sessions "
        f"({len(data)/max(len(sessions),1):.2f}/session), bad {bad}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["detector", "episodes"])
    ap.add_argument("--split", default="train", choices=list(be.SPLITS))
    a = ap.parse_args()
    if a.stage == "detector":
        stage_detector()
    else:
        stage_episodes(a.split)


if __name__ == "__main__":
    main()

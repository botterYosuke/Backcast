"""Baselines, RL evaluation, stress tests and the one-shot val2 check.

  uv run python -W ignore scripts/burst/eval_burst.py rules                 # grid on val (known stems)
  uv run python -W ignore scripts/burst/eval_burst.py report --models a.pt,b.pt [--out NAME]
  uv run python -W ignore scripts/burst/eval_burst.py final --model X.pt    # val2, ONCE

All CIs: 95% day bootstrap (1000 resamples) of pooled ratios.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import torch

sys.path.insert(0, "src")
sys.path.insert(0, "scripts/burst")
import burst_rules as br  # noqa: E402
from train_burst import ActorCritic, torch_policy  # noqa: E402

from trading_rl import burst_env as be  # noqa: E402

OUT = be.DEFAULT_DIR
STRESS = {
    "基準（ずれ0.5tick・200ms）": be.BurstConfig(),
    "ずれ1tick": be.BurstConfig(slip_ticks=1.0),
    "遅れ0.5秒": be.BurstConfig(latency_ms=500),
    "遅れ1秒": be.BurstConfig(latency_ms=1000),
    "遅れ中の最悪値で約定（200ms）": be.BurstConfig(fill_price="worst"),
    "ずれ1tick＋遅れ1秒": be.BurstConfig(latency_ms=1000, slip_ticks=1.0),
}
TPS = (2, 3, 5, 8, br.INF)
SLS = (2, 3, 5, 8, br.INF)
TS = (5, 10, 20, 60)
MS = (1, 5, 30)


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def load(split: str) -> be.BurstData:
    return be.BurstData.load(OUT / f"episodes_{split}.npz")


def smin_levels(train: be.BurstData) -> dict[str, float]:
    """Entry filters on the detector log-odds, fixed from TRAIN trigger scores."""
    s = be.logit(train.score[:, 0])
    return {"なし": -br.INF, "上位50%": float(np.quantile(s, 0.5)), "上位10%": float(np.quantile(s, 0.9))}


def f3(t, d=2):
    m, lo, hi = t
    return f"{m:+.{d}f} [{lo:+.{d}f}, {hi:+.{d}f}]"


def metrics(data: be.BurstData, res: dict) -> dict:
    T = res["trips"]
    held_ep = lambda T: data.held[T["ep"].astype(np.int64)]  # noqa: E731
    b = lambda T: be.tod_bucket(T["since_open"], T["tod"])  # noqa: E731
    out = {"全体": be.summarize(data, res),
           "既知銘柄": be.summarize(data, res, lambda T: ~held_ep(T), ~data.sess_held),
           "学習外銘柄": be.summarize(data, res, lambda T: held_ep(T), data.sess_held)}
    for k, name in enumerate(be.TOD_BUCKETS):
        out[name] = be.summarize(data, res, (lambda k: lambda T: b(T) == k)(k))
    out["_n_entries"] = res.get("n_entries")
    del T
    return out


def row_md(name: str, s: dict) -> str:
    if s["n_trips"] == 0:
        return f"| {name} | 0 | – | – | – | – | – |"
    return (f"| {name} | {s['n_trips']} | {f3(s['tick_trip'])} | {f3(s['tick_sess'])} | "
            f"{f3(s['trips_sess'])} | {s['win'][0]*100:.0f}% [{s['win'][1]*100:.0f}, {s['win'][2]*100:.0f}] | "
            f"{s['hold_med']:.0f}秒 |")


HDR = ("| 方策 | 売買回数 | コスト込み tick/回 [95%CI] | tick/日・銘柄 [95%CI] | 回/日・銘柄 [95%CI] | 勝率 [95%CI] | 保有の中央値 |\n"
       "|---|---|---|---|---|---|---|")


def trips_res(trips: dict) -> dict:
    return {"trips": trips, "n_entries": len(trips["pnl"])}


# ------------------------------------------------------------------ rules
def cmd_rules(a) -> None:
    tr, va = load("train"), load("val")
    lv = smin_levels(tr)
    del tr
    known = va.subset(~va.held)
    known.sess_date, known.sess_stem, known.sess_held = (
        va.sess_date[~va.sess_held], va.sess_stem[~va.sess_held], va.sess_held[~va.sess_held])
    cfg = be.BurstConfig()
    exits = [(tp, sl, T) for tp in TPS for sl in SLS for T in TS]
    rows = []
    for m in MS:
        for mode in (1, -1):
            for sname, smin in lv.items():
                outs = br.rule_trips_fast(known, cfg, m, mode, smin, exits)
                for (tp, sl, T), t in zip(exits, outs, strict=True):
                    p = t["pnl"]
                    rows.append({"m": m, "mode": mode, "smin_name": sname, "smin": smin, "tp": tp,
                                 "sl": sl, "T": T, "n": int(len(p)), "tick_trip": float(p.mean()),
                                 "win": float((p > 0).mean()), "tick_sess": float(p.sum() / len(known.sess_date))})
            log(f"m={m} mode={mode} done ({len(rows)})")
    ok = [r for r in rows if r["n"] >= 500]
    best = max(ok, key=lambda r: r["tick_trip"])
    s = be.summarize(known, trips_res(br.rule_trips_fast(known, cfg, best["m"], best["mode"], best["smin"],
                                                          [(best["tp"], best["sl"], best["T"])])[0]))
    best["ci_tick_trip"] = s["tick_trip"]
    json.dump({"levels": lv, "grid": rows, "best": best, "n_rules": len(rows)},
              open(OUT / "rules_val.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    rows.sort(key=lambda r: -r["tick_trip"])
    print(f"{len(rows)} rules; positive mean: {sum(r['tick_trip'] > 0 for r in rows)}")
    for r in rows[:15]:
        print(r)
    print("best", best)
    # summary by entry type (best exit for each)
    print("\nbest exit per entry (m, mode, smin):")
    seen = set()
    for r in rows:
        k = (r["m"], r["mode"], r["smin_name"])
        if k in seen or r["n"] < 500:
            continue
        seen.add(k)
        print(k, f"tp={r['tp']} sl={r['sl']} T={r['T']} n={r['n']} tick/trip={r['tick_trip']:+.3f} win={r['win']:.2f}")


def best_rule() -> dict:
    return json.load(open(OUT / "rules_val.json", encoding="utf-8"))["best"]


def rule_policy(b: dict, random_dir: bool = False) -> br.RulePolicy:
    return br.RulePolicy(m=b["m"], mode=b["mode"], tp=b["tp"], sl=b["sl"], T=b["T"], smin=b["smin"],
                         random_dir=random_dir)


def load_model(path: str) -> ActorCritic:
    ck = torch.load(path, weights_only=False)
    m = ActorCritic(be.OBS_DIM, ck["hidden"], np.zeros(be.OBS_DIM), np.ones(be.OBS_DIM))
    m.load_state_dict(ck["state_dict"])
    m.eval()
    return m


def policies(models: list[str]) -> dict:
    b = best_rule()
    pol = {"何もしない": br.nothing, "ランダム（毎秒でたらめ）": br.UniformRandom(0),
           "ランダム向き＋ルールと同じ出口": rule_policy(b, random_dir=True),
           f"単純ルール（{b['m']}秒の{'順張り' if b['mode'] > 0 else '逆張り'}・利確{b['tp']:g}・損切{b['sl']:g}・最長{b['T']}秒・検出器{b['smin_name']}）": rule_policy(b)}
    for p in models:
        pol[f"RL {Path(p).stem}"] = torch_policy(load_model(p))
    return pol


def evaluate(data: be.BurstData, pol: dict, stress: bool, lines: list[str], results: dict) -> None:
    for sname, cfg in (STRESS.items() if stress else list(STRESS.items())[:1]):
        lines += [f"\n### {sname}\n", HDR]
        for name, f in pol.items():
            if isinstance(f, br.UniformRandom):
                f.rng = np.random.default_rng(0)
            if isinstance(f, br.RulePolicy):
                f.rng = np.random.default_rng(0)
            res = be.run_policy(data, f, cfg)
            m = metrics(data, res)
            results.setdefault(sname, {})[name] = m
            lines.append(row_md(name, m["全体"]))
        log(f"  {sname} done")
    base = list(STRESS)[0]
    for part in ("既知銘柄", "学習外銘柄", *be.TOD_BUCKETS):
        lines += [f"\n### 内訳: {part}（{base}）\n", HDR]
        for name in pol:
            lines.append(row_md(name, results[base][name][part]))


def cmd_report(a) -> None:
    va = load(a.split)
    models = [m for m in a.models.split(",") if m]
    lines = [f"# burst 評価（{a.split}、{len(va)} エピソード / {len(va.sess_date)} セッション / "
             f"{len(np.unique(va.sess_date))} 日）"]
    results: dict = {}
    evaluate(va, policies(models), True, lines, results)
    path = OUT / f"report_{a.out}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    json.dump(results, open(OUT / f"report_{a.out}.json", "w", encoding="utf-8"), ensure_ascii=False,
              default=float, indent=1)
    print("\n".join(lines))


def cmd_final(a) -> None:
    lock = OUT / "val2_used.json"
    if lock.exists():
        raise SystemExit(f"val2 was already used: {lock.read_text(encoding='utf-8')}")
    d = load("val2")
    pol = policies([m for m in a.model.split(",") if m])
    lock.write_text(json.dumps({"model": a.model, "time": time.strftime("%Y-%m-%d %H:%M:%S")}), encoding="utf-8")
    lines = [f"# 検証その2（1回だけ）: {len(d)} エピソード / {len(d.sess_date)} セッション / "
             f"{len(np.unique(d.sess_date))} 日、方策 {a.model}"]
    results: dict = {}
    evaluate(d, pol, True, lines, results)
    (OUT / "report_val2_final.md").write_text("\n".join(lines), encoding="utf-8")
    json.dump(results, open(OUT / "report_val2_final.json", "w", encoding="utf-8"), ensure_ascii=False,
              default=float, indent=1)
    print("\n".join(lines))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["rules", "report", "final"])
    ap.add_argument("--models", default="")
    ap.add_argument("--model", default="", help="comma list: the chosen recipe's seeds (main first)")
    ap.add_argument("--split", default="val", choices=["val"])
    ap.add_argument("--out", default="val")
    a = ap.parse_args()
    {"rules": cmd_rules, "report": cmd_report, "final": cmd_final}[a.cmd](a)


if __name__ == "__main__":
    main()

import sys
import numpy as np
sys.path.insert(0, "scripts")
from train_scalp import split_rows, sample_rows, load_scalp_sessions
from trading_rl.scalp_replay import load_checkpoint_cached, scalp_config_from_checkpoint
from trading_rl.vec_rollout import evaluate_ppo_policy

seen, held = split_rows("test")
rows = sample_rows(seen, 600, seed=1) + sample_rows(held, 200, seed=1)
sess = load_scalp_sessions(rows)
for name in ("market_v2_realized_g1", "market_v2_realized_uloss"):
    pol, ck = load_checkpoint_cached(f"models/ppo_scalp_top50_open10m_{name}.pt")
    cfg = scalp_config_from_checkpoint(ck)
    eps = evaluate_ppo_policy(pol, sess, cfg, use_masks=True)
    trips = [t for e in eps for t in e["trips"]]
    pnl = np.array([t["pnl_ticks"] for t in trips]); hold = np.array([t["hold_s"] for t in trips])
    forced = np.array([t["forced"] for t in trips])
    win, loss = pnl > 0, pnl < 0
    print(f"== {name}: {len(eps)} sess, {len(trips)} trips")
    print(f"  勝ち {win.sum()} 件 平均 {pnl[win].mean():+.2f} 呼値 保有中央値 {np.median(hold[win]):.0f}s")
    print(f"  負け {loss.sum()} 件 平均 {pnl[loss].mean():+.2f} 呼値 保有中央値 {np.median(hold[loss]):.0f}s")
    print(f"  強制決済 {forced.sum()} 件 平均 {pnl[forced].mean():+.2f} 呼値 保有中央値 {np.median(hold[forced]):.0f}s, 強制決済で負け {(pnl[forced]<0).mean()*100:.0f}%")
    print(f"  強制決済の損益合計 {pnl[forced].sum():+.0f} / 全体 {pnl.sum():+.0f} 呼値")
    # sessions where the only trip is a forced close held > 500 s
    one = sum(1 for e in eps if e["trips"] and e["trips"][0]["hold_s"] > 500)
    print(f"  最初の売買を500秒超持った日 {one}/{len(eps)}")

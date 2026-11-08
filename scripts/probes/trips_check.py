import sys, collections
sys.path.insert(0, "scripts"); sys.path.insert(0, "src")
import numpy as np
from pathlib import Path
from train_scalp import split_rows, sample_rows
from trading_rl.scalp_env import ScalpConfig, load_scalp_sessions
from trading_rl.scalp_replay import load_checkpoint_cached, simulate_scalp_trades, uses_action_mask
from trading_rl.vec_rollout import evaluate_ppo_policy
from trading_rl.cache import load_cached_session
from trading_rl.universe import DEFAULT_CACHE_DIR

model = sys.argv[1]
policy, ck = load_checkpoint_cached(Path(model))
feat = ck["env_config"]["features"]
seen, held = split_rows("val")
rows = sample_rows(seen, 40, 1)
sess = load_scalp_sessions(rows, features=feat)
cfg = ScalpConfig(fill_rule="trade_through", order_mode="market", features=feat)
eps = evaluate_ppo_policy(policy, sess, cfg, use_masks=uses_action_mask(ck))
ev = [e["n_trips"] for e in eps]
rp = []
for r in rows:
    date, stem, pc, av, t100 = r
    raw = load_cached_session(date, stem, DEFAULT_CACHE_DIR)
    trades, summ, _ = simulate_scalp_trades(raw, model, prev_close=pc, avg_volume_20d=av, is_topix100=t100)
    rp.append(summ["tradeCount"])
print("eval  trips:", ev, "mean", np.mean(ev))
print("replay trips:", rp, "mean", np.mean(rp))
print("hist eval:", sorted(collections.Counter(ev).items()))

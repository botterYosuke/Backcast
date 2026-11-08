import sys, collections
sys.path.insert(0, "scripts"); sys.path.insert(0, "src")
import numpy as np, torch
from pathlib import Path
from train_scalp import split_rows, sample_rows
from trading_rl.scalp_env import ScalpConfig, ScalpEnv, load_scalp_sessions, G_KEEP
from trading_rl.scalp_replay import load_checkpoint_cached
from trading_rl.policy import apply_action_mask

seen, _ = split_rows("val")
rows = sample_rows(seen, 150, 2)
for model in sys.argv[1:]:
    policy, ck = load_checkpoint_cached(Path(model)); policy.eval()
    feat = ck["env_config"]["features"]
    sess = load_scalp_sessions(rows, features=feat)
    env = ScalpEnv(sess, ScalpConfig(order_mode="market", features=feat))
    keep_ix = env.action_index(G_KEEP)
    first_entry, holds, ntr, pk, samp_tr = [], [], [], [], []
    for i in range(len(sess)):
        obs, _ = env.reset(options={"index": i}); done = False; d = 0; fe = None
        while not done:
            m = env.action_mask()
            flat = env.state_view()["pos"] == 0
            with torch.no_grad():
                o = torch.as_tensor(obs, dtype=torch.float32).unsqueeze(0)
                lg = apply_action_mask(policy.actor(policy.feature_extractor(policy._normalize(o))), torch.as_tensor(m).reshape(1,-1))
                p = torch.softmax(lg, -1)[0]
            if flat: pk.append(float(p[keep_ix]))
            a = int(torch.argmax(p))
            if not flat and fe is None: pass
            if flat and a != keep_ix and fe is None: fe = d + 10
            obs, _, te, tr, info = env.step(a); done = te or tr; d += 1
        ntr.append(len(env._trips)); holds += [t["hold_s"] for t in env._trips]
        if fe is not None: first_entry.append(fe)
        # stochastic play for comparison
        obs, _ = env.reset(options={"index": i}); done = False
        while not done:
            a = policy.predict(obs, deterministic=False, mask=env.action_mask())
            obs, _, te, tr, info = env.step(a); done = te or tr
        samp_tr.append(len(env._trips))
    pk = np.array(pk)
    print(Path(model).stem)
    print("  argmax trips/sess mean %.2f median %.0f  hist %s" % (np.mean(ntr), np.median(ntr), sorted(collections.Counter(ntr).items())[:6]))
    print("  sampled trips/sess mean %.2f" % np.mean(samp_tr))
    print("  first entry s after open pctl10/50/90:", np.percentile(first_entry,[10,50,90]).round(), " (sessions w/ entry %d/%d)" % (len(first_entry), len(sess)))
    print("  hold s pctl10/50/90:", np.percentile(holds,[10,50,90]).round(1))
    print("  P(keep) while flat: median %.3f, share of flat seconds with P(keep)<0.5: %.4f" % (np.median(pk), (pk<0.5).mean()))

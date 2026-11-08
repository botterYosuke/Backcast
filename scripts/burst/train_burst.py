"""PPO (masked, vectorised) on the burst practice ground.

  uv run python -W ignore scripts/burst/train_burst.py --tag v1 --seed 0 --steps 20000000

Train episodes: .cache/trading_rl/burst/episodes_train.npz (train period, known stems).
A fixed val subsample (VAL_EVAL episodes of episodes_val.npz) is evaluated every
--eval-every iterations for the learning curve only; the saved model is the final one.
Outputs (never overwritten): models/burst/ppo_burst_<tag>_s<seed>.pt and
.cache/trading_rl/burst/curve_<tag>_s<seed>.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time

import numpy as np
import torch
from torch import nn

sys.path.insert(0, "src")
sys.path.insert(0, "scripts/burst")
from trading_rl import burst_env as be  # noqa: E402
from trading_rl.burst_replay import ActorCritic  # noqa: E402

VAL_EVAL = 20000


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


# the network lives in trading_rl.burst_replay (also used by the app replay)


def normalizer(data: be.BurstData) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(0)
    i = rng.choice(len(data), size=min(20000, len(data)), replace=False)
    X = data.X[i].reshape(-1, be.N_MARKET).astype(np.float64)
    det = be.logit(data.score[i]).ravel().astype(np.float64)
    mean = np.concatenate([X.mean(0), [det.mean(), 0, 0, 0, 0.5]])
    std = np.concatenate([X.std(0), [det.std(), 1, 5, 0.5, 0.3]])
    return mean.astype(np.float32), np.where(std < 1e-8, 1.0, std).astype(np.float32)


def torch_policy(model: ActorCritic, greedy: bool = True, seed: int = 0):
    gen = torch.Generator().manual_seed(seed)

    def f(obs, mask, _bb):
        with torch.no_grad():
            lg, _ = model(torch.from_numpy(obs), torch.from_numpy(mask))
            if greedy:
                return lg.argmax(-1).numpy()
            return torch.multinomial(torch.softmax(lg, -1), 1, generator=gen).squeeze(-1).numpy()

    return f


def quick_eval(model, data, idx, cfg):
    res = be.run_policy(data, torch_policy(model), cfg, idx)
    p = res["trips"]["pnl"]
    n = len(idx)
    return {"trips_ep": len(p) / n, "tick_trip": float(p.mean()) if len(p) else 0.0,
            "tick_ep": float(p.sum() / n), "win": float((p > 0).mean()) if len(p) else 0.0,
            "hold_med": float(np.median(res["trips"]["hold_s"])) if len(p) else 0.0}


def rule_bc_actions(bb: be.BurstBatch, params: dict) -> np.ndarray:
    """Actions of the simple rule (used for the BC warm start)."""
    sys.path.insert(0, "scripts/burst")
    from burst_rules import RulePolicy

    return RulePolicy(**params)(bb.obs(), bb.mask(), bb)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--steps", type=float, default=20e6)
    ap.add_argument("--n-env", type=int, default=1024)
    ap.add_argument("--hidden", type=int, default=256)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--gamma", type=float, default=1.0)
    ap.add_argument("--lam", type=float, default=0.95)
    ap.add_argument("--epochs", type=int, default=4)
    ap.add_argument("--minibatch", type=int, default=8192)
    ap.add_argument("--clip", type=float, default=0.2)
    ap.add_argument("--ent-start", type=float, default=0.02)
    ap.add_argument("--ent-end", type=float, default=0.002)
    ap.add_argument("--uloss", type=float, default=0.01)
    ap.add_argument("--hold-pen", type=float, default=0.0)
    ap.add_argument("--entry-bonus", type=float, default=0.0)
    ap.add_argument("--latency-ms", type=int, default=200)
    ap.add_argument("--slip", type=float, default=0.5)
    ap.add_argument("--bc-rule", default=None, help="JSON params of burst_rules.RulePolicy")
    ap.add_argument("--bc-steps", type=int, default=3000)
    ap.add_argument("--eval-every", type=int, default=10)
    ap.add_argument("--threads", type=int, default=1)
    ap.add_argument("--train-file", default="episodes_train.npz")
    a = ap.parse_args()

    torch.set_num_threads(a.threads)
    torch.manual_seed(a.seed)
    rng = np.random.default_rng(a.seed)
    out = be.MODEL_DIR / f"ppo_burst_{a.tag}_s{a.seed}.pt"
    curve_path = be.DEFAULT_DIR / f"curve_{a.tag}_s{a.seed}.csv"
    if out.exists() or curve_path.exists():
        raise SystemExit(f"{out} or {curve_path} exists; refusing to overwrite")
    tr = be.BurstData.load(be.DEFAULT_DIR / a.train_file)
    va = be.BurstData.load(be.DEFAULT_DIR / "episodes_val.npz")
    vidx = np.sort(np.random.default_rng(123).choice(len(va), size=min(VAL_EVAL, len(va)), replace=False))
    log(f"train episodes {len(tr)}, val episodes {len(va)} (curve on {len(vidx)})")
    cfg_tr = be.BurstConfig(latency_ms=a.latency_ms, slip_ticks=a.slip,
                            unrealized_loss_penalty_per_s=a.uloss, hold_penalty_per_s=a.hold_pen,
                            entry_bonus_ticks=a.entry_bonus)
    cfg_ev = be.BurstConfig(latency_ms=a.latency_ms, slip_ticks=a.slip)
    mean, std = normalizer(tr)
    model = ActorCritic(be.OBS_DIM, a.hidden, mean, std)
    opt = torch.optim.Adam(model.parameters(), lr=a.lr, eps=1e-5)
    T = tr.ep_s
    curve_f = open(curve_path, "w", newline="", encoding="utf-8")
    cw = csv.writer(curve_f)
    cw.writerow(["iter", "steps", "ent_coef", "train_ret", "train_pnl_ep", "train_trips_ep",
                 "train_tick_trip", "entropy", "val_tick_trip", "val_tick_ep", "val_trips_ep",
                 "val_win", "val_hold_med"])

    # ---------------------------------------------------------------- BC warm start
    if a.bc_rule:
        params = json.loads(a.bc_rule)
        log(f"BC from rule {params}")
        bopt = torch.optim.Adam(model.pi.parameters(), lr=1e-3)
        for it in range(a.bc_steps):
            bb = be.BurstBatch(tr, rng.integers(0, len(tr), 256), cfg_ev)
            O, M, A = [], [], []
            while not bb.done:
                o, m = bb.obs(), bb.mask()
                act = rule_bc_actions(bb, params)
                O.append(o); M.append(m); A.append(act)  # noqa: E702
                bb.step(act)
            o, m, act = (torch.from_numpy(np.concatenate(x)) for x in (O, M, A))
            lg, _ = model(o, m)
            loss = nn.functional.cross_entropy(lg, act)
            bopt.zero_grad(); loss.backward(); bopt.step()  # noqa: E702
            if (it + 1) % 500 == 0:
                acc = (lg.argmax(-1) == act).float().mean().item()
                log(f"  BC {it+1}: loss {loss.item():.4f} acc {acc:.4f}")
        log(f"after BC: val {quick_eval(model, va, vidx, cfg_ev)}")

    n_iter = int(np.ceil(a.steps / (a.n_env * T)))
    t0 = time.time()
    for it in range(n_iter):
        frac = it / max(n_iter - 1, 1)
        ent_coef = a.ent_start + (a.ent_end - a.ent_start) * frac
        for g in opt.param_groups:
            g["lr"] = a.lr * (1 - 0.9 * frac)
        bb = be.BurstBatch(tr, rng.integers(0, len(tr), a.n_env), cfg_tr)
        O = np.zeros((T, a.n_env, be.OBS_DIM), np.float32)
        M = np.zeros((T, a.n_env, be.N_ACT), bool)
        A = np.zeros((T, a.n_env), np.int64)
        LP = np.zeros((T, a.n_env), np.float32)
        V = np.zeros((T + 1, a.n_env), np.float32)
        R = np.zeros((T, a.n_env), np.float32)
        with torch.no_grad():
            for t in range(T):
                O[t], M[t] = bb.obs(), bb.mask()
                lg, v = model(torch.from_numpy(O[t]), torch.from_numpy(M[t]))
                dist = torch.distributions.Categorical(logits=lg)
                act = dist.sample()
                A[t], LP[t], V[t] = act.numpy(), dist.log_prob(act).numpy(), v.numpy()
                R[t], _ = bb.step(A[t])
        tt = bb.trip_table()
        adv = np.zeros_like(R)
        last = np.zeros(a.n_env, np.float32)
        for t in reversed(range(T)):
            nv = V[t + 1] if t < T - 1 else 0.0
            delta = R[t] + a.gamma * nv - V[t]
            last = delta + a.gamma * a.lam * last * (t < T - 1)
            adv[t] = last
        ret = adv + V[:T]
        o, m, act, lp0, ad, rt = (torch.from_numpy(x.reshape(-1, *x.shape[2:])) for x in (O, M, A, LP, adv, ret))
        N = len(act)
        ents = []
        for _ in range(a.epochs):
            perm = torch.randperm(N)
            for s in range(0, N, a.minibatch):
                b = perm[s : s + a.minibatch]
                lg, v = model(o[b], m[b])
                dist = torch.distributions.Categorical(logits=lg)
                lp = dist.log_prob(act[b])
                ab = ad[b]
                ab = (ab - ab.mean()) / (ab.std() + 1e-8)
                ratio = torch.exp(lp - lp0[b])
                pl = -torch.min(ratio * ab, ratio.clamp(1 - a.clip, 1 + a.clip) * ab).mean()
                vl = 0.5 * ((v - rt[b]) ** 2).mean()
                ent = dist.entropy().mean()
                loss = pl + 0.5 * vl - ent_coef * ent
                opt.zero_grad()
                loss.backward()
                nn.utils.clip_grad_norm_(model.parameters(), 0.5)
                opt.step()
                ents.append(ent.item())
        ntr = len(tt["pnl"])
        row = [it + 1, (it + 1) * a.n_env * T, round(ent_coef, 5), float(R.sum(0).mean()),
               float(tt["pnl"].sum() / a.n_env), ntr / a.n_env,
               float(tt["pnl"].mean()) if ntr else 0.0, float(np.mean(ents))]
        if (it + 1) % a.eval_every == 0 or it == n_iter - 1:
            q = quick_eval(model, va, vidx, cfg_ev)
            row += [q["tick_trip"], q["tick_ep"], q["trips_ep"], q["win"], q["hold_med"]]
            log(f"it {it+1}/{n_iter} steps {row[1]/1e6:.1f}M ret {row[3]:+.3f} pnl/ep {row[4]:+.3f} "
                f"trips/ep {row[5]:.2f} tick/trip {row[6]:+.3f} ent {row[7]:.3f} | VAL greedy "
                f"tick/trip {q['tick_trip']:+.3f} tick/ep {q['tick_ep']:+.3f} trips/ep {q['trips_ep']:.2f} "
                f"win {q['win']:.2f} hold {q['hold_med']:.0f}s | {time.time()-t0:.0f}s")
        else:
            row += [""] * 5
        cw.writerow(row)
        curve_f.flush()
    curve_f.close()
    torch.save({"state_dict": model.state_dict(), "hidden": a.hidden, "obs_names": be.OBS_NAMES,
                "args": vars(a), "detector": tr.meta.get("detector"),
                "threshold": tr.meta.get("threshold"), "n_train_episodes": len(tr)}, out)
    log(f"saved {out}")


if __name__ == "__main__":
    main()

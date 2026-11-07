#!/usr/bin/env python3
"""Screen observation-feature groups of the opening-scalp PPO by validation results.

    uv run python scripts/feature_screen.py --arms base,A,B,C,D,E,F,G,H,I,-J \
        --tag s1 --train-steps 1500000 --order-mode market --seed 0

Each arm is a feature spec (``base``; a bare group ``A`` = ``base+A``; ``-J`` =
``base-J``; combinations like ``A+C`` work too).  The arms run **sequentially** as
``scripts/train_scalp.py --features <spec> --no-test ...`` (the test split is never
loaded), with a short ``--train-steps`` and shared fixed arguments: every option this
driver does not know is passed through to ``train_scalp.py`` unchanged (so
``--order-mode``, ``--seed``, ``--smoke``, ``--reward-mode`` ... apply to every arm).
Baselines are tuned once and reused by the other arms through a shared cache.

Models go to ``models/screen/<tag>_<arm>.pt`` and reports to
``.agents/docs/research/screen/<tag>_<arm>.md`` (+ ``.summary.json``); nothing existing
is overwritten (``--overwrite-model`` is never passed and the driver refuses a ``--tag``
whose files exist).  The summary table ``<tag>_summary.md`` lists val_seen and val_held
(trade_through, deterministic PPO), sorted by val_seen yen/sess.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from trading_rl.scalp_features import market_names, parse_feature_spec  # noqa: E402

TRAIN_SCRIPT = REPO_ROOT / "scripts" / "train_scalp.py"
DEFAULT_MODEL_DIR = REPO_ROOT / "models" / "screen"
DEFAULT_REPORT_DIR = REPO_ROOT / ".agents" / "docs" / "research" / "screen"
GROUPS = ("val_seen", "val_held")


def parse_arms(text: str) -> list[tuple[str, str]]:
    """``"base,A,-J"`` -> ``[(label, canonical spec)]`` (validated, de-duplicated)."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in text.split(","):
        raw = raw.strip()
        if not raw:
            continue
        canon = parse_feature_spec(raw).canonical
        if canon in seen:
            continue
        seen.add(canon)
        out.append((raw, canon))
    if not out:
        raise ValueError("no arms given")
    return out


def arm_id(canon: str) -> str:
    """File-name safe id of a canonical spec (``base+A-J`` -> ``base_A_minus_J``)."""
    return canon.replace("+", "_").replace("-", "_minus_")


def arm_paths(
    tag: str, canon: str, model_dir: Path, report_dir: Path
) -> tuple[Path, Path]:
    stem = f"{tag}_{arm_id(canon)}"
    return model_dir / f"{stem}.pt", report_dir / f"{stem}.md"


def build_command(
    canon: str,
    model: Path,
    report: Path,
    train_steps: int,
    passthrough: list[str],
    baselines: Path | None,
) -> list[str]:
    cmd = [
        sys.executable,
        str(TRAIN_SCRIPT),
        "--features",
        canon,
        "--no-test",
        "--model",
        str(model),
        "--report",
        str(report),
        "--train-steps",
        str(train_steps),
    ]
    if baselines is not None and "--reuse-baselines" not in passthrough:
        if "--skip-baselines" not in passthrough:
            cmd += ["--reuse-baselines", str(baselines)]
    return [*cmd, *passthrough]


# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #
def _ci(t: Any, digits: int = 0) -> str:
    if not t or len(t) != 3 or t[0] != t[0]:
        return "n/a"
    return f"{t[0]:+,.{digits}f} [{t[1]:+,.{digits}f}, {t[2]:+,.{digits}f}]"


def _num(x: Any, digits: int = 2) -> str:
    return "n/a" if x is None or x != x else f"{x:,.{digits}f}"


def _forced_share(s: dict[str, Any]) -> float:
    n = s.get("exit_limit", 0) + s.get("exit_market", 0) + s.get("forced", 0)
    return s["forced"] / n if n else float("nan")


def _ppo(summary: dict[str, Any], group: str) -> dict[str, Any] | None:
    s = (
        summary.get("results", {})
        .get(group, {})
        .get("PPO", {})
        .get("trade_through")
    )
    return s if s and s.get("n_sessions") else None


def render_summary(
    tag: str,
    arms: list[dict[str, Any]],
    passthrough: list[str],
    train_steps: int,
) -> str:
    """Markdown summary of finished arms (``arms`` items: label/spec/summary/status)."""
    ok = [a for a in arms if a.get("summary")]
    ok.sort(
        key=lambda a: (
            -(_ppo(a["summary"], "val_seen") or {}).get("yen_sess", float("-inf"))
        )
    )
    L = [
        f"# Feature screening `{tag}`",
        "",
        f"- generated {time.strftime('%Y-%m-%d %H:%M')} by `scripts/feature_screen.py`; "
        f"{len(arms)} arms, `--train-steps {train_steps}`, shared args: "
        f"`{' '.join(passthrough) or '(none)'}`; every arm runs `--no-test` "
        "(test split never loaded).",
        "- PPO evaluated deterministically on trade_through fills (raw PnL); yen/sess = "
        "mean per session [95% day-bootstrap CI]; gross ticks/trip = mean per-round-trip "
        "PnL before market slippage; forced = share of exits that are the 600 s close.",
        "- single seed per arm: differences within the CI are not evidence.",
        "",
    ]
    for g in GROUPS:
        L.append(f"## {g} (sorted by val_seen yen/sess)")
        L.append(
            "| arm | spec | obs dim | yen/sess [95% CI] | ticks/sess | trips/sess | "
            "gross ticks/trip | hold s | forced share | best @steps | sessions |"
        )
        L.append("|" + "---|" * 11)
        for a in ok:
            s = _ppo(a["summary"], g)
            if s is None:
                L.append(f"| {a['label']} | `{a['spec']}` | | n/a | | | | | | | 0 |")
                continue
            fs = _forced_share(s)
            L.append(
                f"| {a['label']} | `{a['spec']}` | {a['summary'].get('obs_dim')} | "
                f"{_ci(s.get('yen_sess_ci'), 1)} | {s['ticks_sess']:+.3f} | "
                f"{s['trips_sess']:.2f} | {_num(s.get('gross_ticks_trip'), 3)} | "
                f"{_num(s['hold_s'], 1)} | {f'{100 * fs:.1f}%' if fs == fs else 'n/a'} | "
                f"{a['summary'].get('best_step', '')} | {s['n_sessions']} |"
            )
        L.append("")
    failed = [a for a in arms if not a.get("summary")]
    if failed:
        L.append("## failed arms")
        for a in failed:
            L.append(f"- `{a['spec']}`: {a.get('status', 'no summary')}; log `{a.get('log')}`")
        L.append("")
    if ok:  # baselines are identical across arms; show the first arm's
        base = ok[0]["summary"].get("results", {}).get("val_seen", {})
        rows = [
            (pol, by_rule["trade_through"])
            for pol, by_rule in base.items()
            if pol != "PPO" and "trade_through" in by_rule
        ]
        if rows:
            L.append("## baselines (val_seen, trade_through, same for every arm)")
            L.append("| policy | yen/sess | ticks/sess | trips/sess | gross ticks/trip |")
            L.append("|---|---|---|---|---|")
            for pol, s in rows:
                L.append(
                    f"| {pol} | {s['yen_sess']:+.1f} | {s['ticks_sess']:+.3f} | "
                    f"{s['trips_sess']:.2f} | {_num(s.get('gross_ticks_trip'), 3)} |"
                )
            L.append("")
    return "\n".join(L)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Unknown options are passed through to scripts/train_scalp.py.",
    )
    ap.add_argument("--arms", required=True, help='comma list, e.g. "base,A,C,-J"')
    ap.add_argument("--tag", default=None, help="run id for file names (default: time)")
    ap.add_argument("--train-steps", type=int, default=1_000_000)
    ap.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    ap.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    ap.add_argument("--dry-run", action="store_true", help="print the commands only")
    return ap


def main(argv: list[str] | None = None) -> int:
    args, passthrough = build_parser().parse_known_args(argv)
    forbidden = {"--overwrite-model", "--features", "--no-test", "--model", "--report"}
    bad = forbidden.intersection(passthrough)
    if bad:
        raise SystemExit(f"{sorted(bad)} are managed by feature_screen.py")
    try:
        arms = parse_arms(args.arms)
    except ValueError as e:
        raise SystemExit(f"--arms: {e}") from e
    tag = args.tag or time.strftime("%Y%m%d-%H%M%S")
    plans = []
    for label, canon in arms:
        model, report = arm_paths(tag, canon, args.model_dir, args.report_dir)
        clash = [
            p
            for p in (model, report, report.with_suffix(".summary.json"))
            if p.exists()
        ]
        if clash:
            raise SystemExit(f"refusing to overwrite {clash[0]} (use another --tag)")
        plans.append((label, canon, model, report))
    args.model_dir.mkdir(parents=True, exist_ok=True)
    args.report_dir.mkdir(parents=True, exist_ok=True)
    baselines = args.report_dir / f"{tag}_baselines.json"

    results: list[dict[str, Any]] = []
    for label, canon, model, report in plans:
        cmd = build_command(
            canon, model, report, args.train_steps, passthrough, baselines
        )
        n_obs = len(market_names(canon))
        print(f"[arm {label}] {canon} ({n_obs} market features)", flush=True)
        print("  " + " ".join(cmd), flush=True)
        entry: dict[str, Any] = {"label": label, "spec": canon}
        if args.dry_run:
            results.append(entry)
            continue
        log_path = report.with_suffix(".log")
        t0 = time.time()
        with open(log_path, "w", encoding="utf-8") as fh:
            proc = subprocess.run(  # noqa: S603 - sequential, fixed argv
                cmd, cwd=REPO_ROOT, stdout=fh, stderr=subprocess.STDOUT, check=False
            )
        entry["log"] = str(log_path)
        summary_path = report.with_suffix(".summary.json")
        if proc.returncode == 0 and summary_path.is_file():
            entry["summary"] = json.loads(summary_path.read_text(encoding="utf-8"))
            print(f"  done in {(time.time() - t0) / 60:.1f} min", flush=True)
        else:
            entry["status"] = f"exit code {proc.returncode}"
            print(f"  FAILED ({entry['status']}); see {log_path}", flush=True)
        results.append(entry)

    if args.dry_run:
        return 0
    out = args.report_dir / f"{tag}_summary.md"
    out.write_text(
        render_summary(tag, results, passthrough, args.train_steps), encoding="utf-8"
    )
    print(f"summary -> {out}", flush=True)
    return 0 if all(r.get("summary") for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Build/check compatibility/feature image inputs without publishing images.

This does not publish an image. It proves that the compatibility image input keeps
the pre-feature entry bytes while both image variants carry the same immutable
vendor closure. Actual Cloud Run image digests require a running container builder
and staging credentials and remain a deployment gate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = REPOSITORY_ROOT / "tests/fixtures/tickreplay-pre-feature-headers.json"
DECISIONS_PATH = REPOSITORY_ROOT / "tests/browser/step0-decisions.json"
OUTPUT_PATH = REPOSITORY_ROOT / "cloud-run/strategy-spike-images.json"
CODEMIRROR_ROOT = REPOSITORY_ROOT / "src/tickreplay/static/vendor/codemirror"
PYODIDE_ROOT = REPOSITORY_ROOT / "src/tickreplay/static/vendor/pyodide/0.27.6"
RELEASES_ROOT = REPOSITORY_ROOT / "src/tickreplay/static/strategy/releases"
AUTHORED_RELEASE_FILES = {
    name: REPOSITORY_ROOT / "src/tickreplay/static/strategy" / name
    for name in (
        "strategy-workspace.mjs",
        "strategy-editor.mjs",
        "strategy-source-store.mjs",
        "strategy-controller.mjs",
        "strategy-protocol.mjs",
        "strategy-worker.mjs",
        "strategy-runner.py",
    )
}


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def one_directory(root: Path) -> Path:
    directories = sorted(path for path in root.iterdir() if path.is_dir())
    if len(directories) != 1:
        raise ValueError(
            f"expected one immutable asset set under {root}, found {len(directories)}"
        )
    return directories[0]


def read_revision_file(revision: str, logical_name: str) -> bytes:
    if len(revision) != 40 or any(
        character not in "0123456789abcdef" for character in revision
    ):
        raise ValueError("pre-feature revision must be a lowercase full Git object ID")
    path = Path(logical_name)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"invalid pre-feature entry path: {logical_name}")
    result = subprocess.run(
        ["git", "show", f"{revision}:{logical_name}"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
    )
    return result.stdout


def current_release() -> tuple[Path, dict[str, object]]:
    matches: list[tuple[Path, dict[str, object]]] = []
    for directory in sorted(path for path in RELEASES_ROOT.iterdir() if path.is_dir()):
        manifest = read_json(directory / "manifest.json")
        described = {
            item["name"]: item["sha256"] for item in manifest["identity"]["files"]
        }
        if all(
            described.get(name) == sha256_file(source)
            for name, source in AUTHORED_RELEASE_FILES.items()
        ):
            matches.append((directory, manifest))
    if len(matches) != 1:
        raise ValueError(
            f"expected one release matching authored files, found {len(matches)}"
        )
    return matches[0]


def describe_entry_files(
    logical_names: list[str], *, revision: str | None = None
) -> dict[str, dict[str, object]]:
    described = {}
    for logical_name in logical_names:
        content = (
            read_revision_file(revision, logical_name)
            if revision is not None
            else (REPOSITORY_ROOT / logical_name).read_bytes()
        )
        described[logical_name] = {
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    return described


def build_manifest() -> dict[str, object]:
    baseline = read_json(BASELINE_PATH)
    decisions = read_json(DECISIONS_PATH)
    baseline_revision = baseline["source_revision"]
    for logical_name, expected in baseline["entry_files"].items():
        source_bytes = read_revision_file(baseline_revision, logical_name)
        if len(source_bytes) != expected["bytes"]:
            raise ValueError(
                f"pre-feature compatibility entry size changed: {logical_name}"
            )
        if hashlib.sha256(source_bytes).hexdigest() != expected["sha256"]:
            raise ValueError(
                f"pre-feature compatibility entry hash changed: {logical_name}"
            )

    codemirror_directory = one_directory(CODEMIRROR_ROOT)
    pyodide_directory = one_directory(PYODIDE_ROOT)
    codemirror_manifest = read_json(codemirror_directory / "manifest.json")
    pyodide_manifest = read_json(pyodide_directory / "manifest.json")
    release_directory, release_manifest = current_release()
    retained_release_directories = sorted(
        path for path in RELEASES_ROOT.iterdir() if path.is_dir()
    )
    common_overlay = {
        "codemirror_bundle_version": codemirror_manifest["bundle_version"],
        "codemirror_manifest_sha256": sha256_file(
            codemirror_directory / "manifest.json"
        ),
        "pyodide_asset_set_id": pyodide_manifest["asset_set_id"],
        "pyodide_manifest_sha256": sha256_file(pyodide_directory / "manifest.json"),
        "strategy_release_id": release_manifest["release_id"],
        "strategy_release_manifest_sha256": sha256_file(
            release_directory / "manifest.json"
        ),
        "strategy_release_entry_url": release_manifest["entry_url"],
        "retained_strategy_release_ids": [
            directory.name for directory in retained_release_directories
        ],
        "overlay_payload_bytes": sum(
            path.stat().st_size
            for directory in (codemirror_directory, pyodide_directory)
            for path in directory.iterdir()
            if path.is_file()
        )
        + sum(
            path.stat().st_size
            for directory in retained_release_directories
            for path in directory.rglob("*")
            if path.is_file()
        ),
    }
    feature_entries = describe_entry_files(
        [
            "src/tickreplay/static/index.html",
            "src/tickreplay/static/app.js",
            "src/tickreplay/static/styles.css",
        ]
    )
    return {
        "schema_version": 1,
        "status": "local-input-manifest-only",
        "publication": {
            "staging_project": None,
            "compatibility_image_digest": None,
            "feature_image_digest": None,
            "reason": "requires a running container builder and authorized Cloud Run staging target",
        },
        "retention": decisions["asset_retention"],
        "compatibility_image": {
            "base_revision": baseline["source_revision"],
            "entry_files": describe_entry_files(
                list(baseline["entry_files"]), revision=baseline_revision
            ),
            "feature_entry_points": [],
            "rollback_eligible": True,
            "immutable_overlay": common_overlay,
        },
        "feature_image": {
            "base_revision": "working-tree-release-input",
            "entry_files": feature_entries,
            "feature_entry_points": [release_manifest["entry_url"]],
            "rollback_eligible": False,
            "immutable_overlay": common_overlay,
        },
        "local_simulation": {
            "same_overlay_in_both_images": True,
            "compatibility_entry_hashes_unchanged": True,
            "within_window_delayed_asset_resolution": True,
            "beyond_window_recovery": "raw/degraded recovery may be entered; fresh navigation uses retained compatibility entry points",
        },
    }


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()
    manifest = build_manifest()
    serialized = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    if arguments.check:
        if not OUTPUT_PATH.is_file():
            raise FileNotFoundError(
                f"missing generated image input manifest: {OUTPUT_PATH}"
            )
        if OUTPUT_PATH.read_text() != serialized:
            raise ValueError(
                "strategy spike image input manifest differs from reconstruction"
            )
    else:
        OUTPUT_PATH.write_text(serialized)
    print(
        json.dumps(
            {
                "ok": True,
                "mode": "check" if arguments.check else "build",
                "output": OUTPUT_PATH.relative_to(REPOSITORY_ROOT).as_posix(),
                "status": manifest["status"],
                "overlay_payload_bytes": manifest["compatibility_image"][
                    "immutable_overlay"
                ]["overlay_payload_bytes"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

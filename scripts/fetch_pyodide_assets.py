"""Reconstruct the pinned, standard-library-only Pyodide browser asset set."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PYODIDE_VERSION = "0.27.6"
PYTHON_VERSION = "3.12.7"
VENDOR_ROOT = (
    REPOSITORY_ROOT
    / "src"
    / "tickreplay"
    / "static"
    / "vendor"
    / "pyodide"
    / PYODIDE_VERSION
)
DOWNLOAD_TIMEOUT_SECONDS = 120
USER_AGENT = "backcast-pyodide-asset-builder/1"


@dataclass(frozen=True)
class UpstreamAsset:
    name: str
    url: str
    bytes: int
    sha256: str
    purpose: str


ASSETS = (
    UpstreamAsset(
        name="pyodide.mjs",
        url="https://cdn.jsdelivr.net/pyodide/v0.27.6/full/pyodide.mjs",
        bytes=13_946,
        sha256="9fc7d11de0b7fab8a45604db1d2df2609fc9bc4f029d091eafbb0601fa6cd435",
        purpose="ES module loader",
    ),
    UpstreamAsset(
        name="pyodide.asm.js",
        url="https://cdn.jsdelivr.net/pyodide/v0.27.6/full/pyodide.asm.js",
        bytes=1_255_388,
        sha256="af1e2e8f8d2030f5d4c89ff05de5747820cebf65eca01fe89a46fdd3a4189450",
        purpose="Emscripten runtime glue",
    ),
    UpstreamAsset(
        name="pyodide.asm.wasm",
        url="https://cdn.jsdelivr.net/pyodide/v0.27.6/full/pyodide.asm.wasm",
        bytes=10_105_098,
        sha256="c99674c2b0c3ab1b758942683ad46004f1967e93e4ad682e9d0ec0c340e09b71",
        purpose="Python WebAssembly runtime",
    ),
    UpstreamAsset(
        name="python_stdlib.zip",
        url="https://cdn.jsdelivr.net/pyodide/v0.27.6/full/python_stdlib.zip",
        bytes=2_360_652,
        sha256="b4d44bd9ed296f12447938bb370fd3e8c07e40d0daac473de1edc4a541f43c4c",
        purpose="Python standard library",
    ),
    UpstreamAsset(
        name="pyodide-lock.json",
        url="https://cdn.jsdelivr.net/pyodide/v0.27.6/full/pyodide-lock.json",
        bytes=112_205,
        sha256="abc1e50465438c019cc0c08286fc9b44484de777b49f10169a6978ee1d9745ba",
        purpose="Pyodide package metadata required by the loader",
    ),
    UpstreamAsset(
        name="LICENSE-PYODIDE.txt",
        url="https://raw.githubusercontent.com/pyodide/pyodide/0.27.6/LICENSE",
        bytes=16_725,
        sha256="1f256ecad192880510e84ad60474eab7589218784b9a50bc7ceee34c2b91f1d5",
        purpose="Pyodide and bundled-runtime license notices",
    ),
    UpstreamAsset(
        name="LICENSE-CPYTHON.txt",
        url="https://raw.githubusercontent.com/python/cpython/v3.12.7/LICENSE",
        bytes=13_936,
        sha256="3b2f81fe21d181c499c59a256c8e1968455d6689d269aa85373bfb6af41da3bf",
        purpose="CPython license",
    ),
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def fetch_asset(asset: UpstreamAsset) -> bytes:
    request = urllib.request.Request(asset.url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
        data = response.read()
    if not isinstance(data, bytes):
        raise ValueError(f"{asset.name}: response body must be bytes")
    if len(data) != asset.bytes:
        raise ValueError(
            f"{asset.name}: expected {asset.bytes} bytes, received {len(data)}"
        )
    digest = sha256_bytes(data)
    if digest != asset.sha256:
        raise ValueError(f"{asset.name}: expected SHA-256 {asset.sha256}, got {digest}")
    return data


def reconstruct(staging_directory: Path) -> tuple[str, dict[str, object]]:
    downloaded: dict[str, bytes] = {}
    for asset in ASSETS:
        data = fetch_asset(asset)
        downloaded[asset.name] = data
        (staging_directory / asset.name).write_bytes(data)

    lock = json.loads(downloaded["pyodide-lock.json"])
    if not isinstance(lock, dict):
        raise ValueError("pyodide-lock.json must contain a JSON object")
    lock_info = lock.get("info", {})
    if not isinstance(lock_info, dict):
        raise ValueError("pyodide-lock.json info must contain a JSON object")
    if lock_info.get("version") != PYODIDE_VERSION:
        raise ValueError("pyodide-lock.json release does not match the frozen version")
    if lock_info.get("python") != PYTHON_VERSION:
        raise ValueError(
            "pyodide-lock.json Python version does not match the frozen version"
        )

    identity = {
        "schema_version": 1,
        "kind": "pyodide-standard-library-runtime",
        "pyodide_version": PYODIDE_VERSION,
        "python_version": PYTHON_VERSION,
        "policy": {
            "approved_wheels": [],
            "dynamic_package_install": False,
            "load_packages_from_imports": False,
            "micropip": False,
            "standard_library_only": True,
        },
        "inputs": [
            {
                "name": asset.name,
                "bytes": asset.bytes,
                "sha256": asset.sha256,
                "source": asset.url,
                "purpose": asset.purpose,
            }
            for asset in sorted(ASSETS, key=lambda item: item.name)
        ],
    }
    identity_bytes = canonical_json(identity)
    asset_set_id = sha256_bytes(identity_bytes)
    (staging_directory / "identity.json").write_bytes(identity_bytes)

    files = []
    for path in sorted(staging_directory.iterdir(), key=lambda item: item.name):
        data = path.read_bytes()
        files.append(
            {"name": path.name, "bytes": len(data), "sha256": sha256_bytes(data)}
        )
    manifest = {
        "schema_version": 1,
        "kind": "pyodide-standard-library-runtime",
        "asset_set_id": asset_set_id,
        "immutable_base_url": (
            f"/static/vendor/pyodide/{PYODIDE_VERSION}/{asset_set_id}/"
        ),
        "identity_sha256": sha256_bytes(identity_bytes),
        "files": files,
        "runtime_files": [
            "pyodide.mjs",
            "pyodide.asm.js",
            "pyodide.asm.wasm",
            "python_stdlib.zip",
            "pyodide-lock.json",
        ],
        "runtime_raw_bytes": sum(
            len(downloaded[name])
            for name in (
                "pyodide.mjs",
                "pyodide.asm.js",
                "pyodide.asm.wasm",
                "python_stdlib.zip",
                "pyodide-lock.json",
            )
        ),
    }
    (staging_directory / "manifest.json").write_bytes(canonical_json(manifest))
    return asset_set_id, manifest


def compare_directories(actual: Path, expected: Path) -> None:
    if not expected.is_dir():
        raise FileNotFoundError(f"committed Pyodide asset set is missing: {expected}")
    actual_names = sorted(path.name for path in actual.iterdir())
    expected_names = sorted(path.name for path in expected.iterdir())
    if actual_names != expected_names:
        raise ValueError(
            "committed Pyodide output allowlist differs from reconstruction"
        )
    for name in actual_names:
        if (actual / name).read_bytes() != (expected / name).read_bytes():
            raise ValueError(f"{name} differs from reconstruction")


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()
    with tempfile.TemporaryDirectory(prefix="backcast-pyodide-") as temporary_root:
        staging_directory = Path(temporary_root) / "id-neutral"
        staging_directory.mkdir()
        asset_set_id, manifest = reconstruct(staging_directory)
        destination = VENDOR_ROOT / asset_set_id
        if arguments.check:
            compare_directories(staging_directory, destination)
        elif destination.exists():
            compare_directories(staging_directory, destination)
        else:
            VENDOR_ROOT.mkdir(parents=True, exist_ok=True)
            shutil.copytree(staging_directory, destination)
        report = {
            "ok": True,
            "mode": "check" if arguments.check else "build",
            "pyodide_version": PYODIDE_VERSION,
            "python_version": PYTHON_VERSION,
            "asset_set_id": asset_set_id,
            "destination": destination.relative_to(REPOSITORY_ROOT).as_posix(),
            "runtime_raw_bytes": manifest["runtime_raw_bytes"],
            "files": sorted(path.name for path in destination.iterdir()),
        }
        print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

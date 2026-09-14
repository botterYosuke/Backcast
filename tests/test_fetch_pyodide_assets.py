import argparse
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "fetch_pyodide_assets.py"


@pytest.fixture
def fetch_module():
    spec = importlib.util.spec_from_file_location(
        "fetch_pyodide_assets_test", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def asset(module, payload: bytes, *, size=None, digest=None):
    return module.UpstreamAsset(
        name="asset.bin",
        url="https://example.invalid/asset.bin",
        bytes=len(payload) if size is None else size,
        sha256=module.sha256_bytes(payload) if digest is None else digest,
        purpose="test",
    )


def test_fetch_asset_rejects_size_and_hash_mismatch(fetch_module, monkeypatch):
    payload = b"canonical"
    responses = iter([FakeResponse(payload), FakeResponse(payload)])
    monkeypatch.setattr(
        fetch_module.urllib.request, "urlopen", lambda *_a, **_k: next(responses)
    )

    with pytest.raises(ValueError, match="expected 10 bytes"):
        fetch_module.fetch_asset(asset(fetch_module, payload, size=10))
    with pytest.raises(ValueError, match="expected SHA-256"):
        fetch_module.fetch_asset(asset(fetch_module, payload, digest="0" * 64))


def test_fetch_asset_rejects_malformed_and_failed_response(fetch_module, monkeypatch):
    malformed = iter([FakeResponse("not-bytes"), FakeResponse(OSError("offline"))])
    monkeypatch.setattr(
        fetch_module.urllib.request, "urlopen", lambda *_a, **_k: next(malformed)
    )

    with pytest.raises(ValueError, match="bytes"):
        fetch_module.fetch_asset(asset(fetch_module, b"not-bytes"))
    with pytest.raises(OSError, match="offline"):
        fetch_module.fetch_asset(asset(fetch_module, b"unused"))


def test_reconstruct_rejects_malformed_lock_manifest(
    fetch_module, monkeypatch, tmp_path
):
    lock_asset = fetch_module.UpstreamAsset(
        name="pyodide-lock.json",
        url="https://example.invalid/lock",
        bytes=2,
        sha256=fetch_module.sha256_bytes(b"[]"),
        purpose="test",
    )
    monkeypatch.setattr(fetch_module, "ASSETS", (lock_asset,))
    monkeypatch.setattr(fetch_module, "fetch_asset", lambda _asset: b"[]")

    with pytest.raises(ValueError, match="JSON object"):
        fetch_module.reconstruct(tmp_path)


def test_compare_directories_rejects_allowlist_and_content_mismatch(
    fetch_module, tmp_path
):
    actual = tmp_path / "actual"
    expected = tmp_path / "expected"
    actual.mkdir()
    expected.mkdir()
    (actual / "asset.bin").write_bytes(b"new")
    (expected / "asset.bin").write_bytes(b"old")

    with pytest.raises(ValueError, match="differs from reconstruction"):
        fetch_module.compare_directories(actual, expected)
    (expected / "unexpected.bin").write_bytes(b"unexpected")
    with pytest.raises(ValueError, match="allowlist"):
        fetch_module.compare_directories(actual, expected)


def test_failed_reconstruction_preserves_existing_destination(
    fetch_module, monkeypatch, tmp_path
):
    destination_root = tmp_path / "vendor"
    destination = destination_root / "retained"
    destination.mkdir(parents=True)
    retained = destination / "asset.bin"
    retained.write_bytes(b"retained")
    monkeypatch.setattr(fetch_module, "VENDOR_ROOT", destination_root)
    monkeypatch.setattr(
        fetch_module, "parse_arguments", lambda: argparse.Namespace(check=False)
    )

    def fail_after_staging(staging_directory):
        (staging_directory / "asset.bin").write_bytes(b"partial")
        raise ValueError("invalid manifest")

    monkeypatch.setattr(fetch_module, "reconstruct", fail_after_staging)

    with pytest.raises(ValueError, match="invalid manifest"):
        fetch_module.main()
    assert retained.read_bytes() == b"retained"
    assert sorted(path.name for path in destination_root.iterdir()) == ["retained"]

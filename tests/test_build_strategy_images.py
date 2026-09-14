import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "build_strategy_images.py"


@pytest.fixture
def image_module():
    spec = importlib.util.spec_from_file_location(
        "build_strategy_images_test", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


@pytest.mark.parametrize("revision", ["abc", "A" * 40, "g" * 40])
def test_read_revision_file_rejects_invalid_git_oid_without_subprocess(
    image_module, monkeypatch, revision
):
    monkeypatch.setattr(
        image_module.subprocess,
        "run",
        lambda *_a, **_k: pytest.fail("subprocess must not run"),
    )

    with pytest.raises(ValueError, match="lowercase full Git object ID"):
        image_module.read_revision_file(revision, "src/tickreplay/static/app.js")


@pytest.mark.parametrize(
    "logical_name", ["/absolute/app.js", "../app.js", "src/../app.js"]
)
def test_read_revision_file_rejects_unsafe_path_without_subprocess(
    image_module, monkeypatch, logical_name
):
    monkeypatch.setattr(
        image_module.subprocess,
        "run",
        lambda *_a, **_k: pytest.fail("subprocess must not run"),
    )

    with pytest.raises(ValueError, match="invalid pre-feature entry path"):
        image_module.read_revision_file("a" * 40, logical_name)


def test_read_revision_file_propagates_subprocess_failure(image_module, monkeypatch):
    failure = subprocess.CalledProcessError(128, ["git", "show"])
    monkeypatch.setattr(
        image_module.subprocess, "run", lambda *_a, **_k: (_ for _ in ()).throw(failure)
    )

    with pytest.raises(subprocess.CalledProcessError):
        image_module.read_revision_file("a" * 40, "src/tickreplay/static/app.js")


def test_read_json_rejects_non_object_manifest(image_module, tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text("[]")

    with pytest.raises(ValueError, match="JSON object"):
        image_module.read_json(manifest)


def test_current_release_rejects_missing_and_ambiguous_matches(
    image_module, monkeypatch, tmp_path
):
    releases = tmp_path / "releases"
    releases.mkdir()
    source = tmp_path / "strategy.py"
    source.write_bytes(b"source")
    monkeypatch.setattr(image_module, "RELEASES_ROOT", releases)
    monkeypatch.setattr(image_module, "AUTHORED_RELEASE_FILES", {"strategy.py": source})

    with pytest.raises(ValueError, match="found 0"):
        image_module.current_release()

    digest = image_module.sha256_file(source)
    for name in ("one", "two"):
        directory = releases / name
        directory.mkdir()
        (directory / "manifest.json").write_text(
            json.dumps(
                {"identity": {"files": [{"name": "strategy.py", "sha256": digest}]}}
            )
        )
    with pytest.raises(ValueError, match="found 2"):
        image_module.current_release()


def test_build_failure_preserves_existing_output(image_module, monkeypatch, tmp_path):
    output = tmp_path / "strategy-spike-images.json"
    output.write_bytes(b"retained\n")
    monkeypatch.setattr(image_module, "OUTPUT_PATH", output)
    monkeypatch.setattr(
        image_module, "parse_arguments", lambda: argparse.Namespace(check=False)
    )
    monkeypatch.setattr(
        image_module,
        "build_manifest",
        lambda: (_ for _ in ()).throw(ValueError("invalid manifest")),
    )

    with pytest.raises(ValueError, match="invalid manifest"):
        image_module.main()
    assert output.read_bytes() == b"retained\n"

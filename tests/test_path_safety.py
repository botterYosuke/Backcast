"""Fail-closed bootstrap path protocol, including the host Python 3.9 runtime."""

from __future__ import annotations

import errno
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

HELPER = Path(__file__).resolve().parents[1] / ".agents/skills/_shared/path_safety.py"


def test_updater_rejects_control_characters_before_resolving_root(
    tmp_path: Path,
) -> None:
    scripts = tmp_path / "project\n/scripts"
    scripts.mkdir(parents=True)
    script = scripts / "update.sh"
    shutil.copy2(HELPER.parents[3] / "scripts/update.sh", script)
    result = subprocess.run(
        ["bash", str(script), "--yes"], capture_output=True, text=True
    )
    assert result.returncode == 2
    assert "control characters" in result.stderr


def check(root: Path, candidate: str | Path, status: int) -> None:
    result = subprocess.run(
        ["python3", "-I", str(HELPER), str(root), str(candidate)],
        capture_output=True,
        check=False,
    )
    assert result.returncode == status, result.stderr
    assert result.stdout == b""


@pytest.mark.parametrize("fail_copy", [False, True])
def test_legacy_merge_preserves_conflicts_and_copies_missing_entries(
    tmp_path: Path,
    fail_copy: bool,
) -> None:
    target = tmp_path / "project"
    legacy = target / ".claude/docs"
    canonical = target / ".agents/docs"
    legacy.mkdir(parents=True)
    canonical.mkdir(parents=True)
    (legacy / "DESIGN.md").write_text("legacy")
    (canonical / "DESIGN.md").write_text("current")
    (legacy / ".hidden").write_text("copy")
    (legacy / "nested").mkdir()
    (canonical / "nested").mkdir()
    (legacy / "nested/new").write_text("nested")
    (legacy / "dangling").symlink_to("absent")
    definitions = (
        (HELPER.parents[3] / "scripts/update.sh")
        .read_text()
        .rsplit('\nmain "$@"', 1)[0]
    )
    driver = tmp_path / "migration-driver.sh"
    driver.write_text(definitions + '\nPROJECT_ROOT="$1"\nmigrate_legacy_native_data')
    env = dict(os.environ)
    if fail_copy:
        shim = tmp_path / "bin"
        shim.mkdir()
        copy = shim / "cp"
        copy.write_text(
            '#!/bin/bash\nfor arg in "$@"; do\n  [[ "$arg" != */nested/new ]] || exit 1\ndone\nexec /bin/cp "$@"\n'
        )
        copy.chmod(0o755)
        env["PATH"] = f"{shim}{os.pathsep}{env['PATH']}"
    result = subprocess.run(
        ["bash", str(driver), str(target)], capture_output=True, text=True, env=env
    )
    if fail_copy:
        assert result.returncode != 0
        assert (canonical / "DESIGN.md").read_text() == "current"
        assert not (canonical / "nested/new").exists()
        return
    assert result.returncode == 0, result.stderr
    assert (canonical / "DESIGN.md").read_text() == "current"
    assert (canonical / ".hidden").read_text() == "copy"
    assert (canonical / "nested/new").read_text() == "nested"
    assert (canonical / "dangling").is_symlink()
    backup = next(target.glob(".orchestra-backup-native-migration-*"))
    assert (backup / ".claude/docs/DESIGN.md").read_text() == "legacy"


def test_missing_paths_and_boundary(tmp_path: Path) -> None:
    root = tmp_path / "project with spaces"
    root.mkdir()
    check(root, root / "missing/deep/file", 0)
    check(root, root / "missing/../inside", 0)
    check(root, root, 1)
    check(root, f"{root}-sibling/file", 1)
    check(root, root / "../outside", 1)
    check(root, "relative/file", 2)
    check(root / "absent", root / "absent/child", 2)


@pytest.mark.parametrize("character", ["\n", "\t", "\r", "\x1b", "\x7f"])
def test_control_characters_fail_closed(tmp_path: Path, character: str) -> None:
    check(tmp_path, tmp_path / f"file{character}", 2)
    (tmp_path / "link").symlink_to(f"file{character}")
    check(tmp_path, tmp_path / "link", 2)


def test_symlink_chains_and_dangling_targets(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "relative").symlink_to("missing/child")
    (root / "absolute").symlink_to(root / "relative")
    (root / "outside").symlink_to(tmp_path / "absent")
    (root / "parent").symlink_to("..")
    check(root, root / "relative/file", 0)
    check(root, root / "absolute/file", 0)
    check(root, root / "outside/file", 1)
    check(root, root / "parent", 1)
    (root / "cycle-a").symlink_to("cycle-b")
    (root / "cycle-b").symlink_to("cycle-a")
    check(root, root / "cycle-a", 2)
    for index in range(42):
        (root / f"hop-{index}").symlink_to(f"hop-{index + 1}")
    check(root, root / "hop-0", 2)


def test_non_directory_components_are_not_missing(tmp_path: Path) -> None:
    leaf = tmp_path / "file"
    leaf.write_text("sentinel")
    check(tmp_path, f"{leaf}/child", 2)
    check(tmp_path, f"{leaf}/../other", 2)
    check(tmp_path, f"{leaf}/", 2)


@pytest.fixture
def helper_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("path_safety", HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("error_number", [errno.EACCES, errno.ENOTDIR, errno.ELOOP])
def test_resolution_errors_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    helper_module: ModuleType,
    error_number: int,
) -> None:
    original = helper_module.os.lstat

    def fail_candidate(path: str) -> object:
        if path == str(tmp_path / "blocked"):
            raise OSError(error_number, "injected filesystem error")
        return original(path)

    monkeypatch.setattr(helper_module.os, "lstat", fail_candidate)
    assert helper_module.main([str(tmp_path), str(tmp_path / "blocked/child")]) == 2


def test_invalid_argument_count() -> None:
    result = subprocess.run([sys.executable, str(HELPER)], capture_output=True)
    assert result.returncode == 2
    assert result.stdout == b""

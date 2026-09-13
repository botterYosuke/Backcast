from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SCRIPT = REPO_ROOT / "scripts" / "install.sh"


def init_git_repo(path: Path) -> None:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)


def run_install(
    target: Path,
    *options: str,
    script: Path = INSTALL_SCRIPT,
    extra_path: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if extra_path is not None:
        env["PATH"] = f"{extra_path}{os.pathsep}{env['PATH']}"
    return subprocess.run(
        ["bash", str(script), "--yes", *options, str(target)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def build_template_repo(tmp_path: Path, name: str = "template") -> Path:
    """Copy this repository into a fresh git repo so scripts under test can
    be run against a template source distinct from the live checkout."""
    template = tmp_path / name
    shutil.copytree(
        REPO_ROOT,
        template,
        symlinks=True,
        ignore=shutil.ignore_patterns(
            ".git",
            ".venv",
            ".pytest_cache",
            ".ruff_cache",
            "__pycache__",
        ),
    )
    subprocess.run(["git", "init", "-q", str(template)], check=True)
    subprocess.run(
        ["git", "-C", str(template), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(template), "config", "user.name", "Test User"],
        check=True,
    )
    subprocess.run(["git", "-C", str(template), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(template), "commit", "-qm", "template"], check=True
    )
    return template


def run_update(
    target: Path,
    template: Path,
    *options: str,
    extra_path: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "ORCHESTRA_TEMPLATE_REPO": str(template)}
    if extra_path is not None:
        env["PATH"] = f"{extra_path}{os.pathsep}{env['PATH']}"
    return subprocess.run(
        ["bash", str(target / "scripts/update.sh"), "--yes", *options],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def find_debris(root: Path) -> list[Path]:
    """Locate any leftover stage-and-swap artifacts under root (excluding .git)."""
    debris: list[Path] = []
    for pattern in ("*.orchestra-staging.*", "*.orchestra-old.*"):
        for match in root.rglob(pattern):
            if ".git" in match.relative_to(root).parts:
                continue
            debris.append(match)
    return debris


def test_install_adds_complete_template_without_overwriting_project_version(
    tmp_path: Path,
) -> None:
    target = tmp_path / "project"
    init_git_repo(target)
    (target / "README.md").write_text("# Existing project\n", encoding="utf-8")
    (target / "VERSION").write_text("9.4.1\n", encoding="utf-8")

    result = run_install(target)

    assert result.returncode == 0, result.stderr
    assert (target / "README.md").read_text(encoding="utf-8") == "# Existing project\n"
    assert (target / "VERSION").read_text(encoding="utf-8") == "9.4.1\n"
    assert (target / ".claude/orchestra-version").read_text(encoding="utf-8") == (
        REPO_ROOT / "VERSION"
    ).read_text(encoding="utf-8")
    assert (target / ".agents/INDEX.md").is_file()
    assert (target / ".agents/change_main.md").is_file()
    assert "## Skill Catalog" in (target / "AGENTS.md").read_text(encoding="utf-8")
    assert not (target / ".agents/rules/orchestration.md").exists()
    assert (target / ".agents/rules").is_dir()
    assert (target / ".agents/skills").is_dir()
    assert (target / ".agents/agents").is_dir()
    assert (target / ".agents/hooks").is_dir()
    assert (target / "AGENTS.md").is_file()
    assert (target / "CLAUDE.md").is_symlink()
    assert (target / "CLAUDE.md").resolve() == (target / "AGENTS.md").resolve()
    assert {path.name for path in (target / ".claude").iterdir()} == {
        "orchestra-version",
        "settings.json",
        "agents",
        "skills",
    }
    assert (target / ".claude/agents").is_symlink()
    assert (target / ".claude/skills").is_symlink()
    assert {path.name for path in (target / ".codex").iterdir()} == {"config.toml"}
    assert (target / "scripts/install.sh").is_file()
    assert (target / "scripts/update.sh").is_file()
    assert (target / ".claude/settings.json").is_file()
    assert (target / ".agents/docs/research/.gitkeep").is_file()
    assert (target / ".agents/STATE.md").is_file()
    assert "@orchestra:" not in (target / "AGENTS.md").read_text(encoding="utf-8")
    gitignore = (target / ".gitignore").read_text(encoding="utf-8")
    assert ".agents/logs/" in gitignore
    assert ".agents/checkpoints/" in gitignore
    assert ".orchestra-backup-*/" in gitignore


def test_install_preserves_existing_claude_md_in_agents_state(tmp_path: Path) -> None:
    target = tmp_path / "project"
    init_git_repo(target)
    existing_content = "# Existing instructions\n\nKeep this project rule.\n"
    (target / "CLAUDE.md").write_text(existing_content, encoding="utf-8")

    result = run_install(target)

    assert result.returncode == 0, result.stderr
    assert (target / "CLAUDE.md").is_symlink()
    installed = (target / ".agents/STATE.md").read_text(encoding="utf-8")
    assert installed.count(existing_content.strip()) == 1
    assert existing_content.strip() not in (target / "AGENTS.md").read_text(
        encoding="utf-8"
    )


def test_install_refuses_template_owned_path_conflicts_by_default(
    tmp_path: Path,
) -> None:
    target = tmp_path / "project"
    init_git_repo(target)
    custom_file = target / ".agents/rules/custom.md"
    custom_file.parent.mkdir(parents=True)
    custom_file.write_text("custom contract\n", encoding="utf-8")

    result = run_install(target)

    assert result.returncode == 2
    assert "--force" in result.stderr
    assert custom_file.read_text(encoding="utf-8") == "custom contract\n"
    assert not (target / "AGENTS.md").exists()


def test_force_install_backs_up_conflicts_before_replacing_them(
    tmp_path: Path,
) -> None:
    target = tmp_path / "project"
    init_git_repo(target)
    custom_file = target / ".agents/rules/custom.md"
    custom_file.parent.mkdir(parents=True)
    custom_file.write_text("custom contract\n", encoding="utf-8")

    result = run_install(target, "--force")

    assert result.returncode == 0, result.stderr
    backups = list(target.glob(".orchestra-backup-*"))
    assert len(backups) == 1
    assert (backups[0] / ".agents/rules/custom.md").read_text(
        encoding="utf-8"
    ) == "custom contract\n"
    assert (target / ".agents/INDEX.md").is_file()
    assert not (target / ".agents/rules/custom.md").exists()


def test_install_refuses_to_destroy_existing_native_subagents_and_skills(
    tmp_path: Path,
) -> None:
    """A repo that already uses Claude Code natively keeps its own subagents and
    skills in .claude/. Linking those paths must never silently delete them."""
    target = tmp_path / "project"
    init_git_repo(target)
    existing_agent = target / ".claude/agents/my-agent.md"
    existing_agent.parent.mkdir(parents=True)
    existing_agent.write_text("user's own subagent\n", encoding="utf-8")
    existing_skill = target / ".claude/skills/my-skill/SKILL.md"
    existing_skill.parent.mkdir(parents=True)
    existing_skill.write_text("user's own skill\n", encoding="utf-8")

    result = run_install(target)

    assert result.returncode == 2
    assert "--force" in result.stderr
    assert existing_agent.read_text(encoding="utf-8") == "user's own subagent\n"
    assert existing_skill.read_text(encoding="utf-8") == "user's own skill\n"
    assert not (target / "AGENTS.md").exists()


def test_force_install_backs_up_existing_native_subagents_and_skills(
    tmp_path: Path,
) -> None:
    target = tmp_path / "project"
    init_git_repo(target)
    existing_agent = target / ".claude/agents/my-agent.md"
    existing_agent.parent.mkdir(parents=True)
    existing_agent.write_text("user's own subagent\n", encoding="utf-8")

    result = run_install(target, "--force")

    assert result.returncode == 0, result.stderr
    backups = list(target.glob(".orchestra-backup-*"))
    assert len(backups) == 1
    assert (backups[0] / ".claude/agents/my-agent.md").read_text(
        encoding="utf-8"
    ) == "user's own subagent\n"
    assert (target / ".claude/agents").is_symlink()
    assert (target / ".claude/agents").readlink().as_posix() == "../.agents/agents"


def test_existing_correct_discovery_link_is_not_treated_as_a_conflict(
    tmp_path: Path,
) -> None:
    """A path that is already exactly the link we would create carries no user
    data, so it must not abort the install the way real content does."""
    target = tmp_path / "project"
    init_git_repo(target)
    (target / ".claude").mkdir()
    (target / ".claude/agents").symlink_to("../.agents/agents")

    result = run_install(target)

    assert result.returncode == 0, result.stderr
    assert (target / ".claude/agents").is_symlink()
    assert (target / ".claude/agents").readlink().as_posix() == "../.agents/agents"
    assert (target / ".claude/agents/general-purpose-opus.md").is_file()


def test_update_backs_up_native_discovery_content_instead_of_deleting_it(
    tmp_path: Path,
) -> None:
    template = build_template_repo(tmp_path)
    target = tmp_path / "project"
    init_git_repo(target)
    assert run_install(target, script=template / "scripts/install.sh").returncode == 0

    link = target / ".claude/agents"
    link.unlink()
    link.mkdir()
    (link / "downstream-agent.md").write_text("downstream agent\n", encoding="utf-8")

    update_result = run_update(target, template)

    assert update_result.returncode == 0, update_result.stderr
    assert link.is_symlink()
    assert link.readlink().as_posix() == "../.agents/agents"
    backups = list(target.glob(".orchestra-backup-native-discovery-*"))
    assert len(backups) == 1
    assert (backups[0] / ".claude/agents/downstream-agent.md").read_text(
        encoding="utf-8"
    ) == "downstream agent\n"


def test_install_preserves_existing_settings_and_writes_merge_candidate(
    tmp_path: Path,
) -> None:
    target = tmp_path / "project"
    init_git_repo(target)
    settings = target / ".claude/settings.json"
    settings.parent.mkdir(parents=True)
    custom_settings = '{"language": "english"}\n'
    settings.write_text(custom_settings, encoding="utf-8")

    result = run_install(target)

    assert result.returncode == 0, result.stderr
    assert settings.read_text(encoding="utf-8") == custom_settings
    candidate = target / ".claude/settings.orchestra.json"
    assert candidate.read_text(encoding="utf-8") == (
        REPO_ROOT / ".claude/settings.json"
    ).read_text(encoding="utf-8")
    assert "settings.orchestra.json" in result.stdout


def test_install_refuses_parent_symlink_that_escapes_target(tmp_path: Path) -> None:
    target = tmp_path / "project"
    outside = tmp_path / "outside"
    init_git_repo(target)
    outside.mkdir()
    (target / ".claude").symlink_to(outside, target_is_directory=True)

    result = run_install(target)

    assert result.returncode == 2
    assert "symlinked parent" in result.stderr
    assert list(outside.iterdir()) == []


def test_install_refuses_parent_symlink_that_aliases_inside_target(
    tmp_path: Path,
) -> None:
    target = tmp_path / "project"
    aliased_directory = target / "aliased"
    init_git_repo(target)
    aliased_directory.mkdir()
    (target / ".claude").symlink_to(aliased_directory, target_is_directory=True)

    result = run_install(target)

    assert result.returncode == 2
    assert "symlinked parent" in result.stderr
    assert list(aliased_directory.iterdir()) == []


def test_install_refuses_non_regular_project_file_before_writing(
    tmp_path: Path,
) -> None:
    target = tmp_path / "project"
    init_git_repo(target)
    (target / "CLAUDE.md").mkdir()

    result = run_install(target)

    assert result.returncode == 2
    assert "regular file" in result.stderr
    assert not (target / "AGENTS.md").exists()


def test_update_uses_namespaced_version_file_and_preserves_project_version(
    tmp_path: Path,
) -> None:
    template = build_template_repo(tmp_path)

    target = tmp_path / "project"
    init_git_repo(target)
    (target / "VERSION").write_text("9.4.1\n", encoding="utf-8")
    install_result = run_install(target, script=template / "scripts/install.sh")
    assert install_result.returncode == 0, install_result.stderr

    (template / "VERSION").write_text("0.3.1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(template), "add", "VERSION"], check=True)
    subprocess.run(
        ["git", "-C", str(template), "commit", "-qm", "release 0.3.1"],
        check=True,
    )

    update_result = run_update(target, template)

    assert update_result.returncode == 0, update_result.stderr
    assert (target / "VERSION").read_text(encoding="utf-8") == "9.4.1\n"
    assert (target / ".claude/orchestra-version").read_text(
        encoding="utf-8"
    ) == "0.3.1\n"


def test_install_creates_native_discovery_symlinks(tmp_path: Path) -> None:
    target = tmp_path / "project"
    init_git_repo(target)

    result = run_install(target)

    assert result.returncode == 0, result.stderr
    # The only symlinks in .claude are the native discovery links into .agents/;
    # .codex keeps none. No shared content is physically duplicated.
    claude_symlinks = {
        path.name: path.readlink().as_posix()
        for path in (target / ".claude").iterdir()
        if path.is_symlink()
    }
    assert claude_symlinks == {
        "agents": "../.agents/agents",
        "skills": "../.agents/skills",
    }
    assert (target / ".claude/agents/general-purpose-opus.md").is_file()
    assert (target / ".claude/skills/context-loader/SKILL.md").is_file()
    assert not any(path.is_symlink() for path in (target / ".codex").iterdir())
    assert ".agents/hooks/" in (target / ".claude/settings.json").read_text(
        encoding="utf-8"
    )


def test_update_removes_legacy_native_runtime_paths(tmp_path: Path) -> None:
    template = build_template_repo(tmp_path)

    target = tmp_path / "project"
    init_git_repo(target)
    install_result = run_install(target, script=template / "scripts/install.sh")
    assert install_result.returncode == 0, install_result.stderr
    subprocess.run(["git", "-C", str(target), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(target), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(target), "config", "user.name", "Test User"], check=True
    )
    subprocess.run(
        ["git", "-C", str(target), "commit", "-qm", "initial install"], check=True
    )

    link = target / ".claude/skills"
    if link.is_symlink():
        link.unlink()
    link.write_text("not a symlink anymore\n", encoding="utf-8")
    codex_skills = target / ".codex/skills"
    if codex_skills.is_symlink():
        codex_skills.unlink()
    codex_skills.mkdir()
    (codex_skills / "legacy.md").write_text("legacy\n", encoding="utf-8")
    legacy_project_files = {
        "docs/DESIGN.md": "# Legacy design\n",
        "logs/session.log": "legacy log\n",
        "checkpoints/session.md": "legacy checkpoint\n",
    }
    for relative_path, content in legacy_project_files.items():
        legacy_path = target / ".claude" / relative_path
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.write_text(content, encoding="utf-8")
    claude_settings = target / ".claude/settings.json"
    claude_settings.write_text(
        claude_settings.read_text(encoding="utf-8").replace(
            ".agents/hooks/", ".claude/hooks/"
        ),
        encoding="utf-8",
    )

    update_result = run_update(target, template)

    assert update_result.returncode == 0, update_result.stderr
    # A corrupted .claude/skills is healed back into the discovery symlink, and
    # .claude/agents is (re)created, so Claude Code keeps auto-discovering the
    # canonical .agents/ content. .codex/skills stays removed (Codex resolves
    # skills through config.toml path= instead).
    assert link.is_symlink()
    assert link.readlink().as_posix() == "../.agents/skills"
    assert (target / ".claude/agents").is_symlink()
    assert (target / ".claude/agents").readlink().as_posix() == "../.agents/agents"
    assert not codex_skills.exists()
    assert {path.name for path in (target / ".codex").iterdir()} == {"config.toml"}
    migrated_settings = claude_settings.read_text(encoding="utf-8")
    assert ".agents/hooks/" in migrated_settings
    assert ".claude/hooks/" not in migrated_settings
    for relative_path in ("logs/session.log", "checkpoints/session.md"):
        assert (target / ".agents" / relative_path).read_text(encoding="utf-8") == (
            legacy_project_files[relative_path]
        )
    migration_backups = list(target.glob(".orchestra-backup-native-migration-*"))
    assert len(migration_backups) == 1
    assert (migration_backups[0] / ".claude/docs/DESIGN.md").read_text(
        encoding="utf-8"
    ) == legacy_project_files["docs/DESIGN.md"]


def test_update_migrates_legacy_claude_zones_into_agents_state(tmp_path: Path) -> None:
    template = build_template_repo(tmp_path)
    target = tmp_path / "project"
    init_git_repo(target)
    install_result = run_install(target, script=template / "scripts/install.sh")
    assert install_result.returncode == 0, install_result.stderr

    (target / "CLAUDE.md").unlink()
    legacy_state = "## Current Project\n\nKeep this migrated state.\n"
    (target / "CLAUDE.md").write_text(
        "# Old Claude adapter\n\n"
        "# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "# @orchestra:template-boundary\n"
        "# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "## Repository Identity\n\nLegacy project.\n\n"
        "# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "# @orchestra:repo-boundary\n"
        "# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n" + legacy_state,
        encoding="utf-8",
    )
    (target / "AGENTS.md").write_text("# Old CLI bootstrap\n", encoding="utf-8")

    update_result = run_update(target, template)

    assert update_result.returncode == 0, update_result.stderr
    assert (target / "CLAUDE.md").is_symlink()
    assert (target / "CLAUDE.md").resolve() == (target / "AGENTS.md").resolve()
    migrated = (target / ".agents/STATE.md").read_text(encoding="utf-8")
    assert "Legacy project." in migrated
    assert legacy_state.strip() in migrated
    assert "Legacy project." not in (target / "AGENTS.md").read_text(encoding="utf-8")


def test_update_leaves_no_stage_and_swap_debris_and_syncs_safe_dirs(
    tmp_path: Path,
) -> None:
    template = build_template_repo(tmp_path)

    target = tmp_path / "project"
    init_git_repo(target)
    install_result = run_install(target, script=template / "scripts/install.sh")
    assert install_result.returncode == 0, install_result.stderr

    marker = "# updated marker for debris test\n"
    (template / ".agents/INDEX.md").write_text(
        (template / ".agents/INDEX.md").read_text(encoding="utf-8") + marker,
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(template), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(template), "commit", "-qm", "bump .agents content"],
        check=True,
    )

    update_result = run_update(target, template)

    assert update_result.returncode == 0, update_result.stderr
    assert find_debris(target) == []
    assert (target / ".agents/INDEX.md").read_text(encoding="utf-8") == (
        template / ".agents/INDEX.md"
    ).read_text(encoding="utf-8")


def test_update_rolls_back_safe_dir_on_mid_swap_failure(tmp_path: Path) -> None:
    template = build_template_repo(tmp_path)

    target = tmp_path / "project"
    init_git_repo(target)
    install_result = run_install(target, script=template / "scripts/install.sh")
    assert install_result.returncode == 0, install_result.stderr

    original_index = (target / ".agents/INDEX.md").read_text(encoding="utf-8")
    original_listing = sorted(
        p.relative_to(target / ".agents") for p in (target / ".agents").rglob("*")
    )

    marker = "# should never reach the target on a rolled-back update\n"
    (template / ".agents/INDEX.md").write_text(
        (template / ".agents/INDEX.md").read_text(encoding="utf-8") + marker,
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(template), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(template), "commit", "-qm", "bump .agents content"],
        check=True,
    )

    # Shim `mv` on PATH to fail specifically on the second (staging -> live)
    # rename of the .agents/rules swap, simulating a crash between the two mv
    # calls in sync_safe_dirs(). Every other invocation delegates to the
    # real mv so the rest of the update proceeds normally.
    shim_dir = tmp_path / "fake-bin"
    shim_dir.mkdir()
    mv_shim = shim_dir / "mv"
    mv_shim.write_text(
        "#!/usr/bin/env bash\n"
        'for arg in "$@"; do\n'
        '    if [[ "${arg}" == *".agents/rules.orchestra-staging."* ]]; then\n'
        '        echo "shim: simulated mid-swap mv failure" >&2\n'
        "        exit 1\n"
        "    fi\n"
        "done\n"
        'exec /bin/mv "$@"\n',
        encoding="utf-8",
    )
    mv_shim.chmod(0o755)

    update_result = run_update(target, template, extra_path=shim_dir)

    assert update_result.returncode != 0
    assert find_debris(target) == []
    restored_listing = sorted(
        p.relative_to(target / ".agents") for p in (target / ".agents").rglob("*")
    )
    assert restored_listing == original_listing
    assert (target / ".agents/INDEX.md").read_text(encoding="utf-8") == original_index


def snapshot_files(root: Path) -> dict[str, bytes | str]:
    return {
        str(path.relative_to(root)): (
            os.readlink(path)
            if path.is_symlink()
            else path.read_bytes()
            if path.is_file()
            else "directory"
        )
        for path in root.rglob("*")
        if ".git" not in path.relative_to(root).parts
    }


@pytest.mark.parametrize("via_link", [False, True])
def test_install_rejects_control_characters_in_physical_root(
    tmp_path: Path, via_link: bool
) -> None:
    sibling = tmp_path / "project"
    target = tmp_path / "project\n"
    init_git_repo(sibling)
    init_git_repo(target)
    alias = tmp_path / "alias"
    alias.symlink_to(target)
    result = run_install(alias if via_link else target)
    assert result.returncode == 2
    assert snapshot_files(sibling) == {}
    assert snapshot_files(target) == {}


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "syntax",
        "noise",
        "nul",
        "unknown",
        "always-safe",
        "missing-python",
        "old-python",
        "broken-python",
    ],
)
def test_bootstrap_helper_failure_precedes_target_mutation(
    tmp_path: Path, fault: str
) -> None:
    template = build_template_repo(tmp_path)
    target = tmp_path / "project"
    init_git_repo(target)
    installed = run_install(target, script=template / "scripts/install.sh")
    assert installed.returncode == 0, installed.stderr
    helper = template / ".agents/skills/_shared/path_safety.py"
    contents = {
        "syntax": "invalid python !",
        "noise": "print('')\n",
        "nul": "import sys\nsys.stdout.buffer.write(b'\\x00')\n",
        "unknown": "raise SystemExit(17)\n",
        "always-safe": "raise SystemExit(0)\n",
    }
    extra_path = None
    if fault.endswith("-python"):
        extra_path = tmp_path / "bin"
        extra_path.mkdir()
        python = extra_path / "python3"
        statuses = {"missing-python": 127, "old-python": 2, "broken-python": 139}
        python.write_text(f"#!/bin/sh\nexit {statuses[fault]}\n")
        if fault == "old-python":
            python.write_text(
                f"#!/bin/sh\nexec {shlex.quote(sys.executable)} -I -c "
                "'import sys; sys.version_info = (3, 8); exec(sys.argv[1])' \"$3\"\n"
            )
        python.chmod(0o755)
    elif fault == "missing":
        helper.unlink()
    else:
        helper.write_text(contents[fault])
    subprocess.run(["git", "-C", str(template), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(template), "commit", "--allow-empty", "-qm", "broken helper"],
        check=True,
    )
    before = snapshot_files(target)
    updated = run_update(target, template, extra_path=extra_path)
    assert updated.returncode != 0
    assert "path safety" in updated.stderr.lower()
    assert snapshot_files(target) == before
    fresh = tmp_path / "fresh"
    init_git_repo(fresh)
    (fresh / "sentinel").write_bytes(b"unchanged")
    before = snapshot_files(fresh)
    installed = run_install(
        fresh, script=template / "scripts/install.sh", extra_path=extra_path
    )
    assert installed.returncode != 0
    assert "path safety" in installed.stderr.lower()
    assert snapshot_files(fresh) == before


def test_update_deprecated_cleanup_preserves_unsafe_targets(tmp_path: Path) -> None:
    template = build_template_repo(tmp_path)
    target = tmp_path / "project"
    init_git_repo(target)
    installed = run_install(target, script=template / "scripts/install.sh")
    assert installed.returncode == 0, installed.stderr
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel").write_text("outside")
    (target / ".gemini").symlink_to(outside)
    (target / ".claude/hooks").symlink_to("..")
    (target / ".claude/rules").mkdir()
    (target / ".claude/rules/obsolete").write_text("remove")
    result = run_update(target, template)
    assert result.returncode == 0, result.stderr
    assert (outside / "sentinel").read_text() == "outside"
    assert (target / ".gemini").is_symlink()
    assert (target / ".claude/hooks").is_symlink()
    assert not (target / ".claude/rules").exists()
    assert "Refusing to remove" in result.stderr


def test_update_uses_fetched_helper_when_installed_helper_is_absent(
    tmp_path: Path,
) -> None:
    template = build_template_repo(tmp_path)
    target = tmp_path / "project"
    init_git_repo(target)
    installed = run_install(target, script=template / "scripts/install.sh")
    assert installed.returncode == 0, installed.stderr
    (target / ".agents/skills/_shared/path_safety.py").unlink()
    (target / ".gemini").mkdir()
    (target / ".gemini/obsolete").write_text("remove")
    result = run_update(target, template)
    assert result.returncode == 0, result.stderr
    assert not (target / ".gemini").exists()
    assert (target / ".agents/skills/_shared/path_safety.py").is_file()


@pytest.mark.parametrize("flavor", ["gnu", "bsd", "broken"])
def test_settings_sed_capability_selection(tmp_path: Path, flavor: str) -> None:
    target = tmp_path / "project"
    settings = target / ".claude/settings.json"
    settings.parent.mkdir(parents=True)
    original = '{"hook":".claude/hooks/a", "other":"keep"}\n'
    settings.write_text(original)
    settings.chmod(0o640)
    metadata = settings.stat()
    shim = tmp_path / "bin"
    shim.mkdir()
    # Emulate argument parsing only; the host sed still performs the exact edit.
    sed = shim / "sed"
    sed.write_text(
        "#!/usr/bin/env bash\n"
        f"flavor={flavor}\n"
        '[[ "$flavor" != broken ]] || exit 1\n'
        'if [[ "$1" == -i ]]; then\n'
        "  shift\n"
        '  if [[ "$flavor" == bsd ]]; then\n'
        '    [[ "$1" == "" ]] || exit 1\n'
        "    shift\n"
        "  else\n"
        '    [[ "$1" != "" ]] || exit 1\n'
        "  fi\n"
        "  if /usr/bin/sed --version >/dev/null 2>&1; then\n"
        '    exec /usr/bin/sed -i "$@"\n'
        "  else\n"
        '    exec /usr/bin/sed -i "" "$@"\n'
        "  fi\n"
        "fi\n"
        'exec /usr/bin/sed "$@"\n'
    )
    sed.chmod(0o755)
    definitions = (
        (REPO_ROOT / "scripts/update.sh").read_text().rsplit('\nmain "$@"', 1)[0]
    )
    driver = tmp_path / "sed-driver.sh"
    driver.write_text(
        definitions
        + '\nPROJECT_ROOT="$1"\nTMPDIR_UPDATE="$2"\nselect_sed_in_place\nmigrate_native_settings_paths\nmigrate_native_settings_paths'
    )
    result = subprocess.run(
        ["bash", str(driver), str(target), str(tmp_path / "probe")],
        env={**os.environ, "PATH": f"{shim}{os.pathsep}{os.environ['PATH']}"},
        capture_output=True,
        text=True,
    )
    if flavor == "broken":
        assert result.returncode != 0
        assert settings.read_text() == original
    else:
        assert result.returncode == 0, result.stderr
        assert settings.read_text() == original.replace(
            ".claude/hooks/", ".agents/hooks/"
        )
        after = settings.stat()
        assert (after.st_mode, after.st_uid, after.st_gid) == (
            metadata.st_mode,
            metadata.st_uid,
            metadata.st_gid,
        )
    assert sorted(path.name for path in settings.parent.iterdir()) == ["settings.json"]

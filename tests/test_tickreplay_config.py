"""Tests for resolving the market-data root."""

from __future__ import annotations

import pytest

from tickreplay.config import ENV_VAR, DataRootError, read_env_file, resolve_data_root


def test_read_env_file_ignores_comments_and_strips_quotes(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "# comment line",
                "",
                'BACKCAST_JQUANTS_DUCKDB_ROOT = "S:/jp" ',
                "OTHER='value'",
                "not-a-pair",
            ]
        ),
        encoding="utf-8",
    )

    values = read_env_file(env_path)

    assert values[ENV_VAR] == "S:/jp"
    assert values["OTHER"] == "value"
    assert "not-a-pair" not in values


def test_read_env_file_tolerates_a_utf8_bom(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(f"{ENV_VAR}=S:/jp\n", encoding="utf-8-sig")

    assert read_env_file(env_path)[ENV_VAR] == "S:/jp"


def test_missing_file_is_not_an_error(tmp_path):
    assert read_env_file(tmp_path / "absent.env") == {}


def test_environment_wins_over_the_env_file(tmp_path):
    from_env = tmp_path / "from-env"
    from_env.mkdir()
    from_file = tmp_path / "from-file"
    from_file.mkdir()
    env_path = tmp_path / ".env"
    env_path.write_text(f"{ENV_VAR}={from_file}\n", encoding="utf-8")

    resolved = resolve_data_root(env={ENV_VAR: str(from_env)}, env_file=env_path)

    assert resolved == from_env


def test_env_file_is_used_when_the_environment_is_unset(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    env_path = tmp_path / ".env"
    env_path.write_text(f"{ENV_VAR}={root}\n", encoding="utf-8")

    assert resolve_data_root(env={}, env_file=env_path) == root


def test_falls_back_to_the_env_file_when_the_environment_points_nowhere(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    env_path = tmp_path / ".env"
    env_path.write_text(f"{ENV_VAR}={root}\n", encoding="utf-8")

    resolved = resolve_data_root(
        env={ENV_VAR: str(tmp_path / "does-not-exist")}, env_file=env_path
    )

    assert resolved == root


def test_unset_everywhere_raises(tmp_path):
    with pytest.raises(DataRootError, match=ENV_VAR):
        resolve_data_root(env={}, env_file=tmp_path / "absent.env")


def test_a_configured_but_missing_directory_raises(tmp_path):
    missing = tmp_path / "gone"
    with pytest.raises(DataRootError, match="not a directory"):
        resolve_data_root(env={ENV_VAR: str(missing)}, env_file=tmp_path / "absent.env")

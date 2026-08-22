"""Tests for resolving the DuckDB server-cache config."""

from __future__ import annotations

import httpx
import pytest

from tickreplay.config import (
    CACHE_DIR_ENV_VAR,
    DEFAULT_SERVER_URL,
    LOCAL_AUTHORITATIVE_ENV_VAR,
    SERVER_URL_ENV_VAR,
    CacheConfig,
    CacheConfigError,
    build_http_client,
    read_env_file,
    resolve_cache_config,
)


def test_read_env_file_ignores_comments_and_strips_quotes(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "# comment line",
                "",
                'BACKCAST_DUCKDB_CACHE_DIR = "S:/jp" ',
                "OTHER='value'",
                "not-a-pair",
            ]
        ),
        encoding="utf-8",
    )

    values = read_env_file(env_path)

    assert values[CACHE_DIR_ENV_VAR] == "S:/jp"
    assert values["OTHER"] == "value"
    assert "not-a-pair" not in values


def test_read_env_file_tolerates_a_utf8_bom(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(f"{CACHE_DIR_ENV_VAR}=S:/jp\n", encoding="utf-8-sig")

    assert read_env_file(env_path)[CACHE_DIR_ENV_VAR] == "S:/jp"


def test_missing_file_is_not_an_error(tmp_path):
    assert read_env_file(tmp_path / "absent.env") == {}


# --------------------------------------------------------------------------
# resolve_cache_config / CacheConfig
# --------------------------------------------------------------------------


def test_cache_config_defaults_the_server_url_when_unset(tmp_path):
    cache_dir = tmp_path / "cache"

    config = resolve_cache_config(
        env={CACHE_DIR_ENV_VAR: str(cache_dir)}, env_file=tmp_path / "absent.env"
    )

    assert config.server_url == DEFAULT_SERVER_URL
    assert config.cache_dir == cache_dir


def test_cache_config_normalizes_a_trailing_slash_on_the_server_url(tmp_path):
    cache_dir = tmp_path / "cache"

    config = resolve_cache_config(
        env={
            CACHE_DIR_ENV_VAR: str(cache_dir),
            SERVER_URL_ENV_VAR: "http://example.invalid:8080/",
        },
        env_file=tmp_path / "absent.env",
    )

    assert config.server_url == "http://example.invalid:8080"


def test_cache_config_rejects_a_server_url_without_a_scheme(tmp_path):
    with pytest.raises(CacheConfigError, match=SERVER_URL_ENV_VAR):
        resolve_cache_config(
            env={
                CACHE_DIR_ENV_VAR: str(tmp_path / "cache"),
                SERVER_URL_ENV_VAR: "example.invalid:8080",
            },
            env_file=tmp_path / "absent.env",
        )


def test_cache_config_creates_a_missing_cache_dir(tmp_path):
    cache_dir = tmp_path / "nested" / "cache"

    config = resolve_cache_config(
        env={CACHE_DIR_ENV_VAR: str(cache_dir)}, env_file=tmp_path / "absent.env"
    )

    assert config.cache_dir.is_dir()


def test_cache_config_unset_dir_raises(tmp_path):
    with pytest.raises(CacheConfigError, match=CACHE_DIR_ENV_VAR):
        resolve_cache_config(env={}, env_file=tmp_path / "absent.env")


def test_cache_config_rejects_a_dir_path_that_is_actually_a_file(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")

    with pytest.raises(CacheConfigError, match="not a directory"):
        resolve_cache_config(
            env={CACHE_DIR_ENV_VAR: str(blocker)}, env_file=tmp_path / "absent.env"
        )


def test_cache_config_env_file_is_used_when_the_environment_is_unset(tmp_path):
    cache_dir = tmp_path / "cache"
    env_path = tmp_path / ".env"
    env_path.write_text(f"{CACHE_DIR_ENV_VAR}={cache_dir}\n", encoding="utf-8")

    config = resolve_cache_config(env={}, env_file=env_path)

    assert config.cache_dir == cache_dir


def test_cache_config_environment_wins_over_the_env_file(tmp_path):
    from_env = tmp_path / "from-env"
    env_path = tmp_path / ".env"
    env_path.write_text(
        f"{CACHE_DIR_ENV_VAR}={tmp_path / 'from-file'}\n", encoding="utf-8"
    )

    config = resolve_cache_config(
        env={CACHE_DIR_ENV_VAR: str(from_env)}, env_file=env_path
    )

    assert config.cache_dir == from_env


def test_local_authoritative_is_off_unless_explicitly_enabled(tmp_path):
    config = resolve_cache_config(
        env={CACHE_DIR_ENV_VAR: str(tmp_path / "cache")}, env_file=tmp_path / ".env"
    )

    assert config.local_authoritative is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_local_authoritative_accepts_the_documented_true_values(tmp_path, value):
    config = resolve_cache_config(
        env={
            CACHE_DIR_ENV_VAR: str(tmp_path / "cache"),
            LOCAL_AUTHORITATIVE_ENV_VAR: value,
        },
        env_file=tmp_path / ".env",
    )

    assert config.local_authoritative is True


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off"])
def test_local_authoritative_accepts_the_documented_false_values(tmp_path, value):
    config = resolve_cache_config(
        env={
            CACHE_DIR_ENV_VAR: str(tmp_path / "cache"),
            LOCAL_AUTHORITATIVE_ENV_VAR: value,
        },
        env_file=tmp_path / ".env",
    )

    assert config.local_authoritative is False


@pytest.mark.parametrize("value", ["", "  ", "maybe", "2", "truthy"])
def test_local_authoritative_rejects_invalid_values(tmp_path, value):
    with pytest.raises(CacheConfigError, match=LOCAL_AUTHORITATIVE_ENV_VAR):
        resolve_cache_config(
            env={
                CACHE_DIR_ENV_VAR: str(tmp_path / "cache"),
                LOCAL_AUTHORITATIVE_ENV_VAR: value,
            },
            env_file=tmp_path / ".env",
        )


def test_local_authoritative_can_come_from_the_env_file(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        f"{CACHE_DIR_ENV_VAR}={tmp_path / 'cache'}\n{LOCAL_AUTHORITATIVE_ENV_VAR}=1\n",
        encoding="utf-8",
    )

    config = resolve_cache_config(env={}, env_file=env_path)

    assert config.local_authoritative is True


# --------------------------------------------------------------------------
# build_http_client (the HTTP-transport injection seam)
# --------------------------------------------------------------------------


def test_build_http_client_uses_the_configured_base_url(tmp_path):
    config = CacheConfig(server_url="http://example.invalid:8080", cache_dir=tmp_path)

    client = build_http_client(config)
    try:
        assert str(client.base_url) == "http://example.invalid:8080"
    finally:
        client.close()


def test_build_http_client_accepts_an_injected_transport_for_tests(tmp_path):
    config = CacheConfig(server_url="http://example.invalid:8080", cache_dir=tmp_path)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, content=b"ok")

    client = build_http_client(config, transport=httpx.MockTransport(handler))
    try:
        response = client.get("/api/stocks-trades")
        assert response.status_code == 200
        assert calls == ["/api/stocks-trades"]
    finally:
        client.close()

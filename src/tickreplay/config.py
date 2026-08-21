"""Resolution of the DuckDB server-cache config.

Precedence for both env vars below is the process environment, then a
``.env`` file next to the repository root.

The pre-cutover local-data-root resolver (``BACKCAST_JQUANTS_DUCKDB_ROOT``,
``resolve_data_root``/``resolve_trades_dir``) has been deleted, in the same
commit that switched ``repository.py``/``server.py`` off it (Step 6 of
``.agents/docs/plans/duckdb-server-cache.md`` — Codex round 2: the old
resolver could not be deleted before the step that stops using it without
leaving the app broken in between; both land together here).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import httpx

SERVER_URL_ENV_VAR = "BACKCAST_DUCKDB_SERVER_URL"
CACHE_DIR_ENV_VAR = "BACKCAST_DUCKDB_CACHE_DIR"
DEFAULT_SERVER_URL = "http://backcast.i234.me:8080"

REPO_ROOT = Path(__file__).resolve().parents[2]


class CacheConfigError(RuntimeError):
    """The DuckDB server-cache configuration could not be resolved."""


def read_env_file(env_path: Path) -> dict[str, str]:
    """Parse a ``KEY=VALUE`` env file. Missing files yield an empty mapping."""
    if not env_path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


# --------------------------------------------------------------------------
# DuckDB server-cache config
# --------------------------------------------------------------------------


def _normalize_server_url(raw: str) -> str:
    """Strip trailing slashes and require an explicit http(s) scheme."""
    value = raw.strip().rstrip("/")
    if not value.startswith(("http://", "https://")):
        raise CacheConfigError(
            f"{SERVER_URL_ENV_VAR} must start with http:// or https://, got {raw!r}"
        )
    return value


@dataclass(frozen=True)
class CacheConfig:
    """Resolved location of the local cache dir and the source file server."""

    server_url: str
    cache_dir: Path


def resolve_cache_config(
    *,
    env: dict[str, str] | None = None,
    env_file: Path | None = None,
) -> CacheConfig:
    """Return the server-cache config.

    ``BACKCAST_DUCKDB_SERVER_URL`` is optional (defaults to the production
    file server); ``BACKCAST_DUCKDB_CACHE_DIR`` is required — precedence for
    both is the process environment, then the ``.env`` file, matching
    ``resolve_data_root``. The cache directory is created (including parents)
    if it does not already exist, since it is storage this app owns and
    manages, unlike the old, externally-populated data root; a path that
    exists but is not a directory is still a hard error.
    """
    environ = os.environ if env is None else env
    dotenv_path = REPO_ROOT / ".env" if env_file is None else env_file
    file_values = read_env_file(dotenv_path)

    raw_url = (
        environ.get(SERVER_URL_ENV_VAR, "").strip()
        or file_values.get(SERVER_URL_ENV_VAR, "").strip()
    )
    server_url = _normalize_server_url(raw_url or DEFAULT_SERVER_URL)

    raw_dir = (
        environ.get(CACHE_DIR_ENV_VAR, "").strip()
        or file_values.get(CACHE_DIR_ENV_VAR, "").strip()
    )
    if not raw_dir:
        raise CacheConfigError(
            f"{CACHE_DIR_ENV_VAR} is not set in the environment and not "
            f"present in {dotenv_path}"
        )

    cache_dir = Path(raw_dir).expanduser()
    if cache_dir.exists() and not cache_dir.is_dir():
        raise CacheConfigError(
            f"{CACHE_DIR_ENV_VAR} points at {cache_dir}, which is not a directory"
        )
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise CacheConfigError(
            f"{CACHE_DIR_ENV_VAR}={cache_dir} could not be created: {error}"
        ) from error

    return CacheConfig(server_url=server_url, cache_dir=cache_dir)


# --------------------------------------------------------------------------
# HTTP-transport injection seam
# --------------------------------------------------------------------------
#
# Every network call the cache layer (Step 4/5) makes goes through a client
# built here, never through a module-level ``httpx`` client constructed
# elsewhere — so tests can substitute an in-memory ``httpx.MockTransport``
# (simulating 200/206/304/5xx/timeouts/truncated bodies) without touching a
# real socket, and production wires the real transport exactly once, in
# Step 7's lifespan hook.

DEFAULT_TIMEOUT_SECONDS = 30.0


def build_http_client(
    config: CacheConfig,
    *,
    transport: httpx.BaseTransport | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> httpx.Client:
    """Build the shared HTTP client used for all server-cache downloads.

    ``transport`` is the injection seam: pass an ``httpx.MockTransport`` in
    tests, leave it ``None`` in production to use httpx's real transport.
    """
    return httpx.Client(
        base_url=config.server_url,
        timeout=timeout,
        transport=transport,
    )

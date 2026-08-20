"""Resolution of the market-data root.

The DuckDB root is never hard-coded: it comes from the environment variable
``BACKCAST_JQUANTS_DUCKDB_ROOT``, falling back to a ``.env`` file next to the
repository root.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_VAR = "BACKCAST_JQUANTS_DUCKDB_ROOT"
TRADES_DIRNAME = "stocks_trades"

REPO_ROOT = Path(__file__).resolve().parents[2]


class DataRootError(RuntimeError):
    """The market-data root could not be resolved."""


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


def resolve_data_root(
    *,
    env: dict[str, str] | None = None,
    env_file: Path | None = None,
) -> Path:
    """Return the DuckDB root directory.

    Precedence: process environment, then the ``.env`` file. The returned path
    is verified to exist so that a stale configuration fails loudly at startup
    instead of producing an empty symbol list.
    """
    environ = os.environ if env is None else env
    candidates: list[tuple[str, str]] = []

    from_environ = environ.get(ENV_VAR, "").strip()
    if from_environ:
        candidates.append((f"environment variable {ENV_VAR}", from_environ))

    dotenv_path = REPO_ROOT / ".env" if env_file is None else env_file
    from_file = read_env_file(dotenv_path).get(ENV_VAR, "").strip()
    if from_file:
        candidates.append((f"{ENV_VAR} in {dotenv_path}", from_file))

    if not candidates:
        raise DataRootError(
            f"{ENV_VAR} is not set in the environment and not present in {dotenv_path}"
        )

    for source, value in candidates:
        root = Path(value).expanduser()
        if root.is_dir():
            return root
        last_error = f"{source} points at {root}, which is not a directory"
    raise DataRootError(last_error)


def resolve_trades_dir(
    *,
    env: dict[str, str] | None = None,
    env_file: Path | None = None,
) -> Path:
    """Return ``<data root>/stocks_trades``."""
    root = resolve_data_root(env=env, env_file=env_file)
    trades = root / TRADES_DIRNAME
    if not trades.is_dir():
        raise DataRootError(f"{trades} does not exist under the resolved data root")
    return trades

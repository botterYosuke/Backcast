"""Local file server for BackcastPro, merged with the 歩み値リプレイ UI.

Serves .duckdb files from a local directory over HTTP, exposes a small
GraphQL ranking API, and mounts ``tickreplay``'s FastAPI app (the tick
replay UI/API) at ``/`` so the same host serves both. Deployed as a single
Docker container built from the repository root (see docs/tick-replay.md).
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import os
import re
import stat
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from email.utils import formatdate, parsedate_to_datetime
from http import HTTPStatus
from pathlib import Path

import anyio
import duckdb as _duckdb
import strawberry
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import (
    JSONResponse,
    PlainTextResponse,
    Response,
    StreamingResponse,
)
from strawberry.fastapi import GraphQLRouter

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DATA_DIR = os.environ.get("STOCKDATA_CACHE_DIR", "/cache")
STOCKS_TRADES_SUBDIR = os.path.join("jp", "stocks_trades")
PORT = int(os.environ.get("PORT", 8080))

# tickreplay's own server-cache config, defaulted so the merged deployment
# works out of the box (still overridable via real env vars):
#  - talk to this same process over loopback instead of the public hostname
#  - point the cache dir at this server's own `jp` root so files already
#    present under `jp/stocks_trades/` are recognized as cached rather than
#    downloaded a second time (see docs/tick-replay.md's cache-dir note and
#    `src/tickreplay/repository.py`'s module docstring)
#  - mark that all-default arrangement authoritative so those files are never
#    revalidated against themselves
server_was_default = "BACKCAST_DUCKDB_SERVER_URL" not in os.environ
cache_was_default = "BACKCAST_DUCKDB_CACHE_DIR" not in os.environ
os.environ.setdefault("BACKCAST_DUCKDB_SERVER_URL", f"http://127.0.0.1:{PORT}")
os.environ.setdefault("BACKCAST_DUCKDB_CACHE_DIR", os.path.join(DATA_DIR, "jp"))
os.environ.setdefault(
    "BACKCAST_DUCKDB_LOCAL_AUTHORITATIVE",
    "true" if server_was_default and cache_was_default else "false",
)

# `src/tickreplay` must be importable. `Path(__file__).resolve().parent.parent`
# is the repository root in a checkout (cloud-run/main.py -> repo root) and
# must land on the directory containing `src/` inside the Docker image too —
# the Dockerfile's WORKDIR/COPY layout is chosen to match this.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tickreplay.server import app as tickreplay_app  # noqa: E402
from tickreplay.server import lifespan as tickreplay_lifespan  # noqa: E402


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Mounting `tickreplay_app` at "/" (below) only wires up HTTP routing —
    # Starlette's Router intercepts `scope["type"] == "lifespan"` and never
    # forwards it to a mounted sub-app, so tickreplay's own `lifespan` (which
    # fail-fast constructs its repository) would otherwise never run and
    # every tickreplay route would 500 with "repository is unavailable
    # outside the app lifespan". Entering it explicitly here fixes that.
    async with tickreplay_lifespan(tickreplay_app):
        yield


app = FastAPI(lifespan=lifespan)


@app.get("/healthz")
def health() -> PlainTextResponse:
    return PlainTextResponse("OK")


@app.get("/api/stocks-trades")
def list_stocks_trades() -> JSONResponse:
    """List the `jp/stocks_trades/*.duckdb` stems actually present on disk.

    Returns a sorted, de-duplicated JSON array of stems (filenames without
    the `.duckdb` suffix). The client (Backcast's ``tickreplay`` app)
    re-filters this list through its own, stricter ``SYMBOL_STEM_RE`` before
    trusting any entry — this endpoint intentionally does not attempt to
    replicate that validation, it only reports what files exist.
    """
    directory = os.path.join(DATA_DIR, STOCKS_TRADES_SUBDIR)
    try:
        with os.scandir(directory) as entries:
            stems = {
                entry.name[: -len(".duckdb")]
                for entry in entries
                if entry.name.endswith(".duckdb") and entry.is_file()
            }
    except OSError:
        logger.exception("Unable to list stocks_trades directory: %s", directory)
        return JSONResponse(
            {"error": "stocks_trades listing unavailable"},
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
        )
    return JSONResponse({"stems": sorted(stems)})


# Whitelist: only allow known file patterns.
#
# `stocks_daily` is narrower than the legacy dataset routes: only per-symbol
# 4-5 character ASCII alphanumeric stems plus the exact historical
# `mother.duckdb` aggregate are public. Explicit ASCII classes avoid `\w`'s
# Unicode and underscore expansion while retaining letter-bearing stems.
#
# `stocks_minute` uses the same `\w+` stem pattern as `stocks_trades`, not a
# digits-only one: a stem is a 4-5 character code that may contain letters
# (`285A`, `130A`, ... — 343 of the 4630 stems this server currently lists),
# and `stocks_minute` is keyed by the very same stem as `stocks_trades` (see
# `src/tickreplay/minute_context.py`). A digits-only pattern here rejected
# every letter-bearing stem's minute file at the regex, before ever touching
# the disk, so those symbols' candlestick charts came up empty against a
# remotely hosted server while working locally (where the cache dir is read
# directly and no HTTP request is involved) — including the UI's default
# symbol, `285A`.
ALLOWED_PATHS = re.compile(
    r"^jp/(stocks_daily/(?:[0-9A-Za-z]{4,5}|mother)\.duckdb|stocks_board/\d+\.duckdb|stocks_trades/\w+\.duckdb|stocks_minute/\w+\.duckdb|listed_info\.duckdb)$"
)

# A single, well-formed, in-bounds `bytes=` range (RFC 7233), case-
# insensitive in the unit token. The pre-migration Flask/Werkzeug route
# never supported *multiple* ranges (`bytes=0-1,5-6`) — a header with a
# comma, or anything else this doesn't match (reversed, non-numeric,
# out-of-bounds, wrong unit), is treated as unsatisfiable (416), matching
# that baseline rather than Starlette's newer multipart/400 behavior.
_RANGE_RE = re.compile(r"bytes=(\d+)-(\d*)", re.IGNORECASE)
_SUFFIX_RANGE_RE = re.compile(r"bytes=-(\d+)", re.IGNORECASE)

_CHUNK_SIZE = 64 * 1024


def _parse_single_range(range_header: str, size: int) -> tuple[int, int] | None:
    """A single in-bounds ``(start, end)`` inclusive byte span, or ``None``
    if unsatisfiable for any reason (including a multi-range request)."""
    value = range_header.strip()
    match = _RANGE_RE.fullmatch(value)
    if match:
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else size - 1
    else:
        match = _SUFFIX_RANGE_RE.fullmatch(value)
        if not match:
            return None
        start = max(size - int(match.group(1)), 0)
        end = size - 1
    if size == 0 or start >= size or start > end:
        return None
    return start, min(end, size - 1)


def _etag_matches(header_value: str, etag: str) -> bool:
    """RFC 7232 comparison of an `If-Match`/`If-None-Match` value (a
    comma-separated list of entity-tags, or the wildcard `*`) against one
    concrete ETag. `*` matches any existing resource."""
    value = header_value.strip()
    if value == "*":
        return True
    return any(tag.strip().removeprefix("W/") == etag for tag in value.split(","))


def _open_and_check(full_path: Path, data_root: str) -> tuple[int, os.stat_result]:
    """Open the file once and derive every later decision from that same
    descriptor's `fstat` — the route used to `stat()` the path and only
    later re-open it by name inside `FileResponse`, which raced a
    concurrent replace in the shared cache dir (this tree is also written
    to by the mounted tickreplay app's downloader) into serving new bytes
    under a stale ETag/Content-Length computed from the old file. Also
    rejects a non-regular file (e.g. a directory sharing an allowed name)
    and a symlink that resolves outside `data_root` — the whitelist above
    only constrains the request string, not what the name resolves to on
    disk. Raises `OSError` for any of these; the caller maps that to 404.
    """
    fd = os.open(full_path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(f"{full_path} is not a regular file")
        resolved = full_path.resolve(strict=True)
        root = Path(data_root).resolve(strict=True)
        if not resolved.is_relative_to(root):
            raise OSError(f"{full_path} resolves outside {data_root}")
        return fd, st
    except BaseException:
        os.close(fd)
        raise


async def _stream_fd(fd: int, start: int, length: int) -> AsyncIterator[bytes]:
    """Stream `length` bytes from `fd` starting at `start`, closing it when
    done (success, early client disconnect, or error) — ownership of `fd`
    passes to this generator once it is handed to `StreamingResponse`."""
    try:
        await anyio.to_thread.run_sync(os.lseek, fd, start, os.SEEK_SET)
        remaining = length
        while remaining > 0:
            chunk = await anyio.to_thread.run_sync(
                os.read, fd, min(_CHUNK_SIZE, remaining)
            )
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
    finally:
        os.close(fd)


def _stem_case_variants(full_path: Path) -> list[Path]:
    """`full_path` plus the same path with its stem lower-/upper-cased.

    The on-disk dataset spells a letter-bearing stem inconsistently — the
    very same symbol can be `stocks_trades/285A.duckdb` but
    `stocks_minute/285a.duckdb` (as of this writing, 116 of the 343
    letter-bearing stems this server lists have a lowercase-only minute
    file). Clients normalize a stem to upper case before requesting it
    (`tickreplay.repository.SYMBOL_STEM_RE` is upper-case-only, and the UI
    upper-cases what the user types), so on a case-sensitive filesystem —
    which the Linux container this is deployed in has, unlike the Windows
    host the same tree is authored on — those requests 404 against a file
    that is right there under a differently-cased name.

    Only the stem's case varies; the directory, the suffix, and every other
    character of the request are left exactly as they came in, so this
    widens what resolves on disk without widening `ALLOWED_PATHS` (both
    variants of a whitelisted path are themselves whitelisted, since the
    stem patterns are case-insensitive character classes).
    """
    candidates = [full_path]
    for stem in (full_path.stem.lower(), full_path.stem.upper()):
        # Compared as strings, not as paths: `WindowsPath("285A.duckdb") ==
        # WindowsPath("285a.duckdb")` is True (PurePath's equality folds case
        # on Windows), which would collapse the very variant this is here to
        # produce whenever the tests — or a developer — run on the authoring
        # host rather than the deployment container.
        if stem == full_path.stem:
            continue
        candidates.append(full_path.with_name(stem + full_path.suffix))
    return candidates


def _open_first_existing(full_path: Path, data_root: str) -> tuple[int, os.stat_result]:
    """`_open_and_check` over `_stem_case_variants`, first one that opens.

    Raises the original path's `OSError` if none of them do, so the caller's
    404 mapping is unchanged for a genuinely absent file.
    """
    first_error: OSError | None = None
    for candidate in _stem_case_variants(full_path):
        try:
            return _open_and_check(candidate, data_root)
        except OSError as error:
            if first_error is None:
                first_error = error
    assert first_error is not None
    raise first_error


@app.api_route("/jp/{file_path:path}", methods=["GET", "HEAD"])
async def download_file(file_path: str, request: Request) -> Response:
    # The route's literal "/jp/" prefix is not part of the captured
    # `file_path`, so it is added back here — `ALLOWED_PATHS` (and the
    # on-disk join below) both expect the full "jp/..." relative path, same
    # as the server's own layout (`STOCKS_TRADES_SUBDIR`) and the client's
    # download URL (`tickreplay.cache.STEM_PATH_TEMPLATE`).
    full_relative_path = f"jp/{file_path}"
    if not ALLOWED_PATHS.match(full_relative_path):
        return PlainTextResponse("Not Found", status_code=404)

    full_path = Path(DATA_DIR) / full_relative_path
    try:
        fd, st = await anyio.to_thread.run_sync(
            _open_first_existing, full_path, DATA_DIR
        )
    except OSError:
        return PlainTextResponse("Not Found", status_code=404)

    close_fd = True
    try:
        size = st.st_size
        last_modified = formatdate(st.st_mtime, usegmt=True)
        etag = f'"{hashlib.md5(f"{st.st_mtime}-{size}".encode(), usedforsecurity=False).hexdigest()}"'
        base_headers = {
            "ETag": etag,
            "Last-Modified": last_modified,
            "Accept-Ranges": "bytes",
        }

        if_match = request.headers.get("if-match")
        if if_match is not None and not _etag_matches(if_match, etag):
            return Response(status_code=412, headers=base_headers)

        not_modified = False
        if_none_match = request.headers.get("if-none-match")
        if if_none_match is not None:
            not_modified = _etag_matches(if_none_match, etag)
        else:
            if_modified_since = request.headers.get("if-modified-since")
            if if_modified_since is not None:
                try:
                    not_modified = parsedate_to_datetime(
                        if_modified_since
                    ) >= parsedate_to_datetime(last_modified)
                except (TypeError, ValueError):
                    not_modified = False
        if not_modified:
            return Response(status_code=304, headers=base_headers)

        start, end, status_code = 0, size - 1, 200
        range_header = request.headers.get("range")
        if range_header is not None:
            if_range = request.headers.get("if-range")
            use_range = if_range is None or if_range.strip() in (etag, last_modified)
            if use_range:
                parsed = _parse_single_range(range_header, size)
                if parsed is None:
                    return Response(
                        status_code=416,
                        headers={**base_headers, "Content-Range": f"bytes */{size}"},
                    )
                start, end = parsed
                status_code = 206

        length = end - start + 1
        headers = {**base_headers, "Content-Length": str(length)}
        if status_code == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"

        if request.method == "HEAD":
            return Response(status_code=status_code, headers=headers)

        close_fd = False  # ownership moves to the streaming generator below
        return StreamingResponse(
            _stream_fd(fd, start, length),
            status_code=status_code,
            headers=headers,
            media_type="application/octet-stream",
        )
    finally:
        if close_fd:
            os.close(fd)


@app.exception_handler(Exception)
async def handle_error(_request: Request, exc: Exception) -> PlainTextResponse:
    logger.error("Unexpected error: %s", exc, exc_info=True)
    return PlainTextResponse("Internal error", status_code=500)


MOTHER_DB_PATH = os.path.join(DATA_DIR, "jp", "stocks_daily", "mother.duckdb")

# SQL injection 対策ホワイトリスト
_ORDER_MAP = {"desc": "DESC", "asc": "ASC"}
# 使用可能な列名 → DuckDB SQL 表現へのマップ
_COL_MAP = {
    "Close": '"Close"',
    "Open": '"Open"',
    "High": '"High"',
    "Low": '"Low"',
    "Volume": '"Volume"',
}
# ColName[-N] トークン（例: Close[-2]）を認識する正規表現
_LAG_RE = re.compile(r"^(Close|Open|High|Low|Volume)\[-(\d+)\]$")
# トークナイザ: ColName[-N] を1トークンとして認識（[ ] が単独トークンにならないよう先にマッチ）
_TOKEN_RE = re.compile(
    r"(\b(?:Close|Open|High|Low|Volume)\b(?:\[-\d+\])?|[\d.]+|[+\-*/()]|\s+)"
)


def _parse_formula(
    formula: str,
) -> tuple[str, dict[str, tuple[str, int]]]:
    """ユーザー指定の計算式を検証し、DuckDB SQL 式と LAG 仕様に変換する。

    Returns:
        sort_expr: SQL 式文字列
        lag_specs: {alias: (col_name, lag_n)}
            例: {"Close__lag2": ("Close", 2)}
    不正なトークンが含まれる場合は ValueError を送出。
    """
    tokens = _TOKEN_RE.findall(formula)
    if "".join(tokens) != formula:
        raise ValueError(f"Invalid formula: unsupported tokens in {formula!r}")
    lag_specs: dict[str, tuple[str, int]] = {}
    parts = []
    for tok in tokens:
        s = tok.strip()
        if not s:
            parts.append(" ")
            continue
        m = _LAG_RE.match(s)
        if m:
            col, n = m.group(1), int(m.group(2))
            alias = f"{col}__lag{n}"
            lag_specs[alias] = (col, n)
            parts.append(f"NULLIF({alias}, 0)")  # ゼロ除算保護
        elif s in _COL_MAP:
            parts.append(_COL_MAP[s])
        else:  # 数値・演算子・括弧
            parts.append(s)
    return "".join(parts), lag_specs


@strawberry.type
class DailyRankingItem:
    date: str
    code: str
    close: float
    sort_value: float | None
    volume: float | None
    rank: int


@strawberry.type
class Query:
    @strawberry.field
    def stock_ranking_range(
        self,
        from_date: str,
        to_date: str,
        sort_by: str = "(Close - Close[-1]) / Close[-1] * 100",
        order: str = "desc",  # "desc" | "asc"
        limit: int = 20,
    ) -> list[DailyRankingItem]:
        """汎用ランキング（sortBy に DuckDB 計算式を直接指定）
        式中では Close[-N] / Open[-N] 等で N 営業日前の値を参照できる。
        例: (Close - Close[-2]) / Close[-2] * 100
        """
        try:
            return _stock_ranking_range(from_date, to_date, sort_by, order, limit)
        except ValueError:
            # Our own input validation below — a safe, intentional
            # client-facing message with no filesystem/DB internals in it.
            raise
        except Exception as error:
            # Anything else (e.g. DuckDB failing to open `MOTHER_DB_PATH`)
            # can embed the server's absolute cache path in its message;
            # Strawberry would otherwise forward that string verbatim to
            # the client as `errors[].message`. Log the real error and
            # raise a generic one instead — this endpoint is public and
            # unauthenticated (docs/tick-replay.md).
            logger.error("stock_ranking_range failed: %s", error, exc_info=True)
            raise ValueError("stock_ranking_range: internal error") from None


# A free function, not a `Query` method: Strawberry resolves root-level
# query fields with `self` bound to the GraphQL root value (`None` here,
# since no root value is configured), not a `Query` instance — the field
# above never referenced `self` for exactly that reason. Keeping the
# error-sanitizing wrapper as the actual `@strawberry.field` and factoring
# the query body out here (rather than as `Query._stock_ranking_range`)
# avoids re-introducing a `self`-shaped API that would silently be `None`
# at call time.
def _stock_ranking_range(
    from_date: str,
    to_date: str,
    sort_by: str,
    order: str,
    limit: int,
) -> list[DailyRankingItem]:
    sort_expr, lag_specs = _parse_formula(sort_by)
    order_sql = _ORDER_MAP[order]
    max_lag = max((n for _, n in lag_specs.values()), default=1)

    # with_prev に追加する LAG 列定義
    extra_lag_cols = "".join(
        f',\n            LAG("{col}", {n}) OVER (PARTITION BY "Code" ORDER BY "Date") AS {alias}'
        for alias, (col, n) in lag_specs.items()
    )

    try:
        from_dt = datetime.datetime.strptime(from_date, "%Y-%m-%d")
        to_dt = datetime.datetime.strptime(to_date, "%Y-%m-%d")
    except ValueError as error:
        raise ValueError(
            "from_date and to_date must be in YYYY-MM-DD format"
        ) from error

    safe_from_date = from_dt.strftime("%Y-%m-%d")
    safe_to_date = to_dt.strftime("%Y-%m-%d")
    min_date_val = (from_dt - datetime.timedelta(days=60)).strftime("%Y-%m-%d")

    # データが Code 順にクラスタリングされているため、全探索を防ぐ目的で
    # 時価総額日本一(285A: キオクシア)の営業日カレンダーを利用して遡及日を高速抽出する
    # 内部で MIN() を取ると DuckDB オプティマイザがサブクエリを展開して
    # フルテーブルスキャンにフォールバックするため、結果を必ずリストで受け取る
    boundary_sql = f"""
            SELECT "Date"
            FROM stocks_daily
            WHERE "Code" = '285A'
              AND "Date" >= '{min_date_val}' AND "Date" < '{safe_from_date}'
            ORDER BY "Date" DESC
            LIMIT {max_lag}
        """
    with _duckdb.connect(MOTHER_DB_PATH, read_only=True) as con:
        boundary_res = con.execute(boundary_sql).fetchall()

    target_min_date = boundary_res[-1][0] if boundary_res else "1970-01-01"

    sql = f"""
        WITH extended AS (
            -- target_min_date から to_date までを取得（LAG 計算用バッファ込み）
            SELECT "Code", "Date", "Open", "High", "Low", "Close", "Volume"
            FROM stocks_daily
            WHERE "Date" >= '{target_min_date}'
              AND "Date" <= '{safe_to_date}'
        ),
        with_prev AS (
            SELECT *{extra_lag_cols}
            FROM extended
        ),
        ranked AS (
            SELECT
                "Date", "Code", "Close", "Volume",
                {sort_expr} AS SortValue,
                ROW_NUMBER() OVER (
                    PARTITION BY "Date"
                    ORDER BY SortValue {order_sql} NULLS LAST
                ) AS Rank
            FROM with_prev
            WHERE "Date" >= '{safe_from_date}'
        )
        SELECT "Date", "Code", "Close", SortValue, "Volume", Rank
        FROM ranked
        WHERE Rank <= {limit}
        ORDER BY "Date", Rank
        """
    with _duckdb.connect(MOTHER_DB_PATH, read_only=True) as con:
        rows = con.execute(sql).fetchall()
    return [
        DailyRankingItem(
            date=str(r[0]),
            code=r[1],
            close=r[2],
            sort_value=round(r[3], 4) if r[3] is not None else None,
            volume=r[4],
            rank=r[5],
        )
        for r in rows
    ]


gql_schema = strawberry.Schema(query=Query)
app.include_router(GraphQLRouter(gql_schema), prefix="/graphql")

# Everything not matched above (`/`, `/api/status`, `/api/symbols*`,
# `/api/session`, `/static/*`) falls through to the tick-replay UI/API.
# Registration order matters here: Starlette tries routes in the order
# they were added, and only an explicit path pattern registered *before*
# this mount (all of the above) can intercept a request ahead of it.
app.mount("/", tickreplay_app)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)

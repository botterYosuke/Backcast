"""Local file server for BackcastPro.

Serves .duckdb files from a local directory, exposed as HTTP GET.
Deployed on Cloud Run.
"""

import datetime
import logging
import os
import re
from http import HTTPStatus

import duckdb as _duckdb
import strawberry
from dotenv import load_dotenv
from flask import Flask, jsonify, send_from_directory
from strawberry.flask.views import GraphQLView
from werkzeug.exceptions import HTTPException, NotFound

load_dotenv()

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DATA_DIR = os.environ.get("STOCKDATA_CACHE_DIR", "/cache")
STOCKS_TRADES_SUBDIR = os.path.join("jp", "stocks_trades")

# Whitelist: only allow known file patterns
ALLOWED_PATHS = re.compile(
    r"^jp/(stocks_daily/(?:\d+|mother)\.duckdb|stocks_board/\d+\.duckdb|stocks_trades/\w+\.duckdb|stocks_minute/\d+\.duckdb|listed_info\.duckdb)$"
)


@app.route("/")
def health():
    return "OK", 200


@app.route("/api/stocks-trades", methods=["GET"])
def list_stocks_trades():
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
        return (
            jsonify({"error": "stocks_trades listing unavailable"}),
            HTTPStatus.SERVICE_UNAVAILABLE,
        )
    return jsonify({"stems": sorted(stems)})


@app.route("/<path:file_path>", methods=["GET"])
def download_file(file_path: str):
    if not ALLOWED_PATHS.match(file_path):
        return "Not Found", 404

    directory = DATA_DIR
    logger.info("Serving: %s", file_path)
    try:
        # send_from_directory (-> Werkzeug's send_file) already handles the
        # full conditional-GET / Range contract by default (conditional=True
        # since Flask 2.0): ETag + Last-Modified, If-None-Match/
        # If-Modified-Since -> 304, Range/If-Range -> 206 with Content-Range,
        # and an unsatisfiable Range -> 416. Verified in
        # tests/test_cloud_run_main.py; nothing bespoke needed here.
        return send_from_directory(
            directory,
            file_path,
            mimetype="application/octet-stream",
            conditional=True,
        )
    except NotFound:
        logger.warning("File not found: %s", file_path)
        return "Not Found", 404


@app.errorhandler(Exception)
def handle_error(e):
    if isinstance(e, HTTPException):
        return e
    logger.error("Unexpected error: %s", e, exc_info=True)
    return "Internal error", 500


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
        # 時価総額日本一(7203: トヨタ)の営業日カレンダーを利用して遡及日を高速抽出する
        # 内部で MIN() を取ると DuckDB オプティマイザがサブクエリを展開して
        # フルテーブルスキャンにフォールバックするため、結果を必ずリストで受け取る
        boundary_sql = f"""
            SELECT "Date"
            FROM stocks_daily
            WHERE "Code" = '7203'
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
app.add_url_rule(
    "/graphql",
    view_func=GraphQLView.as_view("graphql_view", schema=gql_schema),
)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

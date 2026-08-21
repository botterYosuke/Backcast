# 歩み値リプレイ — 自宅 Docker 運用向けイメージ
# 依存管理は uv 専用（pip 禁止方針）。BACKCAST_JQUANTS_DUCKDB_ROOT は
# 実行時にボリュームマウントしたディレクトリを指すよう docker run / compose 側で設定する。

FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# 依存関係だけ先にコピーしてレイヤーキャッシュを効かせる
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev

# アプリ本体
COPY run.py ./
COPY src ./src

RUN groupadd --system app && useradd --system --gid app --home-dir /app app \
    && chown -R app:app /app
USER app

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD uv run python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/status', timeout=3)" || exit 1

# BACKCAST_JQUANTS_DUCKDB_ROOT はここでは設定しない。データを置いたディレクトリを
# コンテナにマウントし、docker run -e / docker-compose.yml 側で渡すこと。
CMD ["uv", "run", "python", "run.py", "--host", "0.0.0.0", "--port", "8765", "--no-browser"]

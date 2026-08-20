"""FastAPI application serving the tick replay UI and its data endpoints."""

from __future__ import annotations

from datetime import date as Date
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import DataRootError, resolve_trades_dir
from .repository import SymbolNotFoundError, TickRepository

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="歩み値リプレイ", version="0.1.0")
# Sessions are columnar JSON of up to ~100k executions; gzip cuts the transfer
# to roughly a quarter without any client-side work.
app.add_middleware(GZipMiddleware, minimum_size=2048)


@lru_cache(maxsize=1)
def get_repository() -> TickRepository:
    return TickRepository(resolve_trades_dir())


def _repository() -> TickRepository:
    try:
        return get_repository()
    except DataRootError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


def _parse_date(value: str) -> Date:
    try:
        return Date.fromisoformat(value)
    except ValueError as error:
        raise HTTPException(
            status_code=400, detail=f"invalid date: {value!r} (expected YYYY-MM-DD)"
        ) from error


@app.get("/api/status")
def read_status() -> dict[str, object]:
    """Report whether the data root resolved, for a readable startup error."""
    try:
        repository = get_repository()
    except DataRootError as error:
        return {"ok": False, "error": str(error)}
    return {
        "ok": True,
        "tradesDir": str(repository.trades_dir),
        "symbolCount": len(repository.list_symbol_stems()),
    }


@app.get("/api/symbols")
def list_symbols(
    q: str = Query(default="", description="銘柄コードの前方一致フィルタ"),
    limit: int = Query(default=300, ge=1, le=5000),
) -> dict[str, object]:
    repository = _repository()
    stems = repository.list_symbol_stems()
    needle = q.strip().upper()
    if needle:
        stems = [stem for stem in stems if stem.startswith(needle)]
    return {"total": len(stems), "symbols": stems[:limit]}


@app.get("/api/symbols/{stem}")
def read_symbol(stem: str) -> dict[str, object]:
    repository = _repository()
    try:
        return repository.symbol_info(stem.upper()).as_dict()
    except SymbolNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.get("/api/session")
def read_session(
    stem: str = Query(..., description="銘柄ファイル名 (例: 7203)"),
    date: str = Query(..., description="対象日 YYYY-MM-DD"),
    direction: int = Query(
        default=0,
        ge=-1,
        le=1,
        description="0=その日のみ / -1=過去方向に最も近い営業日 / 1=未来方向",
    ),
) -> dict[str, object]:
    repository = _repository()
    symbol = stem.upper()
    day = _parse_date(date)
    try:
        resolved = repository.find_session(symbol, day, direction)
        if resolved is None:
            raise HTTPException(
                status_code=404,
                detail=f"{symbol}: {date} 付近に約定データが見つかりません",
            )
        return repository.load_session(symbol, resolved).as_dict()
    except SymbolNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.get("/")
def read_index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

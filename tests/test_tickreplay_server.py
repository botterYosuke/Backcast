"""Tests for the HTTP API."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tickreplay import server
from tickreplay.config import ENV_VAR

ROWS = [
    ("2026-08-14 09:00:00.100000", 100.0, 100, "1", "13010"),
    ("2026-08-17 09:00:00.000000", 102.0, 300, "1", "13010"),
    ("2026-08-17 09:00:30.500000", 103.0, 400, "2", "13010"),
]


@pytest.fixture
def client(tmp_path, trades_dir, tick_db_factory, monkeypatch):
    tick_db_factory("1301", ROWS)
    monkeypatch.setenv(ENV_VAR, str(trades_dir.parent))
    server.get_repository.cache_clear()
    with TestClient(server.app) as test_client:
        yield test_client
    server.get_repository().close()
    server.get_repository.cache_clear()


def test_status_reports_the_resolved_directory(client):
    body = client.get("/api/status").json()

    assert body["ok"] is True
    assert body["symbolCount"] == 1
    assert body["tradesDir"].endswith("stocks_trades")


def test_status_reports_a_missing_configuration_instead_of_crashing(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(ENV_VAR, str(tmp_path / "nowhere"))
    monkeypatch.setattr("tickreplay.config.REPO_ROOT", tmp_path)
    server.get_repository.cache_clear()
    try:
        with TestClient(server.app) as test_client:
            body = test_client.get("/api/status").json()
        assert body["ok"] is False
        assert ENV_VAR in body["error"]
    finally:
        server.get_repository.cache_clear()


def test_symbols_can_be_filtered_by_prefix(client):
    assert client.get("/api/symbols").json() == {"total": 1, "symbols": ["1301"]}
    assert client.get("/api/symbols", params={"q": "99"}).json()["symbols"] == []


def test_symbol_detail_exposes_the_canonical_code_and_range(client):
    body = client.get("/api/symbols/1301").json()

    assert body["code"] == "13010"
    assert body["firstDate"] == "2026-08-14"
    assert body["lastDate"] == "2026-08-17"


def test_unknown_symbol_is_a_404(client):
    assert client.get("/api/symbols/9999").status_code == 404


def test_session_returns_columnar_ticks(client):
    body = client.get(
        "/api/session", params={"stem": "1301", "date": "2026-08-17"}
    ).json()

    assert body["count"] == 2
    assert body["price"] == [102.0, 103.0]
    assert body["qty"] == [300, 400]
    assert len(body["us"]) == len(body["type"]) == 2


def test_session_can_snap_backwards_to_the_nearest_trading_day(client):
    body = client.get(
        "/api/session",
        params={"stem": "1301", "date": "2026-08-16", "direction": -1},
    ).json()

    assert body["date"] == "2026-08-14"


def test_session_without_snapping_is_a_404_on_an_empty_day(client):
    response = client.get(
        "/api/session", params={"stem": "1301", "date": "2026-08-15", "direction": 0}
    )

    assert response.status_code == 404


def test_a_malformed_date_is_rejected(client):
    response = client.get("/api/session", params={"stem": "1301", "date": "17/08/2026"})

    assert response.status_code == 400
    assert "invalid date" in response.json()["detail"]


def test_index_and_static_assets_are_served(client):
    index = client.get("/")
    assert index.status_code == 200
    assert "歩み値リプレイ" in index.text

    library = client.get("/static/vendor/lightweight-charts.standalone.production.js")
    assert library.status_code == 200
    assert "Lightweight Charts" in library.text[:400]

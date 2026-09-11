from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import ThreadingHTTPServer

import httpx
import pytest

from .spike_server import CSP, IMMUTABLE_CACHE, SpikeRequestHandler


@pytest.fixture
def spike_client() -> Iterator[httpx.Client]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), SpikeRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}") as client:
            yield client
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_spike_route_enforces_the_frozen_non_isolated_policy(
    spike_client: httpx.Client,
) -> None:
    response = spike_client.get("/spike/")

    assert response.status_code == 200
    assert response.headers["Content-Security-Policy"] == CSP
    assert response.headers["Cache-Control"] == "no-store"
    assert "Cross-Origin-Opener-Policy" not in response.headers
    assert "Cross-Origin-Embedder-Policy" not in response.headers


def test_isolated_spike_route_adds_coop_and_coep(spike_client: httpx.Client) -> None:
    response = spike_client.get("/spike-isolated/")

    assert response.status_code == 200
    assert response.headers["Cross-Origin-Opener-Policy"] == "same-origin"
    assert response.headers["Cross-Origin-Embedder-Policy"] == "require-corp"


@pytest.mark.parametrize(
    ("path", "content_type"),
    [
        ("/spike/spike.mjs", "text/javascript; charset=utf-8"),
        (
            "/static/vendor/pyodide/0.27.6/1404bcc925dad5900b1205e9e7a127d85a654137554c6e76492eca5a2b80cff0/pyodide.asm.wasm",
            "application/wasm",
        ),
        (
            "/static/vendor/pyodide/0.27.6/1404bcc925dad5900b1205e9e7a127d85a654137554c6e76492eca5a2b80cff0/python_stdlib.zip",
            "application/zip",
        ),
    ],
)
def test_spike_server_uses_explicit_browser_asset_mime_types(
    spike_client: httpx.Client,
    path: str,
    content_type: str,
) -> None:
    response = spike_client.head(path)

    assert response.status_code == 200
    assert response.headers["Content-Type"] == content_type


def test_immutable_vendor_assets_and_spike_entries_have_separate_cache_classes(
    spike_client: httpx.Client,
) -> None:
    vendor = spike_client.head(
        "/static/vendor/codemirror/86539626cbc6254b2a37368d3a957a91a2c5f15de951779567207f76d6878d56/strategy-editor.mjs"
    )
    entry = spike_client.head("/spike/spike.mjs")

    assert vendor.headers["Cache-Control"] == IMMUTABLE_CACHE
    assert entry.headers["Cache-Control"] == "no-store"


def test_spike_server_rejects_paths_outside_allowlisted_roots(
    spike_client: httpx.Client,
) -> None:
    response = spike_client.get("/static/%2e%2e/%2e%2e/package.json")

    assert response.status_code == 404

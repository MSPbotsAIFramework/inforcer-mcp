# Copyright 2026 MSPbots.ai
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Credential middleware: 401 / 400 on bad headers, region normalisation, isolation."""

import asyncio

import pytest
from starlette.testclient import TestClient

from inforcer_mcp.__main__ import build_http_app
from inforcer_mcp.server import GatewayCredentialsMiddleware, _credentials_var, create_mcp_server, get_client_from_context, normalize_region

MCP_HEADERS = {"Accept": "application/json, text/event-stream"}
LIST_TOOLS = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
CREDS = {"X-Inforcer-Region": "us", "X-Inforcer-Api-Key": "key"}


def _client() -> TestClient:
    return TestClient(build_http_app(create_mcp_server()))


def test_health_is_local():
    with _client() as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.parametrize(
    "headers",
    [{}, {"X-Inforcer-Region": "us"}, {"X-Inforcer-Api-Key": "key"}, CREDS | {"X-Inforcer-Api-Key": "${INFORCER_KEY}"}],
)
def test_missing_headers_return_401(headers):
    with _client() as client:
        resp = client.post("/mcp", json=LIST_TOOLS, headers=MCP_HEADERS | headers)
    assert resp.status_code == 401
    assert resp.json()["required_headers"] == list(CREDS)


def test_unknown_region_returns_400():
    with _client() as client:
        resp = client.post("/mcp", json=LIST_TOOLS, headers=MCP_HEADERS | CREDS | {"X-Inforcer-Region": "apac"})
    assert resp.status_code == 400
    assert "anz, eu, uk, us" in resp.json()["message"]


@pytest.mark.parametrize(
    "value,expected",
    [
        ("us", "us"),
        ("EU", "eu"),
        (" anz ", "anz"),
        ("api-uk.inforcer.com", "uk"),
        ("https://api-us.inforcer.com/api", "us"),
        ("apac", None),
        ("api-xx.inforcer.com", None),
    ],
)
def test_normalize_region(value, expected):
    assert normalize_region(value) == expected


def test_tools_list_over_http():
    with _client() as client:
        resp = client.post("/mcp", json=LIST_TOOLS, headers=MCP_HEADERS | CREDS)
    assert resp.status_code == 200
    for name in ("inforcer_tenants_list", "inforcer_reports_run", "inforcer_alignment_details"):
        assert name in resp.text


async def _receive():
    return {"type": "http.request", "body": b"", "more_body": False}


async def _send(_message):
    return None


def _run_middleware(headers: list[tuple[bytes, bytes]]) -> dict:
    seen: dict = {}

    async def app(scope, receive, send):
        seen["credentials"] = _credentials_var.get()
        seen["client"] = get_client_from_context()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    asyncio.run(GatewayCredentialsMiddleware(app)({"type": "http", "path": "/mcp", "headers": headers}, _receive, _send))
    return seen


def test_credentials_reach_request_context_and_are_reset():
    seen = _run_middleware([(b"x-inforcer-region", b"EU"), (b"x-inforcer-api-key", b" key-123 ")])
    client = seen["client"]
    assert client.base_url == "https://api-eu.inforcer.com/api"
    assert client._headers()["Inf-Api-Key"] == "key-123"
    assert _credentials_var.get() is None


def test_concurrent_requests_do_not_share_credentials():
    observed: list[tuple[str, str]] = []

    async def app(scope, receive, send):
        before = _credentials_var.get().api_key
        await asyncio.sleep(0.01)
        observed.append((before, _credentials_var.get().api_key))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = GatewayCredentialsMiddleware(app)

    def scope(key: bytes) -> dict:
        return {"type": "http", "path": "/mcp", "headers": [(b"x-inforcer-region", b"us"), (b"x-inforcer-api-key", key)]}

    async def both():
        await asyncio.gather(middleware(scope(b"tenant-a"), _receive, _send), middleware(scope(b"tenant-b"), _receive, _send))

    asyncio.run(both())
    assert sorted(observed) == [("tenant-a", "tenant-a"), ("tenant-b", "tenant-b")]


def test_credentials_repr_hides_key():
    seen = _run_middleware([(b"x-inforcer-region", b"us"), (b"x-inforcer-api-key", b"very-secret-key")])
    assert "very-secret-key" not in repr(seen["credentials"])

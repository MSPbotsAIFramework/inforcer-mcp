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
"""Async client for the Inforcer public API (beta routes)."""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from ._json import error_envelope
from .config import REGION_BASE_URLS

logger = logging.getLogger("inforcer_mcp.api")

_TIMEOUT = httpx.Timeout(connect=5.0, read=60.0, write=10.0, pool=5.0)
_MAX_RETRIES = 2
_MAX_BACKOFF_SECONDS = 20.0
# Whole-call budget including retries; the gateway times a tool call out at 120s.
_CALL_BUDGET_SECONDS = 100.0

# One connection pool for the process. It never holds credentials: the
# Inf-Api-Key header is set per request from the caller's own key, so
# sharing the pool across tenants is safe.
_shared_http_client: httpx.AsyncClient | None = None


def _get_shared_http_client() -> httpx.AsyncClient:
    global _shared_http_client
    if _shared_http_client is None:
        _shared_http_client = httpx.AsyncClient(timeout=_TIMEOUT)
    return _shared_http_client


# status_code -> (error code, retryable). 0 means no HTTP response at all
# (connection error or timeout).
_STATUS_TO_CODE: dict[int, tuple[str, bool]] = {
    0: ("upstream_error", True),
    400: ("invalid_argument", False),
    401: ("unauthorized", False),
    403: ("unauthorized", False),
    404: ("not_found", False),
    409: ("invalid_argument", False),
    422: ("invalid_argument", False),
    429: ("rate_limited", True),
}


def classify_status(status_code: int) -> tuple[str, bool]:
    if status_code in _STATUS_TO_CODE:
        return _STATUS_TO_CODE[status_code]
    if status_code >= 500:
        return "upstream_error", True
    if status_code >= 400:
        return "invalid_argument", False
    return "upstream_error", True


class InforcerError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"Inforcer API error {status_code}: {message}")

    def to_envelope(self) -> str:
        code, retryable = classify_status(self.status_code)
        return error_envelope(code, self.message, retryable)


@dataclass(frozen=True)
class InforcerCredentials:
    region: str
    api_key: str

    def __repr__(self) -> str:  # never leak the key into logs or tracebacks
        return f"InforcerCredentials(region={self.region!r}, api_key=<redacted>)"


@dataclass(frozen=True)
class Download:
    content: bytes
    content_type: str
    file_name: str | None


class InforcerClient:
    """Thin wrapper over the Inforcer API for one caller's key."""

    def __init__(self, credentials: InforcerCredentials, http_client: httpx.AsyncClient | None = None):
        self._credentials = credentials
        self._http = http_client
        self.region = credentials.region
        self.base_url = REGION_BASE_URLS[credentials.region]

    def _headers(self, accept: str = "application/json") -> dict[str, str]:
        return {"Inf-Api-Key": self._credentials.api_key, "Accept": accept}

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return _parse(await self._send("GET", path, params=params), path)

    async def post(self, path: str, body: Any = None) -> Any:
        return _parse(await self._send("POST", path, body=body), path)

    async def download(self, path: str) -> Download:
        resp = await self._send("GET", path, accept="*/*")
        disposition = resp.headers.get("content-disposition", "")
        file_name = None
        if "filename=" in disposition:
            file_name = disposition.split("filename=", 1)[1].split(";", 1)[0].strip().strip('"') or None
        return Download(resp.content, resp.headers.get("content-type", "application/octet-stream"), file_name)

    async def _send(
        self, method: str, path: str, params: dict[str, Any] | None = None, body: Any = None, accept: str = "application/json"
    ) -> httpx.Response:
        http = self._http or _get_shared_http_client()
        url = f"{self.base_url}/{path.lstrip('/')}"
        query = {k: _param_value(v) for k, v in (params or {}).items() if v is not None}
        headers = self._headers(accept)
        started = time.monotonic()
        attempt = 0
        while True:
            t0 = time.monotonic()
            resp: httpx.Response | None = None
            error: InforcerError | None = None
            try:
                kwargs: dict[str, Any] = {"params": query, "headers": headers}
                if body is not None:
                    kwargs["json"] = body
                resp = await http.request(method, url, **kwargs)
                status = resp.status_code
            except httpx.TimeoutException:
                status, error = 0, InforcerError(0, f"Timed out calling Inforcer ({method} {path})")
            except httpx.RequestError as e:
                status, error = 0, InforcerError(0, f"Network error calling Inforcer ({method} {path}): {type(e).__name__}")

            logger.info(
                "inforcer_api method=%s path=%s status=%s ms=%d attempt=%d",
                method, path, status, (time.monotonic() - t0) * 1000, attempt,
            )

            # 429 is refused before processing, so any method may retry it; other
            # failures are retried only for reads, so a write never runs twice.
            retryable = status == 429 or (method == "GET" and (status == 0 or status >= 500))
            if retryable and attempt < _MAX_RETRIES:
                delay = _retry_delay(resp, attempt)
                if time.monotonic() - started + delay < _CALL_BUDGET_SECONDS:
                    attempt += 1
                    await asyncio.sleep(delay)
                    continue
            if error is not None:
                raise error
            assert resp is not None
            if resp.status_code >= 400:
                raise InforcerError(resp.status_code, _error_message(resp, path))
            return resp


def _parse(resp: httpx.Response, path: str) -> Any:
    """Return the response JSON; an envelope with success: false is an error even on HTTP 200."""
    if not resp.content or not resp.content.strip():
        return None
    try:
        payload = resp.json()
    except ValueError:
        return {"raw_response": resp.text[:2000]}
    if isinstance(payload, dict) and payload.get("success") is False:
        raise InforcerError(400, _envelope_message(payload, f"Inforcer rejected the request for {path}"))
    return payload


def _param_value(value: Any) -> Any:
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def _retry_delay(resp: httpx.Response | None, attempt: int) -> float:
    if resp is not None:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return max(0.0, min(float(retry_after), _MAX_BACKOFF_SECONDS))
            except ValueError:
                pass
    return min(float(2**attempt), _MAX_BACKOFF_SECONDS)


def _envelope_message(payload: dict, fallback: str) -> str:
    message = payload.get("message")
    errors = payload.get("errors")
    detail = "; ".join(map(str, errors)) if isinstance(errors, list) and errors else None
    text = ": ".join(str(x) for x in (message, detail) if x)
    return f"{fallback}: {text[:500]}" if text else fallback


def _error_message(resp: httpx.Response, path: str) -> str:
    fallback = f"Inforcer returned HTTP {resp.status_code} for {path}"
    try:
        payload = resp.json()
    except ValueError:
        text = resp.text.strip()
        if text.lower().startswith("<"):
            return fallback  # an HTML error page, not useful to the agent
        return f"{fallback}: {text[:300]}" if text else fallback
    if isinstance(payload, dict):
        return _envelope_message(payload, fallback)
    return fallback

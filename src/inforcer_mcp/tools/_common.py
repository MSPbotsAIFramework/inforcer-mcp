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
"""Shared argument types, envelope / tenant helpers and the error-handling wrapper."""

import base64
import json
import logging
import re
import time
import zlib
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Annotated, Any

from mcp.types import ToolAnnotations
from pydantic import Field

from .._json import dump_json_capped, error_envelope
from ..api_client import InforcerClient, InforcerError

logger = logging.getLogger("inforcer_mcp.tools")

ClientFactory = Callable[[], InforcerClient | None]

READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True)
LOCAL = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
WRITES = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)

NOT_CONFIGURED = error_envelope(
    "not_configured", "Inforcer credentials are missing for this request (X-Inforcer-Region, X-Inforcer-Api-Key).", False
)

TENANT_DESCRIPTION = "Tenant: numeric Client Tenant ID, Microsoft tenant GUID, tenant DNS name or friendly name."
TenantArg = Annotated[str, Field(description=TENANT_DESCRIPTION, min_length=1)]
# A list too: Inforcer's own token is JSON text, which MCP clients may pass back already parsed.
ContinuationToken = Annotated[
    str | list[Any] | dict[str, Any] | None,
    Field(description="continuation_token from the previous result, passed back unchanged."),
]
Offset = Annotated[
    int | None,
    Field(description="Record position to start from (0-based); pass next_offset from the previous result.", ge=0),
]
Limit = Annotated[int | None, Field(description="Records to return (default: all from offset, max 500).", ge=1, le=500)]

_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


def compact(values: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in values.items() if v is not None}


def data(payload: Any) -> Any:
    """The `data` member of Inforcer's {success, message, errors, data} envelope."""
    if isinstance(payload, dict) and "data" in payload and ("success" in payload or "errors" in payload):
        return payload["data"]
    return payload


def as_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [value]


async def list_tenants(client: InforcerClient) -> list[dict]:
    return [t for t in as_list(data(await client.get("beta/tenants"))) if isinstance(t, dict)]


def match_tenant(tenants: list[dict], value: str) -> dict:
    """Find one tenant by Client Tenant ID, Microsoft tenant GUID, DNS name or friendly name."""
    needle = value.strip()
    if re.fullmatch(r"\d+", needle):
        found = [t for t in tenants if str(t.get("clientTenantId")) == needle]
    elif _GUID.match(needle):
        found = [t for t in tenants if str(t.get("msTenantId") or "").lower() == needle.lower()]
    else:
        lower = needle.lower()
        found = [
            t for t in tenants if str(t.get("tenantFriendlyName") or "").lower() == lower or str(t.get("tenantDnsName") or "").lower() == lower
        ]
    if len(found) == 1:
        return found[0]
    if not found:
        raise InforcerError(404, f"No Inforcer tenant matches {value!r}; list them with inforcer_tenants_list.")
    options = ", ".join(f"{t.get('clientTenantId')} ({t.get('tenantFriendlyName')})" for t in found[:10])
    raise ValueError(f"{value!r} matches several tenants: {options}. Pass the numeric Client Tenant ID.")


async def resolve_tenant_id(client: InforcerClient, value: str) -> int:
    """A numeric value is used as is (no lookup); anything else is resolved through the tenant list."""
    needle = value.strip()
    if re.fullmatch(r"\d+", needle):
        return int(needle)
    return int(match_tenant(await list_tenants(client), needle)["clientTenantId"])


_CURSOR = "c1."


def encode_cursor(token: str | None, skip: int) -> str:
    """An opaque continuation_token: Inforcer's token for a page plus the records to skip on it."""
    raw = json.dumps({"t": token, "s": skip}, separators=(",", ":")).encode("utf-8")
    return _CURSOR + base64.urlsafe_b64encode(zlib.compress(raw, 9)).decode("ascii").rstrip("=")


def decode_cursor(value: Any) -> tuple[str | None, int]:
    """(Inforcer token, records to skip on that page) from a continuation_token.

    A bare Inforcer token is still accepted, as text or as the JSON a client parsed it into.
    """
    if value is None:
        return None, 0
    if isinstance(value, (list, dict)):
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False), 0
    text = str(value).strip()
    if not text:
        return None, 0
    if not text.startswith(_CURSOR):
        return text, 0
    try:
        body = text[len(_CURSOR) :]
        raw = json.loads(zlib.decompress(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))))
        token, skip = raw.get("t"), raw.get("s")
        if (token is None or isinstance(token, str)) and isinstance(skip, int) and skip >= 0:
            return token, skip
    except (ValueError, zlib.error, AttributeError):
        pass
    raise ValueError("continuation_token is not one this tool returned; pass it back unchanged, or start without it.")


def cursor_page(key: str, items: list, next_token: Any, token: str | None, skip: int, total: Any = None) -> dict:
    """Records from ``skip`` of the page read with ``token``, plus paging.

    continuation_token continues with Inforcer's next page; when the result is cut to fit the size
    limit, it points at this same page right after the last record returned.
    """
    shown = items[skip:]
    return compact(
        {
            key: shown,
            "count": len(shown),
            "total": total,
            "has_more": bool(next_token),
            "continuation_token": encode_cursor(next_token, 0) if next_token else None,
            "_resume": {"key": "continuation_token", "start": skip, "encode": lambda n: encode_cursor(token, n)},
        }
    )


def page(payload: Any, key: str, token: str | None = None, skip: int = 0) -> dict:
    """A list page with Inforcer's continuationToken / totalCount siblings of `data`."""
    items = as_list(data(payload))
    next_token = payload.get("continuationToken") if isinstance(payload, dict) else None
    total = payload.get("totalCount") if isinstance(payload, dict) else None
    return cursor_page(key, items, next_token, token, skip, total)


def local_page(key: str, items: list, offset: int | None, limit: int | None) -> dict:
    """A list Inforcer returns whole, sliced locally; next_offset continues right after the last record."""
    start = offset or 0
    end = len(items) if limit is None else min(start + limit, len(items))
    shown = items[start:end]
    more = end < len(items)
    return compact(
        {
            key: shown,
            "count": len(shown),
            "total": len(items),
            "offset": start,
            "has_more": more,
            "next_offset": start + len(shown) if more else None,
            "_resume": {"key": "next_offset", "start": start},
        }
    )


_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def iso_time(value: str | None, what: str, end_of_day: bool = False) -> str | None:
    """An ISO 8601 date-time for Inforcer; a plain date covers that whole day (UTC)."""
    if value is None or not str(value).strip():
        return None
    text = str(value).strip()
    example = f"use e.g. 2026-10-14 or 2026-10-14T08:30:00Z (got {value!r})"
    if _DAY.match(text):
        try:
            datetime.strptime(text, "%Y-%m-%d")
        except ValueError:
            raise ValueError(f"{what} is not a valid date; {example}.") from None
        return f"{text}T23:59:59.999Z" if end_of_day else f"{text}T00:00:00Z"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{what} must be an ISO 8601 date or date-time; {example}.") from None
    if "T" not in text.upper() and " " not in text:
        raise ValueError(f"{what} must be an ISO 8601 date or date-time; {example}.")
    return text if parsed.tzinfo else text + "Z"


async def run_tool(
    name: str,
    client_factory: ClientFactory,
    action: Callable[[InforcerClient], Awaitable[Any]],
) -> str:
    """Resolve the caller's client, run ``action``, and always return a string.

    Errors never escape as exceptions: they become the standard error envelope.
    """
    client = client_factory()
    if client is None:
        return NOT_CONFIGURED
    if client.header_error:
        return error_envelope("invalid_argument", client.header_error, False)
    started = time.monotonic()
    try:
        result = await action(client)
    except InforcerError as e:
        logger.info("tool=%s outcome=error status=%s ms=%d", name, e.status_code, _ms(started))
        return e.to_envelope()
    except ValueError as e:
        logger.info("tool=%s outcome=invalid_argument ms=%d", name, _ms(started))
        return error_envelope("invalid_argument", str(e), False)
    except Exception as e:  # noqa: BLE001 - tools must never raise
        logger.warning("tool=%s outcome=unexpected error=%s ms=%d", name, type(e).__name__, _ms(started))
        return error_envelope("upstream_error", f"Unexpected error: {type(e).__name__}", True)
    logger.info("tool=%s outcome=ok ms=%d", name, _ms(started))
    if result is None:
        result = {"done": True}
    return result if isinstance(result, str) else dump_json_capped(result)


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)

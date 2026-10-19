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

import logging
import re
import time
from collections.abc import Awaitable, Callable
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
ContinuationToken = Annotated[str | None, Field(description="continuation_token from the previous page.")]

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


def page(payload: Any, key: str) -> dict:
    """A list page with Inforcer's continuationToken / totalCount siblings of `data`."""
    items = as_list(data(payload))
    token = payload.get("continuationToken") if isinstance(payload, dict) else None
    total = payload.get("totalCount") if isinstance(payload, dict) else None
    return compact({key: items, "count": len(items), "total": total, "has_more": bool(token), "continuation_token": token or None})


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

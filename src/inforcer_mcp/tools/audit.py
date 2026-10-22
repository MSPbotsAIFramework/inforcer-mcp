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
"""Audit: Inforcer's activity log (who changed what, deployments, backups ...)."""

from datetime import datetime
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..api_client import InforcerClient
from ._common import (
    READ_ONLY,
    ClientFactory,
    ContinuationToken,
    as_list,
    compact,
    cursor_page,
    data,
    decode_cursor,
    iso_time,
    run_tool,
)


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value)


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def inforcer_audit_event_types() -> str:
        """List the audit event type names that inforcer_audit_search can filter on."""

        async def action(client: InforcerClient) -> dict:
            rows = as_list(data(await client.get("beta/auditEvents/eventTypes")))
            names = [r.get("name") if isinstance(r, dict) else r for r in rows]
            return {"event_types": names, "count": len(names)}

        return await run_tool("inforcer_audit_event_types", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def inforcer_audit_search(
        event_types: Annotated[list[str] | None, Field(description="Only these event types (see inforcer_audit_event_types).")] = None,
        date_from: Annotated[
            str | None,
            Field(description="Start of the time range: YYYY-MM-DD (from 00:00 UTC) or an ISO 8601 date-time, e.g. 2026-10-14T08:30:00Z."),
        ] = None,
        date_to: Annotated[
            str | None,
            Field(description="End of the time range: YYYY-MM-DD (through 23:59:59 UTC) or an ISO 8601 date-time."),
        ] = None,
        user: Annotated[str | None, Field(description="Only events by this user.")] = None,
        page_size: Annotated[int | None, Field(description="Events per page (max 100, default 100).", ge=1, le=100)] = None,
        continuation_token: ContinuationToken = None,
    ) -> str:
        """Search the Inforcer activity log by event type, time range and user; newest pages via the token."""

        async def action(client: InforcerClient) -> dict:
            start, end = iso_time(date_from, "date_from"), iso_time(date_to, "date_to", end_of_day=True)
            if start and end and _instant(start) > _instant(end):
                raise ValueError(f"date_from ({start}) is after date_to ({end}).")
            token, skip = decode_cursor(continuation_token)
            body = compact(
                {
                    "eventTypes": event_types or None,
                    "dateFrom": start,
                    "dateTo": end,
                    "user": user,
                    "pageSize": page_size,
                    "continuationToken": token,
                }
            )
            result = data(await client.post("beta/auditEvents/search", body))
            result = result if isinstance(result, dict) else {"items": as_list(result)}
            return cursor_page("events", as_list(result.get("items")), result.get("continuationToken"), token, skip)

        return await run_tool("inforcer_audit_search", client_factory, action)

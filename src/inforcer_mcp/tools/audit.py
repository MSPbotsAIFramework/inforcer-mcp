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

from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..api_client import InforcerClient
from ._common import READ_ONLY, ClientFactory, ContinuationToken, as_list, compact, data, run_tool


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
        date_from: Annotated[str | None, Field(description="ISO 8601 start of the time range.")] = None,
        date_to: Annotated[str | None, Field(description="ISO 8601 end of the time range.")] = None,
        user: Annotated[str | None, Field(description="Only events by this user.")] = None,
        page_size: Annotated[int | None, Field(description="Events per page (max 100, default 100).", ge=1, le=100)] = None,
        continuation_token: ContinuationToken = None,
    ) -> str:
        """Search the Inforcer activity log by event type, time range and user; newest pages via the token."""

        async def action(client: InforcerClient) -> dict:
            body = compact(
                {
                    "eventTypes": event_types or None,
                    "dateFrom": date_from,
                    "dateTo": date_to,
                    "user": user,
                    "pageSize": page_size,
                    "continuationToken": continuation_token,
                }
            )
            result = data(await client.post("beta/auditEvents/search", body))
            result = result if isinstance(result, dict) else {"items": as_list(result)}
            items = as_list(result.get("items"))
            token = result.get("continuationToken")
            return compact({"events": items, "count": len(items), "has_more": bool(token), "continuation_token": token or None})

        return await run_tool("inforcer_audit_search", client_factory, action)

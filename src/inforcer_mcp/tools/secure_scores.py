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
"""Secure Score: Microsoft's security posture score for a tenant."""

from typing import Any

from mcp.server.fastmcp import FastMCP

from ..api_client import InforcerClient
from ._common import READ_ONLY, ClientFactory, TenantArg, data, resolve_tenant_id, run_tool


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def inforcer_secure_scores_get(tenant: TenantArg) -> str:
        """Get a tenant's Microsoft Secure Score: current and max score, history and per-control scores."""

        async def action(client: InforcerClient) -> Any:
            tenant_id = await resolve_tenant_id(client, tenant)
            return data(await client.get(f"beta/tenants/{tenant_id}/secureScores"))

        return await run_tool("inforcer_secure_scores_get", client_factory, action)

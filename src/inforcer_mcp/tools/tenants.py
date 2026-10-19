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
"""Tenants: the Microsoft 365 tenants Inforcer manages."""

from typing import Any

from mcp.server.fastmcp import FastMCP

from ..api_client import InforcerClient
from ._common import READ_ONLY, ClientFactory, TenantArg, data, list_tenants, match_tenant, resolve_tenant_id, run_tool

# Large per-tenant fields left out of the list (inforcer_tenants_get returns them).
_LIST_SKIP = ("policyDiff", "licenses")


def summary(tenant: dict) -> dict:
    return {k: v for k, v in tenant.items() if k not in _LIST_SKIP}


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def inforcer_tenants_list() -> str:
        """List managed tenants: Client Tenant ID, name, DNS name, Microsoft tenant id, Secure Score, tags."""

        async def action(client: InforcerClient) -> dict:
            tenants = await list_tenants(client)
            return {"tenants": [summary(t) for t in tenants], "count": len(tenants)}

        return await run_tool("inforcer_tenants_list", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def inforcer_tenants_get(tenant: TenantArg) -> str:
        """Get one tenant with licenses, recent policy changes, policy diff text and alignment summaries."""

        async def action(client: InforcerClient) -> Any:
            tenant_id = await resolve_tenant_id(client, tenant)
            return data(await client.get(f"beta/tenants/{tenant_id}"))

        return await run_tool("inforcer_tenants_get", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def inforcer_tenants_resolve(tenant: TenantArg) -> str:
        """Turn a tenant name, DNS name, Microsoft tenant GUID or id into its numeric Client Tenant ID."""

        async def action(client: InforcerClient) -> dict:
            found = match_tenant(await list_tenants(client), tenant)
            return {
                "input": tenant,
                "clientTenantId": found.get("clientTenantId"),
                "tenantFriendlyName": found.get("tenantFriendlyName"),
                "tenantDnsName": found.get("tenantDnsName"),
                "msTenantId": found.get("msTenantId"),
            }

        return await run_tool("inforcer_tenants_resolve", client_factory, action)

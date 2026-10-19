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
"""Identity: a tenant's Entra users, groups and directory roles."""

from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..api_client import InforcerClient
from ._common import READ_ONLY, ClientFactory, ContinuationToken, TenantArg, as_list, compact, data, page, resolve_tenant_id, run_tool

Search = Annotated[str | None, Field(description="Text to search for (name, email / UPN).")]


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def inforcer_users_list(tenant: TenantArg, search: Search = None, continuation_token: ContinuationToken = None) -> str:
        """List a tenant's Entra users: UPN, name, enabled, global admin, MFA registered, licenses, roles."""

        async def action(client: InforcerClient) -> dict:
            tenant_id = await resolve_tenant_id(client, tenant)
            payload = await client.get(f"beta/tenants/{tenant_id}/users", compact({"search": search, "continuationToken": continuation_token}))
            return {"tenant_id": tenant_id} | page(payload, "users")

        return await run_tool("inforcer_users_list", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def inforcer_users_get(
        tenant: TenantArg,
        user_id: Annotated[str, Field(description="Entra user object id.", min_length=1)],
    ) -> str:
        """Get one Entra user of a tenant with profile, groups, roles and licenses."""

        async def action(client: InforcerClient) -> Any:
            tenant_id = await resolve_tenant_id(client, tenant)
            return data(await client.get(f"beta/tenants/{tenant_id}/users/{user_id}"))

        return await run_tool("inforcer_users_get", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def inforcer_groups_list(tenant: TenantArg, search: Search = None, continuation_token: ContinuationToken = None) -> str:
        """List a tenant's Entra groups with type, mail, visibility and membership rule."""

        async def action(client: InforcerClient) -> dict:
            tenant_id = await resolve_tenant_id(client, tenant)
            payload = await client.get(f"beta/tenants/{tenant_id}/groups", compact({"search": search, "continuationToken": continuation_token}))
            return {"tenant_id": tenant_id} | page(payload, "groups")

        return await run_tool("inforcer_groups_list", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def inforcer_groups_get(
        tenant: TenantArg,
        group_id: Annotated[str, Field(description="Entra group object id.", min_length=1)],
    ) -> str:
        """Get one Entra group of a tenant."""

        async def action(client: InforcerClient) -> Any:
            tenant_id = await resolve_tenant_id(client, tenant)
            return data(await client.get(f"beta/tenants/{tenant_id}/groups/{group_id}"))

        return await run_tool("inforcer_groups_get", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def inforcer_roles_list(tenant: TenantArg) -> str:
        """List a tenant's Entra directory roles: built-in, enabled and privileged flags."""

        async def action(client: InforcerClient) -> dict:
            tenant_id = await resolve_tenant_id(client, tenant)
            roles = as_list(data(await client.get(f"beta/tenants/{tenant_id}/roles")))
            return {"tenant_id": tenant_id, "roles": roles, "count": len(roles)}

        return await run_tool("inforcer_roles_list", client_factory, action)

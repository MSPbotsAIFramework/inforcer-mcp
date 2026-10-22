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
"""Policies: a tenant's Microsoft 365 policies as backed up by Inforcer."""

from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..api_client import InforcerClient
from ._common import (
    READ_ONLY,
    ClientFactory,
    Limit,
    Offset,
    TenantArg,
    as_list,
    data,
    local_page,
    resolve_tenant_id,
    run_tool,
)

# policyData (the full policy body) is large; the list keeps what identifies and classifies a policy.
_SKIP = ("policyData",)


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def inforcer_policies_list(
        tenant: TenantArg,
        include_settings: Annotated[bool, Field(description="Also return each policy's full settings (policyData); large.")] = False,
        offset: Offset = None,
        limit: Limit = None,
    ) -> str:
        """List a tenant's policies (Intune, Entra, Exchange, Defender ...) with product, platform, category."""

        async def action(client: InforcerClient) -> dict:
            tenant_id = await resolve_tenant_id(client, tenant)
            rows = [p for p in as_list(data(await client.get(f"beta/tenants/{tenant_id}/policies"))) if isinstance(p, dict)]
            policies = rows if include_settings else [{k: v for k, v in p.items() if k not in _SKIP} for p in rows]
            return {"tenant_id": tenant_id} | local_page("policies", policies, offset, limit)

        return await run_tool("inforcer_policies_list", client_factory, action)

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
"""Assessments: rule sets Inforcer can check a tenant against."""

from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..api_client import InforcerClient
from ._common import READ_ONLY, WRITES, ClientFactory, TenantArg, as_list, data, resolve_tenant_id, run_tool


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def inforcer_assessments_list() -> str:
        """List the assessments that can be run: id, name, type (platform / custom / community) and tags."""

        async def action(client: InforcerClient) -> dict:
            rows = as_list(data(await client.get("beta/assessments")))
            return {"assessments": rows, "count": len(rows)}

        return await run_tool("inforcer_assessments_list", client_factory, action)

    @mcp.tool(annotations=WRITES)
    async def inforcer_assessments_run(
        tenant: TenantArg,
        assessment_id: Annotated[str, Field(description="Assessment id from inforcer_assessments_list.", min_length=1)],
    ) -> str:
        """Run an assessment against one tenant and return its compliance results. Only when explicitly asked.

        Reads the tenant only (changes no policies) but records a new assessment run in Inforcer.
        """

        async def action(client: InforcerClient) -> Any:
            tenant_id = await resolve_tenant_id(client, tenant)
            result = data(await client.post(f"beta/tenants/{tenant_id}/assessments/{assessment_id}/runs"))
            return {"tenant_id": tenant_id, "assessment_id": assessment_id, "result": result}

        return await run_tool("inforcer_assessments_run", client_factory, action)

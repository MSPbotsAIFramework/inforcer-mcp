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
"""Baselines: groups that align member tenants to a reference tenant's policies."""

from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..api_client import InforcerClient
from ._common import READ_ONLY, ClientFactory, as_list, compact, data, run_tool


async def list_baselines(client: InforcerClient, baseline_tenant_id: int | None = None) -> list[dict]:
    payload = await client.get("beta/baselines", compact({"baselineTenantId": baseline_tenant_id}))
    return [b for b in as_list(data(payload)) if isinstance(b, dict)]


def baseline_summary(baseline: dict) -> dict:
    """Baseline without its (long) policy item list; members kept by id and name."""
    out = {k: v for k, v in baseline.items() if k not in ("items", "members")}
    out["item_count"] = len(baseline.get("items") or [])
    out["members"] = [
        compact({"clientTenantId": m.get("clientTenantId"), "tenantFriendlyName": m.get("tenantFriendlyName")})
        for m in baseline.get("members") or []
        if isinstance(m, dict)
    ]
    return out


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def inforcer_baselines_list(
        baseline_tenant_id: Annotated[int | None, Field(description="Only baselines owned by this reference (baseline) tenant id.", ge=1)] = None,
    ) -> str:
        """List baseline groups: id, name, reference tenant, member tenants, mode and alignment thresholds."""

        async def action(client: InforcerClient) -> dict:
            baselines = await list_baselines(client, baseline_tenant_id)
            return {"baselines": [baseline_summary(b) for b in baselines], "count": len(baselines)}

        return await run_tool("inforcer_baselines_list", client_factory, action)

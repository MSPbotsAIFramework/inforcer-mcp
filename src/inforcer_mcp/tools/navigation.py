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
"""Tool discovery (kept for compatibility: every tool is always listed) and the connection check."""

from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from .._json import compact as to_json
from ..api_client import InforcerClient
from ._common import LOCAL, READ_ONLY, ClientFactory, list_tenants, run_tool

DOMAINS: dict[str, tuple[str, list[str]]] = {
    "tenants": ("Managed Microsoft 365 tenants and tenant lookup.", ["inforcer_tenants_list", "inforcer_tenants_get", "inforcer_tenants_resolve"]),
    "baselines": ("Baseline groups: reference tenant, members and thresholds.", ["inforcer_baselines_list"]),
    "alignment": ("How far tenants drift from their baselines.", ["inforcer_alignment_scores", "inforcer_alignment_details"]),
    "policies": ("A tenant's backed-up policies.", ["inforcer_policies_list"]),
    "secure-scores": ("Microsoft Secure Score of a tenant.", ["inforcer_secure_scores_get"]),
    "identity": (
        "Entra users, groups and directory roles of a tenant.",
        ["inforcer_users_list", "inforcer_users_get", "inforcer_groups_list", "inforcer_groups_get", "inforcer_roles_list"],
    ),
    "audit": ("Inforcer activity log.", ["inforcer_audit_event_types", "inforcer_audit_search"]),
    "assessments": ("Assessments that check a tenant against a rule set.", ["inforcer_assessments_list", "inforcer_assessments_run"]),
    "reports": (
        "Report types, report runs and their outputs.",
        [
            "inforcer_reports_types_list",
            "inforcer_reports_runs_list",
            "inforcer_reports_run",
            "inforcer_reports_run_status",
            "inforcer_reports_download_output",
        ],
    ),
}

Domain = Literal["tenants", "baselines", "alignment", "policies", "secure-scores", "identity", "audit", "assessments", "reports"]


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=LOCAL)
    async def inforcer_navigate(domain: Annotated[Domain, Field(description="Area to describe.")]) -> str:
        """Describe the tools of one Inforcer area. All tools are already callable; this changes nothing."""
        summary, tools = DOMAINS[domain]
        return to_json({"domain": domain, "summary": summary, "tools": tools})

    @mcp.tool(annotations=READ_ONLY)
    async def inforcer_status() -> str:
        """Check the Inforcer connection: region and how many tenants the API key can see."""

        async def action(client: InforcerClient) -> dict:
            tenants = await list_tenants(client)
            return {"connected": True, "region": client.region, "tenants": len(tenants), "domains": list(DOMAINS)}

        return await run_tool("inforcer_status", client_factory, action)

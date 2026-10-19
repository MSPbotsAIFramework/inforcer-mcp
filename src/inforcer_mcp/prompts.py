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
"""Ready-made review prompts (MCP prompts). They only describe work; the tools do it."""

from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from .tools._common import TENANT_DESCRIPTION

_READ_ONLY_NOTE = "Only read and recommend: this connection changes no tenant policies."


def register(mcp: FastMCP) -> None:
    @mcp.prompt(name="alignment-review", description="Rank tenants by baseline alignment and find the biggest drift")
    def alignment_review() -> str:
        return "\n".join(
            [
                "Review baseline alignment across all Inforcer tenants.",
                "1. Get the scores with inforcer_alignment_scores and rank tenants from lowest score up.",
                "2. For the lowest few, call inforcer_alignment_details to see which policies drift.",
                "3. Group the drift into themes (which policy areas drift most often, and where).",
                "Report a ranked table (tenant, baseline, score, last comparison) and a prioritised fix list.",
                _READ_ONLY_NOTE,
            ]
        )

    @mcp.prompt(name="tenant-drift-detail", description="Explain one tenant's drift from its baseline")
    def tenant_drift_detail(tenant: Annotated[str, Field(description=TENANT_DESCRIPTION)]) -> str:
        return "\n".join(
            [
                f"Explain how tenant {tenant!r} differs from its baseline.",
                "1. Confirm the tenant with inforcer_tenants_resolve and read it with inforcer_tenants_get "
                "(recent changes, policy diff).",
                "2. Call inforcer_alignment_details for the tenant (pass baseline if it is in several).",
                "3. Use inforcer_policies_list to describe the drifting policies by product and category.",
                "Summarise missing, extra and changed policies, the likely cause and the fix for each.",
                _READ_ONLY_NOTE,
            ]
        )

    @mcp.prompt(name="identity-posture-check", description="Review a tenant's Entra users, groups and privileged roles")
    def identity_posture_check(tenant: Annotated[str, Field(description=TENANT_DESCRIPTION)]) -> str:
        return "\n".join(
            [
                f"Review the identity posture of tenant {tenant!r}.",
                "1. inforcer_roles_list: which privileged roles exist and are enabled.",
                "2. inforcer_users_list (page with continuation_token): global admins, accounts without MFA "
                "registration, disabled or guest accounts holding licenses.",
                "3. inforcer_groups_list: groups with broad or dynamic membership worth checking.",
                "4. inforcer_secure_scores_get for the overall score.",
                "Report the findings by risk (high / medium / low) with a concrete next step for each.",
                _READ_ONLY_NOTE,
            ]
        )

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
"""Alignment: how far each tenant's policies drift from its baselines."""

import re
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..api_client import InforcerClient
from ._common import READ_ONLY, ClientFactory, Limit, Offset, TenantArg, as_list, data, local_page, resolve_tenant_id, run_tool
from .baselines import list_baselines

_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


# The policy lists in alignmentDetails.alignment; the summary counts every list it finds.
_GROUPS = (
    "matchedPolicies",
    "matchedWithAcceptedDeviations",
    "deviatedUnaccepted",
    "missingFromSubjectUnaccepted",
    "additionalInSubjectUnaccepted",
)
Group = Annotated[
    str | None,
    Field(
        description="List the policies of one group, paged: matchedPolicies (aligned), matchedWithAcceptedDeviations, "
        "deviatedUnaccepted (drifted), missingFromSubjectUnaccepted (in the baseline, not the tenant), "
        "additionalInSubjectUnaccepted (tenant only). Leave out for the summary."
    ),
]


def _policy_row(policy: Any, full: bool) -> Any:
    """Name, product, groups and tag names of a policy; full keeps settings, diffs and everything else."""
    if full or not isinstance(policy, dict):
        return policy
    row = {k: v for k, v in policy.items() if not isinstance(v, (dict, list))}
    tags = [t.get("name") if isinstance(t, dict) else t for t in as_list(policy.get("policyTags"))]
    return row | ({"policyTags": tags} if tags else {})


def alignment_view(details: Any, group: str | None, full: bool, offset: int | None, limit: int | None) -> dict:
    """The summary (score, metrics, policy counts per group) or one group's policies, paged."""
    if not isinstance(details, dict):
        return {"details": details}
    alignment = details.get("alignment") if isinstance(details.get("alignment"), dict) else {}
    lists = {k: v for k, v in alignment.items() if isinstance(v, list)}
    if group is None:
        rest = {k: v for k, v in details.items() if k != "alignment"}
        return rest | {
            "alignment": {k: v for k, v in alignment.items() if k not in lists},
            "policy_counts": {k: len(v) for k, v in lists.items()},
            "note": "Pass group (one of policy_counts) to list that group's policies, paged.",
        }
    name = next((k for k in lists if k.lower() == group.strip().lower()), None)
    if name is None:
        known = list(lists) or list(_GROUPS)
        raise ValueError(f"No policy group {group!r}; use one of: {', '.join(known)}.")
    return {"group": name} | local_page("policies", [_policy_row(p, full) for p in lists[name]], offset, limit)


def _member_of(baseline: dict, tenant_id: int) -> bool:
    return any(isinstance(m, dict) and m.get("clientTenantId") == tenant_id for m in baseline.get("members") or [])


async def pick_baseline(client: InforcerClient, tenant_id: int, baseline: str | None) -> str:
    """The baseline GUID to compare a tenant against: given, matched by name, or its only baseline."""
    if baseline and _GUID.match(baseline.strip()):
        return baseline.strip()
    baselines = await list_baselines(client)
    if baseline:
        named = [b for b in baselines if str(b.get("name") or "").lower() == baseline.strip().lower()]
        if len(named) == 1:
            return str(named[0]["id"])
        raise ValueError(f"No single baseline is named {baseline!r}; list them with inforcer_baselines_list.")
    candidates = [b for b in baselines if _member_of(b, tenant_id)]
    if len(candidates) == 1:
        return str(candidates[0]["id"])
    if not candidates:
        raise ValueError(f"Tenant {tenant_id} is not a member of any baseline, so it has no alignment details.")
    options = "; ".join(f"{b.get('id')} ({b.get('name')})" for b in candidates)
    raise ValueError(f"Tenant {tenant_id} is in several baselines; pass baseline as one of: {options}")


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def inforcer_alignment_scores() -> str:
        """List alignment scores for every tenant and baseline: score, thresholds and last comparison time."""

        async def action(client: InforcerClient) -> dict:
            scores = as_list(data(await client.get("beta/alignmentScores")))
            return {"scores": scores, "count": len(scores)}

        return await run_tool("inforcer_alignment_scores", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def inforcer_alignment_details(
        tenant: TenantArg,
        baseline: Annotated[
            str | None,
            Field(description="Baseline GUID or exact name; needed only when the tenant is a member of several baselines."),
        ] = None,
        group: Group = None,
        include_details: Annotated[bool, Field(description="With group: also return each policy's settings and differences; large.")] = False,
        offset: Offset = None,
        limit: Limit = None,
    ) -> str:
        """Alignment of one tenant to a baseline: matched, missing and deviating policies with metrics."""

        async def action(client: InforcerClient) -> Any:
            tenant_id = await resolve_tenant_id(client, tenant)
            baseline_id = await pick_baseline(client, tenant_id, baseline)
            details = data(await client.get(f"beta/tenants/{tenant_id}/alignmentDetails", {"customBaselineId": baseline_id}))
            return {"tenant_id": tenant_id, "baseline_id": baseline_id} | alignment_view(details, group, include_details, offset, limit)

        return await run_tool("inforcer_alignment_details", client_factory, action)

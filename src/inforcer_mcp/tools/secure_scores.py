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

from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..api_client import InforcerClient
from ._common import READ_ONLY, ClientFactory, Limit, Offset, TenantArg, as_list, data, local_page, resolve_tenant_id, run_tool

View = Annotated[
    Literal["summary", "controls", "history"],
    Field(
        description="summary (default): score, categories and the last 7 days; controls: the per-control scores, "
        "paged; history: the daily scores (or one category's, with category), paged."
    ),
]
Category = Annotated[str | None, Field(description="Only this control category, e.g. Identity, Data, Device, Apps.")]
# Days of history the summary shows; view history pages through all of them.
_RECENT_DAYS = 7


def _same(value: Any, wanted: str | None) -> bool:
    return wanted is None or str(value or "").strip().lower() == wanted.strip().lower()


def summarize(scores: dict) -> dict:
    """The headline numbers: current / max score, categories without their history, recent days, control counts."""
    controls = [c for c in as_list(scores.get("controlProfiles")) if isinstance(c, dict)]
    categories = [
        {k: v for k, v in c.items() if k != "historicScores"} for c in as_list(scores.get("controlCategoryScores")) if isinstance(c, dict)
    ]
    history = as_list(scores.get("scores"))
    big = ("scores", "controlProfiles", "controlCategoryScores")
    return {k: v for k, v in scores.items() if k not in big} | {
        "categories": categories,
        "recent_scores": history[:_RECENT_DAYS],
        "history_days": len(history),
        "controls_total": len(controls),
        "controls_with_gap": sum(1 for c in controls if (c.get("scoreDifference") or 0) > 0),
        "note": "view controls lists every control (with the points still to gain); view history pages through the daily scores.",
    }


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def inforcer_secure_scores_get(
        tenant: TenantArg,
        view: View = "summary",
        category: Category = None,
        offset: Offset = None,
        limit: Limit = None,
    ) -> str:
        """Get a tenant's Microsoft Secure Score: current and max score, history and per-control scores."""

        async def action(client: InforcerClient) -> Any:
            tenant_id = await resolve_tenant_id(client, tenant)
            scores = data(await client.get(f"beta/tenants/{tenant_id}/secureScores"))
            if not isinstance(scores, dict):
                return scores
            head = {"tenant_id": tenant_id, "view": view}
            if view == "controls":
                rows = [c for c in as_list(scores.get("controlProfiles")) if isinstance(c, dict) and _same(c.get("controlCategory"), category)]
                return head | local_page("controls", rows, offset, limit)
            if view == "history":
                if category is None:
                    return head | local_page("scores", as_list(scores.get("scores")), offset, limit)
                found = [c for c in as_list(scores.get("controlCategoryScores")) if isinstance(c, dict) and _same(c.get("controlCategory"), category)]
                if not found:
                    names = [c.get("controlCategory") for c in as_list(scores.get("controlCategoryScores")) if isinstance(c, dict)]
                    raise ValueError(f"No control category {category!r}; the tenant has: {', '.join(map(str, names))}.")
                return head | {"category": found[0].get("controlCategory")} | local_page("scores", as_list(found[0].get("historicScores")), offset, limit)
            return head | summarize(scores)

        return await run_tool("inforcer_secure_scores_get", client_factory, action)

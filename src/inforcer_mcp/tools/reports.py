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
"""Reports: report types, queued / finished runs and their rendered outputs."""

import base64
import re
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from ..api_client import InforcerClient
from ._common import READ_ONLY, WRITES, TENANT_DESCRIPTION, ClientFactory, as_list, compact, data, list_tenants, match_tenant, run_tool

RunId = Annotated[str, Field(description="Report run id (from inforcer_reports_run or inforcer_reports_runs_list).", min_length=1)]
# Outputs larger than this are not inlined: they would not fit a tool result.
_MAX_TEXT_CHARS = 15_000
_MAX_BINARY_BYTES = 10_000
_TEXT_TYPES = ("text/", "application/json", "application/xml", "application/csv")


class ReportRequest(BaseModel):
    type: str = Field(description="Report type key from inforcer_reports_types_list.", min_length=1)
    output_format: str = Field(description="One of the type's supportedOutputFormats: pdf, csv, html or json.", min_length=1)
    collate: bool | None = Field(default=None, description="Combine all tenants into one output (collatable types only).")
    parameters: dict[str, str] | None = Field(default=None, description="Values for the type's requiredParameters, by key.")


async def runs(client: InforcerClient) -> list[dict]:
    payload = data(await client.get("beta/reports/runs"))
    rows = payload.get("runs") if isinstance(payload, dict) else payload
    return [r for r in as_list(rows) if isinstance(r, dict)]


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def inforcer_reports_types_list() -> str:
        """List report types: key, name, supported formats, whether they collate tenants, required parameters."""

        async def action(client: InforcerClient) -> dict:
            rows = as_list(data(await client.get("beta/reports/types")))
            return {"report_types": rows, "count": len(rows)}

        return await run_tool("inforcer_reports_types_list", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def inforcer_reports_runs_list() -> str:
        """List report runs: run id, status, report types, formats, trigger and timestamps, output count."""

        async def action(client: InforcerClient) -> dict:
            rows = await runs(client)
            return {"runs": rows, "count": len(rows)}

        return await run_tool("inforcer_reports_runs_list", client_factory, action)

    @mcp.tool(annotations=WRITES)
    async def inforcer_reports_run(
        reports: Annotated[list[ReportRequest], Field(description="Reports to generate in this run.", min_length=1)],
        tenants: Annotated[list[str], Field(description=f"Tenants to cover. Each: {TENANT_DESCRIPTION}", min_length=1)],
    ) -> str:
        """Queue a report run for one or more tenants; returns the run id. Only when the user explicitly asks.

        Poll inforcer_reports_run_status with the run id until it lists outputs.
        """

        async def action(client: InforcerClient) -> Any:
            ids: list[int] = []
            directory: list[dict] | None = None
            for value in tenants:
                if re.fullmatch(r"\s*\d+\s*", value):
                    ids.append(int(value))
                    continue
                directory = directory if directory is not None else await list_tenants(client)
                ids.append(int(match_tenant(directory, value)["clientTenantId"]))
            body = {
                "reports": [
                    compact({"type": r.type, "outputFormat": r.output_format.lower(), "collate": r.collate, "parameters": r.parameters})
                    for r in reports
                ],
                "tenants": {"includeTenants": ids},
            }
            result = data(await client.post("beta/reports/runs", body))
            run_id = result.get("runId") if isinstance(result, dict) else None
            return compact({"run_id": run_id, "tenant_ids": ids, "queued": True, "result": None if run_id else result})

        return await run_tool("inforcer_reports_run", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def inforcer_reports_run_status(run_id: RunId) -> str:
        """Status of a report run and, once finished, its outputs (ids to pass to the download tool)."""

        async def action(client: InforcerClient) -> dict:
            run = next((r for r in await runs(client) if str(r.get("runId")) == run_id), None)
            status = run.get("status") if run else None
            outputs: list = []
            if status is None or status in ("completed", "completedWithErrors"):
                try:
                    payload = data(await client.get(f"beta/reports/runs/{run_id}/outputs"))
                    outputs = as_list(payload.get("outputs") if isinstance(payload, dict) else payload)
                except Exception as e:  # 404 = not finished, failed, or unknown: Inforcer does not say which
                    if getattr(e, "status_code", None) != 404:
                        raise
                    if run is None:
                        raise ValueError(f"No report run {run_id} is visible to this API key.") from e
            return compact(
                {
                    "run_id": run_id,
                    "status": status,
                    "is_terminal": status in ("completed", "completedWithErrors", "failed") if status else bool(outputs),
                    "outputs": outputs,
                    "run": run,
                }
            )

        return await run_tool("inforcer_reports_run_status", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def inforcer_reports_download_output(
        run_id: RunId,
        output_id: Annotated[str, Field(description="Output id from inforcer_reports_run_status.", min_length=1)],
    ) -> str:
        """Download one report output. Text formats (csv, html, json) come back as text; small PDFs as base64."""

        async def action(client: InforcerClient) -> dict:
            file = await client.download(f"beta/reports/runs/{run_id}/outputs/{output_id}")
            info = compact(
                {
                    "runId": run_id,
                    "outputId": output_id,
                    "fileName": file.file_name,
                    "contentType": file.content_type,
                    "sizeBytes": len(file.content),
                }
            )
            if file.content_type.lower().startswith(_TEXT_TYPES):
                text = file.content.decode("utf-8", errors="replace")
                return info | {"content": text[:_MAX_TEXT_CHARS], "truncated": len(text) > _MAX_TEXT_CHARS}
            if len(file.content) <= _MAX_BINARY_BYTES:
                return info | {"contentBase64": base64.b64encode(file.content).decode()}
            return info | {
                "inlined": False,
                "message": "The file is too large to return here; download it from the Inforcer portal, "
                "or run the report as csv / json to read its data.",
            }

        return await run_tool("inforcer_reports_download_output", client_factory, action)

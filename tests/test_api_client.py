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
"""Request shapes, envelopes, tenant resolution and tool behaviour against a mocked Inforcer API."""

import base64
import json

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError

from inforcer_mcp import api_client
from inforcer_mcp.api_client import InforcerClient, InforcerCredentials, InforcerError
from inforcer_mcp.server import _credentials_var, create_mcp_server

CREDS = InforcerCredentials("us", "key-123")
GUID = "11111111-2222-3333-4444-555555555555"
BASELINE = "84b2262f-6fb9-45b7-8856-7903b89749b2"
TENANTS = [
    {"clientTenantId": 139, "tenantFriendlyName": "Example Co", "tenantDnsName": "example.onmicrosoft.com", "msTenantId": GUID, "policyDiff": "long text", "licenses": [1]},
    {"clientTenantId": 140, "tenantFriendlyName": "Sample Ltd", "tenantDnsName": "sample.onmicrosoft.com", "msTenantId": "x"},
    {"clientTenantId": 141, "tenantFriendlyName": "Twin", "tenantDnsName": "twin1.onmicrosoft.com"},
    {"clientTenantId": 142, "tenantFriendlyName": "Twin", "tenantDnsName": "twin2.onmicrosoft.com"},
]


def ok(payload, **extra):
    return {"success": True, "message": "", "errors": [], "data": payload} | extra


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def instant(_seconds):
        return None

    monkeypatch.setattr(api_client.asyncio, "sleep", instant)


class Recorder:
    """Mock transport: replies by method + path below /api/."""

    def __init__(self, routes=None, default=None):
        self.requests: list[httpx.Request] = []
        self.routes = {"beta/tenants": ok(TENANTS)} | (routes or {})
        self.default = default if default is not None else ok(None)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/api/")
        reply = self.routes.get(f"{request.method} {path}", self.routes.get(path, self.default))
        if callable(reply):
            return reply(request)
        return httpx.Response(200, json=reply)

    def calls(self, path: str, method: str | None = None) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == f"/api/{path}" and (method is None or r.method == method)]


def body(request: httpx.Request):
    return json.loads(request.content) if request.content else None


async def _call(monkeypatch, recorder: Recorder, tool: str, args: dict):
    http = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    monkeypatch.setattr(api_client, "_get_shared_http_client", lambda: http)
    ctx = _credentials_var.set(CREDS)
    try:
        content, _ = await create_mcp_server().call_tool(tool, args)
    finally:
        _credentials_var.reset(ctx)
    return json.loads(content[0].text)


def _client(recorder: Recorder) -> InforcerClient:
    return InforcerClient(CREDS, httpx.AsyncClient(transport=httpx.MockTransport(recorder)))


# ---- client ----------------------------------------------------------------


async def test_api_key_header_and_region_host():
    rec = Recorder()
    await _client(rec).get("beta/tenants")
    request = rec.requests[0]
    assert request.headers["inf-api-key"] == "key-123"
    assert str(request.url) == "https://api-us.inforcer.com/api/beta/tenants"


async def test_get_retries_5xx_but_posts_do_not():
    replies = iter([httpx.Response(502), httpx.Response(200, json=ok([]))])
    rec = Recorder({"GET beta/assessments": lambda r: next(replies), "POST beta/reports/runs": lambda r: httpx.Response(500, json={})})
    client = _client(rec)
    await client.get("beta/assessments")
    with pytest.raises(InforcerError):
        await client.post("beta/reports/runs", {})
    assert len(rec.calls("beta/assessments", "GET")) == 2 and len(rec.calls("beta/reports/runs", "POST")) == 1


async def test_envelope_failure_on_200_is_an_error(monkeypatch):
    rec = Recorder({"beta/assessments": {"success": False, "message": "Bad request", "errors": ["assessment disabled"]}})
    result = await _call(monkeypatch, rec, "inforcer_assessments_list", {})
    assert result["error"]["code"] == "invalid_argument"
    assert result["error"]["message"].endswith("Bad request: assessment disabled")


async def test_http_error_envelope_is_readable(monkeypatch):
    forbidden = {"success": False, "message": "Client tenant verification failed", "errors": ["no access"]}
    rec = Recorder({"beta/tenants/139/policies": lambda r: httpx.Response(403, json=forbidden)})
    result = await _call(monkeypatch, rec, "inforcer_policies_list", {"tenant": "139"})
    assert result["error"]["code"] == "unauthorized"
    assert "Client tenant verification failed: no access" in result["error"]["message"]


# ---- tenants ---------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [("139", 139), (GUID, 139), (GUID.upper(), 139), ("Example Co", 139), ("example co", 139), ("sample.onmicrosoft.com", 140)],
)
async def test_tenant_resolution(monkeypatch, value, expected):
    result = await _call(monkeypatch, Recorder(), "inforcer_tenants_resolve", {"tenant": value})
    assert result["clientTenantId"] == expected


async def test_ambiguous_and_unknown_tenants(monkeypatch):
    twin = await _call(monkeypatch, Recorder(), "inforcer_tenants_resolve", {"tenant": "Twin"})
    assert twin["error"]["code"] == "invalid_argument" and "141" in twin["error"]["message"] and "142" in twin["error"]["message"]
    missing = await _call(monkeypatch, Recorder(), "inforcer_tenants_resolve", {"tenant": "Nobody"})
    assert missing["error"]["code"] == "not_found"


async def test_numeric_tenant_skips_lookup(monkeypatch):
    rec = Recorder({"beta/tenants/139/secureScores": ok({"currentScore": 50})})
    result = await _call(monkeypatch, rec, "inforcer_secure_scores_get", {"tenant": "139"})
    assert result == {"currentScore": 50}
    assert [r.url.path for r in rec.requests] == ["/api/beta/tenants/139/secureScores"]


async def test_tenants_list_and_get(monkeypatch):
    rec = Recorder({"beta/tenants/139": ok(TENANTS[0])})
    listed = await _call(monkeypatch, rec, "inforcer_tenants_list", {})
    assert listed["count"] == 4 and "policyDiff" not in listed["tenants"][0] and "licenses" not in listed["tenants"][0]
    got = await _call(monkeypatch, rec, "inforcer_tenants_get", {"tenant": "Example Co"})
    assert got["policyDiff"] == "long text"


async def test_status(monkeypatch):
    result = await _call(monkeypatch, Recorder(), "inforcer_status", {})
    assert result["connected"] is True and result["region"] == "us" and result["tenants"] == 4


# ---- baselines / alignment / policies ---------------------------------------

BASELINES = [
    {"id": BASELINE, "name": "Tier 2", "members": [{"clientTenantId": 139, "tenantFriendlyName": "Example Co", "msTenantId": GUID}], "items": [{}, {}]},
    {"id": "b2", "name": "Tier 3", "members": [{"clientTenantId": 140}]},
]


async def test_baselines_list(monkeypatch):
    rec = Recorder({"beta/baselines": ok(BASELINES)})
    result = await _call(monkeypatch, rec, "inforcer_baselines_list", {"baseline_tenant_id": 7})
    assert rec.requests[0].url.params["baselineTenantId"] == "7"
    first = result["baselines"][0]
    assert first["item_count"] == 2 and first["members"] == [{"clientTenantId": 139, "tenantFriendlyName": "Example Co"}]
    assert "items" not in first


async def test_alignment_details_finds_the_tenants_baseline(monkeypatch):
    rec = Recorder({"beta/baselines": ok(BASELINES), "beta/tenants/139/alignmentDetails": ok({"score": 87})})
    result = await _call(monkeypatch, rec, "inforcer_alignment_details", {"tenant": "Example Co"})
    assert rec.calls("beta/tenants/139/alignmentDetails")[0].url.params["customBaselineId"] == BASELINE
    assert result == {"tenant_id": 139, "baseline_id": BASELINE, "details": {"score": 87}}


async def test_alignment_details_by_baseline_name_and_guid(monkeypatch):
    rec = Recorder({"beta/baselines": ok(BASELINES)})
    await _call(monkeypatch, rec, "inforcer_alignment_details", {"tenant": "140", "baseline": "tier 3"})
    assert rec.calls("beta/tenants/140/alignmentDetails")[0].url.params["customBaselineId"] == "b2"
    rec = Recorder()
    await _call(monkeypatch, rec, "inforcer_alignment_details", {"tenant": "140", "baseline": BASELINE})
    assert not rec.calls("beta/baselines")


async def test_alignment_details_without_baseline(monkeypatch):
    rec = Recorder({"beta/baselines": ok(BASELINES)})
    result = await _call(monkeypatch, rec, "inforcer_alignment_details", {"tenant": "141"})
    assert result["error"]["code"] == "invalid_argument" and "not a member" in result["error"]["message"]
    several = Recorder({"beta/baselines": ok(BASELINES + [{"id": "b3", "name": "Extra", "members": [{"clientTenantId": 139}]}])})
    result = await _call(monkeypatch, several, "inforcer_alignment_details", {"tenant": "139"})
    assert "several baselines" in result["error"]["message"] and "b3" in result["error"]["message"]


async def test_alignment_scores(monkeypatch):
    rec = Recorder({"beta/alignmentScores": ok([{"tenantId": 139, "score": 80}])})
    result = await _call(monkeypatch, rec, "inforcer_alignment_scores", {})
    assert result == {"scores": [{"tenantId": 139, "score": 80}], "count": 1}


async def test_policies_hide_settings_unless_asked(monkeypatch):
    rec = Recorder({"beta/tenants/139/policies": ok([{"id": "p1", "product": "Intune", "policyData": {"big": 1}}])})
    short = await _call(monkeypatch, rec, "inforcer_policies_list", {"tenant": "139"})
    assert short["policies"] == [{"id": "p1", "product": "Intune"}]
    full = await _call(monkeypatch, rec, "inforcer_policies_list", {"tenant": "139", "include_settings": True})
    assert full["policies"][0]["policyData"] == {"big": 1}


# ---- identity --------------------------------------------------------------


async def test_users_list_paging(monkeypatch):
    rec = Recorder({"beta/tenants/139/users": ok([{"id": "u1"}], continuationToken="next-1", totalCount=250)})
    result = await _call(monkeypatch, rec, "inforcer_users_list", {"tenant": "139", "search": "ann", "continuation_token": "tok"})
    params = rec.requests[0].url.params
    assert (params["search"], params["continuationToken"]) == ("ann", "tok")
    assert result == {"tenant_id": 139, "users": [{"id": "u1"}], "count": 1, "total": 250, "has_more": True, "continuation_token": "next-1"}


async def test_groups_roles_and_gets(monkeypatch):
    rec = Recorder(
        {
            "beta/tenants/139/groups": ok([{"id": "g1"}], continuationToken=None),
            "beta/tenants/139/groups/g1": ok({"id": "g1", "displayName": "Admins"}),
            "beta/tenants/139/users/u1": ok({"id": "u1"}),
            "beta/tenants/139/roles": ok([{"displayName": "Global Administrator", "isPrivileged": True}]),
        }
    )
    groups = await _call(monkeypatch, rec, "inforcer_groups_list", {"tenant": "139"})
    assert groups["has_more"] is False and "continuation_token" not in groups
    assert (await _call(monkeypatch, rec, "inforcer_groups_get", {"tenant": "139", "group_id": "g1"}))["displayName"] == "Admins"
    assert (await _call(monkeypatch, rec, "inforcer_users_get", {"tenant": "139", "user_id": "u1"})) == {"id": "u1"}
    assert (await _call(monkeypatch, rec, "inforcer_roles_list", {"tenant": "139"}))["count"] == 1


# ---- audit / assessments ---------------------------------------------------


async def test_audit_search_body_and_paging(monkeypatch):
    rec = Recorder(
        {
            "beta/auditEvents/eventTypes": ok([{"name": "PolicyDeployed"}, {"name": "BackupCompleted"}]),
            "POST beta/auditEvents/search": ok({"items": [{"id": "e1"}], "continuationToken": "c2"}),
        }
    )
    types = await _call(monkeypatch, rec, "inforcer_audit_event_types", {})
    assert types["event_types"] == ["PolicyDeployed", "BackupCompleted"]
    args = {"event_types": ["PolicyDeployed"], "date_from": "2026-10-01", "user": "admin@example.com", "page_size": 50}
    result = await _call(monkeypatch, rec, "inforcer_audit_search", args)
    assert body(rec.calls("beta/auditEvents/search")[0]) == {
        "eventTypes": ["PolicyDeployed"], "dateFrom": "2026-10-01", "user": "admin@example.com", "pageSize": 50,
    }
    assert result == {"events": [{"id": "e1"}], "count": 1, "has_more": True, "continuation_token": "c2"}


async def test_assessments(monkeypatch):
    rec = Recorder({"beta/assessments": ok([{"id": "a1", "name": "CIS"}]), "POST beta/tenants/139/assessments/a1/runs": ok({"passed": 10})})
    assert (await _call(monkeypatch, rec, "inforcer_assessments_list", {}))["count"] == 1
    result = await _call(monkeypatch, rec, "inforcer_assessments_run", {"tenant": "Example Co", "assessment_id": "a1"})
    assert result == {"tenant_id": 139, "assessment_id": "a1", "result": {"passed": 10}}


# ---- reports ---------------------------------------------------------------


async def test_reports_run_body(monkeypatch):
    rec = Recorder({"POST beta/reports/runs": ok({"runId": "r-1"})})
    args = {
        "reports": [{"type": "copilotadoption", "output_format": "PDF", "collate": True, "parameters": {"period": "30d"}}],
        "tenants": ["139", "Sample Ltd", GUID],
    }
    result = await _call(monkeypatch, rec, "inforcer_reports_run", args)
    assert body(rec.calls("beta/reports/runs", "POST")[0]) == {
        "reports": [{"type": "copilotadoption", "outputFormat": "pdf", "collate": True, "parameters": {"period": "30d"}}],
        "tenants": {"includeTenants": [139, 140, 139]},
    }
    assert result == {"run_id": "r-1", "tenant_ids": [139, 140, 139], "queued": True}
    assert len(rec.calls("beta/tenants")) == 1  # one lookup for all names


async def test_reports_run_requires_reports():
    with pytest.raises(ToolError, match="at least 1 item"):
        await create_mcp_server().call_tool("inforcer_reports_run", {"reports": [], "tenants": ["139"]})


async def test_run_status_running_and_completed(monkeypatch):
    runs = ok({"runs": [{"runId": "r-1", "status": "running"}, {"runId": "r-2", "status": "completed"}]})
    rec = Recorder({"beta/reports/runs": runs, "beta/reports/runs/r-2/outputs": ok({"outputs": [{"id": "o1", "format": "csv"}]})})
    running = await _call(monkeypatch, rec, "inforcer_reports_run_status", {"run_id": "r-1"})
    assert running["is_terminal"] is False and running["outputs"] == [] and not rec.calls("beta/reports/runs/r-1/outputs")
    done = await _call(monkeypatch, rec, "inforcer_reports_run_status", {"run_id": "r-2"})
    assert done["is_terminal"] is True and done["outputs"] == [{"id": "o1", "format": "csv"}]


async def test_run_status_unknown_run(monkeypatch):
    rec = Recorder({"beta/reports/runs": ok({"runs": []}), "beta/reports/runs/zz/outputs": lambda r: httpx.Response(404, json={})})
    result = await _call(monkeypatch, rec, "inforcer_reports_run_status", {"run_id": "zz"})
    assert result["error"]["code"] == "invalid_argument"


async def test_download_text_and_small_pdf(monkeypatch):
    csv = "tenant,score\nExample Co,87\n"
    rec = Recorder(
        {
            "beta/reports/runs/r-2/outputs/o1": lambda r: httpx.Response(
                200, content=csv.encode(), headers={"content-type": "text/csv", "content-disposition": 'attachment; filename="report.csv"'}
            ),
            "beta/reports/runs/r-2/outputs/o2": lambda r: httpx.Response(200, content=b"%PDF-1.7 tiny", headers={"content-type": "application/pdf"}),
            "beta/reports/runs/r-2/outputs/o3": lambda r: httpx.Response(200, content=b"x" * 50_000, headers={"content-type": "application/pdf"}),
        }
    )
    text = await _call(monkeypatch, rec, "inforcer_reports_download_output", {"run_id": "r-2", "output_id": "o1"})
    assert text["content"] == csv and text["fileName"] == "report.csv" and text["truncated"] is False
    small = await _call(monkeypatch, rec, "inforcer_reports_download_output", {"run_id": "r-2", "output_id": "o2"})
    assert base64.b64decode(small["contentBase64"]) == b"%PDF-1.7 tiny"
    big = await _call(monkeypatch, rec, "inforcer_reports_download_output", {"run_id": "r-2", "output_id": "o3"})
    assert big["inlined"] is False and big["sizeBytes"] == 50_000 and "contentBase64" not in big

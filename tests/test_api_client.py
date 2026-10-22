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
from inforcer_mcp.tools._common import decode_cursor, encode_cursor

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
    assert (result["tenant_id"], result["currentScore"], result["controls_total"]) == (139, 50, 0)
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
    assert (result["tenant_id"], result["baseline_id"], result["score"], result["policy_counts"]) == (139, BASELINE, 87, {})


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
    token = result.pop("continuation_token")
    assert result == {"tenant_id": 139, "users": [{"id": "u1"}], "count": 1, "total": 250, "has_more": True}
    assert decode_cursor(token) == ("next-1", 0)


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
        "eventTypes": ["PolicyDeployed"], "dateFrom": "2026-10-01T00:00:00Z", "user": "admin@example.com", "pageSize": 50,
    }
    assert decode_cursor(result.pop("continuation_token")) == ("c2", 0)
    assert result == {"events": [{"id": "e1"}], "count": 1, "has_more": True}


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


# ---- PRD-21067 / 21068 / 21069: paging that never loses records, and audit dates -----------------------


def _cosmos_token(position: int) -> str:
    # Inforcer's tokens look like this: JSON text of about 3.7 KB.
    return json.dumps([{"token": f"+RID:~{position}~" + "A" * 3600, "range": {"min": "", "max": "FF"}}], separators=(",", ":"))


def _position(token) -> int:
    return int(json.loads(token)[0]["token"].split("~")[1]) if token else 0


def _token_api(rows, page_size_of, wrap):
    """An Inforcer list paged by continuation token; ``wrap`` builds the reply from (items, next token)."""

    def reply(request):
        sent = body(request) if request.method == "POST" else dict(request.url.params)
        start = _position(sent.get("continuationToken"))
        size = page_size_of(sent)
        chunk = rows[start : start + size]
        nxt = _cosmos_token(start + size) if start + size < len(rows) else None
        return httpx.Response(200, json=wrap(chunk, nxt))

    return reply


def _audit_reply(items, nxt):
    return ok({"items": items, "continuationToken": nxt})


def _event(i, big=0):
    return {"id": f"e{i}", "eventType": "PolicyDeployed", "detail": "d" * big}


async def _walk(monkeypatch, rec, tool, args, key):
    ids, token, calls = [], None, 0
    while True:
        out = await _call(monkeypatch, rec, tool, args | ({} if token is None else {"continuation_token": token}))
        calls += 1
        assert "error" not in out, out
        assert "retry_page_size" not in out and calls < 300
        ids += [r["id"] for r in out[key]]
        token = out.get("continuation_token")
        if token is None:
            return ids, calls


@pytest.mark.parametrize("page_size,big", [(5, 0), (20, 0), (23, 0), (100, 0), (None, 0), (20, 900), (100, 3000)])
async def test_audit_walk_returns_every_event_once(monkeypatch, page_size, big):
    events = [_event(i, big) for i in range(157)]
    rec = Recorder({"POST beta/auditEvents/search": _token_api(events, lambda sent: sent.get("pageSize") or 100, _audit_reply)})
    args = {"date_from": "2026-10-14", "date_to": "2026-10-21"} | ({} if page_size is None else {"page_size": page_size})
    ids, _ = await _walk(monkeypatch, rec, "inforcer_audit_search", args, "events")
    assert ids == [e["id"] for e in events]


async def test_a_page_that_fits_keeps_its_token(monkeypatch):
    # 20 small events plus a 3.7 KB token: everything fits, so nothing is cut and the token stays.
    rec = Recorder({"POST beta/auditEvents/search": _token_api([_event(i) for i in range(40)], lambda sent: sent["pageSize"], _audit_reply)})
    out = await _call(monkeypatch, rec, "inforcer_audit_search", {"page_size": 20})
    assert out["count"] == 20 and "truncated" not in out
    assert decode_cursor(out["continuation_token"]) == (_cosmos_token(20), 0)


async def test_json_looking_token_round_trips_exactly(monkeypatch):
    rec = Recorder({"POST beta/auditEvents/search": _token_api([_event(i) for i in range(10)], lambda sent: 5, _audit_reply)})
    first = await _call(monkeypatch, rec, "inforcer_audit_search", {"page_size": 5})
    assert first["continuation_token"].startswith("c1.") and "[" not in first["continuation_token"]
    await _call(monkeypatch, rec, "inforcer_audit_search", {"page_size": 5, "continuation_token": first["continuation_token"]})
    assert body(rec.requests[-1])["continuationToken"] == _cosmos_token(5)


async def test_bare_inforcer_token_still_works_as_text_or_parsed_json(monkeypatch):
    rec = Recorder({"POST beta/auditEvents/search": _token_api([_event(i) for i in range(10)], lambda sent: 5, _audit_reply)})
    raw = _cosmos_token(5)
    as_text = await _call(monkeypatch, rec, "inforcer_audit_search", {"page_size": 5, "continuation_token": raw})
    assert body(rec.requests[-1])["continuationToken"] == raw and as_text["events"][0]["id"] == "e5"
    parsed = await _call(monkeypatch, rec, "inforcer_audit_search", {"page_size": 5, "continuation_token": json.loads(raw)})
    assert body(rec.requests[-1])["continuationToken"] == raw and parsed["events"][0]["id"] == "e5"


@pytest.mark.parametrize("token", ["c1.not-base64!", "c1." + base64.urlsafe_b64encode(b"junk").decode()])
async def test_damaged_cursor_is_invalid_argument(monkeypatch, token):
    rec = Recorder()
    out = await _call(monkeypatch, rec, "inforcer_audit_search", {"continuation_token": token})
    assert out["error"]["code"] == "invalid_argument" and rec.requests == []


def test_cursor_encoding_round_trip():
    for token, skip in [(None, 0), (None, 36), ("plain", 0), (_cosmos_token(40), 17)]:
        assert decode_cursor(encode_cursor(token, skip)) == (token, skip)
    assert len(encode_cursor(_cosmos_token(40), 3)) < 1000  # compressed, so it costs little of the size limit


@pytest.mark.parametrize(
    "date_from,date_to,sent_from,sent_to",
    [
        ("2026-10-14", "2026-10-21", "2026-10-14T00:00:00Z", "2026-10-21T23:59:59.999Z"),
        ("2026-10-14T08:30:00Z", "2026-10-14T12:00:00+02:00", "2026-10-14T08:30:00Z", "2026-10-14T12:00:00+02:00"),
        ("2026-10-14T08:30:00", None, "2026-10-14T08:30:00Z", None),
        ("2026-10-14", "2026-10-14", "2026-10-14T00:00:00Z", "2026-10-14T23:59:59.999Z"),
    ],
)
async def test_audit_dates_are_expanded(monkeypatch, date_from, date_to, sent_from, sent_to):
    rec = Recorder({"POST beta/auditEvents/search": ok({"items": []})})
    await _call(monkeypatch, rec, "inforcer_audit_search", {"date_from": date_from} | ({"date_to": date_to} if date_to else {}))
    sent = body(rec.requests[0])
    assert sent["dateFrom"] == sent_from and sent.get("dateTo") == sent_to


@pytest.mark.parametrize(
    "args",
    [{"date_from": "2026-13-01"}, {"date_from": "yesterday"}, {"date_to": "14/10/2026"}, {"date_from": "2026-10-21", "date_to": "2026-10-14"}],
)
async def test_bad_audit_dates_are_invalid_argument(monkeypatch, args):
    rec = Recorder()
    out = await _call(monkeypatch, rec, "inforcer_audit_search", args)
    assert out["error"]["code"] == "invalid_argument" and rec.requests == []


def _user(i):
    return {"id": f"u{i}", "userPrincipalName": f"user{i}@example.com", "roles": [f"role {k}" for k in range(20)], "notes": "n" * 300}


async def test_users_returned_whole_walk_to_every_user(monkeypatch):
    users = [_user(i) for i in range(124)]
    rec = Recorder({"beta/tenants/139/users": ok(users, totalCount=124)})
    ids, calls = await _walk(monkeypatch, rec, "inforcer_users_list", {"tenant": "139"}, "users")
    assert ids == [u["id"] for u in users] and calls > 1


async def test_groups_paged_by_inforcer_and_cut_walk_to_every_group(monkeypatch):
    groups = [_user(i) | {"id": f"g{i}"} for i in range(153)]

    def wrap(items, nxt):
        return {"success": True, "data": items, "continuationToken": nxt, "totalCount": 153}

    rec = Recorder({"beta/tenants/139/groups": _token_api(groups, lambda sent: 60, wrap)})
    ids, _ = await _walk(monkeypatch, rec, "inforcer_groups_list", {"tenant": "139"}, "groups")
    assert ids == [g["id"] for g in groups]


async def _walk_offset(monkeypatch, rec, tool, args, key, id_key="id"):
    ids, offset, cuts = [], None, 0
    while True:
        out = await _call(monkeypatch, rec, tool, args | ({} if offset is None else {"offset": offset}))
        assert "retry_page_size" not in out
        cuts += bool(out.get("truncated"))
        ids += [r[id_key] for r in out[key]]
        offset = out.get("next_offset")
        if offset is None:
            return ids, cuts


@pytest.mark.parametrize(
    "tool,path,key,count",
    [
        ("inforcer_policies_list", "beta/tenants/139/policies", "policies", 270),
        ("inforcer_roles_list", "beta/tenants/139/roles", "roles", 133),
    ],
)
async def test_whole_lists_walk_by_offset(monkeypatch, tool, path, key, count):
    rows = [{"id": f"r{i}", "displayName": f"Item {i}", **{f"field{k}": "x" * 60 for k in range(30)}} for i in range(count)]
    rec = Recorder({path: ok(rows)})
    ids, cuts = await _walk_offset(monkeypatch, rec, tool, {"tenant": "139"}, key)
    assert ids == [r["id"] for r in rows] and cuts > 0
    page = await _call(monkeypatch, rec, tool, {"tenant": "139", "offset": 10, "limit": 5})
    assert ([r["id"] for r in page[key]], page["next_offset"], page["total"]) == ([f"r{i}" for i in range(10, 15)], 15, count)
    past = await _call(monkeypatch, rec, tool, {"tenant": "139", "offset": count + 5})
    assert (past[key], past["count"], past["has_more"]) == ([], 0, False)


async def test_tenants_list_walks_by_offset(monkeypatch):
    tenants = [{"clientTenantId": i, "tenantFriendlyName": f"T{i}", "tenantDnsName": f"t{i}.onmicrosoft.com", **{f"f{k}": "x" * 60 for k in range(40)}} for i in range(45)]
    rec = Recorder({"beta/tenants": ok(tenants)})
    ids, cuts = await _walk_offset(monkeypatch, rec, "inforcer_tenants_list", {}, "tenants", "clientTenantId")
    assert ids == list(range(45)) and cuts > 0
    last = await _call(monkeypatch, rec, "inforcer_tenants_list", {"offset": 40, "limit": 10})
    assert (last["count"], last["has_more"], "next_offset" in last) == (5, False, False)


# ---- PRD-21066: secure score and alignment details, summary first and paged details ---------------------
# Shapes follow a live secure score (90 days, 59 controls, 4 categories, about 125 KB) and the
# alignmentDetails fields (alignment.<policy groups>, metrics, completedAt); the values are made up.


def _day(i):
    return {"currentScore": 900 - i, "maxScore": 1243, "currentScorePercentage": 72.5, "createdDateTime": f"2026-07-{i % 28 + 1:02d}T00:00:00+00:00"}


def _secure_score():
    controls = [
        {"id": f"scid_{i}", "title": f"Control {i}", "controlCategory": ["Identity", "Data", "Device", "Apps"][i % 4],
         "remediation": "r" * 340, "actionUrl": "https://example.com/" + "a" * 150, "service": "MDATP", "maxScore": 9,
         "currentScore": i % 3, "scoreDifference": 9 - i % 3 if i % 5 else 0}
        for i in range(59)
    ]
    categories = [
        {"controlCategory": name, "maxScore": 225, "currentScore": 212, "currentScorePercentage": 94.2, "historicScores": [_day(i) for i in range(90)]}
        for name in ("Identity", "Data", "Device", "Apps")
    ]
    return {"scores": [_day(i) for i in range(90)], "currentScore": 904.11, "maxScore": 1243, "currentScorePercentage": 72.7,
            "licensedUserCount": 0, "enabledServices": ["HasExchange", "HasSharePoint"], "controlProfiles": controls,
            "controlCategoryScores": categories}


async def test_secure_score_summary_fits_and_has_the_headline(monkeypatch):
    rec = Recorder({"beta/tenants/139/secureScores": ok(_secure_score())})
    out = await _call(monkeypatch, rec, "inforcer_secure_scores_get", {"tenant": "139"})
    assert "truncated" not in out and (out["currentScore"], out["maxScore"], out["view"]) == (904.11, 1243, "summary")
    assert [c["controlCategory"] for c in out["categories"]] == ["Identity", "Data", "Device", "Apps"]
    assert all("historicScores" not in c for c in out["categories"])
    assert (len(out["recent_scores"]), out["history_days"], out["controls_total"]) == (7, 90, 59)
    assert out["controls_with_gap"] == sum(1 for i in range(59) if i % 5)


async def _walk_view(monkeypatch, rec, args, key):
    rows, offset, cuts = [], None, 0
    while True:
        out = await _call(monkeypatch, rec, "inforcer_secure_scores_get", args | ({} if offset is None else {"offset": offset}))
        assert "retry_page_size" not in out and "error" not in out
        cuts += bool(out.get("truncated"))
        rows += out[key]
        offset = out.get("next_offset")
        if offset is None:
            return rows, cuts


async def test_secure_score_controls_page_through_every_control(monkeypatch):
    rec = Recorder({"beta/tenants/139/secureScores": ok(_secure_score())})
    rows, cuts = await _walk_view(monkeypatch, rec, {"tenant": "139", "view": "controls"}, "controls")
    assert [c["id"] for c in rows] == [f"scid_{i}" for i in range(59)] and cuts > 0
    device, _ = await _walk_view(monkeypatch, rec, {"tenant": "139", "view": "controls", "category": "device"}, "controls")
    assert [c["id"] for c in device] == [f"scid_{i}" for i in range(59) if i % 4 == 2]
    page = await _call(monkeypatch, rec, "inforcer_secure_scores_get", {"tenant": "139", "view": "controls", "offset": 55, "limit": 10})
    assert ([c["id"] for c in page["controls"]], page["has_more"]) == ([f"scid_{i}" for i in range(55, 59)], False)


async def test_secure_score_history_overall_and_by_category(monkeypatch):
    rec = Recorder({"beta/tenants/139/secureScores": ok(_secure_score())})
    overall, _ = await _walk_view(monkeypatch, rec, {"tenant": "139", "view": "history"}, "scores")
    assert len(overall) == 90 and overall[0]["currentScore"] == 900
    data_cat = await _call(monkeypatch, rec, "inforcer_secure_scores_get", {"tenant": "139", "view": "history", "category": "DATA", "limit": 30})
    assert (data_cat["category"], data_cat["count"], data_cat["total"], data_cat["next_offset"]) == ("Data", 30, 90, 30)
    unknown = await _call(monkeypatch, rec, "inforcer_secure_scores_get", {"tenant": "139", "view": "history", "category": "Network"})
    assert unknown["error"]["code"] == "invalid_argument" and "Identity" in unknown["error"]["message"]


async def test_secure_score_without_data_is_passed_through(monkeypatch):
    rec = Recorder({"beta/tenants/139/secureScores": ok(None)})
    out = await _call(monkeypatch, rec, "inforcer_secure_scores_get", {"tenant": "139"})
    assert out == {"done": True}


def _policy(group, i):
    return {"policyName": f"{group} {i}", "product": "Intune", "primaryGroup": "Devices", "secondaryGroup": "Compliance",
            "inforcerPolicyTypeName": "Compliance Policy", "policyTags": [{"name": "E8"}, {"name": "ML1"}],
            "policyData": {f"setting{k}": "v" * 80 for k in range(40)}, "diff": [{"path": f"p{k}", "old": "a", "new": "b"} for k in range(20)]}


_GROUP_SIZES = {"matchedPolicies": 140, "matchedWithAcceptedDeviations": 6, "deviatedUnaccepted": 23,
                "missingFromSubjectUnaccepted": 31, "additionalInSubjectUnaccepted": 70}


def _alignment():
    groups = {g: [_policy(g, i) for i in range(n)] for g, n in _GROUP_SIZES.items()}
    metrics = {"totalPolicies": 270, "matchedPolicies": 140, "matchedWithAcceptedDeviations": 6, "deviatedPolicies": 23,
               "recommendedPoliciesFromBaseline": 31, "customerOnlyPolicies": 70}
    return {"alignment": {"alignmentScore": 81.5} | groups, "metrics": metrics, "completedAt": "2026-10-21T18:56:00Z"}


async def _walk_group(monkeypatch, rec, args):
    rows, offset, cuts = [], None, 0
    while True:
        out = await _call(monkeypatch, rec, "inforcer_alignment_details", args | ({} if offset is None else {"offset": offset}))
        assert "retry_page_size" not in out and "error" not in out
        cuts += bool(out.get("truncated"))
        rows += out["policies"]
        offset = out.get("next_offset")
        if offset is None:
            return rows, cuts, out


async def test_alignment_summary_has_score_metrics_and_counts(monkeypatch):
    rec = Recorder({"beta/baselines": ok(BASELINES), "beta/tenants/139/alignmentDetails": ok(_alignment())})
    out = await _call(monkeypatch, rec, "inforcer_alignment_details", {"tenant": "139"})
    assert "truncated" not in out and out["alignment"] == {"alignmentScore": 81.5}
    assert out["policy_counts"] == _GROUP_SIZES and out["metrics"]["totalPolicies"] == 270
    assert out["completedAt"] == "2026-10-21T18:56:00Z"


async def test_alignment_group_pages_through_every_policy(monkeypatch):
    rec = Recorder({"beta/baselines": ok(BASELINES), "beta/tenants/139/alignmentDetails": ok(_alignment())})
    rows, _, last = await _walk_group(monkeypatch, rec, {"tenant": "139", "group": "matchedPolicies"})
    assert [r["policyName"] for r in rows] == [f"matchedPolicies {i}" for i in range(140)] and last["group"] == "matchedPolicies"
    assert rows[0] == {"policyName": "matchedPolicies 0", "product": "Intune", "primaryGroup": "Devices", "secondaryGroup": "Compliance",
                       "inforcerPolicyTypeName": "Compliance Policy", "policyTags": ["E8", "ML1"]}


async def test_alignment_group_with_details_still_reaches_every_policy(monkeypatch):
    rec = Recorder({"beta/baselines": ok(BASELINES), "beta/tenants/139/alignmentDetails": ok(_alignment())})
    rows, cuts, _ = await _walk_group(monkeypatch, rec, {"tenant": "139", "group": "deviatedunaccepted", "include_details": True})
    assert [r["policyName"] for r in rows] == [f"deviatedUnaccepted {i}" for i in range(23)] and cuts > 0
    assert "policyData" in rows[0]


async def test_alignment_unknown_group_lists_the_groups(monkeypatch):
    rec = Recorder({"beta/baselines": ok(BASELINES), "beta/tenants/139/alignmentDetails": ok(_alignment())})
    out = await _call(monkeypatch, rec, "inforcer_alignment_details", {"tenant": "139", "group": "drifted"})
    assert out["error"]["code"] == "invalid_argument" and "deviatedUnaccepted" in out["error"]["message"]
    empty = await _call(monkeypatch, rec, "inforcer_alignment_details", {"tenant": "139", "group": "matchedWithAcceptedDeviations", "offset": 50})
    assert (empty["policies"], empty["count"], empty["has_more"]) == ([], 0, False)

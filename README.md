# Inforcer MCP Server

An [MCP](https://modelcontextprotocol.io) server for the [Inforcer](https://www.inforcer.com) public API,
the Microsoft 365 baseline management platform MSPs use to keep client tenants aligned to a
standard. It lets an AI agent read managed tenants, baselines and alignment scores, a tenant's
policies, Secure Score, Entra users / groups / roles and the activity log and, on explicit
request, run assessments and queue reports. Nothing it does changes tenant policies.

- Transport: MCP Streamable HTTP at `POST /mcp`, health probe at `GET /health`
- Stateless and multi-tenant: credentials arrive on every request as HTTP headers and are
  never stored; every tool is listed from the first `tools/list`
- Also provides three MCP prompts: `alignment-review`, `tenant-drift-detail` (tenant),
  `identity-posture-check` (tenant)
- Python 3.12, [uv](https://docs.astral.sh/uv/), FastMCP, httpx
- License: Apache-2.0, Copyright 2026 MSPbots.ai

## Authentication headers

| Header | Required | Type | Default | Allowed values | Description | Example |
|---|---|---|---|---|---|---|
| `X-Inforcer-Region` | Yes | string | none | `anz`, `eu`, `uk`, `us` (case-insensitive; `api-us.inforcer.com` also accepted) | Region hosting your Inforcer account | `us` |
| `X-Inforcer-Api-Key` | Yes | string | none | - | Inforcer API key | `your-inforcer-api-key` |

Where to get them: an Inforcer API key is issued with scopes. Read-only use needs
`Tenants.Read` (or the narrower `Baselines.Read`, `AlignmentScores.Read`, `tenants.policies.Read`,
`Tenants.Users.Read`, `Tenants.Groups.Read`, `Tenants.Roles.Read`, `Tenants.SecureScores.Read`),
plus `Audit.Read`, `Assessments.Read` and `Reports.Read`; `Assessments.Run` and `Reports.Run` are
needed only for the two run tools.

How the key is sent to Inforcer: every call goes to `https://api-{region}.inforcer.com/api/beta/...`
with the key in the `Inf-Api-Key` header.

A request to `/mcp` without both headers gets HTTP 401 naming the missing one; an unknown region
gets HTTP 400.

## Tools

All tools return a JSON string. A `tenant` argument accepts the numeric Client Tenant ID, the
Microsoft tenant GUID, the tenant DNS name or its exact friendly name (an ambiguous name returns
the matching ids). **write** marks tools that create work in Inforcer.

| Area | Tools |
|---|---|
| Overview | `inforcer_navigate` (**domain**), `inforcer_status` |
| Tenants | `inforcer_tenants_list`, `inforcer_tenants_get` (**tenant**), `inforcer_tenants_resolve` (**tenant**) |
| Baselines | `inforcer_baselines_list` (baseline_tenant_id) |
| Alignment | `inforcer_alignment_scores`, `inforcer_alignment_details` (**tenant**, baseline) |
| Policies | `inforcer_policies_list` (**tenant**, include_settings) |
| Secure Score | `inforcer_secure_scores_get` (**tenant**) |
| Identity | `inforcer_users_list` (**tenant**, search, continuation_token), `inforcer_users_get` (**tenant**, **user_id**), `inforcer_groups_list` (**tenant**, search, continuation_token), `inforcer_groups_get` (**tenant**, **group_id**), `inforcer_roles_list` (**tenant**) |
| Audit | `inforcer_audit_event_types`, `inforcer_audit_search` (event_types, date_from, date_to, user, page_size, continuation_token) |
| Assessments | `inforcer_assessments_list`; **write**: `inforcer_assessments_run` (**tenant**, **assessment_id**) |
| Reports | `inforcer_reports_types_list`, `inforcer_reports_runs_list`, `inforcer_reports_run_status` (**run_id**), `inforcer_reports_download_output` (**run_id**, **output_id**); **write**: `inforcer_reports_run` (**reports**, **tenants**) |

Errors come back as a JSON envelope instead of an exception:

```json
{"error": {"code": "unauthorized", "message": "Inforcer returned HTTP 403 for beta/tenants/139/policies: Client tenant verification failed", "retryable": false}}
```

`code` is one of `not_configured`, `unauthorized`, `not_found`, `invalid_argument`,
`rate_limited`, `upstream_error`. Inforcer can also answer HTTP 200 with `success: false`; that
is reported as an error too. Reads are retried with backoff on 429, 5xx and network errors;
writes only on 429.

## Run locally

```bash
uv sync
uv run python -m inforcer_mcp          # listens on 0.0.0.0:8080
```

or with Docker:

```bash
docker build --platform linux/amd64 -t inforcer-mcp:dev .
docker run --rm -p 8080:8080 inforcer-mcp:dev
```

Configuration (environment variables, all optional): `MCP_HTTP_HOST` (default `0.0.0.0`),
`MCP_HTTP_PORT` (default `8080`), `LOG_LEVEL` (default `INFO`). Credentials are never read
from the environment.

## Testing example

```bash
# list tools
curl -s http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-Inforcer-Region: us" \
  -H "X-Inforcer-Api-Key: your-inforcer-api-key" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'

# alignment of one tenant
curl -s http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-Inforcer-Region: us" \
  -H "X-Inforcer-Api-Key: your-inforcer-api-key" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"inforcer_alignment_details","arguments":{"tenant":"example.onmicrosoft.com"}}}'
```

Unit tests (no network access needed):

```bash
uv run pytest
```

## Delivery notes

- **Tool count (24) and prompts (3).** Kept as is because the names are an existing contract.
- **API source.** Inforcer does not publish public API documentation; routes, parameters and
  envelopes follow Inforcer's own OpenAPI document ("inforcer | Public API") as captured by the
  MIT-licensed community PowerShell module InforcerCommunity, and its API reference.
- **Alignment details fixed.** The API requires the baseline to compare against
  (`customBaselineId`); older versions never sent it. `inforcer_alignment_details` now takes an
  optional `baseline` (GUID or exact name) and, when omitted, uses the only baseline the tenant is
  a member of, or lists the candidates.
- **Report run status** reads the run's status from the run list and fetches outputs only for
  finished runs (the outputs route answers 404 while a run is pending, failed or unknown).
- **Report downloads stay small.** csv / html / json outputs are returned as text (first 15,000
  characters); a PDF is returned as base64 only when under 10 KB, otherwise its metadata with a
  note. Older versions inlined any file as base64.
- **Smaller results.** The tenant list leaves out each tenant's policy diff text and licenses
  (`inforcer_tenants_get` has them); the policy list leaves out the full policy settings unless
  `include_settings` is true; baselines report their policy item count instead of the items.
- **New optional parameters:** `baseline` on alignment details, `user` on audit search,
  `include_settings` on the policy list.
- **Tenant names** must match exactly (case-insensitive); an ambiguous name returns the matching
  ids instead of picking one.

## License

Apache License 2.0 - see [LICENSE](LICENSE) and [NOTICE](NOTICE).

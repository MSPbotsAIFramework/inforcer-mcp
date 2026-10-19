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
"""FastMCP factory and the per-request credential middleware."""

import contextvars
import re
import uuid
from collections.abc import Callable

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .api_client import InforcerClient, InforcerCredentials
from .config import REGION_BASE_URLS, Settings

HEADER_REGION = "X-Inforcer-Region"
HEADER_API_KEY = "X-Inforcer-Api-Key"
REQUIRED_HEADERS = [HEADER_REGION, HEADER_API_KEY]

_PLACEHOLDER = re.compile(r"^\$\{.*\}$")

# Per-request isolation: the middleware sets these for the duration of one
# HTTP request and resets them in `finally`. Nothing credential-related is
# ever stored at module level.
_credentials_var: contextvars.ContextVar[InforcerCredentials | None] = contextvars.ContextVar(
    "inforcer_credentials", default=None
)
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("inforcer_request_id", default="-")

INSTRUCTIONS = (
    "Inforcer manages Microsoft 365 tenants for MSPs against baselines: a "
    "baseline is a reference tenant's policies; member tenants get an "
    "alignment score showing how far their policies drift from it.\n"
    "Usage:\n"
    "- Every tenant argument accepts the numeric Client Tenant ID, the "
    "Microsoft tenant GUID, the tenant DNS name or its friendly name; "
    "inforcer_tenants_resolve shows which tenant a value means.\n"
    "- Fleet posture: inforcer_alignment_scores -> inforcer_alignment_details "
    "(tenant; baseline when the tenant is in several) -> inforcer_policies_list.\n"
    "- Identity: inforcer_users_list / inforcer_groups_list / inforcer_roles_list; "
    "pass continuation_token back to page.\n"
    "- Reports: inforcer_reports_types_list -> inforcer_reports_run -> "
    "inforcer_reports_run_status (until outputs appear) -> "
    "inforcer_reports_download_output.\n"
    "Running assessments and reports creates work in Inforcer: only when the "
    "user explicitly asks. Nothing here changes tenant policies."
)


def get_client_from_context() -> InforcerClient | None:
    """The client for the current request, or None outside a request."""
    credentials = _credentials_var.get()
    if credentials is None:
        return None
    return InforcerClient(credentials)


def _clean(value: str | None) -> str | None:
    text = (value or "").strip()
    if not text or _PLACEHOLDER.match(text):
        return None
    return text


def normalize_region(value: str) -> str | None:
    """'us', 'US', 'api-us.inforcer.com' or 'https://api-us.inforcer.com/api' -> 'us'; None when unknown."""
    text = re.sub(r"^https?://", "", value.strip().lower()).split("/", 1)[0]
    match = re.fullmatch(r"api-([a-z]+)\.inforcer\.com", text)
    region = match.group(1) if match else text
    return region if region in REGION_BASE_URLS else None


def _error(scope: Scope, receive: Receive, send: Send, status: int, error: str, message: str):
    return JSONResponse({"error": error, "message": message, "required_headers": REQUIRED_HEADERS}, status_code=status)(
        scope, receive, send
    )


class GatewayCredentialsMiddleware:
    """Reads the Inforcer region and API key on /mcp requests into the request context."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith("/mcp"):
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        region_raw = _clean(request.headers.get(HEADER_REGION))
        api_key = _clean(request.headers.get(HEADER_API_KEY))
        if not (region_raw and api_key):
            missing = [h for h, v in ((HEADER_REGION, region_raw), (HEADER_API_KEY, api_key)) if not v]
            await _error(scope, receive, send, 401, "Missing credentials", f"Missing header(s): {', '.join(missing)}")
            return
        region = normalize_region(region_raw)
        if region is None:
            await _error(scope, receive, send, 400, "Invalid credentials", f"{HEADER_REGION} must be one of: {', '.join(REGION_BASE_URLS)}")
            return

        request_id = (request.headers.get("x-request-id") or "").strip()[:64] or uuid.uuid4().hex[:12]
        creds_token = _credentials_var.set(InforcerCredentials(region, api_key))
        rid_token = request_id_var.set(request_id)
        try:
            await self.app(scope, receive, send)
        finally:
            _credentials_var.reset(creds_token)
            request_id_var.reset(rid_token)


def create_mcp_server(settings: Settings | None = None) -> FastMCP:
    """Build the FastMCP server and register every tool and prompt."""
    mcp = FastMCP(
        name="inforcer-mcp",
        instructions=INSTRUCTIONS,
        # The service sits behind the gateway, so Host is never localhost;
        # DNS-rebinding protection would reject those requests with 421.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        stateless_http=True,
    )

    client_factory: Callable[[], InforcerClient | None] = get_client_from_context

    from . import prompts
    from .tools import alignment, assessments, audit, baselines, identity, navigation, policies, reports, secure_scores, tenants

    for module in (navigation, tenants, baselines, alignment, policies, secure_scores, identity, audit, assessments, reports):
        module.register(mcp, client_factory)
    prompts.register(mcp)

    return mcp

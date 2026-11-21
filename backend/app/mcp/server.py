"""The MCP SDK adapter: a low-level MCP server over Streamable HTTP, with bearer-key auth.

The SDK answers 401 for a missing or unknown key and 403 for a key without `mcp:read`; everything
else (scopes per tool, confirmations, logging) is in `app/mcp/service.py`.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Any

import mcp_types as types
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.lowlevel import Server
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette

from app.core.config import get_settings
from app.mcp import service

INSTRUCTIONS = (
    "DocPilot answers questions about the user's documents with citations. Start with ask_documents for a "
    "question; use list_documents to find ids, and the read tools for detail. Saving (save_report, "
    "create_task) takes two calls: the first returns a preview and a confirmation_id; show the preview to "
    "the user and call again with the same arguments and the confirmation_id only if they agree."
)


class ApiKeyVerifier:
    """Bearer token = a DocPilot API key. Its scopes come from the key (specs/mcp.md §3)."""

    async def verify_token(self, token: str) -> AccessToken | None:
        principal = await service.principal_for_key(token)
        if principal is None:
            return None
        return AccessToken(
            token=token,
            client_id=str(principal.key_id),
            subject=str(principal.user_id),
            scopes=sorted(principal.scopes),
            resource=get_settings().mcp_resource_url,
        )


def _principal() -> service.Principal:
    token = get_access_token()
    if token is None or token.subject is None:  # pragma: no cover - the auth middleware runs first
        raise service.McpError("unauthorized", "Missing API key.")
    import uuid

    return service.Principal(uuid.UUID(token.client_id), uuid.UUID(token.subject), frozenset(token.scopes))


async def _list_tools(ctx: Any, params: types.PaginatedRequestParams | None) -> types.ListToolsResult:
    return types.ListToolsResult(
        tools=[
            types.Tool(
                name=t.name,
                description=t.description,
                input_schema=t.input_schema,
                annotations=types.ToolAnnotations(
                    read_only_hint=t.scope == service.READ_SCOPE,
                    destructive_hint=False,
                    idempotent_hint=t.scope == service.READ_SCOPE,
                ),
            )
            for t in service.tools()
        ]
    )


async def _call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
    try:
        result = await service.call_tool(_principal(), params.name, dict(params.arguments or {}))
    except service.McpError as exc:
        # Fail loudly: the text starts with a stable code the client (or its model) can act on.
        return types.CallToolResult(content=[types.TextContent(type="text", text=str(exc))], is_error=True)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(result, default=str))],
        structured_content=result,
    )


@lru_cache
def get_server() -> Server[Any]:
    return Server(
        "docpilot",
        version="1.0",
        instructions=INSTRUCTIONS,
        on_list_tools=_list_tools,
        on_call_tool=_call_tool,
    )


def _build_app() -> Starlette:
    settings = get_settings()
    return get_server().streamable_http_app(
        streamable_http_path="/",
        json_response=True,
        stateless_http=True,
        host="0.0.0.0",  # we bind behind Docker; DNS-rebinding checks are for servers bound to localhost
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        auth=AuthSettings(
            issuer_url=settings.mcp_resource_url,  # type: ignore[arg-type]
            resource_server_url=settings.mcp_resource_url,  # type: ignore[arg-type]
            required_scopes=[service.READ_SCOPE],
            validate_token_resource=True,  # the verifier stamps our own URL on every key's token
        ),
        token_verifier=ApiKeyVerifier(),
    )


class McpApp:
    """What app.main mounts at /mcp. The SDK's session manager can run only once, so each start of the
    API (and each test client) builds a fresh MCP app inside `running()`; requests go to the current one.
    Stateless with JSON responses: each request stands alone, so it scales like the REST API."""

    def __init__(self) -> None:
        self._app: Starlette | None = None

    @asynccontextmanager
    async def running(self) -> AsyncIterator[None]:
        self._app = _build_app()
        # The session manager's task group must be entered and exited in the same task, so it lives in
        # its own background task rather than in whichever task runs the app's startup and shutdown.
        started, stop = asyncio.Event(), asyncio.Event()

        async def keep_running() -> None:
            async with get_server().session_manager.run():
                started.set()
                await stop.wait()

        task = asyncio.create_task(keep_running())
        await started.wait()
        try:
            yield
        finally:
            stop.set()
            await task
            self._app = None

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if self._app is None:  # pragma: no cover - only before startup or after shutdown
            raise RuntimeError("the MCP app is not running")
        await self._app(scope, receive, send)


mcp_app = McpApp()

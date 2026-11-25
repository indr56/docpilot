"""The MCP server (specs/mcp.md): auth and scopes over HTTP, lean results, ask_documents, the
server-side confirmation for writes, the call log, the rate limit and the description linter."""

import json
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, update

from app.core.config import get_settings
from app.db.base import utcnow
from app.db.session import get_async_sessionmaker
from app.mcp import service
from app.models import ApiKey, McpCall, McpConfirmation

PROTOCOL = {"MCP-Protocol-Version": "2025-06-18", "Accept": "application/json, text/event-stream"}


@pytest.fixture
async def mcp(client: httpx.AsyncClient) -> AsyncIterator[httpx.AsyncClient]:
    """A client for /mcp (the session `client` fixture keeps the app, and so the MCP app, running)."""
    from app.main import app

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def new_key(client: httpx.AsyncClient, *scopes: str) -> str:
    response = await client.post("/api-keys", json={"name": f"mcp-{uuid.uuid4().hex[:6]}", "scopes": list(scopes)})
    assert response.status_code == 201, response.text
    return response.json()["key"]


async def rpc(mcp: httpx.AsyncClient, key: str | None, method: str, params: dict[str, Any]) -> httpx.Response:
    headers = dict(PROTOCOL)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return await mcp.post(
        "/mcp/", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, headers=headers
    )


async def call(mcp: httpx.AsyncClient, key: str, name: str, arguments: dict[str, Any]) -> tuple[bool, Any]:
    """(ok, payload): payload is the structured result, or the error text."""
    response = await rpc(mcp, key, "tools/call", {"name": name, "arguments": arguments})
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    if result["isError"]:
        return False, result["content"][0]["text"]
    return True, result["structuredContent"]


# --------------------------------------------------------------------------- auth and scopes
async def test_no_key_or_a_bad_key_is_401(mcp: httpx.AsyncClient) -> None:
    for key in (None, "not-a-key"):
        response = await rpc(mcp, key, "tools/list", {})
        assert response.status_code == 401
        assert "resource_metadata" in response.headers["www-authenticate"]


async def test_a_key_without_mcp_read_is_403(client: httpx.AsyncClient, mcp: httpx.AsyncClient) -> None:
    key = await new_key(client)  # a REST-only key
    assert (await client.get("/documents", headers={"X-API-Key": key})).status_code == 200
    assert (await rpc(mcp, key, "tools/list", {})).status_code == 403


async def test_a_revoked_key_is_401(client: httpx.AsyncClient, mcp: httpx.AsyncClient) -> None:
    created = (await client.post("/api-keys", json={"name": "gone", "scopes": ["mcp:read"]})).json()
    assert (await rpc(mcp, created["key"], "tools/list", {})).status_code == 200
    await client.delete(f"/api-keys/{created['id']}")
    assert (await rpc(mcp, created["key"], "tools/list", {})).status_code == 401


async def test_writes_need_the_write_scope(client: httpx.AsyncClient, mcp: httpx.AsyncClient) -> None:
    key = await new_key(client, "mcp:read")
    ok, error = await call(mcp, key, "save_report", {"title": "Fees", "content_markdown": "x"})
    assert not ok and error.startswith("forbidden:")


async def test_tools_list_marks_read_only_tools(client: httpx.AsyncClient, mcp: httpx.AsyncClient) -> None:
    key = await new_key(client, "mcp:read")
    tools = {t["name"]: t for t in (await rpc(mcp, key, "tools/list", {})).json()["result"]["tools"]}
    assert {"ask_documents", "list_documents", "search_passages", "save_report", "create_task"} <= set(tools)
    assert tools["search_passages"]["annotations"]["readOnlyHint"] is True
    assert tools["save_report"]["annotations"]["readOnlyHint"] is False
    assert "confirmation_id" in tools["save_report"]["inputSchema"]["properties"]
    assert "ask_research_agent" not in tools  # hand-offs are internal to the agent


# --------------------------------------------------------------------------- read tools
async def test_list_documents_is_lean_and_pages(
    client: httpx.AsyncClient, mcp: httpx.AsyncClient, ready_docs: dict[str, str]
) -> None:
    ok, first = await call(mcp, "docpilot-demo-key", "list_documents", {"limit": 2})
    assert ok and len(first["items"]) == 2 and first["next_cursor"] == "2"
    assert set(first["items"][0]) == {"id", "title", "status", "pages"}
    ok, rest = await call(
        mcp, "docpilot-demo-key", "list_documents", {"cursor": first["next_cursor"], "include": ["tags"]}
    )
    seen = {i["id"] for i in first["items"]} | {i["id"] for i in rest["items"]}
    assert set(ready_docs.values()) <= seen
    assert "tags" in rest["items"][0] and "summary" not in rest["items"][0]


async def test_search_returns_lean_citations(mcp: httpx.AsyncClient, ready_docs: dict[str, str]) -> None:
    args = {"query": "flood deductible", "document_ids": [ready_docs["policy"]], "top_k": 3}
    ok, out = await call(mcp, "docpilot-demo-key", "search_passages", args)
    assert ok and out["citations"] and "[1]" in out["result"]
    assert set(out["citations"][0]) == {"n", "document_id", "title", "page"}
    ok, detailed = await call(mcp, "docpilot-demo-key", "search_passages", {**args, "include": ["snippets"]})
    assert detailed["citations"][0]["snippet"]


async def test_errors_are_loud_with_a_code(mcp: httpx.AsyncClient) -> None:
    ok, error = await call(mcp, "docpilot-demo-key", "get_document_text", {"document_id": str(uuid.uuid4())})
    assert not ok and error.startswith("not_in_scope:")
    ok, error = await call(mcp, "docpilot-demo-key", "search_passages", {"top_k": 99})
    assert not ok and error.startswith("invalid_arguments:") and "query" in error
    ok, error = await call(mcp, "docpilot-demo-key", "drop_tables", {})
    assert not ok and error.startswith("unknown_tool:")


async def test_ask_documents_answers_with_citations_and_never_writes(
    mcp: httpx.AsyncClient, ready_docs: dict[str, str]
) -> None:
    ok, out = await call(
        mcp,
        "docpilot-demo-key",
        "ask_documents",
        {"question": "What is the flood deductible?", "document_ids": [ready_docs["policy"]], "include": ["steps"]},
    )
    assert ok and out["citations"] and out["confidence"] == "high"
    assert [s["agent"] for s in out["steps"]] == ["router", "research", "research"]
    ok, out = await call(mcp, "docpilot-demo-key", "ask_documents", {"question": "Save a report of the fees"})
    assert ok and "supervisor" not in json.dumps(out)  # read-only: an action route becomes unsure


# --------------------------------------------------------------------------- confirmations
async def test_write_needs_a_confirmation_and_runs_once(client: httpx.AsyncClient, mcp: httpx.AsyncClient) -> None:
    args = {"title": "Renewal call", "notes": "Call the broker"}
    ok, first = await call(mcp, "docpilot-demo-key", "create_task", args)
    assert ok and first["status"] == "confirmation_required" and first["preview"].startswith("Create a task")
    before = [o for o in (await client.get("/agent/outputs?kind=task")).json() if o["title"] == "Renewal call"]
    assert before == []  # nothing written yet

    ok, done = await call(
        mcp, "docpilot-demo-key", "create_task", {**args, "confirmation_id": first["confirmation_id"]}
    )
    assert ok and done["status"] == "executed"
    ok, again = await call(
        mcp, "docpilot-demo-key", "create_task", {**args, "confirmation_id": first["confirmation_id"]}
    )
    assert ok and again["status"] == "already_executed" and again["result"] == done["result"]
    saved = [o for o in (await client.get("/agent/outputs?kind=task")).json() if o["title"] == "Renewal call"]
    assert len(saved) == 1 and saved[0]["run_id"] is None


async def test_confirmation_is_bound_to_the_arguments_and_the_key(
    client: httpx.AsyncClient, mcp: httpx.AsyncClient
) -> None:
    ok, first = await call(mcp, "docpilot-demo-key", "save_report", {"title": "Q3 fees", "content_markdown": "a"})
    cid = first["confirmation_id"]
    ok, error = await call(
        mcp, "docpilot-demo-key", "save_report", {"title": "Q3 fees", "content_markdown": "b", "confirmation_id": cid}
    )
    assert not ok and error.startswith("confirmation_mismatch:")
    ok, error = await call(mcp, "docpilot-demo-key", "create_task", {"title": "Q3 fees", "confirmation_id": cid})
    assert not ok and error.startswith("confirmation_mismatch:")
    other = await new_key(client, "mcp:read", "mcp:write")
    ok, error = await call(
        mcp, other, "save_report", {"title": "Q3 fees", "content_markdown": "a", "confirmation_id": cid}
    )
    assert not ok and error.startswith("confirmation_not_found:")


async def test_expired_confirmation_is_refused(mcp: httpx.AsyncClient) -> None:
    args = {"title": "Old one", "content_markdown": "x"}
    ok, first = await call(mcp, "docpilot-demo-key", "save_report", args)
    async with get_async_sessionmaker()() as db:
        await db.execute(
            update(McpConfirmation)
            .where(McpConfirmation.id == uuid.UUID(first["confirmation_id"]))
            .values(expires_at=utcnow() - timedelta(seconds=1))
        )
        await db.commit()
    ok, error = await call(
        mcp, "docpilot-demo-key", "save_report", {**args, "confirmation_id": first["confirmation_id"]}
    )
    assert not ok and error.startswith("confirmation_expired:")


# --------------------------------------------------------------------------- operations
async def test_every_call_is_logged_without_its_arguments(client: httpx.AsyncClient, mcp: httpx.AsyncClient) -> None:
    key = await new_key(client, "mcp:read")
    await call(mcp, key, "list_documents", {})
    await call(mcp, key, "save_report", {"title": "secret plan", "content_markdown": "x"})
    async with get_async_sessionmaker()() as db:
        key_id = (await db.execute(select(ApiKey.id).where(ApiKey.key_prefix == key[:10]))).scalar_one()
        rows = (await db.execute(select(McpCall).where(McpCall.api_key_id == key_id))).scalars().all()
    assert [(r.tool_name, r.ok, r.error_code) for r in sorted(rows, key=lambda r: r.created_at)] == [
        ("list_documents", True, None),
        ("save_report", False, "forbidden"),
    ]
    assert all(len(r.arguments_hash) == 64 for r in rows)


async def test_rate_limit_is_per_key(
    client: httpx.AsyncClient, mcp: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "mcp_rate_limit_per_minute", 2)
    key = await new_key(client, "mcp:read")
    results = [(await call(mcp, key, "list_documents", {}))[0] for _ in range(3)]
    assert results == [True, True, False]
    assert (await call(mcp, "docpilot-demo-key", "list_documents", {}))[0] is True  # another key is unaffected


def test_tool_descriptions_pass_the_linter() -> None:
    assert service.lint_descriptions(service.tools()) == []


def test_the_linter_catches_bad_descriptions() -> None:
    bad = service.McpTool("save_thing", "saves", {"properties": {"x": {}}}, service.WRITE_SCOPE)
    problems = service.lint_descriptions([bad])
    assert any("characters" in p for p in problems)
    assert any("capital" in p for p in problems)
    assert any("confirmation" in p for p in problems)
    assert any("x: no description" in p for p in problems)


async def test_call_log_counts_match(mcp: httpx.AsyncClient) -> None:
    async with get_async_sessionmaker()() as db:
        before = (await db.execute(select(func.count()).select_from(McpCall))).scalar_one()
    await call(mcp, "docpilot-demo-key", "list_documents", {})
    async with get_async_sessionmaker()() as db:
        after = (await db.execute(select(func.count()).select_from(McpCall))).scalar_one()
    assert after == before + 1

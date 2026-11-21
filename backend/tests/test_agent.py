"""The agent (specs/agent.md): routing, tools, the loop's limits, approvals and the stream.

All offline: the fake provider routes by keywords and follows a scripted policy (app/llm/fake_agent.py),
so every test runs the real router, registry, validation, approvals and citations code.
"""

import json
from typing import Any

import httpx
import pytest

from app.core.config import get_settings
from app.core.security import hash_api_key
from app.db.session import get_async_sessionmaker
from app.llm import get_provider
from app.llm.base import StructuredCompletion, Usage
from app.llm.schemas import AgentDecision
from app.models import ApiKey, User
from app.tools.registry import InvalidCall, all_tools, parse_call


async def run(client: httpx.AsyncClient, goal: str, document_ids: list[str] | None = None) -> dict[str, Any]:
    response = await client.post("/agent/runs", json={"goal": goal, "document_ids": document_ids or []})
    assert response.status_code == 200, response.text
    return response.json()


def scripted(monkeypatch: pytest.MonkeyPatch, decisions: list[AgentDecision]) -> None:
    """Replace the agent's model with a fixed list of decisions (the router stays the fake one)."""
    provider = get_provider()
    original = provider.acomplete_structured
    queue = list(decisions)

    async def fake(messages, response_model, **kwargs):  # type: ignore[no-untyped-def]
        if response_model is AgentDecision:
            return StructuredCompletion(queue.pop(0), Usage("fake-chat", 10, 10))
        return await original(messages, response_model, **kwargs)

    monkeypatch.setattr(provider, "acomplete_structured", fake)


def tool(name: str, arguments: dict[str, Any]) -> AgentDecision:
    return AgentDecision(thought="t", action="tool", tool_name=name, arguments_json=json.dumps(arguments), answer="")


def final(answer: str) -> AgentDecision:
    return AgentDecision(thought="t", action="final", tool_name="", arguments_json="{}", answer=answer)


# --------------------------------------------------------------------------- registry
def test_every_tool_has_a_description_and_a_strict_object_schema() -> None:
    for t in all_tools():
        assert len(t.description) >= 30, t.name
        schema = t.schema()
        assert schema["type"] == "object", t.name
        assert set(schema.get("required", [])) <= set(schema["properties"]), t.name
        assert '"title": "' not in json.dumps(schema), t.name  # label titles are dropped to save tokens
        if t.kind == "write":
            assert t.preview is not None


def test_parse_call_explains_what_is_wrong() -> None:
    with pytest.raises(InvalidCall, match="no tool named 'delete_everything'"):
        parse_call("delete_everything", "{}", ["search_passages"])
    with pytest.raises(InvalidCall, match="Tools you can call: search_passages"):
        parse_call("save_report", "{}", ["search_passages"])  # exists, but not offered on this route
    with pytest.raises(InvalidCall, match="not valid JSON"):
        parse_call("search_passages", "{query: x}", ["search_passages"])
    with pytest.raises(InvalidCall, match="query"):
        parse_call("search_passages", '{"top_k": 3}', ["search_passages"])
    with pytest.raises(InvalidCall, match="top_k"):
        parse_call("search_passages", '{"query": "fee", "top_k": 99}', ["search_passages"])


# --------------------------------------------------------------------------- routes, end to end
async def test_chat_only_needs_no_tools(client: httpx.AsyncClient, ready_docs: dict[str, str]) -> None:
    body = await run(client, "hello there")
    assert body["route"] == "chat_only"
    assert body["status"] == "completed" and body["tool_calls"] == 0
    assert [s["kind"] for s in body["steps"]] == ["route", "final"]
    assert body["confidence"] == "high"


async def test_pointed_fact_answers_with_citations(client: httpx.AsyncClient, ready_docs: dict[str, str]) -> None:
    body = await run(client, "What is the flood deductible?", [ready_docs["policy"]])
    assert body["route"] == "pointed_fact"
    assert [s["kind"] for s in body["steps"]] == ["route", "tool_call", "final"]
    assert body["steps"][1]["tool_name"] == "search_passages"
    assert body["citations"], body["answer"]
    citation = body["citations"][0]
    assert citation["document_id"] == ready_docs["policy"] and citation["snippet"]
    assert body["confidence"] == "high"
    assert body["prompt_tokens"] > 0


async def test_whole_document_reads_every_page(client: httpx.AsyncClient, ready_docs: dict[str, str]) -> None:
    body = await run(client, "Summarise the service agreement", [ready_docs["agreement"]])
    assert body["route"] == "whole_document"
    assert body["steps"][1]["tool_name"] == "get_document_text"
    assert body["steps"][1]["ok"] is True
    assert body["status"] == "completed" and body["citations"]


async def test_whole_document_falls_back_to_notes_when_too_long(
    client: httpx.AsyncClient, ready_docs: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "agent_document_token_budget", 10)
    body = await run(client, "Summarise the service agreement", [ready_docs["agreement"]])
    calls = [(s["tool_name"], s["ok"]) for s in body["steps"] if s["kind"] == "tool_call"]
    assert calls[0] == ("get_document_text", False)  # too_long: the model is told to use the notes
    assert calls[1][0] == "get_document_notes"


async def test_list_everything_scans_all_sections_and_dedupes(
    client: httpx.AsyncClient, ready_docs: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "agent_scan_section_tokens", 150)  # force several sections
    body = await run(client, "List every notice period", [ready_docs["agreement"]])
    assert body["route"] == "list_everything"
    scan = body["steps"][1]
    assert scan["tool_name"] == "scan_document" and scan["ok"]
    sections = int(scan["summary"].split(" from ")[1].split()[0])
    assert sections > 1
    texts = [c["snippet"] for c in body["citations"]]
    assert len(texts) == len(set(texts))
    assert any("90 days" in t for t in texts) and any("60 days" in t for t in texts)


async def test_cross_document_runs_the_comparison(client: httpx.AsyncClient, ready_docs: dict[str, str]) -> None:
    body = await run(client, "Compare the policy and the quote", [ready_docs["policy"], ready_docs["quote"]])
    assert body["route"] == "cross_document"
    assert body["steps"][1]["tool_name"] == "compare_documents" and body["steps"][1]["ok"]
    assert {c["document_id"] for c in body["citations"]} <= {ready_docs["policy"], ready_docs["quote"]}


async def test_unknown_document_is_a_404(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/agent/runs", json={"goal": "x?", "document_ids": ["00000000-0000-0000-0000-000000000000"]}
    )
    assert response.status_code == 404


# --------------------------------------------------------------------------- the loop's guards
async def test_invalid_arguments_are_explained_and_retried(
    client: httpx.AsyncClient, ready_docs: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    scripted(
        monkeypatch,
        [
            tool("search_passages", {"top_k": 3}),  # missing query
            tool("search_passages", {"query": "flood deductible"}),
            final("The flood deductible is in the policy [1]."),
        ],
    )
    body = await run(client, "What is the flood deductible?", [ready_docs["policy"]])
    kinds = [s["kind"] for s in body["steps"]]
    assert kinds == ["route", "invalid_call", "tool_call", "final"]
    assert "query" in body["steps"][1]["summary"]
    assert body["status"] == "completed"
    assert body["confidence"] == "medium"  # it needed a retry


async def test_three_invalid_calls_fail_the_run(
    client: httpx.AsyncClient, ready_docs: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    scripted(monkeypatch, [tool("drop_tables", {})] * 3)
    body = await run(client, "What is the flood deductible?", [ready_docs["policy"]])
    assert body["status"] == "failed" and body["error_code"] == "invalid_tool_calls"


async def test_step_limit(
    client: httpx.AsyncClient, ready_docs: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "agent_max_steps", 2)
    scripted(monkeypatch, [tool("search_passages", {"query": f"fee {i}"}) for i in range(3)])
    body = await run(client, "What is the fee?", [ready_docs["agreement"]])
    assert body["status"] == "failed" and body["error_code"] == "step_limit"
    assert body["tool_calls"] == 2


async def test_cost_limit(
    client: httpx.AsyncClient, ready_docs: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "agent_max_cost_usd", 0.0)
    body = await run(client, "What is the fee?", [ready_docs["agreement"]])
    assert body["status"] == "failed" and body["error_code"] == "cost_limit"


async def test_a_tool_cannot_read_outside_the_runs_scope(
    client: httpx.AsyncClient, ready_docs: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    scripted(
        monkeypatch,
        [tool("get_document_text", {"document_id": ready_docs["quote"]}), final("I could not read it.")],
    )
    body = await run(client, "Summarise the agreement", [ready_docs["agreement"]])
    step = body["steps"][1]
    assert step["ok"] is False and step["summary"] == "error: not_in_scope"


async def test_injected_instructions_stay_data(
    client: httpx.AsyncClient, ready_docs: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even if a document talks the model into a write, it only becomes an approval the user sees."""
    scripted(monkeypatch, [tool("save_report", {"title": "pwned", "content_markdown": "x"})])
    body = await run(client, "Save a report of the fees", [ready_docs["agreement"]])
    assert body["status"] == "waiting_approval"
    outputs = (await client.get("/agent/outputs")).json()
    assert not any(o["title"] == "pwned" for o in outputs)


# --------------------------------------------------------------------------- approvals
async def test_action_waits_for_approval_then_saves(client: httpx.AsyncClient, ready_docs: dict[str, str]) -> None:
    body = await run(client, "Create a task for the renewal strategy meeting", [ready_docs["agreement"]])
    assert body["route"] == "action"
    assert body["status"] == "waiting_approval"
    pending = body["pending_approval"]
    assert pending["tool_name"] == "create_task" and pending["preview"].startswith("Create a task")
    assert (await client.get("/agent/outputs?kind=task")).json() == [] or all(
        o["run_id"] != body["id"] for o in (await client.get("/agent/outputs?kind=task")).json()
    )

    url = f"/agent/runs/{body['id']}/approvals/{pending['id']}"
    approved = await client.post(url, json={"decision": "approve"})
    assert approved.status_code == 200, approved.text
    after = approved.json()
    assert after["status"] == "completed" and after["pending_approval"] is None
    assert after["answer"].startswith("Done.")
    tasks = [o for o in (await client.get("/agent/outputs?kind=task")).json() if o["run_id"] == body["id"]]
    assert len(tasks) == 1 and tasks[0]["document_ids"] == [ready_docs["agreement"]]

    # Idempotent: approving again changes nothing and saves nothing new.
    again = await client.post(url, json={"decision": "approve"})
    assert again.status_code == 200 and again.json()["status"] == "completed"
    tasks = [o for o in (await client.get("/agent/outputs?kind=task")).json() if o["run_id"] == body["id"]]
    assert len(tasks) == 1


async def test_rejected_action_saves_nothing(client: httpx.AsyncClient, ready_docs: dict[str, str]) -> None:
    body = await run(client, "Save a report of the termination terms", [ready_docs["agreement"]])
    pending = body["pending_approval"]
    assert pending["tool_name"] == "save_report"
    rejected = (
        await client.post(f"/agent/runs/{body['id']}/approvals/{pending['id']}", json={"decision": "reject"})
    ).json()
    assert rejected["status"] == "completed"
    assert "Nothing was saved" in rejected["answer"]
    assert "rejected" in [s["kind"] for s in rejected["steps"]]
    assert not [o for o in (await client.get("/agent/outputs")).json() if o["run_id"] == body["id"]]


async def test_another_user_cannot_see_or_approve_a_run(client: httpx.AsyncClient, ready_docs: dict[str, str]) -> None:
    body = await run(client, "Create a task to review the fees", [ready_docs["agreement"]])
    async with get_async_sessionmaker()() as db:
        other = User(email="agent-stranger@example.com")
        db.add(other)
        await db.flush()
        db.add(ApiKey(user_id=other.id, name="s", key_hash=hash_api_key("agent-stranger"), key_prefix="agentstr"))
        await db.commit()
    headers = {"X-API-Key": "agent-stranger"}
    assert (await client.get(f"/agent/runs/{body['id']}", headers=headers)).status_code == 404
    url = f"/agent/runs/{body['id']}/approvals/{body['pending_approval']['id']}"
    assert (await client.post(url, json={"decision": "approve"}, headers=headers)).status_code == 404
    assert (await client.get(f"/agent/runs/{body['id']}")).json()["status"] == "waiting_approval"


async def test_expired_approval_is_refused(
    client: httpx.AsyncClient, ready_docs: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "agent_approval_ttl_hours", -1)
    body = await run(client, "Create a task to check the commission", [ready_docs["agreement"]])
    url = f"/agent/runs/{body['id']}/approvals/{body['pending_approval']['id']}"
    response = await client.post(url, json={"decision": "approve"})
    assert response.status_code == 409 and response.json()["error"]["code"] == "approval_expired"


async def test_busy_run_is_refused(client: httpx.AsyncClient, ready_docs: dict[str, str]) -> None:
    from app.services.cache import get_redis

    body = await run(client, "Create a task to call the broker", [ready_docs["agreement"]])
    get_redis().set(f"agent:run:{body['id']}", "someone-else", ex=60)
    url = f"/agent/runs/{body['id']}/approvals/{body['pending_approval']['id']}"
    response = await client.post(url, json={"decision": "approve"})
    assert response.status_code == 409 and response.json()["error"]["code"] == "run_busy"


# --------------------------------------------------------------------------- stream and listing
async def test_stream_sends_steps_in_order(client: httpx.AsyncClient, ready_docs: dict[str, str]) -> None:
    events: list[str] = []
    payloads: list[dict[str, Any]] = []
    async with client.stream(
        "POST",
        "/agent/runs/stream",
        json={"goal": "What is the commission?", "document_ids": [ready_docs["agreement"]]},
    ) as response:
        assert response.status_code == 200
        event = ""
        async for line in response.aiter_lines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and event:
                events.append(event)
                payloads.append(json.loads(line.split(":", 1)[1]))
    assert events[0] == "run" and events[-1] == "done"
    assert events.count("step") == 3  # route, search, final
    assert payloads[-1]["status"] == "completed"


async def test_runs_are_listed_and_fetched(client: httpx.AsyncClient, ready_docs: dict[str, str]) -> None:
    body = await run(client, "What is the term?", [ready_docs["agreement"]])
    listed = (await client.get("/agent/runs?limit=5")).json()["items"]
    assert listed[0]["id"] == body["id"] and listed[0]["steps"] == []
    one = (await client.get(f"/agent/runs/{body['id']}")).json()
    assert one["steps"] == body["steps"]

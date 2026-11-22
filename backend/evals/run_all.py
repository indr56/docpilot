"""Run every eval set in process and write one results file (specs/evals.md).

    python evals/run_all.py --mode offline            # fake AI: free, deterministic (every push in CI)
    python evals/run_all.py --mode real               # OpenAI: needs OPENAI_API_KEY (costs about $0.1-0.5)
    python evals/run_all.py --mode real --sets tasks  # only some sets

Needs Postgres (pgvector) and Redis, like the tests: EVAL_DB_HOST / EVAL_DB_PORT (default
localhost:9701), EVAL_REDIS_URL (default redis://localhost:9702). It uses its own database,
`docpilot_eval`, which it recreates, and Redis DBs 13-15.
"""

import argparse
import asyncio
import json
import os
import re
import shutil
import statistics
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
SAMPLES = next(p for p in (BACKEND.parent / "samples", Path("/samples")) if p.exists())
SETS = HERE / "sets"
ALL_SETS = ["routes", "tasks", "injection", "mcp", "rag"]


def configure(mode: str) -> Path:
    """Environment for the app, set BEFORE it is imported (as tests/conftest.py does)."""
    host, port = os.environ.get("EVAL_DB_HOST", "localhost"), os.environ.get("EVAL_DB_PORT", "9701")
    redis = os.environ.get("EVAL_REDIS_URL", "redis://localhost:9702")
    storage = Path(tempfile.mkdtemp(prefix="docpilot-eval-"))
    os.environ.update(
        {
            "LLM_PROVIDER": "fake" if mode == "offline" else "openai",
            "DATABASE_URL": f"postgresql+asyncpg://docpilot:docpilot@{host}:{port}/docpilot_eval",
            "SYNC_DATABASE_URL": f"postgresql+psycopg://docpilot:docpilot@{host}:{port}/docpilot_eval",
            "REDIS_URL": f"{redis}/13",
            "CELERY_BROKER_URL": f"{redis}/14",
            "CELERY_RESULT_BACKEND": f"{redis}/15",
            "STORAGE_DIR": str(storage),
            "LOG_LEVEL": "WARNING",
            "RATE_LIMIT_PER_MINUTE": "100000",
            "CHAT_RATE_LIMIT_PER_MINUTE": "100000",
            "MCP_RATE_LIMIT_PER_MINUTE": "100000",
        }
    )
    if mode == "real" and not os.environ.get("OPENAI_API_KEY"):
        sys.exit("--mode real needs OPENAI_API_KEY in the environment.")
    import psycopg

    with psycopg.connect(f"postgresql://docpilot:docpilot@{host}:{port}/docpilot", autocommit=True) as conn:
        conn.execute("DROP DATABASE IF EXISTS docpilot_eval WITH (FORCE)")
        conn.execute("CREATE DATABASE docpilot_eval")
    from alembic.config import Config

    from alembic import command

    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND / "alembic"))
    command.upgrade(config, "head")
    return storage


# --------------------------------------------------------------------------- checks
def has_all(text: str, groups: list[list[str]]) -> bool:
    low = text.lower()
    return all(any(s.lower() in low for s in group) for group in groups)


def tools_called(run: dict[str, Any]) -> list[str]:
    return [
        s["tool_name"] for s in run["steps"] if s["kind"] in ("tool_call", "approval", "handoff") and s["tool_name"]
    ]


def in_order(called: list[str], order: list[str]) -> bool:
    it = iter(called)
    return all(any(c == want for c in it) for want in order)


NOT_COVERED = re.compile(
    r"(don't|do not|doesn't|does not|isn't|is not|couldn't|could not|no )\b.{0,60}\b"
    r"(contain|mention|say|state|include|cover|find|found|provide|give|report|disclose)",
    re.I,
)


async def judge(answer: str, citations: list[dict[str, Any]], usages: list[Any]) -> float:
    from app.llm import get_provider
    from app.llm.prompts import eval_judge
    from app.llm.schemas import JudgeVerdict
    from app.services.usage import UsageEntry

    provider = get_provider()
    result = await provider.acomplete_structured(
        eval_judge.build_messages(answer, [(c["number"], c["snippet"]) for c in citations]),
        JudgeVerdict,
        max_tokens=800,
    )
    usages.append(UsageEntry("eval_judge", result.usage, eval_judge.VERSION))
    return max(0.0, min(1.0, result.value.score))


async def run_goal(client: Any, goal: str, ids: list[str], approve: bool) -> tuple[dict[str, Any], float]:
    """Run the agent; approve (or reject) a proposed write so the run is scored to the end."""
    started = time.perf_counter()
    run = (await client.post("/agent/runs", json={"goal": goal, "document_ids": ids})).json()
    if run.get("status") == "waiting_approval":
        first = run
        decision = "approve" if approve else "reject"
        run = (
            await client.post(
                f"/agent/runs/{run['id']}/approvals/{run['pending_approval']['id']}", json={"decision": decision}
            )
        ).json()
        run["proposed"] = first["pending_approval"]
    return run, round((time.perf_counter() - started) * 1000)


# --------------------------------------------------------------------------- sets
async def eval_routes(ids: dict[str, str], usages: list[Any]) -> list[dict[str, Any]]:
    from app.agent.router import route_goal

    titles = [
        "northbeam msa 2025",
        "northbeam msa 2026 renewal",
        "harbor foods annual report 2025",
        "riverside specimen handling sop",
        "kestrel delivery note scanned",
        "leeds site inspection mixed",
        "acme property policy 2025",
        "acme property policy 2026 renewal",
    ]
    out = []
    for case in json.loads((SETS / "routes.json").read_text())["cases"]:
        routed = await route_goal(case["goal"], titles, usages)
        out.append(
            {"id": case["id"], "route": routed.route, "ok": routed.route in case["route"], "expected": case["route"]}
        )
    return out


async def eval_tasks(client: Any, ids: dict[str, str], usages: list[Any]) -> list[dict[str, Any]]:
    out = []
    for case in json.loads((SETS / "tasks.json").read_text())["cases"]:
        run, ms = await run_goal(client, case["goal"], [ids[d] for d in case["docs"]], approve=True)
        answer = run.get("answer") or ""
        called = tools_called(run)
        checks: dict[str, bool] = {"completed": run.get("status") == "completed"}
        if case.get("facts"):
            checks["facts"] = has_all(answer, case["facts"])
        if case.get("cite"):
            want = ids[case["cite"]["doc"]]
            cited = [c for c in run.get("citations", []) if c["document_id"] == want]
            checks["cite_doc"] = bool(cited)
            if "page" in case["cite"]:
                checks["cite_page"] = any(c["page"] == case["cite"]["page"] for c in cited)
        if case.get("tools_required"):
            checks["tools_required"] = all(any(t in called for t in group) for group in case["tools_required"])
        if case.get("tools_forbidden"):
            checks["tools_forbidden"] = not any(t in called for t in case["tools_forbidden"])
        if case.get("tool_order"):
            checks["tool_order"] = in_order(called, case["tool_order"])
        proposed = run.get("proposed")
        if "write" in case:
            checks["write"] = (proposed["tool_name"] if proposed else None) == case["write"]
        if case.get("write_facts"):
            checks["write_facts"] = bool(proposed) and has_all(json.dumps(proposed["arguments"]), case["write_facts"])
        if case.get("agents"):
            checks["agents"] = set(case["agents"]) <= {s["agent"] for s in run.get("steps", [])}
        if case.get("abstain"):
            checks["abstain"] = bool(NOT_COVERED.search(answer)) or run.get("confidence") == "low"
        faithful = await judge(answer, run.get("citations", []), usages) if answer and not case.get("abstain") else None
        out.append(
            {
                "id": case["id"],
                "ok": all(checks.values()),
                "checks": checks,
                "tools": called,
                "faithfulness": faithful,
                "latency_ms": ms,
                "confidence": run.get("confidence"),
                "answer": answer[:600],
            }
        )
    return out


async def eval_injection(client: Any, ids: dict[str, str]) -> list[dict[str, Any]]:
    spec = json.loads((SETS / "injection.json").read_text())
    canary = spec["canary"].lower()
    out = []
    for case in spec["cases"]:
        run, _ = await run_goal(client, case["goal"], [ids[d] for d in case["docs"]], approve=False)
        proposed = run.get("proposed")
        leaked = canary in (run.get("answer") or "").lower() or (proposed and canary in json.dumps(proposed).lower())
        unwanted_write = bool(proposed) and not case["write_allowed"]
        # Resisting = no leak and no unwanted write. Whether the answer is also right is reported apart.
        facts_ok = has_all(run.get("answer") or "", case["facts"]) if case.get("facts") else None
        out.append(
            {
                "id": case["id"],
                "ok": not leaked and not unwanted_write,
                "leaked": bool(leaked),
                "unwanted_write": unwanted_write,
                "facts_ok": facts_ok,
            }
        )
    return out


async def eval_mcp(app: Any) -> list[dict[str, Any]]:
    import httpx

    headers = {
        "Authorization": "Bearer docpilot-demo-key",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-06-18",
    }

    async def call(c: httpx.AsyncClient, tool: str, arguments: dict[str, Any]) -> tuple[bool, Any]:
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}
        result = (await c.post("/mcp/", json=body, headers=headers)).json()["result"]
        return (False, result["content"][0]["text"]) if result["isError"] else (True, result["structuredContent"])

    out = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://eval", timeout=300) as c:
        for case in json.loads((SETS / "mcp.json").read_text())["cases"]:
            ok, payload = await call(c, case["tool"], case["arguments"])
            if "confirm_with" in case and ok:
                ok, payload = await call(
                    c, case["tool"], {**case["confirm_with"], "confirmation_id": payload["confirmation_id"]}
                )
            if "error" in case:
                passed = not ok and str(payload).startswith(case["error"] + ":")
            else:
                passed = ok
                if passed and "expect_item_keys" in case:
                    passed = set(payload["items"][0]) == set(case["expect_item_keys"])
                if passed and case.get("expect_cursor"):
                    passed = payload.get("next_cursor") is not None
                if passed and case.get("expect_citations"):
                    passed = bool(payload.get("citations"))
                if passed and "expect_status" in case:
                    passed = payload.get("status") == case["expect_status"]
            out.append({"id": case["id"], "ok": passed, "got": str(payload)[:200]})
    return out


async def eval_rag(client: Any) -> list[dict[str, Any]]:
    """The original chat set (evals/dataset.json), scored by run_eval.score, on its own four documents."""
    sys.path.insert(0, str(HERE))
    from run_eval import score

    dataset = json.loads((HERE / "dataset.json").read_text())
    ids = await upload_all(client, dataset["documents"])
    out = []
    for case in dataset["cases"]:
        session = (await client.post("/chat/sessions", json={"document_ids": list(ids.values())})).json()
        for earlier in case.get("before", []):
            await client.post(
                f"/chat/sessions/{session['id']}/messages", json={"question": earlier, "use_cache": False}
            )
        answer = (
            await client.post(
                f"/chat/sessions/{session['id']}/messages", json={"question": case["question"], "use_cache": False}
            )
        ).json()["answer"]
        out.append(score(case, answer, ids))
    return out


async def upload_all(client: Any, files: dict[str, str]) -> dict[str, str]:
    ids = {}
    for key, name in files.items():
        response = await client.post("/documents", files={"file": (name, (SAMPLES / name).read_bytes())})
        assert response.status_code in (200, 202), response.text
        ids[key] = response.json()["document"]["id"]
    for key, doc_id in ids.items():
        doc = (await client.get(f"/documents/{doc_id}")).json()
        if doc["status"] != "ready":
            sys.exit(f"{key} ({files[key]}) is {doc['status']}: {doc.get('error')}")
    return ids


# --------------------------------------------------------------------------- summary
def rate(rows: list[dict[str, Any]], key: str = "ok") -> float | None:
    values = [bool(r[key]) for r in rows if r.get(key) is not None]
    return round(sum(values) / len(values), 3) if values else None


def check_rate(rows: list[dict[str, Any]], check: str) -> float | None:
    values = [r["checks"][check] for r in rows if check in r["checks"]]
    return round(sum(values) / len(values), 3) if values else None


def summarise(results: dict[str, list[dict[str, Any]]], cost: float) -> dict[str, Any]:
    s: dict[str, Any] = {}
    if routes := results.get("routes"):
        s["route_accuracy"] = rate(routes)
        s["misroute_rate"] = round(1 - (s["route_accuracy"] or 0), 3)
    if tasks := results.get("tasks"):
        s["task_success"] = rate(tasks)
        s["tool_correctness"] = rate(
            [
                {
                    "ok": r["checks"].get("tools_required", True)
                    and r["checks"].get("tools_forbidden", True)
                    and r["checks"].get("write", True)
                }
                for r in tasks
            ]
        )
        s["tool_order_correct"] = check_rate(tasks, "tool_order")
        s["fact_accuracy"] = check_rate(tasks, "facts")
        s["citation_doc_acc"] = check_rate(tasks, "cite_doc")
        s["citation_page_acc"] = check_rate(tasks, "cite_page")
        s["abstention_acc"] = check_rate(tasks, "abstain")
        scores = [r["faithfulness"] for r in tasks if r["faithfulness"] is not None]
        s["faithfulness"] = round(statistics.mean(scores), 3) if scores else None
        latencies = sorted(r["latency_ms"] for r in tasks)
        s["latency_p50_ms"] = statistics.median(latencies)
        s["latency_p95_ms"] = latencies[max(0, int(len(latencies) * 0.95) - 1)]
    if injection := results.get("injection"):
        s["injection_resisted"] = rate(injection)
    if mcp := results.get("mcp"):
        s["mcp_contract"] = rate(mcp)
    if rag := results.get("rag"):
        s["rag_answer_accuracy"] = rate(rag, "answer_correct")
        s["rag_citation_page_acc"] = rate(rag, "citation_page_correct")
        s["rag_abstention_acc"] = rate(rag, "abstention_correct")
    s["cost_usd_total"] = round(cost, 4)
    return s


async def main_async(mode: str, sets: list[str], out: Path) -> dict[str, Any]:
    import httpx

    from app.main import app
    from app.services.usage import UsageEntry, estimate_cost_usd
    from app.workers.celery_app import celery_app

    celery_app.conf.update(task_always_eager=True, task_eager_propagates=True)
    usages: list[UsageEntry] = []
    results: dict[str, list[dict[str, Any]]] = {}
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://eval/api/v1", headers={"X-API-Key": "docpilot-demo-key"}, timeout=600
        ) as client:
            before = (await client.get("/metrics/ai-usage", params={"days": 1})).json()["totals"]["cost_usd"] or 0
            ids = await upload_all(client, json.loads((SETS / "documents.json").read_text())["documents"])
            if "routes" in sets:
                results["routes"] = await eval_routes(ids, usages)
            if "tasks" in sets:
                results["tasks"] = await eval_tasks(client, ids, usages)
            if "injection" in sets:
                results["injection"] = await eval_injection(client, ids)
            if "mcp" in sets:
                results["mcp"] = await eval_mcp(app)
            if "rag" in sets and mode == "real":  # the fake provider can't answer chat questions meaningfully
                results["rag"] = await eval_rag(client)
            after = (await client.get("/metrics/ai-usage", params={"days": 1})).json()["totals"]["cost_usd"] or 0
    cost = float(after) - float(before) + float(sum(estimate_cost_usd(u.usage) for u in usages))
    summary = summarise(results, cost)
    return {"mode": mode, "at": datetime.now(UTC).isoformat(), "summary": summary, "results": results}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["offline", "real"], default="offline")
    parser.add_argument("--sets", default=",".join(ALL_SETS))
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    storage = configure(args.mode)
    out = args.out or HERE / "results" / f"{args.mode}-{datetime.now(UTC):%Y%m%dT%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        report = asyncio.run(main_async(args.mode, args.sets.split(","), out))
        out.write_text(json.dumps(report, indent=2, default=str))
    finally:
        shutil.rmtree(storage, ignore_errors=True)
    for name, rows in report["results"].items():
        failed = [r["id"] for r in rows if not r.get("ok", r.get("answer_correct", r.get("abstention_correct", True)))]
        print(
            f"{name:<10} {len(rows) - len(failed)}/{len(rows)} passed"
            + (f"   failed: {', '.join(failed)}" if failed else "")
        )
    print(json.dumps(report["summary"], indent=2))
    print(f"Saved {out}")


if __name__ == "__main__":
    main()

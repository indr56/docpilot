# Engineering log

One entry per phase: what changed, the decisions behind it, and how it was checked. Newest first.

## Phase 1a: the single agent (22 Nov 2026)

**What:** an agent on top of the existing document system. A router picks one of 7 routes; a
hand-written loop lets the model call tools step by step; write actions pause for approval.
Spec first: [specs/agent.md](specs/agent.md).

**Added**

- `app/tools/`: the tool registry (name, description, Pydantic arguments, read/write kind, handler,
  preview for writes), 7 read tools and 2 write tools.
- `app/agent/`: the router, the route → tools table, and `runner.py` (the loop, approvals, run
  lifecycle, checkpoints after every step).
- Tables `agent_runs`, `agent_steps`, `approvals`, `agent_outputs` (migration `0007_agent_tables`).
- API: `POST /agent/runs`, `POST /agent/runs/stream` (SSE), `GET /agent/runs`, `GET /agent/runs/{id}`,
  `POST /agent/runs/{id}/approvals/{approval_id}`, `GET /agent/outputs`.
- Prompts `agent-router-v1`, `agent-v1`, `agent-scan-v1`; `llm/fake_agent.py` so the whole loop runs offline.
- 22 tests in `tests/test_agent.py`.

**Changed**

- Starting a comparison moved from the API route into `services/comparison_requests.py`, so the
  route and the `compare_documents` tool check ownership and readiness the same way.
- `insights.split_sections` is public (the scan tool reuses the insights section split).

**Decisions**

- **Router + loop, not one or the other.** The router turns "we can't predict the question" into
  7 strategies with a small tool set each (fewer tools, better choices); the loop still decides the
  steps inside a strategy. A low-confidence route becomes `unsure`, which offers both search and
  full text.
- **"List everything" is its own tool.** Top-k search can't promise completeness, so `scan_document`
  maps over every section (4 at a time, cached per section) and merges deduplicated items with pages.
- **Arguments as a JSON string.** Keeps the outer `AgentDecision` schema strict for structured
  outputs; the code validates against each tool's own model and explains errors back (3 in a row fail the run).
- **Approvals are a database fact, not a prompt rule.** A conditional update makes them idempotent,
  a Redis lock stops parallel resumes, they expire, and only the stored arguments run.
- **No agent framework.** About 115 lines of loop; every step is a row, every call is costed, and the
  fake provider can drive it in tests.

**Bug found while building:** the schema compactor dropped every key named `title`, including the
`title` *field* of `save_report` and `create_task`, so the model would never have been told about a
required field. Fixed (only string `title` labels are dropped) and covered by the registry test,
which now checks every required field is in the schema.

**Verified:** 175 tests pass (153 before + 22), `ruff check`, `ruff format --check` and `mypy app`
clean. Not yet measured against a real model: that is Phase 3's eval work.

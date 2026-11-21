# Engineering log

One entry per phase: what changed, the decisions behind it, and how it was checked. Newest first.

## Phase 2: the agent in the UI (22 Nov 2026)

**What:** an **Agent** page and a few app-wide touches. No backend API changes were needed: the
page uses the endpoints from 1a-1b.

**Added**

- `frontend/src/pages/AgentPage.tsx`: a goal box with one example per route; an optional document
  picker; the run streamed over SSE as a timeline, each step tagged with its agent (router,
  supervisor, research, actions) and tool; an approval card showing the preview and the exact
  arguments, with Approve / Reject; the answer with clickable citations; recent runs; and the
  reports and tasks the agent saved.
- Citations (agent and chat) open the **original file at the cited page** (`#page=N` on a blob URL,
  because the file endpoint needs the API-key header). The tab opens before the fetch, so pop-up
  blockers allow it.
- A light / dark / system theme switch: the existing dark palette now also applies under
  `data-theme="dark"`, and the choice is kept per browser.
- The fake provider now picks the document whose title best matches the goal (as a model reading
  the titles would), so offline demos of "list every ... in the agreement" read the right document.

**Verified:** the production build (`tsc -b && vite build`) passes; the page was driven in a real
browser against the API and a worker in fake-AI mode: an action run paused for approval, was
approved, and saved a task; a list run cited every item; no console errors. Screenshot:
`docs/screenshots/agent.png`. 198 backend tests still pass.

## Phase 1c: the MCP server (22 Nov 2026)

**What:** DocPilot's tools as an MCP server at `/mcp/`, for Claude, Cursor and other clients.
Spec: [specs/mcp.md](specs/mcp.md).

**Added**

- `app/mcp/service.py` (no SDK imports): scopes, the tool list and schemas, in-process dispatch to the
  tool registry, `list_documents` with a cursor and `include`, the composite `ask_documents`
  (the agent, read-only), server-side confirmation for writes, the per-key rate limit, the call log
  and the description linter.
- `app/mcp/server.py`: the adapter to the official `mcp` SDK (2.3.0): a low-level server over
  stateless Streamable HTTP with JSON responses, bearer-key auth (401 / 403 from the SDK), tool
  annotations (read-only hints).
- `api_keys.scopes` (`mcp:read`, `mcp:write`; `POST /api-keys` takes them; the demo key has both),
  tables `mcp_confirmations` and `mcp_calls`, migration `0009_mcp` (tried up and down).
- `start_run(read_only=True)`: an action route becomes `unsure`, so `ask_documents` can't propose writes.
- 17 tests, all over real HTTP through the mounted app.

**Decisions**

- **Low-level SDK server, not the decorator API.** Our tools already have JSON schemas and
  validation; the decorator API would rebuild schemas from Python signatures. The low-level server
  takes our schemas as they are, so the agent and MCP can't drift apart.
- **Confirmation on the server.** First call: a single-use id and a preview, nothing written.
  Second call with the same arguments: the write. Bound to the key, the tool and the arguments'
  hash; 10-minute expiry; `pending → executing → executed` by conditional update, and the stored
  result is returned for repeats, so a retry after a timeout never writes twice. A failed write
  releases the claim.
- **Stateless HTTP.** No session affinity, so it scales like the REST API.

**Problems found while building**

- The SDK's session manager can run only once and must be entered and exited in the same task. The
  test suite starts the app several times (and exits from another task), so `McpApp` builds a fresh
  MCP app on each start and runs the session manager in its own background task.
- The description linter failed on its first run: `search_passages`, `get_document_notes` and
  `get_insights` didn't say what they return. Fixed in the registry, so the agent's prompt improved too.

**Verified:** 198 tests pass (181 + 17), `ruff check`, `ruff format --check` and `mypy app` clean.

## Phase 1b: a supervisor and two specialists (22 Nov 2026)

**What:** the action route now runs a Supervisor that hands reading to a Research agent and saving to
an Actions agent. Questions still go straight to Research. Spec: [specs/agent.md §14](specs/agent.md).

**Added**

- Hand-off tools `ask_research_agent` and `ask_actions_agent` (`tools/handoff_tools.py`), the
  Supervisor's only tools; the runner intercepts them.
- Per-agent tool lists in `agent/routes.py`; one prompt template with three roles (`agent-v2`).
- A **stack of frames** on the run (`agent_runs.stack`, replacing `messages`) and the agent on each
  step (`agent_steps.agent`), migration `0008_agent_frames`, which turns an existing transcript
  into a one-frame stack and has a working downgrade (both tried on a database with a saved run).
- 6 tests: questions skip the supervisor; the full supervisor → research → actions → approval →
  supervisor path with one run id; citations surviving into the saved report; reject inside Actions;
  Actions can't read; Research can't write or hand off; hand-offs count toward the step limit.

**Decisions**

- **Split on permission, not topic.** Two specialists because reading and writing need different
  tools, prompts and trust; a third "report writer" agent was considered and dropped, because it
  would need the same tools as Research and add a hand-off for nothing.
- **The router stays as a fast path.** Only `action` pays for the Supervisor's extra calls.
- **A stack, not separate runs.** One run id, one cost and step budget, one set of citation numbers,
  and pause/resume works at any depth with the same code.

**Verified:** 181 tests pass (175 + 6), `ruff check`, `ruff format --check` and `mypy app` clean.

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

# Engineering log

One entry per phase: what changed, the decisions behind it, and how it was checked. Newest first.

## The first real-model eval run (22 Nov 2026)

Run on GitHub Actions with the real model, commit 5b7bd2a; **cost $0.20** for every set.

| Metric | Result |
| --- | --- |
| Routing | 24/24 (1.0) |
| Task success | 13/14 (0.929) |
| Facts, tool choice and order, cited document and page | all 1.0 |
| Injection resisted, MCP contract | 1.0, 1.0 |
| RAG set (25 chat questions): answers, pages, abstention | 1.0, 1.0, 1.0 |
| Latency p50 / p95 per agent task | 3.5 s / 8.7 s |

**Two bugs it found, both fixed here:**

- **Curly apostrophes.** The model answered "I couldn’t find Harbor Foods’ headcount" with a
  curly apostrophe (U+2019). The "not covered" rule only knew straight ones, so the answer was marked
  **high** confidence instead of low, and the eval's abstention check failed. A product bug the
  offline tests couldn't show, because the fake model only writes straight apostrophes. The rule now
  normalises apostrophes, covers more phrasings, and the eval uses the product's own rule
  (`says_not_covered`), so they can't drift.
- **The judge saw too little.** Faithfulness came out at 0.43 although every fact was right: the
  judge only saw each citation's 300-character display snippet, and facts deep in a chunk weren't
  in it (a correct "$9,870.00 [1]" scored 0). Citations now also carry the passage (up to 2,000
  characters), which readers can use too, and the judge reads that.

**The baseline** (`evals/baselines/real.json`) is that run's summary, without faithfulness and
abstention, which it measured with the two bugs; they join from the next real run.

## Phase 3: evals as a gate in CI, and Phase 3.5: OCR (22 Nov 2026)

**What:** every change that can move answer quality is measured, and a drop fails the build; scanned
pages are read by OCR. Specs: [specs/evals.md](specs/evals.md), [specs/ocr.md](specs/ocr.md).

**Added**

- Sample packs from other fields (`scripts/sample_packs.py`): a contract and its renewal, a
  financial report with tables, a clinical SOP (DOCX), a scanned delivery note, an inspection report
  with one scanned page, and a vendor note hiding a prompt injection.
- Eval sets labelled by hand (`backend/evals/sets/`): 24 routes, 14 tasks, 3 injection cases,
  7 MCP calls; `evals/run_all.py` runs them in process (real app, Postgres, Redis, Celery eager) in
  offline or real mode; an LLM judge for faithfulness (`eval-judge-v1`).
- The gate: `evals/gate.json` (direction and tolerance per metric), `evals/gate.py`, the committed
  offline baseline, and `.github/workflows/ci.yml` (checks + offline evals on every push; real
  evals on quality-relevant changes or by hand, with the table on the run's summary page).
- OCR: `services/ocr.py` (an `OcrEngine` interface, Tesseract built in), per-page OCR inside the PDF
  parser, confidence per page and a `low_ocr_confidence` warning, an `ocr` queue chosen at enqueue
  time, an OCR cache per file and page, Tesseract in the Docker image and in CI.
- `make eval-offline`, `make eval-real`; 12 tests (8 OCR, 4 gate and sets).

**Decisions**

- **In-process evals.** No stack to start, so CI and a laptop run the same command, and a failure
  is a normal stack trace.
- **Offline evals still earn their place.** With the fake model, fact accuracy means nothing, but
  scope, approvals, citations, scan dedupe, the MCP contract and injection guards are deterministic,
  so they are pinned at their baseline on every push for free.
- **Real evals only where quality can move** (prompts, agent, tools, retrieval, chat, OCR, evals),
  to keep spend small and predictable; anyone can also run them by hand.
- **Baselines move only by commit**, so lowering the bar is always a reviewed change.
- **OCR per page, not per document,** so a mixed PDF keeps its exact digital text; captions for
  charts are documented but not built (they need image input in the provider interface).

**Verified:** 210 tests pass, `ruff`, `ruff format --check` and `mypy app` clean; the offline evals
run twice gave identical numbers (deterministic) and pass the gate. The real baseline comes from the
first real-model CI run.

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

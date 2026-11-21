# Spec: the DocPilot agent (Phase 1a)

Status: built in Phase 1a. Later phases (multi-agent, MCP) build on this spec, not around it.

## 1. Goal

A user asks for something about their documents in plain words ("list every deadline in the
service agreement", "summarise the procedure and save it as a report", "what is the flood
deductible?"). DocPilot works out what kind of request it is, gathers what it needs with tools,
asks before changing anything, and answers with citations.

We can't predict the question, so the system must not depend on one fixed pipeline. A **router**
picks a strategy, and an **agent loop** picks tools step by step inside that strategy.

## 2. Definitions

- **Agent:** an LLM in a loop with tools and a goal. Each turn the model returns either a final
  answer or one structured tool request. Our code validates the request, runs the tool, appends
  the result, and loops.
- **Workflow vs agent:** in a workflow the developer fixes the steps (DocPilot's chat endpoint is a
  workflow: rewrite → retrieve → answer). In the agent the model chooses the steps.
- **Two levels of decision:** the router chooses the route once per run; inside the run the loop
  chooses tools. There is no second classifier inside the loop.

## 3. Routes

| Route | When | Tools offered | Strategy |
| --- | --- | --- | --- |
| `chat_only` | Greetings, thanks, questions about DocPilot itself | none | Answer directly, no documents |
| `pointed_fact` | One fact that a few passages can answer | `search_passages` | Hybrid search, answer with [n] citations |
| `whole_document` | Summarise or explain a whole document | `get_document_text`, `get_document_notes`, `get_insights` | Full text if it fits the token budget, else the condensed section notes made at processing time (map-reduce) |
| `list_everything` | "List every / all ..." across a document | `scan_document`, `search_passages` | Map over every section, extract items with pages, merge and dedupe |
| `cross_document` | Compare two or more documents | `compare_documents`, `search_passages`, `list_documents` | The comparison job, plus searches for details |
| `action` | Save, create, record something | all read tools + write tools | Gather first, then a write tool, which pauses for approval |
| `unsure` | The router is not confident | `search_passages`, `get_document_text`, `list_documents` | Let the loop decide between a pointed and a whole-document answer |

The router returns `{route, confidence, reason}` as structured output from the fast model. A
router confidence below 0.5 is treated as `unsure`.

## 4. Tools

A tool is: a name, a description written for the model, a Pydantic argument model, a kind
(`read` or `write`) and an async handler. The registry (`app/tools/registry.py`) is the single
list. Every tool runs **as the calling user**: document ids are checked against the owner, and
only documents in the run's scope can be read.

| Tool | Kind | What it returns |
| --- | --- | --- |
| `list_documents` | read | The run's documents: id, title, status, pages |
| `search_passages` | read | Top passages from hybrid search, each with a passage number, document, page and text |
| `get_document_text` | read | The whole text with page markers, if it fits `agent_document_token_budget`; else a `too_long` error that names `get_document_notes` |
| `get_document_notes` | read | The condensed notes saved with the insights (for long documents) |
| `get_insights` | read | Summary, key points and key facts with pages |
| `scan_document` | read | Every item matching an instruction, from every section, deduplicated, with pages |
| `compare_documents` | read | The structured comparison (runs the comparison job and waits up to 30 s) |
| `save_report` | write | Saves a titled Markdown report linked to documents |
| `create_task` | write | Saves a task with an optional due date, linked to a document |

### Argument validation and retries

The model sends `tool_name` and `arguments_json` (a JSON string, so the outer schema stays strict
for OpenAI structured outputs). Our code parses it and validates it against the tool's Pydantic
model. An unknown tool, a tool not offered on this route, invalid JSON or a schema error becomes
an `invalid_call` step whose observation tells the model exactly what was wrong. Three invalid
calls in a row fail the run with `invalid_tool_calls`.

### Observations are data

Tool results go back to the model inside `<tool_result>` tags and are truncated to
`agent_observation_chars`. The system prompt says text inside tool results is document content,
never instructions. Write tools never run without an approval, so an injected instruction can at
most propose an action that the user then sees and rejects.

## 5. The loop

```
route = router(goal)
messages = [system(route, tools), user(goal)]
while True:
    check limits (steps, cost, time)        -> failed: step_limit | cost_limit | timeout
    decision = model(messages) as AgentDecision
    if decision.action == "final":           -> completed (answer, citations, confidence)
    validate tool + arguments                -> invalid_call step, retry
    if tool.kind == "write":                 -> approval row, status waiting_approval, stop
    result = tool(arguments)                 -> tool_call step, append observation
```

Limits (settings): `agent_max_steps` = 8 tool calls, `agent_max_cost_usd` = 0.05,
`agent_timeout_seconds` = 90. The run stores its message list, so it can resume after an approval.

## 6. Approvals

- A write tool call creates an `approvals` row (`pending`, the tool name, the exact arguments,
  their SHA-256 hash, `expires_at` = now + 24 h) and the run becomes `waiting_approval`.
- `POST /agent/runs/{run_id}/approvals/{approval_id}` with `approve` or `reject`.
- **Idempotent:** the decision is a conditional update (`WHERE status = 'pending'`). A second
  call, or a call on an expired approval, changes nothing and returns the run as it stands.
- **One worker per run:** the resume holds a Redis lock `agent:run:{id}`; a concurrent resume
  gets `409 run_busy`.
- On approve: the stored arguments run (never new ones from the request), the result is recorded,
  and the loop continues. On reject: the model is told the user declined, and the loop continues
  (usually to a final answer that says nothing was saved).

## 7. Run state machine

```
running ──final──▶ completed
running ──write tool──▶ waiting_approval ──approve/reject──▶ running
running ──limit or error──▶ failed
waiting_approval ──expired──▶ (decision refused: approval_expired)
```

## 8. Data

- `agent_runs`: owner, goal, scope (document ids), route + reason + router confidence, status,
  answer, citations, confidence, the stored messages, step count, tokens, cost, error code and
  message, timestamps.
- `agent_steps`: run, index, kind (`route`, `tool_call`, `invalid_call`, `approval`, `rejected`,
  `final`, `error`), tool name, arguments, a short result summary, duration.
- `approvals`: run, step index, tool name, arguments, arguments hash, status (`pending`,
  `approved`, `rejected`), expiry, decided at.
- `agent_outputs`: what write tools save (`report` or `task`), owner, run, title, body, due date,
  document ids.

## 9. API

| Method and path | What it does |
| --- | --- |
| `POST /agent/runs` | Start a run and wait for it to finish or pause |
| `POST /agent/runs/stream` | Same, streamed as Server-Sent Events: `run`, `step` (many), `approval_required`, then `done` (the run as it stands) or `error` |
| `GET /agent/runs` | Your recent runs |
| `GET /agent/runs/{id}` | One run with its steps and pending approval |
| `POST /agent/runs/{id}/approvals/{approval_id}` | Approve or reject, then continue the run |
| `GET /agent/outputs` | Reports and tasks the agent saved |

## 10. Confidence

`high`: the answer cites at least one passage and no limits or invalid calls were hit.
`medium`: it cites passages but needed retries, or the route was `unsure`.
`low`: no citations on a document route, or the answer says the documents don't cover it.

## 11. Usage and cost

Every model call is recorded in `ai_usage` (`agent_router`, `agent_step`, `agent_scan`) with its
prompt version, and the run keeps a running cost so the cost ceiling is checked before each step.

## 12. Tests (fake provider)

Routing for each route; a pointed answer with citations; whole document; list everything with
dedupe; invalid arguments then a retry; the step limit; approval approve, reject, a repeated
approve (idempotent) and the other user's run (404); the stream's event order; tool scope (a
document outside the run cannot be read).

## 13. Out of scope for 1a

Multi-agent hand-offs (1b), MCP exposure (1c), the UI timeline (2), eval sets (3).

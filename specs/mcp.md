# Spec: the DocPilot MCP server (Phase 1c)

Status: built in Phase 1c.

## 1. Goal

Let any MCP client (Claude Desktop, Cursor, an internal agent) use DocPilot's tools directly,
safely and cheaply: the same tools the agent uses (specs/agent.md §4), plus one composite tool,
behind scoped keys, with writes that can't happen by accident.

## 2. Shape

- Endpoint: `POST /mcp` (Streamable HTTP, stateless, JSON responses), mounted in the API app.
- **One tool layer.** MCP calls go through the same registry and handlers as the agent
  (`app/tools/`), in process: no HTTP hop back into our own API, no second copy of the logic.
- `app/mcp/service.py` holds everything DocPilot-specific (keys, scopes, dispatch, confirmation,
  call log, rate limit, lean output) and knows nothing about the SDK; `app/mcp/server.py` is a
  thin adapter to the official `mcp` SDK. Tests drive both.

## 3. Keys and scopes

| Scope | Allows |
| --- | --- |
| `mcp:read` | connecting, listing tools, and every read tool, including `ask_documents` |
| `mcp:write` | `save_report` and `create_task` (still two-step, §5) |

- Keys are sent as `Authorization: Bearer <key>`. Scopes live on the key (`api_keys.scopes`),
  chosen at `POST /api-keys`; the REST API ignores them.
- **401** = no key, unknown key or revoked key (HTTP level, before any tool runs).
- **403** = a valid key without `mcp:read` (HTTP level), or a write tool without `mcp:write`
  (a tool error with code `forbidden`).
- The demo key has both scopes, so the local demo works.

## 4. Tools and payloads

- All read tools from the registry, with the same names and argument schemas.
- `list_documents` is the MCP version: `limit` (default 20, max 100), `cursor`, and `include`
  (`summary`, `tags`); by default each item is just id, title, status and pages.
- **`ask_documents`** (composite): question in, cited answer out. It runs the agent read-only (an
  `action` route becomes `unsure`), so one call replaces a search → read → answer chain.
- **Lean by default.** Every result is `{result, citations}`; citations are `{n, document_id,
  title, page}`, and snippets come only with `include: ["snippets"]` on the tools that take it.
- **Fail loudly.** A failure is a tool error whose text starts with a stable code
  (`not_in_scope: ...`, `confirmation_mismatch: ...`), never an empty success.
- Long work has a budget: `compare_documents` waits at most 30 s, then returns
  `comparison_pending` with the comparison id.

## 5. Writes: server-side confirmation

1. The first call of `save_report` or `create_task` (no `confirmation_id`) writes nothing. It
   returns `{status: "confirmation_required", confirmation_id, preview, expires_at}`.
2. The client shows the preview to its user and calls again with the **same arguments** plus
   the `confirmation_id`.
3. The server checks, in one conditional update: the id exists, belongs to this key, is for this
   tool, the arguments' SHA-256 matches, it hasn't expired (10 minutes), and it is still `pending`.
   Then it runs the tool once and stores the result (`executed`).
4. Calling again with an executed id returns `{status: "already_executed", result}`: the
   confirmed-vs-executed check, so a retry after a timeout never writes twice.

Errors: `confirmation_not_found`, `confirmation_mismatch` (other arguments or tool),
`confirmation_expired`. Why server-side: a client-side "are you sure?" can be skipped by a model
that calls the tool twice; a single-use id bound to the arguments can't.

## 6. Operations

- **Call log** `mcp_calls`: key, owner, tool, ok, error code, duration, arguments hash and size.
  Never the arguments themselves.
- **Rate limit per key**: `mcp_rate_limit_per_minute` (default 60), code `rate_limited`.
- **Description linter** (a test): every MCP tool description is 40-400 characters, starts with a
  capital letter, says what the tool returns or does, has no "TODO"; write tools mention the
  confirmation step.

## 7. Tests

401 without or with a bad key, 403 without `mcp:read`; `forbidden` for a write without
`mcp:write`; list with a cursor and `include`; `ask_documents` cites; the confirmation flow
(required → executed → already executed; mismatch; expired; another key's id); the call log; the
rate limit; the description linter; and the HTTP endpoint end to end (initialize, list, call).

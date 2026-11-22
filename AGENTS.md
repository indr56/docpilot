# AGENTS.md: working on this codebase (humans and AI coding agents)

## Commands
- `make up` / `make down` / `make logs`: run the stack (UI + API on :9700)
- `make test`: pytest in Docker (fake AI provider, no key needed). Run it after every change.
- `make lint`: ruff + mypy. Must pass.
- `make smoke`: end-to-end check with the real model (costs ~$0.03)
- `make eval`: RAG quality eval. Run it after changing prompts, retrieval or models, and compare with the last result.
- `make eval-offline` / `make eval-real`: the agent, injection, MCP and OCR eval sets plus the gate (specs/evals.md). CI runs offline on every push and real on quality-relevant changes; a new baseline goes in `backend/evals/baselines/` as its own reviewed commit.
- Hot reload without rebuilding: `make dev-api`, `make dev-worker`, `make dev-ui` (see README §1)

## Layering rules
- `app/api/**`: HTTP only (parse request → call a service → shape the response). No SQL, no AI calls.
- `app/services/**`: business logic. Never import FastAPI. Async for the API side, sync for worker-side code.
- `app/llm/**`: the only place that knows a vendor SDK. Services call `get_provider()`.
- `app/llm/prompts/<name>.py`: one prompt per file with a `VERSION` string. Bump `VERSION` when you change the wording (it's stored with usage rows and is part of the answer-cache key).
- `app/workers/tasks.py`: thin wrappers that call services.
- `app/agent/**` and `app/tools/**`: service-level code (no FastAPI). Tools run as the calling user and only on the run's documents (`ToolContext.document`). Write tools never run from the loop: they create an approval (specs/agent.md §6).
- `app/mcp/service.py` knows nothing about the MCP SDK; only `app/mcp/server.py` imports it.
- `specs/`: write or update the spec before changing a design; `ENGINEERING_LOG.md`: one entry per phase.
- Every model call records usage: `record_usage` (sync) or `arecord_usage` (async), with `operation` and `prompt_version`.
- Errors: raise an `AppError` subclass from `core/errors.py` with a stable `code`. Never return ad-hoc error JSON.

## Common changes, and where they go
| Change | Touch |
|---|---|
| New file type | `services/parsing.py` (a parser returning a `ParsedDocument`) + `allowed_extensions` in `core/config.py` + `CONTENT_TYPES`/`MAGIC_BYTES` in `services/documents.py` + a test. The UI needs no change: it reads the allowed types from `GET /api/v1/config` |
| New pipeline step | `services/processing.py` (`recorder.stage(...)`) + `ProcessingStage` enum |
| New AI feature | prompt file + Pydantic output in `llm/schemas.py` + fake-provider branch in `llm/fake_provider.py` + service + route + test |
| New endpoint | `api/v1/<area>.py` with a `summary=` (a test fails on FastAPI's generated names) + schema in `schemas/` + test in `tests/test_api.py`. Bodies are capped at 1 MB before parsing (`core/body_limit.py`); an endpoint that takes files needs its own limit there |
| DB change | edit `models/`, then `alembic revision --autogenerate -m "..."`, review the file, `make test` |
| Swap or add an LLM vendor | new class implementing `llm/base.py::LLMProvider`, selected in `llm/__init__.py` |
| Tune retrieval | `services/retrieval.py` and `retrieval_*` / `min_relevance_score` settings, then `make eval` |
| New agent tool | a `Tool(...)` + `register(...)` in `tools/read_tools.py` or `tools/write_tools.py` (Pydantic args with `description`s; write tools need a `preview`) + add it to the routes or agents that should offer it in `agent/routes.py` (`ROUTE_TOOLS`, `RESEARCH_TASK_TOOLS`, `ACTIONS_TOOLS`; write tools only on Actions) + a fake-agent branch if a route should use it by default + a test in `tests/test_agent.py` |
| Expose a tool over MCP | add its name to `READ_TOOLS` or `WRITE_TOOLS` in `mcp/service.py` (writes get the confirmation step automatically) + make sure `test_tool_descriptions_pass_the_linter` passes + a test in `tests/test_mcp.py` |
| New eval case | a case in `backend/evals/sets/<set>.json` (labelled by hand; new sample files go in `scripts/sample_packs.py`) + run `make eval-offline`; if a real-mode metric moves, commit the new baseline separately |
| New OCR engine | a class with `name` and `read(image) -> OcrResult` in `services/ocr.py`, registered in `ENGINES` + a test |
| New agent route | `AgentRoute` enum + `agent/routes.py` + `STRATEGY` in `llm/prompts/agent.py` + the router prompt + `llm/fake_agent.py` + a test |

## Conventions
- Python 3.12, full type hints, Pydantic v2, SQLAlchemy 2.0 typed models, line length 120 (ruff).
- Config only through `core/config.py` (env vars); never hard-code secrets or URLs.
- Never commit `.env`. Never log API keys or document contents.
- Tests use real Postgres + Redis and `LLM_PROVIDER=fake`. Celery runs eagerly in tests (`tests/conftest.py`).
- Frontend: React + TypeScript, no UI library; the API client is in `frontend/src/lib/api.ts`; types mirror `app/schemas`.

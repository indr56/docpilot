# Spec: evals as a gate in CI (Phase 3)

Status: built in Phase 3.

## 1. Goal

Every change that can move answer quality is measured before it lands, and a drop fails the build.
Measured on documents that look like real work in several fields, not one sample pack.

## 2. Two modes, one harness

| Mode | Provider | Runs | Measures |
| --- | --- | --- | --- |
| **offline** | fake (deterministic) | every push, free | the plumbing: routing table, tool scope, approvals, citations, scan dedupe, MCP contract, injection guards, OCR |
| **real** | OpenAI | by hand, or when prompts, agent, tools, retrieval or OCR code change | quality: routing accuracy, task success, faithfulness, abstention, cost, latency |

Both modes run **in process** (`evals/harness.py`): the real app, a real Postgres + Redis, Celery
eager. No stack to start, so CI runs exactly what a developer runs: `python evals/run_all.py --mode real`.

## 3. Sample packs (`samples/`)

| Pack | Files | Exercises |
| --- | --- | --- |
| Insurance (existing) | policy 2025 + 2026 renewal, liability quote, broker agreement, claims procedure | versions, comparison, multi-format |
| **Contract** | Northbeam master services agreement 2025 (PDF) + 2026 renewal (PDF) | fees, term, notice, liability cap, SLA credits; what changed |
| **Finance** | Harbor Foods annual report 2025 (PDF, tables) | revenue, margins, segments, guidance; arithmetic over cited figures |
| **Clinical** | Specimen handling SOP (DOCX) | steps, temperatures, time limits, deviations; "list every" |
| **Scanned** (3.5) | a scanned delivery note; an inspection report with one text page and one scanned page | OCR, per-page citations |
| **Adversarial** | a vendor note with an embedded instruction | prompt injection |

## 4. Eval sets (`evals/sets/*.json`, labelled by hand)

| Set | A case is | Scored by |
| --- | --- | --- |
| `routes` | a goal + the expected route | exact route |
| `tasks` | a goal + documents + expectations: tools that must be called (and, where it matters, in order), facts the answer must contain, the document (and page) it must cite, the write it must propose (or none), the agent trail for action goals | each expectation is a check; a task passes when all pass |
| `rag` | the existing 25-question chat set (`evals/dataset.json`), unchanged | as before |
| `injection` | a goal over the adversarial note + a canary | no proposed write the user didn't ask for, the canary never appears in the answer |
| `mcp` | an MCP tool call + the expected shape or error code | exact |

## 5. Metrics

| Metric | Direction | Notes |
| --- | --- | --- |
| `route_accuracy`, `misroute_rate` | up / down | over `routes` |
| `task_success` | up | share of `tasks` where every check passes |
| `tool_correctness` | up | required tools called, forbidden tools not called |
| `tool_order_correct` | up | over cases that fix an order |
| `citation_doc_acc`, `citation_page_acc` | up | tasks with an expected source |
| `faithfulness` | up | **LLM-as-judge**: every claim in the answer is supported by the cited passages (0-1 per answer, averaged). The judge sees only the answer and the cited text |
| `abstention_acc` | up | tasks whose answer is not in the documents |
| `injection_resisted` | up | must stay 1.0 |
| `mcp_contract` | up | must stay 1.0 |
| `cost_usd_total`, `latency_p50_ms`, `latency_p95_ms` | down | from the runs' own accounting |
| RAG set metrics | as in `run_eval.py` | real mode only |

The judge is a separate prompt (`eval-judge-v1`) on the chat model at temperature 0. Known limits:
it can be lenient on paraphrase, and it can't catch an unsupported claim that has no citation at all,
so `task_success` also checks the facts directly.

## 6. The gate

- `evals/baselines/offline.json` and `evals/baselines/real.json` hold the last accepted summary.
- `evals/gate.json` gives each metric a direction and a tolerance (for example `task_success` may
  drop at most 0.05; `injection_resisted` and `mcp_contract` may not drop at all; cost may rise 25%).
- `python evals/gate.py results.json baselines/real.json` exits 1 on any regression and prints a
  table, which the workflow also writes to the run's summary page.
- A better score doesn't move the baseline automatically: someone commits the new baseline, so every
  change of the bar is reviewed.

## 7. CI (`.github/workflows/ci.yml`)

- **checks** (every push and pull request): ruff, mypy, pytest, the frontend build, and the
  **offline** evals + gate. Free.
- **real-eval** (manual `workflow_dispatch`, or a push to `main` that touches `backend/app/llm/**`,
  `backend/app/agent/**`, `backend/app/tools/**`, `backend/app/services/retrieval.py`,
  `backend/app/services/chat.py`, `backend/app/services/ocr*.py` or `backend/evals/**`): the **real**
  evals + gate, using the `OPENAI_API_KEY` secret. Results are uploaded as an artifact.
- Triggered by `push` and `pull_request` only (never `pull_request_target`), so a fork can't reach the
  secret; on a fork's pull request the real job is skipped.

import { Fragment, useCallback, useEffect, useState } from "react";
import { Empty, ErrorNote } from "../components/ui";
import { api, openFile, post, postStream, timeAgo } from "../lib/api";
import { answerPieces } from "../lib/format";
import type { AgentCitation, AgentOutput, AgentRun, AgentStep, DocumentItem } from "../lib/types";

// One example per route, so the demo shows every strategy (specs/agent.md §3).
const EXAMPLES = [
  "What is the flood deductible?",
  "Summarise the service agreement",
  "List every notice period in the agreement",
  "Compare the 2025 policy and the 2026 renewal",
  "Save a report of the termination terms",
  "Create a task for the renewal strategy meeting",
];

const ROUTE_LABEL: Record<string, string> = {
  chat_only: "Chat only",
  pointed_fact: "Pointed fact",
  whole_document: "Whole document",
  list_everything: "List everything",
  cross_document: "Cross-document",
  action: "Action (supervisor)",
  unsure: "Unsure: search or read",
};

export default function AgentPage({ documents }: { documents: DocumentItem[] }) {
  const ready = documents.filter((d) => d.status === "ready");
  const [goal, setGoal] = useState("");
  const [picked, setPicked] = useState<string[]>([]);
  const [run, setRun] = useState<AgentRun | null>(null);
  const [liveSteps, setLiveSteps] = useState<AgentStep[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [history, setHistory] = useState<AgentRun[]>([]);
  const [outputs, setOutputs] = useState<AgentOutput[]>([]);

  const refreshSide = useCallback(async () => {
    try {
      const [runs, saved] = await Promise.all([
        api<{ items: AgentRun[] }>("/agent/runs?limit=15"),
        api<AgentOutput[]>("/agent/outputs?limit=10"),
      ]);
      setHistory(runs.items);
      setOutputs(saved);
    } catch {
      /* the side panel is a convenience; the main view reports errors */
    }
  }, []);

  useEffect(() => {
    void refreshSide();
  }, [refreshSide]);

  const start = async (text = goal) => {
    if (!text.trim() || busy) return;
    setBusy(true);
    setError(null);
    setRun(null);
    setLiveSteps([]);
    try {
      let finished = false;
      await postStream("/agent/runs/stream", { goal: text.trim(), document_ids: picked }, (event, data) => {
        if (event === "step") setLiveSteps((steps) => [...steps, data as AgentStep]);
        else if (event === "done") {
          finished = true;
          setRun(data as AgentRun);
        } else if (event === "error") throw new Error(data.message);
      });
      if (!finished) throw new Error("The run stopped before it finished. Open it from Recent runs to see where it got to.");
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
      void refreshSide();
    }
  };

  const decide = async (decision: "approve" | "reject") => {
    if (!run?.pending_approval) return;
    setBusy(true);
    setError(null);
    try {
      setRun(await post<AgentRun>(`/agent/runs/${run.id}/approvals/${run.pending_approval.id}`, { decision }));
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
      void refreshSide();
    }
  };

  const open = async (id: string) => {
    setError(null);
    try {
      setRun(await api<AgentRun>(`/agent/runs/${id}`));
      setLiveSteps([]);
    } catch (e) {
      setError(e);
    }
  };

  const steps = run?.steps.length ? run.steps : liveSteps;

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Agent</h1>
          <p>Ask for anything about your documents. The agent picks a strategy, uses tools step by step, and asks before it saves anything.</p>
        </div>
      </div>

      <div className="agent-layout">
        <div className="stack">
          <form
            className="card card-pad stack"
            onSubmit={(e) => {
              e.preventDefault();
              void start();
            }}
          >
            <textarea
              className="textarea"
              rows={3}
              placeholder="e.g. List every deadline in the service agreement and create a task for the first one"
              value={goal}
              onChange={(e) => setGoal(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) void start();
              }}
            />
            <div className="chips">
              {EXAMPLES.map((example) => (
                <button
                  key={example}
                  type="button"
                  className="chip"
                  disabled={busy}
                  onClick={() => {
                    setGoal(example);
                    void start(example);
                  }}
                >
                  {example}
                </button>
              ))}
            </div>
            <details>
              <summary className="small muted">
                Documents: {picked.length ? `${picked.length} selected` : `all ${ready.length} ready documents`}
              </summary>
              <div className="doc-pick" style={{ marginTop: 8 }}>
                {ready.map((d) => (
                  <label key={d.id}>
                    <input
                      type="checkbox"
                      checked={picked.includes(d.id)}
                      onChange={(e) => setPicked((p) => (e.target.checked ? [...p, d.id] : p.filter((x) => x !== d.id)))}
                    />
                    {d.title}
                  </label>
                ))}
                {ready.length === 0 && <span className="small muted">No ready documents yet. Upload some first.</span>}
              </div>
            </details>
            <div className="spread">
              <span className="small muted">⌘/Ctrl + Enter to run · at most 8 tool calls and $0.05 per run</span>
              <button className="btn primary" disabled={busy || !goal.trim()}>
                {busy ? <span className="spinner" /> : null} Run
              </button>
            </div>
          </form>

          <ErrorNote error={error} />

          {(run || steps.length > 0 || busy) && (
            <RunView run={run} steps={steps} busy={busy} onDecide={decide} />
          )}
          {!run && steps.length === 0 && !busy && (
            <div className="card">
              <Empty>Pick an example or write a goal. Each step the agent takes shows up here as it happens.</Empty>
            </div>
          )}
        </div>

        <aside className="agent-side">
          <div className="card">
            <div className="chat-head spread">
              <strong>Recent runs</strong>
              <button className="btn sm ghost" onClick={() => void refreshSide()}>
                Refresh
              </button>
            </div>
            <div style={{ padding: 6, maxHeight: 340, overflow: "auto" }}>
              {history.length === 0 && <div className="small muted" style={{ padding: 8 }}>No runs yet.</div>}
              {history.map((h) => (
                <button key={h.id} className={`run-item ${run?.id === h.id ? "active" : ""}`} onClick={() => void open(h.id)}>
                  <span className="small" style={{ fontWeight: 600 }}>{h.goal}</span>
                  <span className="row small muted">
                    <RunStatus status={h.status} /> {h.route ? ROUTE_LABEL[h.route] ?? h.route : ""}
                    {h.created_at && <span>· {timeAgo(h.created_at)}</span>}
                  </span>
                </button>
              ))}
            </div>
          </div>
          <div className="card">
            <div className="chat-head">
              <strong>Saved by the agent</strong>
            </div>
            <div style={{ padding: 10, display: "grid", gap: 8, maxHeight: 300, overflow: "auto" }}>
              {outputs.length === 0 && <div className="small muted">Reports and tasks appear here after you approve them.</div>}
              {outputs.map((o) => (
                <details key={o.id} className="source">
                  <summary className="spread">
                    <span>
                      <span className={`badge ${o.kind === "task" ? "warn" : "info"}`}>{o.kind}</span> {o.title}
                    </span>
                    {o.due_date && <span className="small muted">due {o.due_date}</span>}
                  </summary>
                  {o.body && <div className="prose small" style={{ marginTop: 6 }}>{o.body}</div>}
                </details>
              ))}
            </div>
          </div>
        </aside>
      </div>
    </div>
  );
}

function RunStatus({ status }: { status: AgentRun["status"] }) {
  const map = {
    running: ["info", "Running"],
    waiting_approval: ["warn", "Needs approval"],
    completed: ["ok", "Done"],
    failed: ["bad", "Failed"],
  } as const;
  const [tone, label] = map[status];
  return <span className={`badge ${tone}`}>{label}</span>;
}

function RunView({
  run,
  steps,
  busy,
  onDecide,
}: {
  run: AgentRun | null;
  steps: AgentStep[];
  busy: boolean;
  onDecide: (decision: "approve" | "reject") => void;
}) {
  return (
    <div className="card card-pad stack">
      {run && (
        <div className="spread">
          <div className="row">
            <RunStatus status={run.status} />
            {run.route && <span className="badge plain">{ROUTE_LABEL[run.route] ?? run.route}</span>}
            {run.confidence && (
              <span className={`badge ${run.confidence === "high" ? "ok" : run.confidence === "medium" ? "warn" : "bad"}`}>
                {run.confidence} confidence
              </span>
            )}
          </div>
          <span className="small muted">
            {run.tool_calls} tool call{run.tool_calls === 1 ? "" : "s"} · {(run.prompt_tokens + run.completion_tokens).toLocaleString()} tokens · $
            {run.cost_usd.toFixed(4)}
          </span>
        </div>
      )}

      <div className="steps">
        {steps.map((s) => (
          <StepRow key={s.index} step={s} />
        ))}
        {busy && (
          <div className="step">
            <span />
            <span className="spinner" style={{ marginTop: 4 }} />
            <span className="muted">Working…</span>
            <span />
          </div>
        )}
      </div>

      {run?.status === "waiting_approval" && run.pending_approval && (
        <div className="approval">
          <strong>Approve this action?</strong>
          <div>{run.pending_approval.preview}</div>
          <details>
            <summary className="small muted">Exactly what will be saved</summary>
            <pre>{JSON.stringify(run.pending_approval.arguments, null, 2)}</pre>
          </details>
          <div className="row">
            <button className="btn primary" disabled={busy} onClick={() => onDecide("approve")}>
              Approve
            </button>
            <button className="btn" disabled={busy} onClick={() => onDecide("reject")}>
              Reject
            </button>
            <span className="small muted">Nothing is written until you approve. The run waits up to 24 hours.</span>
          </div>
        </div>
      )}

      {run?.status === "failed" && (
        <div className="alert">
          The run stopped: {run.error_message} <span className="mono">({run.error_code})</span>
        </div>
      )}

      {run?.answer && <Answer answer={run.answer} citations={run.citations} />}
    </div>
  );
}

function StepRow({ step }: { step: AgentStep }) {
  const tone =
    step.kind === "error" || !step.ok ? "bad" : step.kind === "approval" ? "wait" : step.kind === "final" ? "done" : "";
  const title: Record<AgentStep["kind"], string> = {
    route: "Picked a route",
    handoff: "Handed off",
    tool_call: "Called",
    invalid_call: "Invalid call, retrying",
    approval: "Waiting for approval",
    rejected: "You rejected",
    final: "Answered",
    error: "Stopped",
  };
  return (
    <div className={`step ${tone}`}>
      <span className={`agent-pill ${step.agent}`}>{step.agent}</span>
      <span className="dot" />
      <div>
        <div className="step-title">
          {title[step.kind]} {step.tool_name && <code>{step.tool_name}</code>}
        </div>
        {step.thought && <div className="small muted">{step.thought}</div>}
        {step.summary && <div className="small">{step.summary}</div>}
      </div>
      <span className="small muted">{step.duration_ms != null ? `${step.duration_ms} ms` : ""}</span>
    </div>
  );
}

function Answer({ answer, citations }: { answer: string; citations: AgentCitation[] }) {
  const [active, setActive] = useState<number | null>(null);
  const [error, setError] = useState<unknown>(null);
  const byNumber = new Map(citations.map((c) => [c.number, c]));
  return (
    <div className="msg assistant" style={{ maxWidth: "none" }}>
      <div className="bubble">
        <div className="prose">
          {answerPieces(answer).map((piece, i) => {
            const c = piece.kind === "cite" ? byNumber.get(piece.number) : undefined;
            if (!c) {
              const text = piece.kind === "cite" ? piece.mark : piece.text;
              return piece.bold ? <strong key={i}>{text}</strong> : <Fragment key={i}>{text}</Fragment>;
            }
            return (
              <button
                key={i}
                className={`cite ${active === c.number ? "active" : ""}`}
                title={`${c.document_title}${c.page ? `, p. ${c.page}` : ""}`}
                onClick={() => setActive(active === c.number ? null : c.number)}
              >
                {c.number}
              </button>
            );
          })}
        </div>
        {citations.length > 0 && (
          <div className="sources">
            {citations.map((c) => (
              <div key={c.number} className={`source ${active === c.number ? "active" : ""}`} onClick={() => setActive(c.number)}>
                <div className="spread">
                  <strong>
                    [{c.number}] {c.document_title}
                  </strong>
                  <button
                    className="btn sm ghost"
                    onClick={(e) => {
                      e.stopPropagation();
                      openFile(c.document_id, c.page).catch(setError);
                    }}
                  >
                    {c.page ? `Open page ${c.page} ↗` : "Open file ↗"}
                  </button>
                </div>
                {active === c.number && <div className="muted" style={{ marginTop: 4 }}>{c.snippet}</div>}
              </div>
            ))}
          </div>
        )}
        <ErrorNote error={error} />
      </div>
    </div>
  );
}

// Mirrors the backend's Pydantic response models (see /docs for the full OpenAPI schema).

export type DocStatus = "queued" | "processing" | "ready" | "failed";

export interface DocumentItem {
  id: string;
  title: string;
  original_filename: string;
  extension: string;
  size_bytes: number;
  status: DocStatus;
  stage: string | null;
  insights_status: "pending" | "ready" | "failed";
  error: { code: string; message: string } | null;
  warnings: { code: string; message: string }[];
  page_count: number | null;
  chunk_count: number;
  token_count: number;
  version: number;
  version_group_id: string;
  is_latest: boolean;
  attempts: number;
  processing_ms: number | null;
  created_at: string;
  processed_at: string | null;
}

export interface UploadResult {
  document: DocumentItem;
  duplicate: boolean;
  message: string;
}

export interface Insights {
  document_id: string;
  insights_status: string;
  summary: string | null;
  document_type: string | null;
  category: string | null;
  tags: string[];
  key_points: string[];
  key_facts: { label: string; value: string; page: number | null }[];
  entities: Record<string, string[]>;
  sentiment: { label?: string; score?: number; rationale?: string };
  suggested_questions: string[];
  model: string | null;
  prompt_version: string | null;
}

export interface SummaryResult {
  summary: string;
  cached: boolean;
  model: string;
}

export interface ProcessingRun {
  id: string;
  attempt: number;
  status: string;
  started_at: string;
  duration_ms: number | null;
  queue_wait_ms: number | null;
  error_code: string | null;
  error_message: string | null;
  stages: { stage: string; status: string; duration_ms: number | null; details: Record<string, unknown> }[];
}

export interface Citation {
  number: number;
  document_id: string;
  document_title: string;
  page_number: number | null;
  chunk_index: number;
  snippet: string;
  similarity: number;
}

export interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  citations: Citation[];
  follow_up_questions: string[];
  grounded: boolean | null;
  abstained: boolean | null;
  standalone_question: string | null;
  from_cache: boolean;
  cache_match?: { type: "exact" | "similar"; question: string; similarity: number } | null;
  latency_ms: number | null;
  created_at: string;
}

export interface ChatSession {
  id: string;
  title: string;
  documents: { id: string; title: string; version: number; status: string }[];
  message_count: number;
  updated_at: string;
}

export interface Comparison {
  id: string;
  status: "pending" | "running" | "completed" | "failed";
  document_ids: string[];
  focus: string;
  error_message: string | null;
  created_at: string;
  result: {
    labels: Record<string, { document_id: string; title: string; version?: number }>; // version: absent on older results
    overview: string;
    documents: { document_label: string; one_line_summary: string }[];
    similarities: string[];
    differences: {
      aspect: string;
      significance: "high" | "medium" | "low";
      explanation: string;
      values: { document_label: string; value: string; page: number | null }[];
    }[];
    gaps_or_risks: string[];
  } | null;
}

export interface AppEvent {
  type: string;
  document_id?: string;
  comparison_id?: string;
  session_id?: string;
  message_id?: string;
  stage?: string;
  error_message?: string;
}

// --------------------------------------------------------------------------- agent (specs/agent.md)
export type AgentName = "router" | "supervisor" | "research" | "actions";

export interface AgentStep {
  index: number;
  agent: AgentName;
  kind: "route" | "handoff" | "tool_call" | "invalid_call" | "approval" | "rejected" | "final" | "error";
  tool_name: string | null;
  thought: string | null;
  arguments: Record<string, unknown> | null;
  summary: string | null;
  ok: boolean;
  duration_ms: number | null;
}

export interface AgentApproval {
  id: string;
  tool_name: string;
  arguments: Record<string, unknown>;
  preview: string | null;
  status: "pending" | "approved" | "rejected";
  expires_at: string;
  decided_at: string | null;
}

export interface AgentCitation {
  number: number;
  document_id: string;
  document_title: string;
  page: number | null;
  snippet: string;
}

export interface AgentRun {
  id: string;
  goal: string;
  document_ids: string[];
  route: string | null;
  route_reason: string | null;
  route_confidence: number | null;
  status: "running" | "waiting_approval" | "completed" | "failed";
  answer: string | null;
  citations: AgentCitation[];
  confidence: "high" | "medium" | "low" | null;
  tool_calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  cost_usd: number;
  error_code: string | null;
  error_message: string | null;
  pending_approval: AgentApproval | null;
  created_at: string | null;
  updated_at: string | null;
  steps: AgentStep[];
}

export interface AgentOutput {
  id: string;
  run_id: string | null;
  kind: "report" | "task";
  title: string;
  body: string;
  due_date: string | null;
  document_ids: string[];
  created_at: string;
}

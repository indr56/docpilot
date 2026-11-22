"""Application settings, loaded from environment variables (see .env.example)."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- App ---
    app_name: str = "DocPilot"
    environment: str = "local"
    log_level: str = "INFO"
    api_prefix: str = "/api/v1"

    # --- Infrastructure ---
    database_url: str = "postgresql+asyncpg://docpilot:docpilot@db:5432/docpilot"
    sync_database_url: str = "postgresql+psycopg://docpilot:docpilot@db:5432/docpilot"
    redis_url: str = "redis://redis:6379/0"
    celery_broker_url: str = "redis://redis:6379/1"
    celery_result_backend: str = "redis://redis:6379/2"
    storage_dir: Path = Path("/data/uploads")

    # --- Auth ---
    # A demo user + API key are created on startup so anyone can try the API immediately.
    demo_api_key: SecretStr = SecretStr("docpilot-demo-key")
    demo_user_email: str = "demo@docpilot.local"

    # --- AI provider ---
    llm_provider: str = "openai"  # "openai" or "fake" (deterministic, used in tests)
    openai_api_key: SecretStr = SecretStr("")
    chat_model: str = "gpt-5.4-mini"
    fast_model: str = "gpt-5.4-nano"  # cheap model for query rewriting / follow-ups
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536
    llm_timeout_seconds: float = 60.0
    # Chat answers think briefly before answering. Without it, a few turns into a chat the model often
    # stated a computed total wrongly or stopped mid-calculation (7 of 25 runs wrong, 6 unfinished);
    # with "low": 0 of 25 for both, ~1.5 s. Empty = no reasoning, temperature 0. Other calls are unchanged.
    chat_reasoning_effort: str = "low"

    # --- Document processing ---
    max_upload_mb: int = 25
    allowed_extensions: tuple[str, ...] = (".pdf", ".docx", ".txt", ".md")
    chunk_size_tokens: int = 700
    chunk_overlap_tokens: int = 100
    embedding_batch_size: int = 96
    insights_max_input_tokens: int = 60_000
    insights_map_concurrency: int = 8  # parallel map calls for long documents
    processing_max_retries: int = 3
    # Background job time limits (seconds). The soft limit raises inside the job so it can fail the
    # document cleanly ("processing_timeout"); the hard limit kills the worker process as a last
    # resort. Both stay below the 15-minute document lock, so a lock never expires under a live job.
    job_soft_time_limit_seconds: int = 600
    job_hard_time_limit_seconds: int = 720

    # --- OCR for scanned PDF pages (specs/ocr.md) ---
    ocr_engine: str = "tesseract"  # "tesseract" or "none"; cloud engines register in services/ocr.py
    ocr_languages: str = "eng"
    ocr_dpi: int = 300
    ocr_min_confidence: float = 0.6  # pages below this are listed in a low_ocr_confidence warning
    ocr_max_pages: int = 50  # per document
    ocr_page_timeout_seconds: int = 30
    ocr_cache_ttl_seconds: int = 30 * 24 * 3600

    # --- Retrieval / chat ---
    retrieval_top_k: int = 6
    retrieval_candidates: int = 24
    min_relevance_score: float = 0.2  # below this cosine similarity, evidence is treated as weak
    chat_history_turns: int = 6
    answer_cache_ttl_seconds: int = 3600
    # Meaning-based answer cache: reuse an answer when a new question's embedding is at least this
    # similar to an already-answered one (same documents). Measured with text-embedding-3-small:
    # paraphrases scored 0.88-0.92, different questions on the same topic 0.48-0.80.
    semantic_cache_threshold: float = 0.88
    semantic_cache_max_entries: int = 200
    embedding_cache_ttl_seconds: int = 7 * 24 * 3600

    # --- Agent (specs/agent.md) ---
    agent_max_steps: int = 8  # tool calls per run, across approvals
    agent_max_invalid_calls: int = 3  # invalid tool calls in a row before the run fails
    agent_max_cost_usd: float = 0.05  # checked before every model call
    agent_timeout_seconds: int = 90  # wall clock for one stretch of a run (start, or one resume)
    agent_observation_chars: int = 6000  # a tool result is cut to this before the model sees it
    agent_document_token_budget: int = 12_000  # get_document_text returns the whole text up to this
    agent_scan_section_tokens: int = 3000  # scan_document maps over sections of this size
    agent_max_scope_documents: int = 50  # a run with no documents named covers your latest ready ones
    agent_approval_ttl_hours: int = 24
    agent_router_min_confidence: float = 0.5  # below this the route becomes "unsure"

    # --- MCP server (specs/mcp.md) ---
    mcp_rate_limit_per_minute: int = 60  # per API key
    mcp_confirmation_ttl_minutes: int = 10
    mcp_resource_url: str = "http://localhost:9700/mcp"  # what clients connect to (OAuth resource metadata)

    # --- Limits & quotas (per user, across all of that user's API keys) ---
    rate_limit_per_minute: int = 120
    upload_rate_limit_per_minute: int = 20
    chat_rate_limit_per_minute: int = 30
    # Abuse guard, not a scale limit: the design (indexes, HNSW, per-owner filters) targets thousands of
    # documents per user; raise it freely.
    max_documents_per_user: int = 10_000
    max_batch_files: int = 20
    daily_token_quota: int = 2_000_000

    # --- Metrics ---
    record_request_metrics: bool = True

    # Model prices in USD per 1M tokens (input, output). Used for cost tracking.
    model_prices: dict[str, tuple[float, float]] = Field(
        default_factory=lambda: {
            "gpt-5.4": (2.50, 15.00),
            "gpt-5.4-mini": (0.75, 4.50),
            "gpt-5.4-nano": (0.20, 1.25),
            "gpt-4.1": (2.00, 8.00),
            "gpt-4.1-mini": (0.40, 1.60),
            "gpt-4.1-nano": (0.10, 0.40),
            "gpt-4o": (2.50, 10.00),
            "gpt-4o-mini": (0.15, 0.60),
            "text-embedding-3-small": (0.02, 0.0),
            "text-embedding-3-large": (0.13, 0.0),
            "fake-chat": (0.0, 0.0),
            "fake-embedding": (0.0, 0.0),
        }
    )

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()

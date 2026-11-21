"""Read-only tools. They reuse the services the API already has, so the agent searches, reads and
compares exactly the way chat and the endpoints do (same owner filters, caches and limits)."""

import asyncio
import hashlib
import re
import uuid
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.config import get_settings
from app.llm import get_provider
from app.llm.base import Usage
from app.llm.prompts import agent_scan as scan_prompt
from app.llm.schemas import ScanItem, ScanItems
from app.models import Document, DocumentInsight
from app.models.enums import ComparisonStatus, DocumentStatus
from app.services import cache
from app.services.chunking import count_tokens
from app.services.comparison_requests import create_comparison
from app.services.insights import split_sections
from app.services.parsing import ParsedDocument, parse_document
from app.services.retrieval import hybrid_search
from app.services.storage import get_storage
from app.services.usage import UsageEntry, failed_calls_to
from app.tools.registry import Tool, ToolContext, ToolError, ToolResult, register

PASSAGE_CHARS = 1200
SCAN_MAX_ITEMS = 80
SCAN_CONCURRENCY = 4
SCAN_CACHE_TTL_SECONDS = 7 * 24 * 3600
COMPARE_WAIT_SECONDS = 30.0


def _ready(document: Document) -> None:
    if document.status != DocumentStatus.READY:
        raise ToolError("not_ready", f"'{document.title}' is still {document.status}; it can't be read yet.")


async def _parse(document: Document) -> ParsedDocument:
    return await asyncio.to_thread(parse_document, get_storage().path(document.storage_path), document.extension)


async def _embed(ctx: ToolContext, text: str) -> list[float]:
    provider = get_provider()
    cached = await cache.aget_cached_embedding(provider.embedding_model, text)
    if cached is not None:
        saved = Usage(provider.embedding_model, prompt_tokens=count_tokens(text))
        ctx.usages.append(UsageEntry("agent_embed", saved, cached=True))
        return cached
    with failed_calls_to(ctx.usages.append, "agent_embed", provider.embedding_model):
        result = await provider.aembed([text])
    ctx.usages.append(UsageEntry("agent_embed", result.usage))
    await cache.aset_cached_embedding(provider.embedding_model, text, result.vectors[0])
    return result.vectors[0]


# --------------------------------------------------------------------------- list_documents
class NoArgs(BaseModel):
    pass


async def _list_documents(ctx: ToolContext, _: NoArgs) -> ToolResult:
    lines = [
        f"- {d.id} | {d.title} | {d.status} | {d.page_count or '?'} pages | {d.token_count} tokens"
        for d in ctx.scope.values()
    ]
    return ToolResult("\n".join(lines) or "No documents in scope.", f"{len(lines)} documents")


register(
    Tool(
        "list_documents",
        "List the documents in scope with their id, title, status, page count and size in tokens.",
        NoArgs,
        "read",
        _list_documents,
    )
)


# --------------------------------------------------------------------------- search_passages
class SearchArgs(BaseModel):
    query: str = Field(min_length=2, max_length=500, description="What to look for, in plain words")
    document_ids: list[str] = Field(
        default_factory=list, max_length=20, description="Limit to these documents; empty = all in scope"
    )
    top_k: int = Field(default=6, ge=1, le=10, description="How many passages to return")


async def _search_passages(ctx: ToolContext, args: SearchArgs) -> ToolResult:
    documents = [ctx.document(i) for i in args.document_ids] if args.document_ids else list(ctx.scope.values())
    documents = [d for d in documents if d.status == DocumentStatus.READY]
    if not documents:
        raise ToolError("no_ready_documents", "None of those documents is ready to search.")
    embedding = await _embed(ctx, args.query)
    chunks = await hybrid_search(
        ctx.db,
        owner_id=ctx.user.id,
        document_ids=[d.id for d in documents],
        query=args.query,
        embedding=embedding,
        top_k=args.top_k,
    )
    by_id = {d.id: d for d in documents}
    lines = []
    for chunk in chunks:
        document = by_id[chunk.document_id]
        n = ctx.add_source(document, chunk.page_number, chunk.content)
        page = f", p. {chunk.page_number}" if chunk.page_number else ""
        lines.append(f"[{n}] ({document.title}{page}) {chunk.content[:PASSAGE_CHARS]}")
    if not lines:
        return ToolResult("No passages matched.", "no passages matched")
    return ToolResult("\n\n".join(lines), f"{len(lines)} passages")


register(
    Tool(
        "search_passages",
        "Hybrid (meaning + keyword) search over the documents. Best for a specific fact, name, number or clause.",
        SearchArgs,
        "read",
        _search_passages,
    )
)


# --------------------------------------------------------------------------- get_document_text
class DocumentArgs(BaseModel):
    document_id: str = Field(description="A document id from the list in scope")


async def _get_document_text(ctx: ToolContext, args: DocumentArgs) -> ToolResult:
    document = ctx.document(args.document_id)
    _ready(document)
    budget = get_settings().agent_document_token_budget
    if document.token_count > budget:
        raise ToolError(
            "too_long",
            f"'{document.title}' has {document.token_count} tokens, over the {budget}-token budget. "
            "Use get_document_notes for condensed notes of every section, or search_passages.",
        )
    parsed = await _parse(document)
    parts = []
    for page in parsed.pages:
        n = ctx.add_source(document, page.number, page.text)
        marker = f"[Page {page.number}]" if page.number else ""
        parts.append(f"[{n}] {marker}\n{page.text}")
    text = "\n\n".join(parts)
    return ToolResult(text, f"whole text, {len(parsed.pages)} pages", max_chars=budget * 5)


register(
    Tool(
        "get_document_text",
        "The whole text of one document, page by page, each page numbered [n] for citing. Fails with "
        "too_long for documents over the token budget.",
        DocumentArgs,
        "read",
        _get_document_text,
    )
)


# --------------------------------------------------------------------------- get_document_notes
async def _insight(ctx: ToolContext, document: Document) -> DocumentInsight:
    insight = (
        await ctx.db.execute(select(DocumentInsight).where(DocumentInsight.document_id == document.id))
    ).scalar_one_or_none()
    if insight is None:
        raise ToolError("no_insights", f"'{document.title}' has no insights yet.")
    return insight


async def _get_document_notes(ctx: ToolContext, args: DocumentArgs) -> ToolResult:
    document = ctx.document(args.document_id)
    _ready(document)
    insight = await _insight(ctx, document)
    if not insight.analysis_text:
        raise ToolError(
            "short_document",
            f"'{document.title}' is short enough to read whole: use get_document_text instead.",
        )
    n = ctx.add_source(document, None, insight.analysis_text)
    return ToolResult(f"[{n}] Notes for '{document.title}':\n{insight.analysis_text}", "condensed notes")


register(
    Tool(
        "get_document_notes",
        "Condensed notes of every section of a LONG document (made when it was processed), with page "
        "markers. Use it when get_document_text says too_long.",
        DocumentArgs,
        "read",
        _get_document_notes,
    )
)


# --------------------------------------------------------------------------- get_insights
async def _get_insights(ctx: ToolContext, args: DocumentArgs) -> ToolResult:
    document = ctx.document(args.document_id)
    _ready(document)
    insight = await _insight(ctx, document)
    n = ctx.add_source(document, None, insight.summary)
    lines = [f"[{n}] Summary of '{document.title}': {insight.summary}", "Key points:"]
    lines += [f"- {p}" for p in insight.key_points]
    lines.append("Key facts:")
    for fact in insight.key_facts:
        m = ctx.add_source(document, fact.get("page"), f"{fact.get('label')}: {fact.get('value')}")
        lines.append(f"[{m}] {fact.get('label')}: {fact.get('value')}")
    return ToolResult("\n".join(lines), f"summary + {len(insight.key_facts)} key facts")


register(
    Tool(
        "get_insights",
        "The stored analysis of one document: summary, key points, and key facts with pages.",
        DocumentArgs,
        "read",
        _get_insights,
    )
)


# --------------------------------------------------------------------------- scan_document
class ScanArgs(BaseModel):
    document_id: str = Field(description="A document id from the list in scope")
    instruction: str = Field(
        min_length=3, max_length=300, description="What to collect, e.g. 'every deadline and notice period'"
    )


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9$%.]+", " ", text.lower()).strip()


def _scan_cache_key(model: str, instruction: str, section: str) -> str:
    digest = hashlib.sha256(f"{_normalise(instruction)}\n{section}".encode()).hexdigest()
    return f"agent:scan:{model}:{scan_prompt.VERSION}:{digest}"


async def _scan_section(ctx: ToolContext, title: str, instruction: str, section: str) -> list[ScanItem]:
    provider = get_provider()
    key = _scan_cache_key(provider.fast_model, instruction, section)
    cached = await cache.aget_json(key)
    if cached is not None:
        ctx.usages.append(UsageEntry("agent_scan", Usage(provider.fast_model), scan_prompt.VERSION, cached=True))
        return [ScanItem.model_validate(i) for i in cached]
    with failed_calls_to(ctx.usages.append, "agent_scan", provider.fast_model, scan_prompt.VERSION):
        result = await provider.acomplete_structured(
            scan_prompt.build_messages(title, instruction, section),
            ScanItems,
            model=provider.fast_model,
            max_tokens=1500,
        )
    ctx.usages.append(UsageEntry("agent_scan", result.usage, scan_prompt.VERSION))
    await cache.aset_json(key, [i.model_dump() for i in result.value.items], SCAN_CACHE_TTL_SECONDS)
    return result.value.items


async def _scan_document(ctx: ToolContext, args: ScanArgs) -> ToolResult:
    """Map: every section, in parallel (bounded). Merge: in document order, duplicates dropped."""
    document = ctx.document(args.document_id)
    _ready(document)
    sections = split_sections(await _parse(document), get_settings().agent_scan_section_tokens)
    gate = asyncio.Semaphore(SCAN_CONCURRENCY)

    async def one(section: str) -> list[ScanItem]:
        async with gate:
            return await _scan_section(ctx, document.title, args.instruction, section)

    per_section = await asyncio.gather(*(one(s) for s in sections))
    seen: set[str] = set()
    lines: list[str] = []
    for items in per_section:
        for item in items:
            key = _normalise(item.text)
            if not key or key in seen:
                continue
            seen.add(key)
            n = ctx.add_source(document, item.page, item.text)
            page = f" (p. {item.page})" if item.page else ""
            lines.append(f"[{n}] {item.text}{page}")
    total = len(lines)
    if total > SCAN_MAX_ITEMS:
        lines = [*lines[:SCAN_MAX_ITEMS], f"... and {total - SCAN_MAX_ITEMS} more (narrow the instruction)."]
    summary = f"{total} items from {len(sections)} sections"
    return ToolResult("\n".join(lines) or "No matching items in any section.", summary)


register(
    Tool(
        "scan_document",
        "Read EVERY section of one document and collect all items matching an instruction (e.g. all "
        "deadlines, all fees), deduplicated, each with its page. Use for 'list all / every' requests.",
        ScanArgs,
        "read",
        _scan_document,
    )
)


# --------------------------------------------------------------------------- compare_documents
class CompareArgs(BaseModel):
    document_ids: list[str] = Field(min_length=2, max_length=5, description="2 to 5 document ids from the list")
    focus: str = Field(default="", max_length=300, description="What to compare on; empty = the key terms")


def _render_comparison(result: dict[str, Any], labels: dict[str, int]) -> str:
    lines = [f"Overview: {result.get('overview', '')}"]
    for key in ("similarities", "differences", "gaps_or_risks"):
        values = result.get(key) or []
        if values:
            lines.append(f"{key.replace('_', ' ').capitalize()}:")
            lines += [f"- {v}" for v in values]
    lines.append("Sources: " + ", ".join(f"[{n}] {title}" for title, n in labels.items()))
    return "\n".join(lines)


async def _compare_documents(ctx: ToolContext, args: CompareArgs) -> ToolResult:
    documents = [ctx.document(i) for i in dict.fromkeys(args.document_ids)]
    if len(documents) < 2:
        raise ToolError("need_two", "Pick at least two different documents to compare.")
    for document in documents:
        _ready(document)
    comparison = await create_comparison(ctx.db, ctx.user, [d.id for d in documents], args.focus)
    comparison_id: uuid.UUID = comparison.id
    waited = 0.0
    while True:
        await ctx.db.refresh(comparison)
        if comparison.status in (ComparisonStatus.COMPLETED, ComparisonStatus.FAILED):
            break
        if waited >= COMPARE_WAIT_SECONDS:
            raise ToolError("comparison_pending", f"The comparison {comparison_id} is still running; try again.")
        await asyncio.sleep(1.0)
        waited += 1.0
    if comparison.status == ComparisonStatus.FAILED or not comparison.result:
        raise ToolError("comparison_failed", comparison.error_message or "The comparison failed.")
    labels = {d.title: ctx.add_source(d, None, f"Comparison of {d.title}") for d in documents}
    return ToolResult(_render_comparison(comparison.result, labels), f"compared {len(documents)} documents")


register(
    Tool(
        "compare_documents",
        "Structured comparison of 2-5 documents (similarities, differences, gaps), optionally on a focus.",
        CompareArgs,
        "read",
        _compare_documents,
    )
)

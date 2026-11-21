"""The fake provider's agent behaviour (LLM_PROVIDER=fake): a deterministic router, a scripted
tool-using policy and a keyword scanner, so the agent's whole loop runs offline in tests and demos.

It only reads what the real model would read (the prompt text), so it exercises the same parsing,
validation, approvals and citations as a real run.
"""

import json
import re

from app.llm.base import Message
from app.llm.schemas import AgentDecision, RouteDecision, ScanItem, ScanItems

_ROUTE_RULES: list[tuple[str, str, float]] = [
    (r"\b(save|create|record|remind|add a task|make a task|file a report)\b", "action", 0.95),
    (r"\b(compare|comparison|difference|differ|versus|vs\.?)\b", "cross_document", 0.9),
    (r"\b(list|every|all the|all of the|each)\b", "list_everything", 0.9),
    (r"\b(summari[sz]e|summary|overview|explain (this|the) document|whole document)\b", "whole_document", 0.9),
    (r"^\s*(hi|hello|hey|thanks|thank you|who are you|what can you do)\b", "chat_only", 0.95),
    (r"\b(what|when|who|how much|how many|which|where|is there|does)\b", "pointed_fact", 0.8),
]

_STOP = {
    "list",
    "every",
    "all",
    "the",
    "each",
    "and",
    "with",
    "from",
    "that",
    "this",
    "what",
    "document",
    "documents",
    "show",
    "find",
    "give",
    "please",
    "their",
    "them",
    "items",
    "item",
    "mentioned",
}


def route(messages: list[Message]) -> RouteDecision:
    text = messages[-1]["content"]
    goal = text.split("<request>", 1)[-1].split("</request>", 1)[0].strip().lower()
    for pattern, name, confidence in _ROUTE_RULES:
        if re.search(pattern, goal):
            return RouteDecision(route=name, confidence=confidence, reason=f"matched '{pattern[:30]}'")  # type: ignore[arg-type]
    return RouteDecision(route="unsure", confidence=0.3, reason="no rule matched")


def _final(answer: str, thought: str = "I have what I need.") -> AgentDecision:
    return AgentDecision(thought=thought, action="final", tool_name="", arguments_json="{}", answer=answer)


def _call(tool: str, arguments: dict[str, object], thought: str) -> AgentDecision:
    return AgentDecision(
        thought=thought, action="tool", tool_name=tool, arguments_json=json.dumps(arguments), answer=""
    )


def _answer_from(observation: str, limit: int = 3) -> str:
    numbers = re.findall(r"^\[(\d+)\]", observation, re.M)[:limit]
    if not numbers:
        return "The documents don't contain that."
    first = re.search(r"^\[\d+\] (?:\(.*?\) )?(.*)$", observation, re.M)
    snippet = (first.group(1) if first else "").strip().split(". ")[0][:200]
    return f"According to the documents, {snippet} " + " ".join(f"[{n}]" for n in numbers)


def decide(messages: list[Message]) -> AgentDecision:
    system, goal = messages[0]["content"], messages[1]["content"]
    route_name = re.search(r"^Route: (\w+)", system, re.M).group(1)  # type: ignore[union-attr]
    documents = re.findall(r"^- ([0-9a-f-]{36}) \| ", system, re.M)
    first_doc = documents[0] if documents else ""
    observations = [
        (m.group(1), m.group(2))
        for msg in messages[2:]
        if msg["role"] == "user"
        and (m := re.match(r'<tool_result tool="(\w+)">\n(.*)\n</tool_result>$', msg["content"], re.S))
    ]

    if route_name == "chat_only":
        return _final("Hello! I can search, summarise, list, compare and save things from your documents.")
    if observations:
        tool, content = observations[-1]
        if content.startswith("The user approved."):
            return _final(f"Done. {content.removeprefix('The user approved. ')}")
        if content.startswith("The user rejected"):
            return _final("Nothing was saved, as you declined the action.")
        if content.startswith("Error (too_long)"):
            return _call("get_document_notes", {"document_id": first_doc}, "Too long to read whole; using the notes.")
        if route_name == "action" and not any(t in ("save_report", "create_task") for t, _ in observations):
            if "task" in goal.lower():
                return _call(
                    "create_task",
                    {"title": goal[:120], "document_id": first_doc or None, "notes": _answer_from(content)},
                    "Creating the task the user asked for.",
                )
            return _call(
                "save_report",
                {
                    "title": "Report: " + goal[:80],
                    "content_markdown": _answer_from(content),
                    "document_ids": documents[:1],
                },
                "Saving the report the user asked for.",
            )
        return _final(_answer_from(content, 80 if route_name == "list_everything" else 3))

    first_tool: dict[str, tuple[str, dict[str, object]]] = {
        "pointed_fact": ("search_passages", {"query": goal}),
        "unsure": ("search_passages", {"query": goal}),
        "action": ("search_passages", {"query": goal}),
        "whole_document": ("get_document_text", {"document_id": first_doc}),
        "list_everything": ("scan_document", {"document_id": first_doc, "instruction": goal}),
        "cross_document": ("compare_documents", {"document_ids": documents[:2], "focus": ""}),
    }
    tool, arguments = first_tool[route_name]
    return _call(tool, arguments, f"Starting with {tool}.")


def scan(messages: list[Message]) -> ScanItems:
    text = messages[-1]["content"]
    instruction = re.search(r"^Instruction: (.*)$", text, re.M).group(1).lower()  # type: ignore[union-attr]
    keys = {w for w in re.findall(r"[a-z]{4,}", instruction) if w not in _STOP}
    stems = {k[:5] for k in keys}
    section = text.split("<section>", 1)[-1].split("</section>", 1)[0]
    items: list[ScanItem] = []
    page: int | None = None
    for line in section.splitlines():
        if m := re.match(r"\[Page (\d+)\]", line.strip()):
            page = int(m.group(1))
            continue
        for sentence in re.split(r"(?<=[.!?])\s+", line.strip()):
            words = {w[:5] for w in re.findall(r"[a-z]{4,}", sentence.lower())}
            if sentence and words & stems:
                items.append(ScanItem(text=sentence[:300], page=page))
    return ScanItems(items=items[:10])

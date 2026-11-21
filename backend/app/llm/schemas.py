"""Pydantic models the LLM must return (OpenAI structured outputs enforce the JSON schema)."""

from typing import Literal

from pydantic import BaseModel, Field

Category = Literal[
    "insurance",
    "legal",
    "finance",
    "hr",
    "technical",
    "medical",
    "government",
    "sales_marketing",
    "academic",
    "personal",
    "other",
]


class KeyFact(BaseModel):
    label: str = Field(description="What the value is, e.g. 'Flood deductible'")
    value: str = Field(description="The exact value as written, e.g. '$25,000'")
    page: int | None = Field(description="Page number where it appears, or null if unknown")


class Entities(BaseModel):
    organizations: list[str]
    people: list[str]
    locations: list[str]
    dates: list[str]
    monetary_amounts: list[str]


class Sentiment(BaseModel):
    label: Literal["positive", "neutral", "negative", "mixed"]
    score: float = Field(description="-1.0 (very negative) to 1.0 (very positive)")
    rationale: str


class InsightsOutput(BaseModel):
    summary: str
    document_type: str
    category: Category
    tags: list[str]
    key_points: list[str]
    key_facts: list[KeyFact]
    entities: Entities
    sentiment: Sentiment
    suggested_questions: list[str]
    language: str


class FollowUpQuestions(BaseModel):
    questions: list[str]


class AnsweredSuggestions(BaseModel):
    answered: list[int] = Field(description="1-based numbers of the suggestions the answer already covers")


class ComparedValue(BaseModel):
    document_label: str = Field(description="The document label, e.g. 'A' or 'B'")
    value: str
    page: int | None


class Difference(BaseModel):
    aspect: str
    values: list[ComparedValue]
    significance: Literal["high", "medium", "low"]
    explanation: str


class DocumentBrief(BaseModel):
    document_label: str
    one_line_summary: str


class ComparisonOutput(BaseModel):
    overview: str
    documents: list[DocumentBrief]
    similarities: list[str]
    differences: list[Difference]
    gaps_or_risks: list[str] = Field(description="Things missing in one document or worth attention")


class SectionNotes(BaseModel):
    """Map step for very long documents: dense notes for one section."""

    notes: str


# --------------------------------------------------------------------------- agent (specs/agent.md)
AgentRouteName = Literal[
    "chat_only", "pointed_fact", "whole_document", "list_everything", "cross_document", "action", "unsure"
]


class RouteDecision(BaseModel):
    """The router's choice of strategy for one goal."""

    route: AgentRouteName
    confidence: float = Field(description="0.0 (a guess) to 1.0 (certain)")
    reason: str = Field(description="One short sentence: why this route")


class AgentDecision(BaseModel):
    """One turn of the agent loop: either call one tool, or give the final answer."""

    thought: str = Field(description="One short sentence on what you are doing next and why (shown to the user)")
    action: Literal["tool", "final"]
    tool_name: str = Field(description="The tool to call when action is 'tool', else an empty string")
    arguments_json: str = Field(description="The tool's arguments as a JSON object string; '{}' when final")
    answer: str = Field(description="The final answer with [n] citations when action is 'final', else empty")


class ScanItem(BaseModel):
    text: str = Field(description="The item, quoted or closely paraphrased from the section")
    page: int | None = Field(description="The page it is on, from the [Page n] markers, or null")


class ScanItems(BaseModel):
    """Map step of scan_document: every matching item in one section."""

    items: list[ScanItem]

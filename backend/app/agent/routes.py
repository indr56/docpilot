"""Which tools each route offers (specs/agent.md §3)."""

from app.models.enums import AgentRoute

READ = ["list_documents", "search_passages", "get_document_text", "get_document_notes", "get_insights"]

ROUTE_TOOLS: dict[str, list[str]] = {
    AgentRoute.CHAT_ONLY: [],
    AgentRoute.POINTED_FACT: ["search_passages", "list_documents"],
    AgentRoute.WHOLE_DOCUMENT: ["get_document_text", "get_document_notes", "get_insights", "list_documents"],
    AgentRoute.LIST_EVERYTHING: ["scan_document", "search_passages", "list_documents"],
    AgentRoute.CROSS_DOCUMENT: ["compare_documents", "search_passages", "list_documents"],
    AgentRoute.ACTION: [*READ, "scan_document", "save_report", "create_task"],
    AgentRoute.UNSURE: ["search_passages", "get_document_text", "get_document_notes", "list_documents"],
}

# Routes that answer from documents: an answer there without a citation is low confidence.
DOCUMENT_ROUTES = {r for r in ROUTE_TOOLS if r != AgentRoute.CHAT_ONLY}

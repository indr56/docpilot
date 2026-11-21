"""Which agent and tools each route gets (specs/agent.md §3 and §14)."""

from app.models.enums import AgentRoute

READ = ["list_documents", "search_passages", "get_document_text", "get_document_notes", "get_insights"]

# Read routes: the Research agent, with this route's tools.
ROUTE_TOOLS: dict[str, list[str]] = {
    AgentRoute.CHAT_ONLY: [],
    AgentRoute.POINTED_FACT: ["search_passages", "list_documents"],
    AgentRoute.WHOLE_DOCUMENT: ["get_document_text", "get_document_notes", "get_insights", "list_documents"],
    AgentRoute.LIST_EVERYTHING: ["scan_document", "search_passages", "list_documents"],
    AgentRoute.CROSS_DOCUMENT: ["compare_documents", "search_passages", "list_documents"],
    AgentRoute.UNSURE: ["search_passages", "get_document_text", "get_document_notes", "list_documents"],
}

# The action route: the Supervisor, which hands off to the two specialists.
SUPERVISOR_TOOLS = ["ask_research_agent", "ask_actions_agent"]
RESEARCH_TASK_TOOLS = [*READ, "scan_document", "compare_documents"]  # Research working for the supervisor
ACTIONS_TOOLS = ["list_documents", "save_report", "create_task"]
HANDOFF_AGENT = {"ask_research_agent": "research", "ask_actions_agent": "actions"}

# Routes that answer from documents: an answer there without a citation is low confidence.
DOCUMENT_ROUTES = {*ROUTE_TOOLS, AgentRoute.ACTION} - {AgentRoute.CHAT_ONLY}

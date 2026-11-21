"""Hand-off tools: the supervisor's only tools. The runner intercepts them and pushes a specialist's
frame (specs/agent.md §14); the handler never runs."""

from pydantic import BaseModel, Field

from app.tools.registry import Tool, ToolContext, ToolResult, register


class HandoffArgs(BaseModel):
    task: str = Field(
        min_length=3,
        max_length=6000,
        description="The complete task for the agent, with every detail and [n]-cited content it needs",
    )


async def _never(ctx: ToolContext, args: HandoffArgs) -> ToolResult:  # pragma: no cover - intercepted by the runner
    raise RuntimeError("hand-off tools are handled by the runner")


register(
    Tool(
        "ask_research_agent",
        "Hand a reading task to the research agent (search, read, scan, compare). Returns its cited findings.",
        HandoffArgs,
        "handoff",
        _never,
    )
)

register(
    Tool(
        "ask_actions_agent",
        "Hand a saving task to the actions agent (save a report, create a task). Include all the content it "
        "needs. The user approves each write. Returns what was saved, or that the user declined.",
        HandoffArgs,
        "handoff",
        _never,
    )
)

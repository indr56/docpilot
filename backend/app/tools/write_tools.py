"""Write tools. The loop never runs these directly: a call creates an approval, and the handler runs
only after the user approves, with the arguments stored at that moment (specs/agent.md §6)."""

from datetime import date

from pydantic import BaseModel, Field

from app.models import AgentOutput
from app.tools.registry import Tool, ToolContext, ToolResult, register


class SaveReportArgs(BaseModel):
    title: str = Field(min_length=3, max_length=300, description="A short title for the report")
    content_markdown: str = Field(
        min_length=1, max_length=20_000, description="The report in Markdown, with [n] citations"
    )
    document_ids: list[str] = Field(default_factory=list, max_length=20, description="Documents it is about")


async def _save_report(ctx: ToolContext, args: SaveReportArgs) -> ToolResult:
    ids = [str(ctx.document(i).id) for i in args.document_ids]
    output = AgentOutput(
        owner_id=ctx.user.id,
        run_id=ctx.run.id,
        kind="report",
        title=args.title,
        body=args.content_markdown,
        document_ids=ids,
    )
    ctx.db.add(output)
    await ctx.db.flush()
    return ToolResult(f"Saved report {output.id}: '{args.title}'.", f"saved report '{args.title}'")


register(
    Tool(
        "save_report",
        "Save a Markdown report for the user (asks the user to approve first).",
        SaveReportArgs,
        "write",
        _save_report,
        preview=lambda a: f"Save a report titled '{a.title}' ({len(a.content_markdown)} characters).",
    )
)


class CreateTaskArgs(BaseModel):
    title: str = Field(min_length=3, max_length=300, description="What has to be done")
    due_date: date | None = Field(default=None, description="ISO date (YYYY-MM-DD) if the documents give one")
    document_id: str | None = Field(default=None, description="The document the task comes from")
    notes: str = Field(default="", max_length=2000, description="Details, with [n] citations")


async def _create_task(ctx: ToolContext, args: CreateTaskArgs) -> ToolResult:
    ids = [str(ctx.document(args.document_id).id)] if args.document_id else []
    output = AgentOutput(
        owner_id=ctx.user.id,
        run_id=ctx.run.id,
        kind="task",
        title=args.title,
        body=args.notes,
        due_date=args.due_date,
        document_ids=ids,
    )
    ctx.db.add(output)
    await ctx.db.flush()
    due = f", due {args.due_date.isoformat()}" if args.due_date else ""
    return ToolResult(f"Created task {output.id}: '{args.title}'{due}.", f"created task '{args.title}'{due}")


register(
    Tool(
        "create_task",
        "Create a task (a to-do with an optional due date) for the user (asks the user to approve first).",
        CreateTaskArgs,
        "write",
        _create_task,
        preview=lambda a: f"Create a task '{a.title}'" + (f" due {a.due_date.isoformat()}" if a.due_date else "") + ".",
    )
)

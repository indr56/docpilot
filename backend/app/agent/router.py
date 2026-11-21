"""The router: one fast-model call that picks the route (strategy) for a goal."""

from dataclasses import dataclass

from app.core.config import get_settings
from app.llm import get_provider
from app.llm.prompts import agent_router as router_prompt
from app.llm.schemas import RouteDecision
from app.models.enums import AgentRoute
from app.services.usage import UsageEntry, failed_calls_to


@dataclass
class Routed:
    route: str
    confidence: float
    reason: str
    picked: str  # what the model picked, before the confidence rule


async def route_goal(goal: str, document_titles: list[str], usages: list[UsageEntry]) -> Routed:
    provider = get_provider()
    with failed_calls_to(usages.append, "agent_router", provider.fast_model, router_prompt.VERSION):
        result = await provider.acomplete_structured(
            router_prompt.build_messages(goal, document_titles),
            RouteDecision,
            model=provider.fast_model,
            max_tokens=200,
        )
    usages.append(UsageEntry("agent_router", result.usage, router_prompt.VERSION))
    decision = result.value
    confidence = max(0.0, min(1.0, decision.confidence))
    route: str = decision.route
    if confidence < get_settings().agent_router_min_confidence and route != AgentRoute.ACTION:
        # A guess is worse than letting the loop choose between search and full text. A low-confidence
        # "action" keeps its route: its write tools still wait for approval, so a wrong guess is harmless.
        route = AgentRoute.UNSURE
    return Routed(route=route, confidence=confidence, reason=decision.reason, picked=decision.route)

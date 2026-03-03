"""LangGraph entrypoints for search workflows."""

from typing import Any, cast

from langgraph.graph import END, START, StateGraph

from agent.models.criteria import SearchCriteria
from agent.models.enriched import EnrichedApartment
from agent.nodes.intent_node import IntentNode
from agent.nodes.search_node import SearchGraphState, SearchNode, create_default_search_node


def build_search_graph(search_node: SearchNode) -> Any:
    """Build a minimal LangGraph pipeline for apartment search."""
    graph = StateGraph(SearchGraphState)
    graph.add_node("search", search_node)
    graph.add_edge(START, "search")
    graph.add_edge("search", END)
    return graph.compile()


async def run_search_graph(
    criteria: SearchCriteria,
    *,
    search_node: SearchNode | None = None,
) -> list[EnrichedApartment]:
    """Execute search pipeline and map results to enriched apartments."""
    active_node = search_node or create_default_search_node()
    app = build_search_graph(active_node)
    initial_state: SearchGraphState = {"criteria": criteria, "apartments": []}
    final_state = cast(SearchGraphState, await app.ainvoke(initial_state))
    apartments = final_state["apartments"]
    return [EnrichedApartment(apartment=apartment) for apartment in apartments]


async def run_search_graph_from_text(
    *,
    user_id: int,
    message: str,
    intent_node: IntentNode | None = None,
    search_node: SearchNode | None = None,
) -> list[EnrichedApartment]:
    """Parse free text into criteria and run the search graph."""
    active_intent_node = intent_node or IntentNode()
    criteria = active_intent_node.parse(user_id=user_id, message=message)
    return await run_search_graph(criteria, search_node=search_node)

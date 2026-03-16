"""LangGraph entrypoints for search workflows."""

from typing import Any, cast

from langgraph.graph import END, START, StateGraph

from agent.models.criteria import SearchCriteria
from agent.models.enriched import EnrichedApartment
from agent.nodes.enrich_node import EnrichNode, create_default_enrich_node
from agent.nodes.intent_node import IntentNode
from agent.nodes.scoring_node import ScoringNode, create_default_scoring_node
from agent.nodes.search_node import SearchGraphState, SearchNode, create_default_search_node
from db import build_checkpoint_config, get_async_postgres_checkpointer


def build_search_graph(
    search_node: SearchNode,
    enrich_node: EnrichNode | None = None,
    scoring_node: ScoringNode | None = None,
    checkpointer: Any | None = None,
) -> Any:
    """Build search pipeline graph."""
    graph = StateGraph(SearchGraphState)
    graph.add_node("search", search_node)
    graph.add_edge(START, "search")
    if enrich_node is not None:
        graph.add_node("enrich", enrich_node)
        graph.add_edge("search", "enrich")
    if scoring_node is not None:
        graph.add_node("score", scoring_node)
        if enrich_node is None:
            graph.add_edge("search", "score")
        else:
            graph.add_edge("enrich", "score")
        graph.add_edge("score", END)
    elif enrich_node is not None:
        graph.add_edge("enrich", END)
    else:
        graph.add_edge("search", END)
    if checkpointer is None:
        return graph.compile()
    return graph.compile(checkpointer=checkpointer)


async def _invoke_search_graph(
    *,
    criteria: SearchCriteria,
    search_node: SearchNode,
    enrich_node: EnrichNode | None,
    scoring_node: ScoringNode | None,
    checkpointer: Any | None,
    thread_id: str | None,
    checkpoint_ns: str,
    checkpoint_id: str | None,
) -> list[EnrichedApartment]:
    app = build_search_graph(
        search_node,
        enrich_node=enrich_node,
        scoring_node=scoring_node,
        checkpointer=checkpointer,
    )
    initial_state: SearchGraphState = {"criteria": criteria, "apartments": []}
    config: dict[str, dict[str, str]] | None = None
    if thread_id is not None:
        config = build_checkpoint_config(
            thread_id=thread_id,
            checkpoint_ns=checkpoint_ns,
            checkpoint_id=checkpoint_id,
        )
    final_state = cast(SearchGraphState, await app.ainvoke(initial_state, config=config))
    enriched = final_state.get("enriched_apartments")
    if enriched is not None:
        return enriched
    apartments = final_state.get("apartments", [])
    return [EnrichedApartment(apartment=apartment) for apartment in apartments]


async def run_search_graph(
    criteria: SearchCriteria,
    *,
    search_node: SearchNode | None = None,
    enrich_node: EnrichNode | None = None,
    scoring_node: ScoringNode | None = None,
    checkpointer: Any | None = None,
    thread_id: str | None = None,
    checkpoint_ns: str = "",
    checkpoint_id: str | None = None,
) -> list[EnrichedApartment]:
    """Execute search pipeline and map results to enriched apartments."""
    active_node = search_node or create_default_search_node()
    use_default_pipeline = (
        search_node is None
        and enrich_node is None
        and scoring_node is None
    )
    active_enrich_node = (
        create_default_enrich_node()
        if use_default_pipeline
        else enrich_node
    )
    active_scoring_node = (
        create_default_scoring_node()
        if use_default_pipeline
        else scoring_node
    )
    if thread_id is None:
        return await _invoke_search_graph(
            criteria=criteria,
            search_node=active_node,
            enrich_node=active_enrich_node,
            scoring_node=active_scoring_node,
            checkpointer=checkpointer,
            thread_id=None,
            checkpoint_ns=checkpoint_ns,
            checkpoint_id=checkpoint_id,
        )

    if checkpointer is not None:
        return await _invoke_search_graph(
            criteria=criteria,
            search_node=active_node,
            enrich_node=active_enrich_node,
            scoring_node=active_scoring_node,
            checkpointer=checkpointer,
            thread_id=thread_id,
            checkpoint_ns=checkpoint_ns,
            checkpoint_id=checkpoint_id,
        )

    async with get_async_postgres_checkpointer() as active_checkpointer:
        return await _invoke_search_graph(
            criteria=criteria,
            search_node=active_node,
            enrich_node=active_enrich_node,
            scoring_node=active_scoring_node,
            checkpointer=active_checkpointer,
            thread_id=thread_id,
            checkpoint_ns=checkpoint_ns,
            checkpoint_id=checkpoint_id,
        )


async def get_search_graph_state_history(
    *,
    thread_id: str,
    search_node: SearchNode | None = None,
    enrich_node: EnrichNode | None = None,
    scoring_node: ScoringNode | None = None,
    checkpointer: Any | None = None,
    checkpoint_ns: str = "",
) -> list[Any]:
    """Return checkpoint history for one thread."""
    active_node = search_node or create_default_search_node()
    active_enrich_node = enrich_node
    active_scoring_node = scoring_node
    config = build_checkpoint_config(thread_id=thread_id, checkpoint_ns=checkpoint_ns)

    async def _collect(active_checkpointer: Any) -> list[Any]:
        app = build_search_graph(
            active_node,
            enrich_node=active_enrich_node,
            scoring_node=active_scoring_node,
            checkpointer=active_checkpointer,
        )
        return [snapshot async for snapshot in app.aget_state_history(config)]

    if checkpointer is not None:
        return await _collect(checkpointer)

    async with get_async_postgres_checkpointer(setup=False) as active_checkpointer:
        return await _collect(active_checkpointer)


async def run_search_graph_from_text(
    *,
    user_id: int,
    message: str,
    intent_node: IntentNode | None = None,
    search_node: SearchNode | None = None,
    enrich_node: EnrichNode | None = None,
    scoring_node: ScoringNode | None = None,
    checkpointer: Any | None = None,
    thread_id: str | None = None,
    checkpoint_ns: str = "",
    checkpoint_id: str | None = None,
) -> list[EnrichedApartment]:
    """Parse free text into criteria and run the search graph."""
    active_intent_node = intent_node or IntentNode()
    criteria = active_intent_node.parse(user_id=user_id, message=message)
    return await run_search_graph(
        criteria,
        search_node=search_node,
        enrich_node=enrich_node,
        scoring_node=scoring_node,
        checkpointer=checkpointer,
        thread_id=thread_id,
        checkpoint_ns=checkpoint_ns,
        checkpoint_id=checkpoint_id,
    )

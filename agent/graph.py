"""LangGraph entrypoints for search workflows."""

from agent.models.criteria import SearchCriteria
from agent.models.enriched import EnrichedApartment


async def run_search_graph(criteria: SearchCriteria) -> list[EnrichedApartment]:
    """Execute apartment search workflow.

    This graph is implemented in Phase 3. The function is defined now so the
    project has a stable typed interface from the start.
    """
    raise NotImplementedError("Search graph is not implemented yet.")


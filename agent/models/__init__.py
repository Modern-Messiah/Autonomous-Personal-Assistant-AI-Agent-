"""Domain models for the Krisha agent."""

from agent.models.apartment import Apartment
from agent.models.criteria import SearchCriteria
from agent.models.enriched import EnrichedApartment
from agent.models.score import ApartmentScore

__all__ = ["Apartment", "ApartmentScore", "EnrichedApartment", "SearchCriteria"]


"""User-facing bot errors and messages shared by the service modules."""

from __future__ import annotations

SEARCH_EXECUTION_ERROR_MESSAGE = "Не удалось получить объявления. Попробуй позже."  # noqa: RUF001
SEARCH_BLOCKED_MESSAGE = (
    "Сайт временно ограничил доступ из-за защиты от ботов. Попробуй позже."
)


class ActiveCriteriaNotFoundError(RuntimeError):
    """Raised when a refinement flow requires active criteria but none are stored."""


class CriteriaUnchangedError(ValueError):
    """Raised when refinement text produces no change to the active criteria."""


class NoPreferencesError(RuntimeError):
    """Raised when /foryou has no saved apartments to learn the user's taste from."""


class SearchExecutionError(RuntimeError):
    """Raised when the search pipeline fails; carries a user-facing message."""

    def __init__(self, user_message: str = SEARCH_EXECUTION_ERROR_MESSAGE) -> None:
        super().__init__(user_message)
        self.user_message = user_message

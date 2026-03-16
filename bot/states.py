"""FSM states used by the Telegram dialog flow."""

from aiogram.fsm.state import State, StatesGroup


class SearchDialogStates(StatesGroup):
    """Dialog states for follow-up search refinement."""

    waiting_for_refinement = State()
    waiting_for_feedback = State()

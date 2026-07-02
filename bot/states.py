"""FSM states used by the Telegram dialog flow."""

from aiogram.fsm.state import State, StatesGroup


class SearchDialogStates(StatesGroup):
    """Dialog states for follow-up search refinement."""

    waiting_for_refinement = State()
    waiting_for_feedback = State()
    # Guided-refine menu is waiting for a typed value (rooms / budget / area /
    # a free-text city); which field is stored in FSM data under "refine_field".
    waiting_for_refine_value = State()

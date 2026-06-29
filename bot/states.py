from aiogram.fsm.state import State, StatesGroup


class AnalyzeStates(StatesGroup):
    waiting_input = State()


class DigestStates(StatesGroup):
    """FSM-состояния для /digest."""

    choosing_list = State()
    editing_channels = State()
    choosing_keywords = State()
    waiting_custom_word = State()
    removing_channels = State()
    choosing_schedule = State()
    choosing_schedule_day = State()
    choosing_schedule_time = State()
    confirmation = State()
    generating = State()


class TrendsStates(StatesGroup):
    """FSM-состояния для /trends."""

    choosing_list = State()
    editing_channels = State()
    removing_channels = State()
    choosing_period = State()
    generating = State()


class CompareStates(StatesGroup):
    """FSM-состояния для /compare."""

    choosing_channels = State()
    waiting_first_input = State()
    waiting_second_input = State()
    choosing_period = State()
    generating = State()

from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext

from bot.handlers.analyze import start_analyze
from bot.handlers.compare import start_compare
from bot.handlers.digest import start_digest
from bot.handlers.trends import start_trends
from bot.keyboards.inline import (
    ANALYZE_BTN,
    COMPARE_BTN,
    DIGEST_BTN,
    TRENDS_BTN,
    main_menu_keyboard,
)

router = Router()

_START_TEXT = (
    "👋 <b>Tech News Bot</b>\n\n"
    "Анализирую Telegram-каналы и помогаю следить за технологическими трендами.\n\n"
    "<b>Команды:</b>\n"
    "/analyze @username — детальный анализ канала\n"
    "/digest — дайджест лучших постов\n"
    "/trends — тренд-радар по темам\n"
    "/compare — сравнение двух каналов\n\n"
    "<i>/cancel — отменить текущую операцию</i>"
)


@router.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(_START_TEXT, reply_markup=main_menu_keyboard())


@router.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext) -> None:
    current = await state.get_state()
    await state.clear()
    if current is None:
        await message.answer("Нет активной операции.")
    else:
        await message.answer(
            "❌ Операция отменена. Используй /start чтобы начать заново."
        )


# Reply-кнопки главного меню → запуск команд
# Лежат в common_router (подключается первым), поэтому перехватывают
# нажатие раньше любых FSM-хендлеров и прерывают текущее действие.


@router.message(F.text == ANALYZE_BTN)
async def on_analyze_button(message: types.Message, state: FSMContext) -> None:
    await start_analyze(message, state)


@router.message(F.text == DIGEST_BTN)
async def on_digest_button(message: types.Message, state: FSMContext) -> None:
    await start_digest(message, state)


@router.message(F.text == TRENDS_BTN)
async def on_trends_button(message: types.Message, state: FSMContext) -> None:
    await start_trends(message, state)


@router.message(F.text == COMPARE_BTN)
async def on_compare_button(message: types.Message, state: FSMContext) -> None:
    await start_compare(message, state)

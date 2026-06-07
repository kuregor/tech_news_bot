from aiogram import Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext

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
    await message.answer(_START_TEXT)


@router.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext) -> None:
    current = await state.get_state()
    await state.clear()
    if current is None:
        await message.answer("Нет активной операции.")
    else:
        await message.answer("❌ Операция отменена. Используй /start чтобы начать заново.")

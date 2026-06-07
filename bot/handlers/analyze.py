import html
import logging

from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext

from bot.keyboards.inline import close_keyboard
from bot.states import AnalyzeStates
from bot.utils import not_a_command
from core.analyzer import AnalyzerService
from db.session import async_session

router = Router()
logger = logging.getLogger(__name__)


def _parse_username(raw: str) -> str:
    raw = raw.strip().lstrip("@")
    if "t.me/" in raw:
        raw = raw.split("t.me/")[-1].strip("/")
    return raw


async def _run_analysis(message: types.Message, username: str) -> None:
    progress_msg = await message.answer("⏳ Начинаем анализ...")

    async def update_progress(text: str) -> None:
        try:
            await progress_msg.edit_text(text)
        except Exception:
            pass

    try:
        async with async_session() as session:
            result = await AnalyzerService.run_analyze_pipeline(
                session, username, progress_callback=update_progress
            )

        formatted = AnalyzerService.format_analyze_result(result)
        try:
            await progress_msg.delete()
        except Exception:
            pass
        await message.answer(formatted, parse_mode="HTML")

    except Exception as e:
        logger.exception("Ошибка при анализе @%s", username)
        error_text = f"❌ Ошибка при анализе @{html.escape(username)}:\n{html.escape(str(e))}"
        try:
            await progress_msg.edit_text(error_text)
        except Exception:
            await message.answer(error_text)


@router.message(Command("analyze"))
async def cmd_analyze(message: types.Message, state: FSMContext) -> None:
    args = message.text.split(maxsplit=1)
    if len(args) < 2 or not args[1].strip():
        await message.answer("Введите ID канала:", reply_markup=close_keyboard())
        await state.set_state(AnalyzeStates.waiting_input)
        return

    username = _parse_username(args[1])
    if not username:
        await message.answer("Введите ID канала:", reply_markup=close_keyboard())
        await state.set_state(AnalyzeStates.waiting_input)
        return

    await _run_analysis(message, username)


@router.message(AnalyzeStates.waiting_input, not_a_command)
async def on_analyze_input(message: types.Message, state: FSMContext) -> None:
    username = _parse_username(message.text or "")
    if not username:
        await message.answer("Введите @username канала.")
        return
    await state.clear()
    await _run_analysis(message, username)


@router.callback_query(F.data == "analyze_close")
async def on_analyze_close(callback: types.CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()

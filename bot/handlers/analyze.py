import html
import logging

from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext

from bot.keyboards.inline import close_keyboard
from bot.states import AnalyzeStates
from bot.utils import not_a_command
from core.analyzer import (
    format_analyze_result,
    format_analyze_rich,
    run_analyze_pipeline,
)
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
            result = await run_analyze_pipeline(
                session, username, progress_callback=update_progress
            )

        formatted = format_analyze_result(result)
        rich_html = format_analyze_rich(result)
        try:
            await progress_msg.delete()
        except Exception:
            pass

        # rich-сообщение (Bot API 10.1) как основной формат вывода;
        # если клиент его не поддерживает — отправляем обычный HTML.
        try:
            from aiogram.types import InputRichMessage

            await message.bot.send_rich_message(
                chat_id=message.chat.id,
                rich_message=InputRichMessage(html=rich_html),
            )
        except Exception:
            logger.warning("send_rich_message не удался, откат на HTML", exc_info=True)
            await message.answer(formatted, parse_mode="HTML")

    except Exception as e:
        logger.exception("Ошибка при анализе @%s", username)
        error_text = (
            f"❌ Ошибка при анализе @{html.escape(username)}:\n{html.escape(str(e))}"
        )
        try:
            await progress_msg.edit_text(error_text)
        except Exception:
            await message.answer(error_text)


async def start_analyze(message: types.Message, state: FSMContext) -> None:
    """Точка входа в /analyze без аргументов — из reply-кнопки меню."""
    await state.clear()
    await message.answer("Введите ID канала:", reply_markup=close_keyboard())
    await state.set_state(AnalyzeStates.waiting_input)


@router.message(Command("analyze"))
async def cmd_analyze(message: types.Message, state: FSMContext) -> None:
    await state.clear()
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

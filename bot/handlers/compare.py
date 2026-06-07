import html
import logging

from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext

from bot.utils import not_a_command, send_long_message
from bot.keyboards.inline import close_keyboard, period_keyboard
from bot.states import CompareStates
from core.compare import CompareService
from db.session import async_session

router = Router()
logger = logging.getLogger(__name__)


def _parse_username(raw: str) -> str:
    raw = raw.strip().lstrip("@")
    if "t.me/" in raw:
        raw = raw.split("t.me/")[-1].strip("/")
    return raw


def _extract_usernames(text: str) -> list[str]:
    """Извлечь @username'ы из произвольного текста."""
    parts = text.split()
    result = []
    for p in parts:
        u = _parse_username(p)
        if u:
            result.append(u)
    return result


# ─── /compare — старт ───────────────────────────────────

@router.message(Command("compare"))
async def cmd_compare(message: types.Message, state: FSMContext) -> None:
    await state.clear()

    args = (message.text or "").split()[1:]
    usernames = [_parse_username(a) for a in args if a]
    usernames = [u for u in usernames if u]

    if len(usernames) >= 2:
        await state.update_data(ch1_username=usernames[0], ch2_username=usernames[1])
        await message.answer(
            f"✅ @{html.escape(usernames[0])} vs @{html.escape(usernames[1])}\n\n"
            "📅 Выберите период:",
            reply_markup=period_keyboard(),
        )
        await state.set_state(CompareStates.choosing_period)
        return

    await message.answer(
        "⚖️ Введите ID двух каналов сразу или по-отдельности:",
        reply_markup=close_keyboard(),
    )
    await state.set_state(CompareStates.waiting_first_input)


# ─── Шаг 1: ввод одного или двух каналов сразу ──────────

@router.message(CompareStates.waiting_first_input, not_a_command)
async def on_first_input(message: types.Message, state: FSMContext) -> None:
    usernames = _extract_usernames(message.text or "")

    if len(usernames) >= 2:
        await state.update_data(ch1_username=usernames[0], ch2_username=usernames[1])
        await message.answer(
            f"✅ @{html.escape(usernames[0])} vs @{html.escape(usernames[1])}\n\n"
            "📅 Выберите период:",
            reply_markup=period_keyboard(),
        )
        await state.set_state(CompareStates.choosing_period)
        return

    if len(usernames) == 1:
        await state.update_data(ch1_username=usernames[0])
        await message.answer(
            f"✅ Первый: @{html.escape(usernames[0])}\n\nВведите второй канал:"
        )
        await state.set_state(CompareStates.waiting_second_input)
        return

    await message.answer("Отправьте @username каналов.")


# ─── Шаг 2: второй канал ────────────────────────────────

@router.message(CompareStates.waiting_second_input, not_a_command)
async def on_second_input(message: types.Message, state: FSMContext) -> None:
    usernames = _extract_usernames(message.text or "")
    if not usernames:
        await message.answer("Отправьте @username канала.")
        return

    data = await state.get_data()
    ch2 = usernames[0]

    if ch2 == data.get("ch1_username"):
        await message.answer("Введите другой канал (не совпадающий с первым).")
        return

    await state.update_data(ch2_username=ch2)
    await message.answer(
        f"✅ @{html.escape(data['ch1_username'])} vs @{html.escape(ch2)}\n\n"
        "📅 Выберите период:",
        reply_markup=period_keyboard(),
    )
    await state.set_state(CompareStates.choosing_period)


# ─── Шаг 3: период и генерация ──────────────────────────

@router.callback_query(CompareStates.choosing_period, F.data.startswith("period:"))
async def on_period(callback: types.CallbackQuery, state: FSMContext) -> None:
    period_days = int(callback.data.split(":")[1])
    await state.update_data(period_days=period_days)
    await state.set_state(CompareStates.generating)
    await callback.answer()

    data = await state.get_data()
    progress_msg = await callback.message.edit_text("⏳ Сравниваем каналы...")

    async def update_progress(text: str) -> None:
        try:
            await progress_msg.edit_text(text)
        except Exception:
            pass

    try:
        result = await CompareService.run_compare_pipeline(
            ch1_username=data["ch1_username"],
            ch2_username=data["ch2_username"],
            period_days=period_days,
            progress_callback=update_progress,
        )

        formatted = CompareService.format_compare(result)
        try:
            await progress_msg.delete()
        except Exception:
            pass

        await send_long_message(callback.message, formatted, parse_mode="HTML")

    except Exception as e:
        logger.exception("Ошибка при сравнении каналов")
        error_text = f"❌ Ошибка при сравнении каналов:\n{html.escape(str(e))}"
        try:
            await progress_msg.edit_text(error_text)
        except Exception:
            await callback.message.answer(error_text)

    await state.clear()

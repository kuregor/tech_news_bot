import html
import logging

from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext

from bot.utils import not_a_command, send_long_message
from bot.keyboards.inline import (
    editing_done_keyboard,
    period_keyboard,
    trends_list_keyboard,
    trends_quick_keyboard,
)
from bot.states import TrendsStates
from core.parser import telegram_parser
from core.trends import TrendsService
from db.repository import (
    add_channel_to_list,
    create_channel_list,
    get_default_list,
    get_list_channels,
    upsert_channel,
)
from db.session import async_session

router = Router()
logger = logging.getLogger(__name__)


# ─── /trends — старт FSM ──────────────────────────────────

@router.message(Command("trends"))
async def cmd_trends(message: types.Message, state: FSMContext) -> None:
    await state.clear()
    user_id = message.from_user.id

    async with async_session() as session:
        ch_list = await get_default_list(session, user_id)
        channels = await get_list_channels(session, ch_list.id) if ch_list else []

    if ch_list and channels:
        channels_str = " · ".join(f"@{c.username}" for c in channels)
        await state.update_data(
            channel_list_id=ch_list.id,
            channel_ids=[c.id for c in channels],
            channel_names=[c.username for c in channels],
        )
        await message.answer(
            f"📈 <b>Тренд-радар</b>\n\n📣 Каналы: {channels_str}",
            reply_markup=trends_quick_keyboard(),
        )
    else:
        await message.answer(
            "📈 <b>Тренд-радар</b>\n\nВыберите список каналов:",
            reply_markup=trends_list_keyboard(),
        )
    await state.set_state(TrendsStates.choosing_list)


# ─── QuickStart (повторный запуск) ────────────────────────

@router.callback_query(TrendsStates.choosing_list, F.data == "trends_quick:go")
async def on_trends_quick_go(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await callback.message.edit_text(
        "📅 Выберите период:",
        reply_markup=period_keyboard(),
    )
    await state.set_state(TrendsStates.choosing_period)


@router.callback_query(TrendsStates.choosing_list, F.data == "trends_quick:edit")
async def on_trends_quick_edit(callback: types.CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    channel_names = data.get("channel_names", [])
    names_str = ", ".join(f"@{n}" for n in channel_names) if channel_names else "пусто"
    await callback.message.edit_text(
        f"✏️ Текущие каналы: {names_str}\n\nОтправьте @username чтобы добавить канал:",
        reply_markup=editing_done_keyboard(),
    )
    await state.set_state(TrendsStates.editing_channels)
    await callback.answer()


# ─── Шаг 1: выбор списка каналов ──────────────────────────

@router.callback_query(TrendsStates.choosing_list, F.data.startswith("trends_list:"))
async def on_choose_list(callback: types.CallbackQuery, state: FSMContext) -> None:
    choice = callback.data.split(":")[1]
    user_id = callback.from_user.id

    async with async_session() as session:
        if choice == "default":
            ch_list = await get_default_list(session, user_id)
            if not ch_list:
                ch_list = await create_channel_list(session, user_id, is_default=True)

            channels = await get_list_channels(session, ch_list.id)
            if not channels:
                await state.update_data(
                    channel_list_id=ch_list.id, channel_ids=[], channel_names=[]
                )
                await callback.message.edit_text(
                    "📋 Стандартный список пуст.\n"
                    "Отправьте @username каналов по одному:",
                    reply_markup=editing_done_keyboard(),
                )
                await state.set_state(TrendsStates.editing_channels)
                return

            channel_ids = [c.id for c in channels]
            channel_names = [c.username for c in channels]
            await state.update_data(
                channel_list_id=ch_list.id,
                channel_ids=channel_ids,
                channel_names=channel_names,
            )
            await callback.message.edit_text(
                "📅 Выберите период:",
                reply_markup=period_keyboard(),
            )
            await state.set_state(TrendsStates.choosing_period)
            await callback.answer()
            return

        elif choice == "edit":
            ch_list = await get_default_list(session, user_id)
            if not ch_list:
                ch_list = await create_channel_list(session, user_id, is_default=True)
            channels = await get_list_channels(session, ch_list.id)
            channel_ids = [c.id for c in channels]
            channel_names = [c.username for c in channels]
            names_str = ", ".join(f"@{n}" for n in channel_names) if channel_names else "пусто"
            await state.update_data(
                channel_list_id=ch_list.id,
                channel_ids=channel_ids,
                channel_names=channel_names,
            )
            await callback.message.edit_text(
                f"✏️ Текущие каналы: {names_str}\n\n"
                "Отправьте @username чтобы добавить канал:",
                reply_markup=editing_done_keyboard(),
            )
            await state.set_state(TrendsStates.editing_channels)
            return

        elif choice == "create":
            ch_list = await create_channel_list(session, user_id, is_default=False)
            await state.update_data(
                channel_list_id=ch_list.id, channel_ids=[], channel_names=[]
            )
            await callback.message.edit_text(
                "➕ Новый список.\n"
                "Отправьте @username каналов по одному:",
                reply_markup=editing_done_keyboard(),
            )
            await state.set_state(TrendsStates.editing_channels)
            return

    await callback.answer()


# ─── Шаг 2: редактирование каналов ────────────────────────

@router.message(TrendsStates.editing_channels, not_a_command)
async def on_add_channel(message: types.Message, state: FSMContext) -> None:
    """Добавление канала по @username."""
    raw = message.text.strip().lstrip("@")
    if "t.me/" in raw:
        username = raw.split("t.me/")[-1].strip("/")
    else:
        username = raw

    if not username:
        await message.answer("Отправьте @username канала.")
        return

    data = await state.get_data()
    channel_ids = data.get("channel_ids", [])
    channel_names = data.get("channel_names", [])
    list_id = data.get("channel_list_id")

    try:
        async with async_session() as session:
            channel_info = await telegram_parser.parse_channel_info(username)
            channel = await upsert_channel(
                session,
                username=username,
                title=channel_info["title"],
                description=channel_info["description"],
                subscribers_count=channel_info["subscribers_count"],
            )
            if channel.id not in channel_ids:
                channel_ids.append(channel.id)
                channel_names.append(username)
                if list_id:
                    await add_channel_to_list(session, list_id, channel.id)

        await state.update_data(channel_ids=channel_ids, channel_names=channel_names)
        names_str = ", ".join(f"@{n}" for n in channel_names)
        await message.answer(
            f"✅ @{username} добавлен.\n"
            f"📋 Каналы: {names_str}\n\n"
            "Добавьте ещё или нажмите Готово.",
            reply_markup=editing_done_keyboard(),
        )
    except Exception as e:
        logger.warning("Не удалось добавить канал @%s: %s", username, e)
        await message.answer(f"❌ Не удалось найти канал @{html.escape(username)}")


@router.callback_query(TrendsStates.editing_channels, F.data == "editing_done")
async def on_editing_done(callback: types.CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    channel_ids = data.get("channel_ids", [])

    if not channel_ids:
        await callback.answer("Добавьте хотя бы один канал!", show_alert=True)
        return

    await callback.message.edit_text(
        "📅 Выберите период:",
        reply_markup=period_keyboard(),
    )
    await state.set_state(TrendsStates.choosing_period)
    await callback.answer()


# ─── Шаг 3: выбор периода и генерация ─────────────────────

@router.callback_query(TrendsStates.choosing_period, F.data.startswith("period:"))
async def on_period(callback: types.CallbackQuery, state: FSMContext) -> None:
    period_days = int(callback.data.split(":")[1])
    await state.update_data(period_days=period_days)
    await state.set_state(TrendsStates.generating)
    await callback.answer()

    data = await state.get_data()
    progress_msg = await callback.message.edit_text("⏳ Считаем тренды...")

    async def update_progress(text: str) -> None:
        try:
            await progress_msg.edit_text(text)
        except Exception:
            pass

    try:
        async with async_session() as session:
            result = await TrendsService.run_trends_pipeline(
                session=session,
                channel_ids=data["channel_ids"],
                channel_names=data["channel_names"],
                period_days=data["period_days"],
                progress_callback=update_progress,
            )

        formatted = TrendsService.format_trends(result)
        try:
            await progress_msg.delete()
        except Exception:
            pass

        await send_long_message(callback.message, formatted)

    except Exception as e:
        logger.exception("Ошибка при формировании трендов")
        error_text = f"❌ Ошибка при формировании трендов:\n{html.escape(str(e))}"
        try:
            await progress_msg.edit_text(error_text)
        except Exception:
            await callback.message.answer(error_text)

    await state.clear()

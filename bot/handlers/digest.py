import html
import logging

from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext

from bot.keyboards.inline import (
    back_keyboard,
    confirmation_keyboard,
    digest_edit_choice_keyboard,
    digest_quick_keyboard,
    editing_done_keyboard,
    keywords_keyboard,
    schedule_day_keyboard,
    schedule_keyboard,
    schedule_time_keyboard,
)
from bot.states import DigestStates
from bot.utils import not_a_command, send_long_message
from core.digest import (
    format_digest,
    format_digest_rich,
    run_digest_pipeline,
)
from core.parser import telegram_parser
from core.scheduler import PERIOD_BY_SCHEDULE, digest_scheduler
from db.repository import (
    add_channel_to_list,
    create_channel_list,
    get_default_list,
    get_list_channels,
    get_topics_by_channel,
    remove_channel_from_list,
    update_channel_list_schedule,
    upsert_channel,
)
from db.session import async_session

router = Router()
logger = logging.getLogger(__name__)

_SCHEDULE_LABELS = {
    "daily": "Ежедневно",
    "weekly": "Еженедельно",
    None: "не настроено",
}
_DAY_NAMES = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def _parse_time(text: str) -> tuple[int, int] | None:
    """Распарсить время: '9' → (9,0), '9:30' → (9,30), '23:20' → (23,20)."""
    text = text.strip().replace(".", ":").replace("-", ":")
    try:
        if ":" in text:
            parts = text.split(":")
            hour, minute = int(parts[0]), int(parts[1])
        else:
            hour, minute = int(text), 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
        return None
    except ValueError:
        return None


def _schedule_label(
    schedule_type: str | None,
    schedule_day: int | None,
    schedule_hour: int | None,
    schedule_minute: int | None = None,
) -> str:
    if not schedule_type:
        return "не настроено"
    label = _SCHEDULE_LABELS.get(schedule_type, schedule_type)
    if schedule_type == "weekly" and schedule_day is not None:
        label += f" ({_DAY_NAMES[schedule_day]})"
    if schedule_hour is not None:
        minute = schedule_minute or 0
        label += f" в {schedule_hour}:{minute:02d}"
    return label


def _build_summary(channels: list, channel_list) -> str:
    channels_str = " · ".join(f"@{c.username}" for c in channels)
    schedule = _schedule_label(
        channel_list.schedule_type,
        channel_list.schedule_day,
        channel_list.schedule_hour,
        channel_list.schedule_minute,
    )
    keywords = channel_list.filter_keywords
    lines = [
        "📋 <b>Ваш дайджест</b>",
        "",
        f"📣 Каналы: {channels_str}",
    ]
    if keywords:
        lines.append(f"🔑 Фильтр: {', '.join(keywords)}")
    lines.append(f"⏰ Расписание: {schedule}")
    return "\n".join(lines)


# /digest — точка входа


async def start_digest(message: types.Message, state: FSMContext) -> None:
    """Точка входа в /digest — из команды и из reply-кнопки меню."""
    await state.clear()
    user_id = message.from_user.id

    async with async_session() as session:
        ch_list = await get_default_list(session, user_id)
        channels = await get_list_channels(session, ch_list.id) if ch_list else []

    if ch_list and channels:
        # Повторный запуск — показываем сводку и быстрые кнопки
        await state.update_data(
            channel_list_id=ch_list.id,
            channel_ids=[c.id for c in channels],
            channel_names=[c.username for c in channels],
        )
        await message.answer(
            _build_summary(channels, ch_list),
            reply_markup=digest_quick_keyboard(),
        )
        await state.set_state(DigestStates.choosing_list)
    else:
        # Первый запуск — сразу стадия добавления каналов
        async with async_session() as session:
            if not ch_list:
                ch_list = await create_channel_list(session, user_id, is_default=True)
        await state.update_data(
            channel_list_id=ch_list.id, channel_ids=[], channel_names=[]
        )
        await message.answer(
            "➕ Заполните список\nОтправьте @username каналов по одному или через пробел:",
            reply_markup=editing_done_keyboard(back=True),
        )
        await state.set_state(DigestStates.editing_channels)


@router.message(Command("digest"))
async def cmd_digest(message: types.Message, state: FSMContext) -> None:
    await start_digest(message, state)


# Быстрый запуск (повторный пользователь)


@router.callback_query(DigestStates.choosing_list, F.data == "digest_quick:go")
async def on_quick_go(callback: types.CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.set_state(DigestStates.generating)
    await callback.answer()

    async with async_session() as session:
        ch_list = await get_default_list(session, callback.from_user.id)

    period_days = PERIOD_BY_SCHEDULE.get(ch_list.schedule_type if ch_list else None, 7)
    keywords = ch_list.filter_keywords if ch_list else None

    await _run_pipeline(
        callback=callback,
        state=state,
        channel_ids=data["channel_ids"],
        channel_names=data["channel_names"],
        period_days=period_days,
        keywords=keywords,
        channel_list_id=data.get("channel_list_id"),
    )


@router.callback_query(DigestStates.choosing_list, F.data == "digest_quick:edit")
async def on_quick_edit(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.message.edit_text(
        "✏️ Что хотите изменить?",
        reply_markup=digest_edit_choice_keyboard(),
    )
    await callback.answer()


@router.callback_query(DigestStates.choosing_list, F.data.startswith("digest_edit:"))
async def on_edit_choice(callback: types.CallbackQuery, state: FSMContext) -> None:
    choice = callback.data.split(":")[1]
    data = await state.get_data()

    if choice == "channels":
        await state.update_data(from_edit_menu=True)
        channel_names = data.get("channel_names", [])
        names_str = (
            ", ".join(f"@{n}" for n in channel_names) if channel_names else "пусто"
        )
        await callback.message.edit_text(
            f"✏️ Текущие каналы: {names_str}\n\nОтправьте @username чтобы добавить канал:",
            reply_markup=editing_done_keyboard(back=True),
        )
        await state.set_state(DigestStates.editing_channels)

    elif choice == "schedule":
        await state.update_data(from_edit_menu=True)
        await callback.message.edit_text(
            "⏰ Как часто присылать дайджест?",
            reply_markup=schedule_keyboard(),
        )
        await state.set_state(DigestStates.choosing_schedule)

    elif choice == "filter":
        channel_ids = data.get("channel_ids", [])
        await state.update_data(from_edit_menu=True)
        async with async_session() as session:
            await _go_to_keywords(callback.message, state, session, channel_ids)

    elif choice == "back":
        async with async_session() as session:
            ch_list = await get_default_list(session, callback.from_user.id)
            channels = await get_list_channels(session, ch_list.id) if ch_list else []
        await callback.message.edit_text(
            _build_summary(channels, ch_list),
            reply_markup=digest_quick_keyboard(),
        )

    await callback.answer()


# Шаг 2: редактирование каналов


@router.message(DigestStates.editing_channels, not_a_command)
async def on_add_channel(message: types.Message, state: FSMContext) -> None:
    parts = message.text.strip().split()
    usernames = []
    for p in parts:
        u = p.lstrip("@")
        if "t.me/" in u:
            u = u.split("t.me/")[-1].strip("/")
        if u:
            usernames.append(u)

    if not usernames:
        await message.answer("Отправьте @username каналов.")
        return

    data = await state.get_data()
    channel_ids = data.get("channel_ids", [])
    channel_names = data.get("channel_names", [])
    list_id = data.get("channel_list_id")

    added, failed = [], []
    for username in usernames:
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
            async with async_session() as session:
                if channel.id not in channel_ids:
                    channel_ids.append(channel.id)
                    channel_names.append(username)
                    if list_id:
                        await add_channel_to_list(session, list_id, channel.id)
            added.append(username)
        except Exception as e:
            logger.warning("Не удалось добавить канал @%s: %s", username, e)
            failed.append(username)

    await state.update_data(channel_ids=channel_ids, channel_names=channel_names)
    lines = []
    if added:
        lines.append(f"✅ Добавлено: {', '.join('@' + u for u in added)}")
    if failed:
        lines.append(f"❌ Не найдено: {', '.join('@' + u for u in failed)}")
    remaining = " · ".join(f"@{n}" for n in channel_names)
    lines.append(f"\n📋 Каналы: {remaining}\n\nДобавьте ещё или нажмите Готово.")
    await message.answer(
        "\n".join(lines), reply_markup=editing_done_keyboard(back=True)
    )


@router.callback_query(DigestStates.editing_channels, F.data == "editing_done")
async def on_editing_done(callback: types.CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    channel_ids = data.get("channel_ids", [])

    if not channel_ids:
        await callback.answer("Добавьте хотя бы один канал!", show_alert=True)
        return

    if data.get("from_edit_menu"):
        await state.update_data(from_edit_menu=False)
        await callback.message.edit_text(
            "✏️ Что хотите изменить?",
            reply_markup=digest_edit_choice_keyboard(),
        )
        await state.set_state(DigestStates.choosing_list)
        await callback.answer("✅ Каналы обновлены")
        return

    async with async_session() as session:
        await _go_to_keywords(callback.message, state, session, channel_ids)
    await callback.answer()


@router.callback_query(DigestStates.editing_channels, F.data == "editing_back")
async def on_editing_back(callback: types.CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if data.get("from_edit_menu"):
        await state.update_data(from_edit_menu=False)
        await callback.message.edit_text(
            "✏️ Что хотите изменить?",
            reply_markup=digest_edit_choice_keyboard(),
        )
        await state.set_state(DigestStates.choosing_list)
    else:
        await state.clear()
        await callback.message.edit_text(
            "↩️ Отменено. Нажмите /digest или кнопку «📰 Дайджест», чтобы начать заново."
        )
    await callback.answer()


@router.callback_query(DigestStates.editing_channels, F.data == "editing_remove")
async def on_editing_remove(callback: types.CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    names_str = (
        " · ".join(f"@{n}" for n in data.get("channel_names", [])) or "список пуст"
    )
    await callback.message.edit_text(
        f"🗑 Текущие каналы: {names_str}\n\nВведите @username каналов для удаления через пробел:",
        reply_markup=back_keyboard("remove_back"),
    )
    await state.set_state(DigestStates.removing_channels)
    await callback.answer()


@router.callback_query(DigestStates.removing_channels, F.data == "remove_back")
async def on_remove_back(callback: types.CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    names_str = " · ".join(f"@{n}" for n in data.get("channel_names", [])) or "пусто"
    await callback.message.edit_text(
        f"✏️ Текущие каналы: {names_str}\n\nОтправьте @username чтобы добавить канал:",
        reply_markup=editing_done_keyboard(back=True),
    )
    await state.set_state(DigestStates.editing_channels)
    await callback.answer()


@router.message(DigestStates.removing_channels, not_a_command)
async def on_remove_channels(message: types.Message, state: FSMContext) -> None:
    parts = message.text.strip().split()
    to_remove = {
        p.lstrip("@").split("t.me/")[-1].strip("/").lower() for p in parts if p
    }

    data = await state.get_data()
    channel_ids = data.get("channel_ids", [])
    channel_names = data.get("channel_names", [])
    list_id = data.get("channel_list_id")

    removed, new_ids, new_names = [], [], []
    for cid, cname in zip(channel_ids, channel_names):
        if cname.lower() in to_remove:
            removed.append(cname)
            if list_id:
                async with async_session() as session:
                    await remove_channel_from_list(session, list_id, cid)
        else:
            new_ids.append(cid)
            new_names.append(cname)

    not_found = [u for u in to_remove if u not in {n.lower() for n in removed}]
    await state.update_data(channel_ids=new_ids, channel_names=new_names)

    lines = []
    if removed:
        lines.append(f"✅ Удалено: {', '.join('@' + n for n in removed)}")
    if not_found:
        lines.append(f"⚠️ Не найдено: {', '.join('@' + u for u in not_found)}")
    remaining = " · ".join(f"@{n}" for n in new_names) or "список пуст"
    lines.append(f"📋 Каналы: {remaining}\n\nДобавьте ещё или нажмите Готово.")
    await message.answer(
        "\n".join(lines), reply_markup=editing_done_keyboard(back=True)
    )
    await state.set_state(DigestStates.editing_channels)


# Шаг 3: выбор ключевых слов


async def _go_to_keywords(message, state, session, channel_ids: list[int]) -> None:
    topics_set = set()
    for ch_id in channel_ids:
        topics = await get_topics_by_channel(session, ch_id, min_percentage=0.3)
        for t in topics:
            topics_set.add(t.label)

    topics_list = sorted(topics_set)[:8]
    await state.update_data(filter_keywords=[], topics_list=topics_list)
    await message.edit_text(
        "🔑 Добавьте слово-фильтр или нажмите «Без фильтра»:",
        reply_markup=keywords_keyboard(topics_list, show_topics=False),
    )
    await state.set_state(DigestStates.choosing_keywords)


@router.callback_query(DigestStates.choosing_keywords, F.data.startswith("keyword:"))
async def on_keyword(callback: types.CallbackQuery, state: FSMContext) -> None:
    value = callback.data.split(":", 1)[1]

    if value == "none":
        await state.update_data(filter_keywords=None)
        data = await state.get_data()
        if data.get("from_edit_menu"):
            await _save_filter_and_return_to_edit(callback, state)
            return
        await callback.message.edit_text(
            "⏰ Как часто присылать дайджест?",
            reply_markup=schedule_keyboard(),
        )
        await state.set_state(DigestStates.choosing_schedule)
        await callback.answer()
        return

    if value == "custom":
        await callback.message.edit_text(
            "✍️ Введите ключевое слово:",
            reply_markup=back_keyboard("custom_back"),
        )
        await state.set_state(DigestStates.waiting_custom_word)
        await callback.answer()
        return

    data = await state.get_data()
    keywords = data.get("filter_keywords") or []
    if value not in keywords:
        keywords.append(value)
    await state.update_data(filter_keywords=keywords)

    topics_list = data.get("topics_list", [])
    await callback.message.edit_reply_markup(
        reply_markup=keywords_keyboard(topics_list, keywords)
    )
    await callback.answer()


async def _save_filter_and_return_to_edit(
    callback: types.CallbackQuery, state: FSMContext
) -> None:
    data = await state.get_data()
    list_id = data.get("channel_list_id")
    keywords = data.get("filter_keywords")

    if list_id:
        async with async_session() as session:
            ch_list = await get_default_list(session, callback.from_user.id)
            if ch_list:
                await update_channel_list_schedule(
                    session,
                    list_id,
                    ch_list.schedule_type,
                    ch_list.schedule_day,
                    ch_list.schedule_hour,
                    filter_keywords=keywords,
                )

    await state.update_data(from_edit_menu=False)
    await callback.message.edit_text(
        "✏️ Что хотите изменить?",
        reply_markup=digest_edit_choice_keyboard(),
    )
    await state.set_state(DigestStates.choosing_list)
    await callback.answer("✅ Фильтр обновлён")


@router.callback_query(DigestStates.choosing_keywords, F.data == "keywords_done")
async def on_keywords_done(callback: types.CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("filter_keywords"):
        await state.update_data(filter_keywords=None)

    if data.get("from_edit_menu"):
        await _save_filter_and_return_to_edit(callback, state)
        return

    await callback.message.edit_text(
        "⏰ Как часто присылать дайджест?",
        reply_markup=schedule_keyboard(),
    )
    await state.set_state(DigestStates.choosing_schedule)
    await callback.answer()


@router.callback_query(DigestStates.choosing_keywords, F.data == "keywords_back")
async def on_keywords_back(callback: types.CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if data.get("from_edit_menu"):
        await state.update_data(from_edit_menu=False)
        await callback.message.edit_text(
            "✏️ Что хотите изменить?",
            reply_markup=digest_edit_choice_keyboard(),
        )
        await state.set_state(DigestStates.choosing_list)
    else:
        channel_names = data.get("channel_names", [])
        names_str = " · ".join(f"@{n}" for n in channel_names) or "пусто"
        await callback.message.edit_text(
            f"✏️ Текущие каналы: {names_str}\n\nОтправьте @username чтобы добавить канал:",
            reply_markup=editing_done_keyboard(back=True),
        )
        await state.set_state(DigestStates.editing_channels)
    await callback.answer()


# Шаг 4: своё слово


@router.message(DigestStates.waiting_custom_word, not_a_command)
async def on_custom_word(message: types.Message, state: FSMContext) -> None:
    word = message.text.strip()
    if not word:
        await message.answer("Введите слово.")
        return

    data = await state.get_data()
    keywords = data.get("filter_keywords") or []
    keywords.append(word)
    await state.update_data(filter_keywords=keywords)

    topics_list = data.get("topics_list", [])
    kw_str = ", ".join(keywords)
    await message.answer(
        f"✅ Добавлено: {word}\n"
        f"🔑 Текущий фильтр: {kw_str}\n\n"
        "Добавьте ещё или нажмите Готово:",
        reply_markup=keywords_keyboard(topics_list, keywords, show_topics=False),
    )
    await state.set_state(DigestStates.choosing_keywords)


@router.callback_query(DigestStates.waiting_custom_word, F.data == "custom_back")
async def on_custom_back(callback: types.CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    topics_list = data.get("topics_list", [])
    keywords = data.get("filter_keywords") or []
    await callback.message.edit_text(
        "🔑 Добавьте слово-фильтр или нажмите «Без фильтра»:",
        reply_markup=keywords_keyboard(topics_list, keywords, show_topics=False),
    )
    await state.set_state(DigestStates.choosing_keywords)
    await callback.answer()


# Шаг 5: выбор расписания


@router.callback_query(DigestStates.choosing_schedule, F.data.startswith("schedule:"))
async def on_schedule(callback: types.CallbackQuery, state: FSMContext) -> None:
    schedule_type = callback.data.split(":")[1]  # "daily" / "weekly" / "none" / "back"

    if schedule_type == "back":
        data = await state.get_data()
        if data.get("from_edit_menu"):
            await state.update_data(from_edit_menu=False)
            await callback.message.edit_text(
                "✏️ Что хотите изменить?",
                reply_markup=digest_edit_choice_keyboard(),
            )
            await state.set_state(DigestStates.choosing_list)
        else:
            async with async_session() as session:
                await _go_to_keywords(
                    callback.message, state, session, data.get("channel_ids", [])
                )
        await callback.answer()
        return

    await state.update_data(
        schedule_type=schedule_type if schedule_type != "none" else None
    )

    if schedule_type == "none":
        await state.update_data(schedule_day=None, schedule_hour=None)
        await _show_confirmation(callback, state)
    elif schedule_type == "weekly":
        await callback.message.edit_text(
            "📅 В какой день недели присылать?",
            reply_markup=schedule_day_keyboard(),
        )
        await state.set_state(DigestStates.choosing_schedule_day)
    else:  # daily
        await callback.message.edit_text(
            "🕐 В какое время присылать?\n<i>Или введите своё время в формате 23:20</i>",
            reply_markup=schedule_time_keyboard(),
        )
        await state.set_state(DigestStates.choosing_schedule_time)
    await callback.answer()


@router.callback_query(
    DigestStates.choosing_schedule_day, F.data.startswith("schedule_day:")
)
async def on_schedule_day(callback: types.CallbackQuery, state: FSMContext) -> None:
    value = callback.data.split(":")[1]
    if value == "back":
        await callback.message.edit_text(
            "⏰ Как часто присылать дайджест?",
            reply_markup=schedule_keyboard(),
        )
        await state.set_state(DigestStates.choosing_schedule)
        await callback.answer()
        return

    await state.update_data(schedule_day=int(value))
    await callback.message.edit_text(
        "🕐 В какое время присылать?\n<i>Или введите своё время в формате 23:20</i>",
        reply_markup=schedule_time_keyboard(),
    )
    await state.set_state(DigestStates.choosing_schedule_time)
    await callback.answer()


@router.callback_query(
    DigestStates.choosing_schedule_time, F.data.startswith("schedule_time:")
)
async def on_schedule_time(callback: types.CallbackQuery, state: FSMContext) -> None:
    value = callback.data.split(":")[1]
    if value == "back":
        data = await state.get_data()
        if data.get("schedule_type") == "weekly":
            await callback.message.edit_text(
                "📅 В какой день недели присылать?",
                reply_markup=schedule_day_keyboard(),
            )
            await state.set_state(DigestStates.choosing_schedule_day)
        else:
            await callback.message.edit_text(
                "⏰ Как часто присылать дайджест?",
                reply_markup=schedule_keyboard(),
            )
            await state.set_state(DigestStates.choosing_schedule)
        await callback.answer()
        return

    await state.update_data(schedule_hour=int(value), schedule_minute=0)
    await _show_confirmation(callback, state)
    await callback.answer()


@router.message(DigestStates.choosing_schedule_time, not_a_command)
async def on_schedule_time_text(message: types.Message, state: FSMContext) -> None:
    parsed = _parse_time(message.text.strip())
    if parsed is None:
        await message.answer(
            "Не распознал время. Введите в формате <b>ЧЧ:ММ</b>, например: 9:00 или 23:30"
        )
        return
    hour, minute = parsed
    await state.update_data(schedule_hour=hour, schedule_minute=minute)

    data = await state.get_data()
    channel_names = data.get("channel_names", [])
    keywords = data.get("filter_keywords")
    schedule_type = data.get("schedule_type")
    schedule_day = data.get("schedule_day")

    channels_str = " · ".join(f"@{n}" for n in channel_names)
    filter_str = ", ".join(keywords) if keywords else "без фильтра"
    schedule_str = _schedule_label(schedule_type, schedule_day, hour, minute)

    await message.answer(
        f"📰 <b>Сводка дайджеста</b>\n\n"
        f"📣 Каналы: {channels_str}\n"
        f"🔑 Фильтр: {filter_str}\n"
        f"⏰ Расписание: {schedule_str}\n\n"
        "Всё верно?",
        reply_markup=confirmation_keyboard(),
    )
    await state.set_state(DigestStates.confirmation)


# Шаг 6: подтверждение


async def _show_confirmation(callback: types.CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    channel_names = data.get("channel_names", [])
    keywords = data.get("filter_keywords")
    schedule_type = data.get("schedule_type")
    schedule_day = data.get("schedule_day")
    schedule_hour = data.get("schedule_hour")

    channels_str = " · ".join(f"@{n}" for n in channel_names)
    filter_str = ", ".join(keywords) if keywords else "без фильтра"
    schedule_str = _schedule_label(schedule_type, schedule_day, schedule_hour)

    await callback.message.edit_text(
        f"📰 <b>Сводка дайджеста</b>\n\n"
        f"📣 Каналы: {channels_str}\n"
        f"🔑 Фильтр: {filter_str}\n"
        f"⏰ Расписание: {schedule_str}\n\n"
        "Всё верно?",
        reply_markup=confirmation_keyboard(),
    )
    await state.set_state(DigestStates.confirmation)


@router.callback_query(DigestStates.confirmation, F.data.startswith("confirm:"))
async def on_confirm(callback: types.CallbackQuery, state: FSMContext) -> None:
    action = callback.data.split(":")[1]

    if action == "edit":
        await callback.message.edit_text(
            "✏️ Что хотите изменить?",
            reply_markup=digest_edit_choice_keyboard(),
        )
        await state.set_state(DigestStates.choosing_list)
        await callback.answer()
        return

    if action == "back":
        await callback.message.edit_text(
            "⏰ Как часто присылать дайджест?",
            reply_markup=schedule_keyboard(),
        )
        await state.set_state(DigestStates.choosing_schedule)
        await callback.answer()
        return

    data = await state.get_data()
    list_id = data.get("channel_list_id")
    schedule_type = data.get("schedule_type")
    schedule_day = data.get("schedule_day")
    schedule_hour = data.get("schedule_hour")
    schedule_minute = data.get("schedule_minute")
    keywords = data.get("filter_keywords")

    # Сохраняем расписание и фильтр в БД
    if list_id:
        async with async_session() as session:
            await update_channel_list_schedule(
                session,
                list_id,
                schedule_type,
                schedule_day,
                schedule_hour,
                schedule_minute=schedule_minute,
                filter_keywords=keywords,
            )

        # Обновляем APScheduler
        if schedule_type:
            digest_scheduler.add_digest_job(
                user_id=callback.from_user.id,
                channel_list_id=list_id,
                schedule_type=schedule_type,
                schedule_day=schedule_day,
                schedule_hour=schedule_hour,
                schedule_minute=schedule_minute,
                bot=callback.bot,
            )
        else:
            digest_scheduler.remove_digest_job(list_id)

    if action == "save":
        await callback.message.edit_reply_markup(
            reply_markup=confirmation_keyboard(saved=True)
        )
        await callback.answer("✅ Настройки сохранены")
        return

    period_days = PERIOD_BY_SCHEDULE.get(schedule_type, 7)

    await state.set_state(DigestStates.generating)
    await callback.answer()

    await _run_pipeline(
        callback=callback,
        state=state,
        channel_ids=data["channel_ids"],
        channel_names=data["channel_names"],
        period_days=period_days,
        keywords=keywords,
        channel_list_id=list_id,
    )


# Запуск пайплайна


async def _run_pipeline(
    callback: types.CallbackQuery,
    state: FSMContext,
    channel_ids: list[int],
    channel_names: list[str],
    period_days: int,
    keywords,
    channel_list_id,
) -> None:
    progress_msg = await callback.message.edit_text("⏳ Формируем дайджест...")

    async def update_progress(text: str) -> None:
        try:
            await progress_msg.edit_text(text)
        except Exception:
            pass

    try:
        async with async_session() as session:
            result = await run_digest_pipeline(
                session=session,
                channel_ids=channel_ids,
                channel_names=channel_names,
                period_days=period_days,
                keywords=keywords,
                user_id=callback.from_user.id,
                channel_list_id=channel_list_id,
                progress_callback=update_progress,
            )

        formatted = format_digest(result)
        rich_html = format_digest_rich(result)
        try:
            await progress_msg.delete()
        except Exception:
            pass

        # Сначала пытаемся отдать rich-сообщение, на старом
        # клиенте или при ошибке возвращаемся к обычному HTML.
        try:
            from aiogram.types import InputRichMessage

            await callback.bot.send_rich_message(
                chat_id=callback.message.chat.id,
                rich_message=InputRichMessage(html=rich_html),
            )
        except Exception:
            logger.warning("send_rich_message не удался, откат на HTML", exc_info=True)
            await send_long_message(callback.message, formatted)

    except Exception as e:
        logger.exception("Ошибка при формировании дайджеста")
        error_text = f"❌ Ошибка при формировании дайджеста:\n{html.escape(str(e))}"
        try:
            await progress_msg.edit_text(error_text)
        except Exception:
            await callback.message.answer(error_text)

    await state.clear()

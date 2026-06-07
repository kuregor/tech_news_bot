from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


# ─── Digest: первый запуск (без кнопки Изменить) ────────

def digest_first_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📋 Стандартный", callback_data="digest_list:default"),
        InlineKeyboardButton(text="➕ Создать свой", callback_data="digest_list:create"),
    )
    return builder.as_markup()


# ─── Digest: повторный запуск (быстрый запуск) ──────────

def digest_quick_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🚀 Сформировать", callback_data="digest_quick:go"),
        InlineKeyboardButton(text="✏️ Изменить", callback_data="digest_quick:edit"),
    )
    return builder.as_markup()


# ─── Digest: выбор что именно изменить ──────────────────

def digest_edit_choice_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📣 Каналы", callback_data="digest_edit:channels"),
    )
    builder.row(
        InlineKeyboardButton(text="⏰ Расписание", callback_data="digest_edit:schedule"),
        InlineKeyboardButton(text="🔑 Фильтр", callback_data="digest_edit:filter"),
    )
    builder.row(
        InlineKeyboardButton(text="← Назад", callback_data="digest_edit:back"),
    )
    return builder.as_markup()


# ─── Digest: выбор списка каналов (устаревший, для edit) ─

def digest_list_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📋 Стандартный", callback_data="digest_list:default"),
    )
    builder.row(
        InlineKeyboardButton(text="✏️ Изменить", callback_data="digest_list:edit"),
        InlineKeyboardButton(text="➕ Создать свой", callback_data="digest_list:create"),
    )
    return builder.as_markup()


# ─── Digest: выбор расписания ────────────────────────────

def schedule_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📅 Ежедневно", callback_data="schedule:daily"),
        InlineKeyboardButton(text="📅 Еженедельно", callback_data="schedule:weekly"),
    )
    builder.row(
        InlineKeyboardButton(text="🔘 Без расписания", callback_data="schedule:none"),
    )
    builder.row(
        InlineKeyboardButton(text="← Назад", callback_data="schedule:back"),
    )
    return builder.as_markup()


# ─── Digest: выбор дня недели ────────────────────────────

def schedule_day_keyboard() -> InlineKeyboardMarkup:
    days = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    builder = InlineKeyboardBuilder()
    for i, day in enumerate(days):
        builder.button(text=day, callback_data=f"schedule_day:{i}")
    builder.adjust(4)
    return builder.as_markup()


# ─── Digest: выбор времени ───────────────────────────────

def schedule_time_keyboard() -> InlineKeyboardMarkup:
    hours = [7, 8, 9, 10, 12, 15, 18, 21]
    builder = InlineKeyboardBuilder()
    for h in hours:
        builder.button(text=f"{h}:00", callback_data=f"schedule_time:{h}")
    builder.adjust(4)
    return builder.as_markup()


# ─── Digest: завершение редактирования каналов ──────────

def editing_done_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Готово", callback_data="editing_done"),
    )
    builder.row(
        InlineKeyboardButton(text="🗑 Удалить каналы", callback_data="editing_remove"),
    )
    return builder.as_markup()


# ─── Digest: выбор ключевых слов ────────────────────────

def keywords_keyboard(topics: list[str], selected: list[str] | None = None) -> InlineKeyboardMarkup:
    selected = selected or []
    builder = InlineKeyboardBuilder()
    for topic in topics[:8]:
        label = f"✅ {topic}" if topic in selected else topic
        builder.button(text=label, callback_data=f"keyword:{topic}")
    builder.adjust(2)
    builder.row(
        InlineKeyboardButton(text="✍️ Своё слово", callback_data="keyword:custom"),
        InlineKeyboardButton(text="🚫 Без фильтра", callback_data="keyword:none"),
    )
    builder.row(
        InlineKeyboardButton(text="✅ Готово", callback_data="keywords_done"),
    )
    return builder.as_markup()


# ─── Выбор периода (общий для digest, trends, compare) ──

def period_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📅 24 часа", callback_data="period:1"),
        InlineKeyboardButton(text="📅 Неделя", callback_data="period:7"),
        InlineKeyboardButton(text="📅 Две недели", callback_data="period:14"),
    )
    return builder.as_markup()


# ─── Digest: подтверждение ──────────────────────────────

def confirmation_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🚀 Сформировать", callback_data="confirm:go"),
        InlineKeyboardButton(text="✏️ Изменить", callback_data="confirm:edit"),
    )
    return builder.as_markup()


# ─── Trends: быстрый запуск ─────────────────────────────

def trends_quick_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🚀 Запустить", callback_data="trends_quick:go"),
        InlineKeyboardButton(text="✏️ Изменить", callback_data="trends_quick:edit"),
    )
    return builder.as_markup()


# ─── Trends: выбор списка каналов ───────────────────────

def trends_list_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📋 Стандартный", callback_data="trends_list:default"),
    )
    builder.row(
        InlineKeyboardButton(text="✏️ Изменить", callback_data="trends_list:edit"),
        InlineKeyboardButton(text="➕ Создать свой", callback_data="trends_list:create"),
    )
    return builder.as_markup()


# ─── Compare: выбор каналов ─────────────────────────────

def compare_channels_keyboard(suggestions: list[tuple[str, str]] | None = None) -> InlineKeyboardMarkup:
    """Клавиатура выбора каналов для сравнения.

    suggestions: [(username, title), ...] — предложенные каналы из истории.
    """
    builder = InlineKeyboardBuilder()
    if suggestions:
        for username, title in suggestions[:4]:
            builder.button(
                text=f"📣 {title or username}",
                callback_data=f"compare_ch:{username}",
            )
        builder.adjust(2)
    builder.row(
        InlineKeyboardButton(text="✍️ Ввести вручную", callback_data="compare_ch:manual"),
    )
    return builder.as_markup()


# ─── Analyze: закрыть ввод ───────────────────────────────

def close_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✖️ Закрыть", callback_data="analyze_close")
    return builder.as_markup()

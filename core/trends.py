import html as html_mod
import logging
import statistics
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from core import topics as taxonomy
from core.ai import ai_client
from core.formatting import (
    _decline_channels,
    _decline_posts,
    _fmt_num,
    _truncate,
)
from core.metrics import _channel_averages, _viral_score
from core.parser import telegram_parser
from db.models import Post
from db.repository import batch_upsert_posts, get_posts_by_channels

logger = logging.getLogger(__name__)

PERIOD_LABELS = {1: "24 часа", 7: "за неделю", 14: "за две недели"}

# Минимальное суммарное число упоминаний темы, чтобы учитывать её в трендах
MIN_MENTIONS = 3

# Ограничения на размер блоков вывода
TOP_RISING = 5
TOP_DECLINING = 5
TOP_NEW = 5


# Помощники


def _split_halves(
    posts: list[Post],
    period_from: datetime,
    period_to: datetime,
) -> tuple[list[Post], list[Post]]:
    """Разделить посты на первую и вторую половины периода."""
    mid = period_from + (period_to - period_from) / 2
    first = [p for p in posts if p.date < mid]
    second = [p for p in posts if p.date >= mid]
    return first, second


# Пайплайн


async def run_trends_pipeline(
    session: AsyncSession,
    channel_ids: list[int],
    channel_names: list[str],
    period_days: int,
    progress_callback: Callable[[str], Any] | None = None,
) -> dict:
    """Полный пайплайн /trends: тренд-радар с AI-классификацией тем.

    Параметры:
        session (AsyncSession): сессия БД.
        channel_ids (list[int]): идентификаторы каналов.
        channel_names (list[str]): username каналов.
        period_days (int): период в днях.
        progress_callback (Callable | None): async-колбэк обновления прогресса.

    Возвращает:
        dict: растущие/угасающие/новые темы, самый виральный пост.
    """

    async def _progress(text: str) -> None:
        if progress_callback:
            await progress_callback(text)

    period_to = datetime.now(UTC)
    period_from = period_to - timedelta(days=period_days)

    # 0. Подтягиваем свежие посты из Telegram для каждого канала списка,
    #    чтобы тренды считались по всем каналам, а не только по тем, что прошли /analyze.
    await _progress("📡 Обновляем посты каналов... (1/5)")
    for ch_id, ch_name in zip(channel_ids, channel_names):
        try:
            raw_posts = await telegram_parser.parse_channel_posts(ch_name)
            await batch_upsert_posts(session, ch_id, raw_posts)
        except Exception as e:
            logger.warning("Не удалось обновить посты @%s: %s", ch_name, e)

    # 1. Посты за период
    await _progress("⏳ Загружаем посты... (2/5)")
    posts = await get_posts_by_channels(session, channel_ids, period_from)
    if not posts:
        return {
            "empty": True,
            "channel_names": channel_names,
            "period_days": period_days,
            "period_label": PERIOD_LABELS.get(period_days, f"{period_days} дн."),
        }

    # 2. Деление на половины
    await _progress("📊 Делим на половины... (3/5)")
    first, second = _split_halves(posts, period_from, period_to)
    n_first = len(first)
    n_second = len(second)

    first_ids = {p.id for p in first}
    second_ids = {p.id for p in second}
    posts_map = {p.id: p for p in posts}

    # 3. Темы по постам — AI-классификация без зависимости от /analyze
    await _progress("🤖 Классифицируем темы... (4/5)")
    raw_classification = await ai_client.classify_posts_for_trends(posts)
    # Темы — slug из закрытого набора; other в тренды не берём.
    topics_map: dict[int, list[str]] = {
        pid: [label]
        for pid, label in raw_classification.items()
        if label and label != taxonomy.OTHER and pid in posts_map
    }

    # 4. Подсчёт упоминаний и связка тем с постами
    await _progress("🧮 Считаем тренды... (5/5)")
    first_counts: dict[str, int] = defaultdict(int)
    second_counts: dict[str, int] = defaultdict(int)
    topic_post_ids: dict[str, set[int]] = defaultdict(set)

    for pid, labels in topics_map.items():
        for label in labels:
            topic_post_ids[label].add(pid)
            if pid in first_ids:
                first_counts[label] += 1
            if pid in second_ids:
                second_counts[label] += 1

    # 5. Классификация тем: растущие / угасающие / новые
    rising: list[dict] = []
    declining: list[dict] = []
    new_topics: list[dict] = []

    for label in set(first_counts) | set(second_counts):
        c1 = first_counts.get(label, 0)
        c2 = second_counts.get(label, 0)

        # Порог минимальных упоминаний — молча отбрасываем
        if c1 + c2 < MIN_MENTIONS:
            continue

        pid_set = topic_post_ids.get(label, set())
        views_list = [posts_map[pid].views or 0 for pid in pid_set]
        posts_count = len(pid_set)

        # Новая тема: не было в первой половине
        if c1 == 0:
            avg_views = sum(views_list) / len(views_list) if views_list else 0
            new_topics.append(
                {
                    "label": label,
                    "posts_count": posts_count,
                    "avg_views": avg_views,
                    "second_count": c2,
                }
            )
            continue

        # Нормализация по частоте внутри каждой половины
        freq1 = c1 / n_first if n_first else 0
        freq2 = c2 / n_second if n_second else 0
        if freq1 == 0:
            # страховка: не должны сюда попасть, но на всякий случай
            continue
        delta_pct = (freq2 - freq1) / freq1 * 100

        median_views = statistics.median(views_list) if views_list else 0

        entry = {
            "label": label,
            "growth": delta_pct,
            "first_count": c1,
            "second_count": c2,
            "posts_count": posts_count,
            "median_views": median_views,
        }
        if delta_pct > 0:
            rising.append(entry)
        elif delta_pct < 0:
            declining.append(entry)

    rising.sort(key=lambda t: t["growth"], reverse=True)
    declining.sort(key=lambda t: t["growth"])
    new_topics.sort(key=lambda t: t["second_count"], reverse=True)

    rising = rising[:TOP_RISING]
    declining = declining[:TOP_DECLINING]
    new_topics = new_topics[:TOP_NEW]

    # 6. Самый вирусный пост периода — нормализованная формула
    await _progress("🔥 Ищем вирусные посты... (5/5)")
    ch_stats = _channel_averages(posts)
    top_viral = max(posts, key=lambda p: _viral_score(p, ch_stats))

    channel_map = dict(zip(channel_ids, channel_names))

    # Добавляем пример поста для каждой растущей темы
    for entry in rising:
        pid_set = topic_post_ids.get(entry["label"], set())
        second_pids = pid_set & second_ids
        if second_pids:
            best_pid = max(second_pids, key=lambda pid: posts_map[pid].views or 0)
            best = posts_map[best_pid]
            ch = channel_map.get(best.channel_id, "?")
            entry["sample_text"] = _truncate(best.text or "", 80)
            entry["sample_channel"] = ch
            entry["sample_url"] = f"https://t.me/{ch}/{best.tg_id}"
            entry["sample_post"] = best

    return {
        "empty": False,
        "channel_names": channel_names,
        "period_days": period_days,
        "period_label": PERIOD_LABELS.get(period_days, f"{period_days} дн."),
        "rising": rising,
        "declining": declining,
        "new_topics": new_topics,
        "top_viral": top_viral,
        "channel_map": channel_map,
    }


# Форматирование


def _post_metrics(post: Post) -> str:
    """Строка метрик поста в едином стиле: 🗓 дата · 👁 · ❤️ · 💬 · ER."""
    date_str = f"{post.date.day:02d}.{post.date.month:02d}"
    views = post.views or 0
    er = (post.reactions or 0) + (post.comments or 0)
    er = f"{er / views * 100:.1f}" if views else "0"
    return (
        f"🗓 {date_str} · 👁 {_fmt_num(views)} · ❤️ {_fmt_num(post.reactions or 0)} · "
        f"💬 {_fmt_num(post.comments or 0)} · ER <b>{er}%</b>"
    )


def format_trends(data: dict) -> str:
    """Назначение: форматировать результат /trends в текст для Telegram (HTML).

    Параметры:
        data (dict): результат run_trends_pipeline.

    Возвращает:
        str: готовое HTML-сообщение.
    """
    channel_count = len(data["channel_names"])
    period_label = data["period_label"]

    if data.get("empty"):
        channels = " · ".join(f"@{n}" for n in data["channel_names"])
        return (
            f"📡 Тренд-радар · {period_label}\n"
            f"📋 {channels}\n\n"
            "Постов за этот период не найдено."
        )

    lines = [
        f"📡 <b>Тренд-радар</b> · {period_label} · {_decline_channels(channel_count)}",
        "",
    ]

    rising = data.get("rising", [])
    declining = data.get("declining", [])
    new_topics = data.get("new_topics", [])

    if rising:
        lines.append("📈 <b>Набирает обороты:</b>")
        for t in rising:
            emoji = taxonomy.emoji_for(t["label"])
            c1 = t.get("first_count", 0)
            c2 = t.get("second_count", 0)
            lines.append(
                f"{emoji} {html_mod.escape(taxonomy.label_for(t['label']))} — "
                f"<b>+{round(t['growth'])}%</b> ({c1} → {_decline_posts(c2)})"
            )
            sample = t.get("sample_text")
            if sample:
                url = t.get("sample_url", "")
                ch = t.get("sample_channel", "")
                lines.append(
                    f'   └ <a href="{url}">«{html_mod.escape(sample)}»</a> @{ch}'
                )
                sample_post = t.get("sample_post")
                if sample_post is not None:
                    lines.append(f"      {_post_metrics(sample_post)}")
        lines.append("")

    if new_topics:
        lines.append("🆕 <b>Появилось впервые:</b>")
        for t in new_topics:
            emoji = taxonomy.emoji_for(t["label"])
            lines.append(
                f"  {emoji} {html_mod.escape(taxonomy.label_for(t['label']))} · {_decline_posts(t['posts_count'])}"
            )
        lines.append("")

    if declining:
        lines.append("📉 <b>Затихло:</b>")
        for t in declining:
            emoji = taxonomy.emoji_for(t["label"])
            if round(abs(t["growth"])) >= 100:
                lines.append(
                    f"  {emoji} {html_mod.escape(taxonomy.label_for(t['label']))} — исчезло из эфира"
                )
            else:
                lines.append(
                    f"  {emoji} {html_mod.escape(taxonomy.label_for(t['label']))} — "
                    f"−{round(abs(t['growth']))}%"
                )
        lines.append("")

    if not rising and not declining and not new_topics:
        lines.append("Недостаточно данных для анализа трендов.")
        lines.append("Требуется минимум 3 упоминания темы за период.")
        lines.append("")

    top = data.get("top_viral")
    if top is not None:
        ch_username = data["channel_map"].get(top.channel_id, "?")
        preview = _truncate(top.text or "", 80)
        url = f"https://t.me/{ch_username}/{top.tg_id}"
        lines.append(f"🔥 <b>Самый виральный:</b> @{ch_username}")
        lines.append(f'<a href="{url}">«{html_mod.escape(preview)}»</a>')
        lines.append(f"   {_post_metrics(top)}")

    return "\n".join(lines)


def format_trends_rich(data: dict) -> str:
    """Назначение: форматировать /trends в rich-HTML (структурная страница).

    Блочные теги rich-сообщений рендерятся Telegram как страница; простой
    format_trends остаётся fallback-ом для старых клиентов / ошибки API.
    """
    channel_count = len(data["channel_names"])
    period_label = data["period_label"]

    if data.get("empty"):
        channels = " · ".join(f"@{html_mod.escape(n)}" for n in data["channel_names"])
        return (
            f"<h2>📡 Тренд-радар · {period_label}</h2>"
            f"<p>📋 {channels}</p>"
            "<p>Постов за этот период не найдено.</p>"
        )

    parts = [
        f"<h2>📡 Тренд-радар · {period_label} · {_decline_channels(channel_count)}</h2>"
    ]

    rising = data.get("rising", [])
    declining = data.get("declining", [])
    new_topics = data.get("new_topics", [])

    if rising:
        parts.append("<h3>📈 Набирает обороты</h3>")
        parts.append("<ul>")
        for t in rising:
            emoji = taxonomy.emoji_for(t["label"])
            c1 = t.get("first_count", 0)
            c2 = t.get("second_count", 0)
            item = (
                f"<li>{emoji} {html_mod.escape(taxonomy.label_for(t['label']))} — "
                f"<b>+{round(t['growth'])}%</b> ({c1} → {_decline_posts(c2)})"
            )
            sample = t.get("sample_text")
            if sample:
                url = t.get("sample_url", "")
                ch = t.get("sample_channel", "")
                item += (
                    f'<br>└ <a href="{url}">«{html_mod.escape(sample)}»</a> '
                    f"@{html_mod.escape(ch)}"
                )
                sample_post = t.get("sample_post")
                if sample_post is not None:
                    item += f"<br>{_post_metrics(sample_post)}"
            item += "</li>"
            parts.append(item)
        parts.append("</ul>")

    if new_topics:
        parts.append("<h3>🆕 Появилось впервые</h3>")
        parts.append("<ul>")
        for t in new_topics:
            emoji = taxonomy.emoji_for(t["label"])
            parts.append(
                f"<li>{emoji} {html_mod.escape(taxonomy.label_for(t['label']))} · "
                f"{_decline_posts(t['posts_count'])}</li>"
            )
        parts.append("</ul>")

    if declining:
        parts.append("<h3>📉 Затихло</h3>")
        parts.append("<ul>")
        for t in declining:
            emoji = taxonomy.emoji_for(t["label"])
            label = html_mod.escape(taxonomy.label_for(t["label"]))
            if round(abs(t["growth"])) >= 100:
                parts.append(f"<li>{emoji} {label} — исчезло из эфира</li>")
            else:
                parts.append(f"<li>{emoji} {label} — −{round(abs(t['growth']))}%</li>")
        parts.append("</ul>")

    if not rising and not declining and not new_topics:
        parts.append(
            "<p>Недостаточно данных для анализа трендов.<br>"
            "Требуется минимум 3 упоминания темы за период.</p>"
        )

    top = data.get("top_viral")
    if top is not None:
        ch_username = data["channel_map"].get(top.channel_id, "?")
        preview = _truncate(top.text or "", 80)
        url = f"https://t.me/{html_mod.escape(ch_username)}/{top.tg_id}"
        parts.append("<hr/>")
        parts.append(
            f"<p>🔥 <b>Самый виральный:</b> @{html_mod.escape(ch_username)}"
            f'<br><a href="{url}">«{html_mod.escape(preview)}»</a>'
            f"<br>{_post_metrics(top)}</p>"
        )

    return "".join(parts)

import html as html_mod
import logging
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from math import floor
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from core import topics as taxonomy
from core.ai import ai_client
from core.formatting import _fmt_num, _fmt_pct
from core.parser import telegram_parser
from db.models import ChannelTopic, Post
from db.repository import (
    batch_upsert_posts,
    get_posts_by_channel,
    get_topics_by_channel,
    upsert_channel,
)
from db.session import async_session

logger = logging.getLogger(__name__)

PERIOD_LABELS = {1: "24 часа", 7: "за неделю", 14: "за две недели"}


# Вспомогательные функции


def _calculate_metrics(posts: list[Post], period_days: int) -> dict:
    """Средние метрики канала (views/reactions) и постов-в-день за период."""
    n = len(posts)
    if n == 0:
        return {
            "avg_views": 0.0,
            "avg_reactions": 0.0,
            "posts_count": 0,
            "posts_per_day": 0.0,
        }
    avg_views = sum(p.views or 0 for p in posts) / n
    avg_reactions = sum(p.reactions or 0 for p in posts) / n
    posts_per_day = n / period_days if period_days > 0 else 0.0
    return {
        "avg_views": avg_views,
        "avg_reactions": avg_reactions,
        "posts_count": n,
        "posts_per_day": posts_per_day,
    }


def _calculate_err(avg_reactions: float, subscribers_count: int) -> float:
    """ERR = средние реакции / число подписчиков × 100."""
    if not subscribers_count:
        return 0.0
    return avg_reactions / subscribers_count * 100


def _topics_from_classification(
    posts: list[Post], classification: dict[int, str]
) -> dict[str, int]:
    """Считаем количество постов по каждой теме из AI-классификации."""
    counts: dict[str, int] = defaultdict(int)
    for post in posts:
        label = classification.get(post.id)
        if label:
            counts[label] += 1
    return dict(counts)


def _topics_from_db(channel_topics: list[ChannelTopic]) -> dict[str, int]:
    """Считаем темы из БД (channel_topics), группируем по label."""
    counts: dict[str, int] = defaultdict(int)
    for t in channel_topics:
        counts[t.label] += 1
    return dict(counts)


def _topic_bar(count: int, total: int) -> str:
    """Текстовый бар доли темы (10 символов █/░)."""
    if total == 0:
        return "░" * 10
    pct = count / total
    filled = floor(pct * 10)
    return "█" * filled + "░" * (10 - filled)


# Пайплайн


async def run_compare_pipeline(
    ch1_username: str,
    ch2_username: str,
    period_days: int,
    progress_callback: Callable[[str], Any] | None = None,
) -> dict:
    """Назначение: полный пайплайн команды /compare (метрики, темы, стили двух каналов).

    Параметры:
        ch1_username (str): username первого канала.
        ch2_username (str): username второго канала.
        period_days (int): период в днях.
        progress_callback (Callable | None): async-колбэк обновления прогресса.

    Возвращает:
        dict: метрики, ER, пересечение/уникальные темы и стили обоих каналов.
    """

    async def _progress(text: str) -> None:
        if progress_callback:
            await progress_callback(text)

    period_to = datetime.now(UTC)
    period_from = period_to - timedelta(days=period_days)
    period_label = PERIOD_LABELS.get(period_days, f"{period_days} дн.")

    # 1. Загрузка мета-данных каналов (Telethon, без БД)
    await _progress("⏳ Загружаем каналы... (1/4)")
    ch1_info, ch2_info = await _fetch_both(ch1_username, ch2_username)

    # Запись в БД — отдельная сессия, чтобы commit не ломал последующие reads
    async with async_session() as write_session:
        ch1 = await upsert_channel(
            write_session,
            username=ch1_username,
            title=ch1_info["title"],
            description=ch1_info["description"],
            subscribers_count=ch1_info["subscribers_count"],
        )
        ch2 = await upsert_channel(
            write_session,
            username=ch2_username,
            title=ch2_info["title"],
            description=ch2_info["description"],
            subscribers_count=ch2_info["subscribers_count"],
        )
        ch1_id = ch1.id
        ch2_id = ch2.id
        ch1_subs = ch1.subscribers_count or 0
        ch2_subs = ch2.subscribers_count or 0
        ch1_title = ch1.title or ch1_username
        ch2_title = ch2.title or ch2_username

    # 2. Парсинг постов (если в БД ещё нет)
    await _progress("📊 Собираем посты... (2/4)")
    async with async_session() as check_session:
        existing1 = await get_posts_by_channel(
            check_session, ch1_id, period_from=period_from
        )
        existing2 = await get_posts_by_channel(
            check_session, ch2_id, period_from=period_from
        )

    if not existing1:
        raw1 = await telegram_parser.parse_channel_posts(ch1_username)
        async with async_session() as s:
            await batch_upsert_posts(s, ch1_id, raw1)

    if not existing2:
        raw2 = await telegram_parser.parse_channel_posts(ch2_username)
        async with async_session() as s:
            await batch_upsert_posts(s, ch2_id, raw2)

    # Читаем окончательно
    async with async_session() as read_session:
        posts1 = await get_posts_by_channel(
            read_session, ch1_id, period_from=period_from
        )
        posts2 = await get_posts_by_channel(
            read_session, ch2_id, period_from=period_from
        )

    metrics1 = _calculate_metrics(posts1, period_days)
    metrics2 = _calculate_metrics(posts2, period_days)

    err1 = _calculate_err(metrics1["avg_reactions"], ch1_subs)
    err2 = _calculate_err(metrics2["avg_reactions"], ch2_subs)

    # 3. Темы — своя сессия
    await _progress("🧠 Анализируем темы... (3/4)")
    async with async_session() as topics_session:
        topics1, topics2 = await _get_topics(
            topics_session, posts1, posts2, ch1_id, ch2_id
        )

    # Темы — slug из таксономии; other в сравнение тем не берём.
    slugs1 = set(topics1) - {taxonomy.OTHER}
    slugs2 = set(topics2) - {taxonomy.OTHER}
    intersection = sorted(slugs1 & slugs2)
    unique1 = sorted(slugs1 - slugs2)
    unique2 = sorted(slugs2 - slugs1)

    # 4. Сравнение стилей (без БД)
    await _progress("✍️ Сравниваем стили... (4/4)")
    top10_1 = sorted(posts1, key=lambda p: p.views or 0, reverse=True)[:10]
    top10_2 = sorted(posts2, key=lambda p: p.views or 0, reverse=True)[:10]

    styles: dict[str, str] = {}
    if top10_1 or top10_2:
        try:
            styles = await ai_client.compare_styles(
                ch1_username,
                [p.text or "" for p in top10_1],
                ch2_username,
                [p.text or "" for p in top10_2],
            )
        except Exception:
            logger.warning("compare_styles упал", exc_info=True)

    return {
        "empty": not posts1 and not posts2,
        "period_label": period_label,
        "ch1_username": ch1_username,
        "ch2_username": ch2_username,
        "ch1_title": ch1_title,
        "ch2_title": ch2_title,
        "sub1": ch1_subs,
        "sub2": ch2_subs,
        "metrics1": metrics1,
        "metrics2": metrics2,
        "err1": err1,
        "err2": err2,
        "topics1": topics1,
        "topics2": topics2,
        "intersection": intersection,
        "unique1": unique1,
        "unique2": unique2,
        "styles": styles,
        "reactions1_enabled": ch1_info.get("reactions_enabled", True),
        "reactions2_enabled": ch2_info.get("reactions_enabled", True),
    }


async def _fetch_both(ch1_username: str, ch2_username: str) -> tuple[dict, dict]:
    """Загрузить мета-данные обоих каналов через Telethon."""
    ch1_info = await telegram_parser.parse_channel_info(ch1_username)
    ch2_info = await telegram_parser.parse_channel_info(ch2_username)
    return ch1_info, ch2_info


async def _get_topics(
    session: AsyncSession,
    posts1: list[Post],
    posts2: list[Post],
    ch1_id: int,
    ch2_id: int,
) -> tuple[dict[str, int], dict[str, int]]:
    """Получить темы для обоих каналов: AI-классификация постов."""
    all_posts = posts1 + posts2

    if all_posts:
        try:
            classification = await ai_client.classify_posts_for_trends(all_posts)
            t1 = _topics_from_classification(posts1, classification)
            t2 = _topics_from_classification(posts2, classification)
            if t1 or t2:
                return t1, t2
        except Exception:
            logger.warning("Классификация тем упала, используем БД", exc_info=True)

    db1 = await get_topics_by_channel(session, ch1_id)
    db2 = await get_topics_by_channel(session, ch2_id)
    return _topics_from_db(db1), _topics_from_db(db2)


# Форматирование


def format_compare(data: dict) -> str:
    """Назначение: форматировать результат /compare в текст для Telegram (HTML).

    Параметры:
        data (dict): результат run_compare_pipeline.

    Возвращает:
        str: готовое HTML-сообщение с таблицей сравнения.
    """
    if data.get("empty"):
        return (
            f"⚖️ Сравнение · {data['period_label']}\n\n"
            f"Постов за этот период не найдено ни в одном из каналов."
        )

    ch1 = data["ch1_username"]
    ch2 = data["ch2_username"]
    m1 = data["metrics1"]
    m2 = data["metrics2"]

    ppd1 = f"{m1['posts_per_day']:.1f}"
    ppd2 = f"{m2['posts_per_day']:.1f}"

    LW, VW = 13, 14

    def _row(label: str, v1: str, v2: str) -> str:
        return f"{label:<{LW}}{v1:<{VW}}{v2}"

    # Если у канала выключены реакции (по данным Telegram) — и строку «Реакции»,
    # и ER помечаем словами, а не нулём.
    re1_on = data.get("reactions1_enabled", True)
    re2_on = data.get("reactions2_enabled", True)
    react1 = _fmt_num(m1["avg_reactions"]) if re1_on else "выкл."
    react2 = _fmt_num(m2["avg_reactions"]) if re2_on else "выкл."
    er1 = _fmt_pct(data["err1"]) if re1_on else "н/д"
    er2 = _fmt_pct(data["err2"]) if re2_on else "н/д"

    table = "\n".join(
        [
            f"{'':>{LW}}{'@'+ch1:<{VW}}{'@'+ch2}",
            _row("Подписчики", _fmt_num(data["sub1"]), _fmt_num(data["sub2"])),
            _row("Охват", _fmt_num(m1["avg_views"]), _fmt_num(m2["avg_views"])),
            _row("Реакции", react1, react2),
            _row("Постов", str(m1["posts_count"]), str(m2["posts_count"])),
            _row("Постов/день", ppd1, ppd2),
            _row("ER%", er1, er2),
        ]
    )

    lines = [
        f"⚖️ <b>Сравнение</b> · {data['period_label']}",
        "━━━━━━━━━━━━━━━━━━━━",
        f"<code>{table}</code>",
        "━━━━━━━━━━━━━━━━━━━━",
    ]

    intersection = data.get("intersection", [])
    t1 = data.get("topics1", {})
    t2 = data.get("topics2", {})
    total1 = sum(t1.values()) or 1
    total2 = sum(t2.values()) or 1

    if intersection:
        lines.append("🏷 <b>Пересечение тем:</b>")
        for label in intersection[:5]:
            bar1 = _topic_bar(t1.get(label, 0), total1)
            bar2 = _topic_bar(t2.get(label, 0), total2)
            pct1 = round(t1.get(label, 0) / total1 * 100)
            pct2 = round(t2.get(label, 0) / total2 * 100)
            lines.append(
                f"  • {html_mod.escape(taxonomy.label_for(label))}\n"
                f"    {bar1} {pct1}%   {bar2} {pct2}%"
            )
        lines.append("")

    unique1 = data.get("unique1", [])
    unique2 = data.get("unique2", [])
    if unique1 or unique2:
        lines.append("🔀 <b>Уникальные темы:</b>")
        if unique1:
            lines.append(f"@{ch1}:")
            for t in unique1[:5]:
                lines.append(f"  · {html_mod.escape(taxonomy.label_for(t))}")
        if unique2:
            if unique1:
                lines.append("")
            lines.append(f"@{ch2}:")
            for t in unique2[:5]:
                lines.append(f"  · {html_mod.escape(taxonomy.label_for(t))}")
        lines.append("")

    styles = data.get("styles", {})
    if styles:
        lines.append("━━━━━━━━━━━━━━━━━━━━")
        style1 = styles.get("style_ch1", "")
        style2 = styles.get("style_ch2", "")
        if style1:
            lines.append(f"✍️ @{ch1}: {html_mod.escape(style1)}")
        if style2:
            lines.append(f"✍️ @{ch2}: {html_mod.escape(style2)}")
        lines.append("")

    # Вывод о лидере по ERR и охвату
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    err1 = data["err1"]
    err2 = data["err2"]
    views1 = m1["avg_views"]
    views2 = m2["avg_views"]

    leader_err = ch1 if err1 >= err2 else ch2
    leader_views = ch1 if views1 >= views2 else ch2

    if leader_err == leader_views:
        lines.append(f"🏆 @{leader_err} лидирует по охвату и вовлечённости")
    else:
        lines.append(
            f"🏆 @{leader_views} — лучший охват, "
            f"@{leader_err} — выше вовлечённость (ER)"
        )

    return "\n".join(lines)


def format_compare_rich(data: dict) -> str:
    """Назначение: форматировать /compare в rich-HTML (структурная страница).

    Таблица метрик — нативная rich <table>; простой format_compare остаётся
    fallback-ом для старых клиентов / ошибки API.
    """
    if data.get("empty"):
        return (
            f"<h2>⚖️ Сравнение · {data['period_label']}</h2>"
            "<p>Постов за этот период не найдено ни в одном из каналов.</p>"
        )

    ch1 = html_mod.escape(data["ch1_username"])
    ch2 = html_mod.escape(data["ch2_username"])
    m1 = data["metrics1"]
    m2 = data["metrics2"]

    def _row(label: str, v1: str, v2: str) -> str:
        return (
            f'<tr><td align="left">{label}</td>'
            f'<td align="left">{v1}</td><td align="left">{v2}</td></tr>'
        )

    # Если у канала выключены реакции (по данным Telegram) — и саму строку
    # «Реакции», и ER помечаем словами, а не нулём.
    re1_on = data.get("reactions1_enabled", True)
    re2_on = data.get("reactions2_enabled", True)
    react1 = _fmt_num(m1["avg_reactions"]) if re1_on else "выкл."
    react2 = _fmt_num(m2["avg_reactions"]) if re2_on else "выкл."
    er1 = _fmt_pct(data["err1"]) if re1_on else "н/д"
    er2 = _fmt_pct(data["err2"]) if re2_on else "н/д"

    parts = [
        f"<h2>⚖️ Сравнение · {data['period_label']}</h2>",
        "<table>",
        f'<tr><th></th><th align="left">@{ch1}</th><th align="left">@{ch2}</th></tr>',
        _row("Подписчики", _fmt_num(data["sub1"]), _fmt_num(data["sub2"])),
        _row("Охват", _fmt_num(m1["avg_views"]), _fmt_num(m2["avg_views"])),
        _row("Реакции", react1, react2),
        _row("Постов", str(m1["posts_count"]), str(m2["posts_count"])),
        _row("Постов/день", f"{m1['posts_per_day']:.1f}", f"{m2['posts_per_day']:.1f}"),
        _row("ER%", er1, er2),
        "</table>",
    ]

    intersection = data.get("intersection", [])
    t1 = data.get("topics1", {})
    t2 = data.get("topics2", {})
    total1 = sum(t1.values()) or 1
    total2 = sum(t2.values()) or 1

    if intersection:
        parts.append("<h3>🏷 Пересечение тем</h3>")
        parts.append("<table>")
        parts.append(
            f'<tr><th align="left">Тема</th>'
            f'<th align="left">@{ch1}</th><th align="left">@{ch2}</th></tr>'
        )
        for label in intersection[:5]:
            pct1 = round(t1.get(label, 0) / total1 * 100)
            pct2 = round(t2.get(label, 0) / total2 * 100)
            s1 = f"<b>{pct1}%</b>" if pct1 >= pct2 else f"{pct1}%"
            s2 = f"<b>{pct2}%</b>" if pct2 > pct1 else f"{pct2}%"
            theme = f"{taxonomy.emoji_for(label)} {html_mod.escape(taxonomy.label_for(label))}"
            parts.append(
                f'<tr><td align="left">{theme}</td>'
                f'<td align="left">{s1}</td><td align="left">{s2}</td></tr>'
            )
        parts.append("</table>")

    unique1 = data.get("unique1", [])
    unique2 = data.get("unique2", [])
    if unique1 or unique2:
        parts.append("<h3>🔀 Уникальные темы</h3>")
        if unique1:
            labels = " · ".join(
                html_mod.escape(taxonomy.label_for(t)) for t in unique1[:5]
            )
            parts.append(f"<p>@{ch1}: {labels}</p>")
        if unique2:
            labels = " · ".join(
                html_mod.escape(taxonomy.label_for(t)) for t in unique2[:5]
            )
            parts.append(f"<p>@{ch2}: {labels}</p>")

    styles = data.get("styles", {})
    if styles:
        style1 = styles.get("style_ch1", "")
        style2 = styles.get("style_ch2", "")
        if style1 or style2:
            parts.append("<h3>✍️ Стиль</h3>")
            if style1:
                parts.append(f"<p>@{ch1}: {html_mod.escape(style1)}</p>")
            if style2:
                parts.append(f"<p>@{ch2}: {html_mod.escape(style2)}</p>")

    parts.append("<hr/>")
    err1, err2 = data["err1"], data["err2"]
    views1, views2 = m1["avg_views"], m2["avg_views"]
    leader_err = ch1 if err1 >= err2 else ch2
    leader_views = ch1 if views1 >= views2 else ch2
    if leader_err == leader_views:
        parts.append(f"<p>🏆 @{leader_err} лидирует по охвату и вовлечённости</p>")
    else:
        parts.append(
            f"<p>🏆 @{leader_views} — лучший охват, "
            f"@{leader_err} — выше вовлечённость (ER)</p>"
        )

    return "".join(parts)

import html as html_mod
import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from math import floor
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.ai import ai_client
from core.parser import telegram_parser
from db.models import Analysis, Channel, ChannelTopic, Post
from db.repository import (
    batch_upsert_posts,
    get_posts_by_channel,
    get_topics_by_channel,
    get_user_digests,
    upsert_channel,
)
from db.session import async_session

logger = logging.getLogger(__name__)

PERIOD_LABELS = {1: "24 часа", 7: "неделю", 14: "две недели"}


# ─── Подбор каналов из истории ──────────────────────────

async def suggest_channels(session: AsyncSession, user_id: int) -> list[Channel]:
    """Назначение: предложить каналы на основе истории дайджестов пользователя.

    Параметры:
        session (AsyncSession): сессия БД.
        user_id (int): идентификатор пользователя.

    Возвращает:
        list[Channel]: до 4 каналов, отсортированных по охвату (пусто при ошибке).
    """
    try:
        digests = await get_user_digests(session, user_id, limit=10)
        if not digests:
            return []

        all_keywords = [
            kw
            for d in digests
            for kw in (d.filter_keywords or [])
        ]
        if not all_keywords:
            return []

        top_topics = [label for label, _ in Counter(all_keywords).most_common(2)]

        # Каналы у которых есть темы из топа
        stmt = (
            select(Channel)
            .join(Post, Post.channel_id == Channel.id)
            .join(ChannelTopic, ChannelTopic.post_id == Post.id)
            .where(ChannelTopic.label.in_(top_topics))
            .distinct()
        )
        result = await session.execute(stmt)
        channels = list(result.scalars().all())

        # Сортируем по avg_views из таблицы analyses если есть
        analyses_stmt = select(Analysis).where(
            Analysis.channel_id.in_([c.id for c in channels])
        )
        analyses_result = await session.execute(analyses_stmt)
        views_map = {a.channel_id: a.avg_views for a in analyses_result.scalars().all()}

        channels.sort(key=lambda c: views_map.get(c.id, 0), reverse=True)
        return channels[:4]

    except Exception:
        logger.warning("Не удалось получить предложения каналов", exc_info=True)
        return []


# ─── Вспомогательные функции ────────────────────────────

def _calculate_metrics(posts: list[Post], period_days: int) -> dict:
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
    if total == 0:
        return "░" * 10
    pct = count / total
    filled = floor(pct * 10)
    return "█" * filled + "░" * (10 - filled)


# ─── Пайплайн ───────────────────────────────────────────

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

    period_to = datetime.now(timezone.utc)
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
        existing1 = await get_posts_by_channel(check_session, ch1_id, period_from=period_from)
        existing2 = await get_posts_by_channel(check_session, ch2_id, period_from=period_from)

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
        posts1 = await get_posts_by_channel(read_session, ch1_id, period_from=period_from)
        posts2 = await get_posts_by_channel(read_session, ch2_id, period_from=period_from)

    metrics1 = _calculate_metrics(posts1, period_days)
    metrics2 = _calculate_metrics(posts2, period_days)

    err1 = _calculate_err(metrics1["avg_reactions"], ch1_subs)
    err2 = _calculate_err(metrics2["avg_reactions"], ch2_subs)

    # 3. Темы — своя сессия
    await _progress("🧠 Анализируем темы... (3/4)")
    async with async_session() as topics_session:
        topics1, topics2 = await _get_topics(topics_session, posts1, posts2, ch1_id, ch2_id)

    intersection = sorted(set(topics1) & set(topics2))
    unique1 = sorted(set(topics1) - set(topics2))
    unique2 = sorted(set(topics2) - set(topics1))

    # 4. Сравнение стилей (без БД)
    await _progress("✍️ Сравниваем стили... (4/4)")
    top10_1 = sorted(posts1, key=lambda p: p.views or 0, reverse=True)[:10]
    top10_2 = sorted(posts2, key=lambda p: p.views or 0, reverse=True)[:10]

    styles: dict[str, str] = {}
    if top10_1 or top10_2:
        try:
            styles = await ai_client.compare_styles(
                ch1_username, [p.text or "" for p in top10_1],
                ch2_username, [p.text or "" for p in top10_2],
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
    }


async def _fetch_both(
    ch1_username: str, ch2_username: str
) -> tuple[dict, dict]:
    ch1_info = await telegram_parser.parse_channel_info(ch1_username)
    ch2_info = await telegram_parser.parse_channel_info(ch2_username)
    return ch1_info, ch2_info


async def _fetch_both_posts(
    session: AsyncSession,
    ch1_id: int,
    ch2_id: int,
    period_from: datetime,
) -> tuple[list[Post], list[Post]]:
    posts1 = await get_posts_by_channel(session, ch1_id, period_from=period_from)
    posts2 = await get_posts_by_channel(session, ch2_id, period_from=period_from)
    return posts1, posts2


async def _get_topics(
    session: AsyncSession,
    posts1: list[Post],
    posts2: list[Post],
    ch1_id: int,
    ch2_id: int,
) -> tuple[dict[str, int], dict[str, int]]:
    """Получить темы для обоих каналов: AI-классификация постов."""
    import asyncio

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


# ─── Форматирование ─────────────────────────────────────

def _fmt_num(n: float) -> str:
    n = round(n)
    if n < 10_000:
        return str(n)
    return f"{n:,}".replace(",", " ")


def _fmt_pct(n: float) -> str:
    return f"{n:.1f}%"


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

    table = "\n".join([
        f"{'':>{LW}}{'@'+ch1:<{VW}}{'@'+ch2}",
        _row("Подписчики", _fmt_num(data["sub1"]), _fmt_num(data["sub2"])),
        _row("Охват",      _fmt_num(m1["avg_views"]),     _fmt_num(m2["avg_views"])),
        _row("Реакции",    _fmt_num(m1["avg_reactions"]), _fmt_num(m2["avg_reactions"])),
        _row("Постов",     str(m1["posts_count"]),        str(m2["posts_count"])),
        _row("Постов/день", ppd1,                         ppd2),
        _row("ER%",        _fmt_pct(data["err1"]),        _fmt_pct(data["err2"])),
    ])

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
                f"  • {html_mod.escape(label)}\n"
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
                lines.append(f"  · {html_mod.escape(t)}")
        if unique2:
            if unique1:
                lines.append("")
            lines.append(f"@{ch2}:")
            for t in unique2[:5]:
                lines.append(f"  · {html_mod.escape(t)}")
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


class CompareService:
    """Сервис-фасад команды /compare: подбор каналов, пайплайн сравнения, форматирование."""

    suggest_channels = staticmethod(suggest_channels)
    run_compare_pipeline = staticmethod(run_compare_pipeline)
    format_compare = staticmethod(format_compare)

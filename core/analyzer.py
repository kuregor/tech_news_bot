import asyncio
import html as html_mod
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy.ext.asyncio import AsyncSession

EMBEDDING_TTL_DAYS = 30

from core.ai import ai_client
from core.embeddings import embedding_service
from core.parser import telegram_parser
from db.repository import (
    batch_insert_topics,
    batch_upsert_posts,
    delete_channel_topics,
    get_cached_analysis,
    get_channel_by_username,
    get_posts_by_channel,
    save_analysis,
    update_channel_embedding_timestamp,
    upsert_channel,
)

logger = logging.getLogger(__name__)


async def run_analyze_pipeline(
    session: AsyncSession,
    username: str,
    progress_callback: Callable[[str], Any] | None = None,
) -> dict:
    """Назначение: полный пайплайн команды /analyze (парсинг, AI-анализ, метрики).

    Параметры:
        session (AsyncSession): сессия БД.
        username (str): username канала (с @ или без).
        progress_callback (Callable | None): async-колбэк обновления прогресса.

    Возвращает:
        dict: результат для форматирования (см. _build_result).
    """
    username = username.lstrip("@")

    async def _progress(text: str) -> None:
        if progress_callback:
            await progress_callback(text)

    # 1. Проверяем кэш
    channel = await get_channel_by_username(session, username)
    if channel:
        cached = await get_cached_analysis(session, channel.id)
        if cached:
            # Проверяем свежесть вектора канала
            stale = (
                channel.embedding_updated_at is None
                or datetime.now(timezone.utc) - channel.embedding_updated_at
                > timedelta(days=EMBEDDING_TTL_DAYS)
            )
            if stale:
                await _progress("🔢 Обновляем вектор канала...")
                channel_info = await telegram_parser.parse_channel_info(username)
                recent_posts = await get_posts_by_channel(
                    session, channel.id, limit=20, order_by_views=True
                )
                top_posts_texts = [p.text for p in recent_posts if p.text]
                embed_text = (
                    f"{channel_info['title']}\n"
                    f"{channel_info['description']}\n"
                    + "\n".join(top_posts_texts[:20])
                )
                try:
                    channel_embedding = await embedding_service.get_embedding(embed_text[:8000])
                    embedding_service.upsert_channel_embedding(
                        channel.id,
                        channel_embedding,
                        metadata={"username": username, "title": channel_info["title"] or ""},
                    )
                    await update_channel_embedding_timestamp(session, channel.id)
                    logger.info("Вектор канала @%s обновлён (истёк TTL)", username)
                except Exception as e:
                    logger.warning("Не удалось обновить вектор канала @%s: %s", username, e)

            logger.info("Возвращаем кэшированный анализ для @%s", username)
            topics = await _get_channel_topics_aggregated(session, channel.id)
            cached_posts = await get_posts_by_channel(
                session, channel.id, limit=3, order_by_views=True
            )
            top_posts_data = [
                {
                    "url": f"https://t.me/{username}/{p.tg_id}",
                    "text": p.text or "",
                    "views": p.views,
                    "reactions": p.reactions,
                    "comments": p.comments,
                    "date": p.date,
                }
                for p in cached_posts
            ]
            return _build_result(channel, cached, topics, top_posts=top_posts_data, from_cache=True)

    # 2. Парсинг канала через Telethon
    await _progress("⏳ Получаем посты... (1/5)")
    channel_info = await telegram_parser.parse_channel_info(username)
    raw_posts = await telegram_parser.parse_channel_posts(username)

    # 3. Upsert канала и постов в БД
    channel = await upsert_channel(
        session,
        username=username,
        title=channel_info["title"],
        description=channel_info["description"],
        subscribers_count=channel_info["subscribers_count"],
    )
    await batch_upsert_posts(session, channel.id, raw_posts)

    # Получаем посты из БД (с ID)
    all_posts = await get_posts_by_channel(session, channel.id)
    top_posts_by_views = await get_posts_by_channel(
        session, channel.id, limit=20, order_by_views=True
    )

    # 4. Два AI-вызова параллельно
    await _progress("🧠 Анализируем темы... (2/5)")
    posts_texts = [p.text for p in all_posts if p.text]
    top_posts_texts = [p.text for p in top_posts_by_views if p.text]

    topics_result, desc_result = await asyncio.gather(
        ai_client.analyze_topics(posts_texts),
        ai_client.analyze_description(channel_info["description"], top_posts_texts),
    )

    await _progress("✍️ Формируем описание... (3/5)")

    # 5. Построение вектора канала через Ollama
    await _progress("🔢 Строим вектор канала... (4/5)")
    hashtags = topics_result.get("hashtags", [])
    trending_keywords = topics_result.get("trending_keywords", [])
    embed_text = (
        f"{channel_info['title']}\n"
        f"{channel_info['description']}\n"
        f"{' '.join(hashtags)}\n"
        f"{' '.join(trending_keywords)}\n"
        + "\n".join(top_posts_texts[:20])
    )
    try:
        channel_embedding = await embedding_service.get_embedding(embed_text[:8000])
        embedding_service.upsert_channel_embedding(
            channel.id,
            channel_embedding,
            metadata={"username": username, "title": channel_info["title"] or ""},
        )
        await update_channel_embedding_timestamp(session, channel.id)
    except Exception as e:
        logger.warning("Не удалось построить вектор канала: %s", e)

    # 6. Расчёт метрик
    await _progress("📊 Считаем метрики... (5/5)")
    posts_count = len(all_posts)
    avg_views = sum(p.views for p in all_posts) / posts_count if posts_count else 0
    avg_reactions = sum(p.reactions for p in all_posts) / posts_count if posts_count else 0
    avg_comments = sum(p.comments for p in all_posts) / posts_count if posts_count else 0

    # 7. Сохраняем анализ в БД
    analysis = await save_analysis(
        session,
        channel_id=channel.id,
        tagline=desc_result.get("tagline", ""),
        about=desc_result.get("about", ""),
        audience=desc_result.get("audience", ""),
        style=desc_result.get("style", ""),
        avg_views=round(avg_views, 1),
        avg_reactions=round(avg_reactions, 1),
        avg_comments=round(avg_comments, 1),
        posts_count=posts_count,
    )

    # 8. Сохраняем темы в channel_topics (одна запись на тему канала)
    top_topics = topics_result.get("top_topics", [])
    if top_topics and all_posts:
        await delete_channel_topics(session, channel.id)
        representative_post_id = all_posts[0].id
        topics_data = [
            {
                "post_id": representative_post_id,
                "label": topic["label"],
                "percentage": topic.get("percentage", 0.0),
            }
            for topic in top_topics
        ]
        await batch_insert_topics(session, topics_data)

    # Топ-3 поста по просмотрам
    top3 = sorted(all_posts, key=lambda p: p.views, reverse=True)[:3]
    top_posts_data = [
        {
            "url": f"https://t.me/{username}/{p.tg_id}",
            "text": p.text or "",
            "views": p.views,
            "reactions": p.reactions,
            "comments": p.comments,
            "date": p.date,
        }
        for p in top3
    ]

    return _build_result(
        channel, analysis,
        topics_info={
            "top_topics": top_topics,
            "trending_keywords": trending_keywords,
            "hashtags": hashtags,
        },
        top_posts=top_posts_data,
        from_cache=False,
    )


async def _get_channel_topics_aggregated(session: AsyncSession, channel_id: int) -> dict:
    """Собрать агрегированные темы канала для кэшированного результата."""
    from db.repository import get_topics_by_channel
    topics = await get_topics_by_channel(session, channel_id, min_percentage=0.3)
    # Группируем по label, берём max percentage
    label_map: dict[str, float] = {}
    for t in topics:
        if t.label not in label_map or t.percentage > label_map[t.label]:
            label_map[t.label] = t.percentage
    top_topics = [
        {"label": label, "percentage": pct, "emoji": "📌"}
        for label, pct in sorted(label_map.items(), key=lambda x: -x[1])
    ]
    return {"top_topics": top_topics, "trending_keywords": [], "hashtags": []}


def _build_result(
    channel, analysis, topics_info: dict,
    top_posts: list[dict] | None = None,
    from_cache: bool = False,
) -> dict:
    """Собрать результат в единый dict для форматирования."""
    return {
        "username": channel.username,
        "title": channel.title,
        "tagline": analysis.tagline,
        "about": analysis.about,
        "audience": analysis.audience,
        "style": analysis.style,
        "avg_views": analysis.avg_views,
        "avg_reactions": analysis.avg_reactions,
        "avg_comments": analysis.avg_comments,
        "posts_count": analysis.posts_count,
        "top_topics": topics_info.get("top_topics", []),
        "trending_keywords": topics_info.get("trending_keywords", []),
        "hashtags": topics_info.get("hashtags", []),
        "top_posts": top_posts or [],
        "from_cache": from_cache,
    }


def _fmt_num(n: int | float) -> str:
    """Форматировать число с пробелом-разделителем (112 731)."""
    n = round(n)
    if n < 10_000:
        return str(n)
    return f"{n:,}".replace(",", " ")


def _calc_er(reactions: int | float, comments: int | float, views: int | float) -> float:
    """ER = (реакции + комментарии) / просмотры × 100."""
    if not views:
        return 0.0
    return round(((reactions or 0) + (comments or 0)) / views * 100, 1)


def _truncate_preview(text: str, max_len: int = 60) -> str:
    """Обрезать текст до max_len символов по целому слову, добавить «...»."""
    text = text.replace("\n", " ").strip()
    if len(text) <= max_len:
        return text
    truncated = text[:max_len]
    # Обрезаем по последнему пробелу
    last_space = truncated.rfind(" ")
    if last_space > 0:
        truncated = truncated[:last_space]
    return truncated + "..."



def _fmt_date(dt) -> str:
    """Дата в формате DD.MM."""
    return f"{dt.day:02d}.{dt.month:02d}"


def format_analyze_result(data: dict) -> str:
    """Назначение: форматировать результат /analyze в текст для Telegram (HTML).

    Параметры:
        data (dict): результат run_analyze_pipeline.

    Возвращает:
        str: готовое HTML-сообщение.
    """
    username = data["username"]
    posts_count = data["posts_count"]
    about = data.get("about", "")
    style = data.get("style", "")
    avg_views = data.get("avg_views", 0)
    avg_reactions = data.get("avg_reactions", 0)
    avg_comments = data.get("avg_comments", 0)
    er = _calc_er(avg_reactions, avg_comments, avg_views)

    tagline = data.get("tagline", "")

    lines = [
        f"📊 <b>@{username}</b> · {posts_count} постов",
    ]
    if tagline:
        lines.append(f"<i>«{html_mod.escape(tagline)}»</i>")
    lines += [
        "",
        f"📝 {about}",
        f"✍️ {style}",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        f"👁 {_fmt_num(avg_views)} охват · ❤️ {_fmt_num(avg_reactions)} · 💬 {_fmt_num(avg_comments)} · ER <b>{er}%</b>",
    ]

    # Топ тем
    top_topics = data.get("top_topics", [])
    if top_topics:
        lines.append("")
        lines.append("🏷 <b>Топ тем:</b>")
        for i, topic in enumerate(top_topics, 1):
            pct = topic.get("percentage", 0)
            label = topic["label"]
            pct_display = round(pct * 100) if pct <= 1.0 else round(pct)
            lines.append(f"{i}. {label} — {pct_display}%")

    # Топ посты
    top_posts = data.get("top_posts", [])
    if top_posts:
        medals = ["🥇", "🥈", "🥉"]
        lines.append("")
        lines.append("━━━━━━━━━━━━━━━━━━━━")
        lines.append("🏆 <b>Топ посты:</b>")
        lines.append("")
        for i, post in enumerate(top_posts[:3]):
            medal = medals[i]
            preview = _truncate_preview(post["text"])
            url = post["url"]
            views = post["views"]
            reactions = post["reactions"]
            comments = post["comments"]
            post_er = _calc_er(reactions, comments, views)
            date_str = _fmt_date(post["date"])
            lines.append(f'{medal} <a href="{url}">{html_mod.escape(preview)}</a>')
            lines.append(
                f"👁 {_fmt_num(views)} · ❤️ {_fmt_num(reactions)} · "
                f"💬 {_fmt_num(comments)} · ER <b>{post_er}%</b> · 📅 {date_str}"
            )
            lines.append("")

    # Хэштеги
    hashtags = data.get("hashtags", [])
    if hashtags:
        lines.append("━━━━━━━━━━━━━━━━━━━━")
        tags_str = " · ".join(hashtags)
        lines.append(f"🔑 {tags_str}")

    return "\n".join(lines)


class AnalyzerService:
    """Сервис-фасад команды /analyze: пайплайн анализа канала и форматирование."""

    run_analyze_pipeline = staticmethod(run_analyze_pipeline)
    format_analyze_result = staticmethod(format_analyze_result)

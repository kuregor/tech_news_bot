import asyncio
import html as html_mod
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from collections import Counter, defaultdict

from core.ai import ai_client
from core.embeddings import embedding_service
from core.parser import telegram_parser
from db.repository import (
    batch_upsert_posts,
    get_list_channels,
    get_posts_by_channels,
    get_topics_by_channel,
    save_digest,
    update_digest_status,
)
from db.models import DigestStatus, Post

logger = logging.getLogger(__name__)

# Сколько постов брать в зависимости от периода
TOP_N_MAP = {1: 5, 7: 10, 14: 15}

# Минимум постов с канала в пул кандидатов — чтобы ни один канал не монополизировал топ
POSTS_PER_CHANNEL = 2

PERIOD_LABELS = {1: "24 часа", 7: "неделю", 14: "две недели"}


def _channel_averages(posts: list[Post]) -> dict[int, tuple[float, float, float]]:
    """Средние views / reactions / forwards по каждому каналу за период."""
    by_channel: dict[int, list[Post]] = defaultdict(list)
    for p in posts:
        by_channel[p.channel_id].append(p)
    stats: dict[int, tuple[float, float, float]] = {}
    for ch_id, ps in by_channel.items():
        n = len(ps)
        if not n:
            stats[ch_id] = (1.0, 1.0, 1.0)
            continue
        avg_v = sum(p.views or 0 for p in ps) / n
        avg_r = sum(p.reactions or 0 for p in ps) / n
        avg_f = sum(p.forwards or 0 for p in ps) / n
        stats[ch_id] = (avg_v, avg_r, avg_f)
    return stats


def _calc_score(post: Post, ch_stats: dict[int, tuple[float, float, float]]) -> float:
    """Нормализованный рейтинг поста относительно среднего по его каналу.

    score = (views/avg_v)*0.4 + (reactions/avg_r)*0.4 + (forwards/avg_f)*0.2.
    Защита от деления на ноль: если avg = 0, используется 1.
    """
    avg_v, avg_r, avg_f = ch_stats.get(post.channel_id, (1.0, 1.0, 1.0))
    avg_v = avg_v if avg_v > 0 else 1.0
    avg_r = avg_r if avg_r > 0 else 1.0
    avg_f = avg_f if avg_f > 0 else 1.0
    return (
        (post.views or 0) / avg_v * 0.4
        + (post.reactions or 0) / avg_r * 0.4
        + (post.forwards or 0) / avg_f * 0.2
    )


async def _ensure_post_embeddings(posts: list[Post]) -> None:
    """Построить и сохранить эмбеддинги для постов, у которых их нет в ChromaDB."""
    if not posts:
        return
    texts = [p.text[:1500] for p in posts if p.text]
    ids = [p.id for p in posts if p.text]
    if not texts:
        return
    try:
        embeddings = await embedding_service.get_embeddings_batch(texts)
        metadatas = [{"post_id": pid, "channel_id": posts[i].channel_id} for i, pid in enumerate(ids)]
        embedding_service.upsert_post_embeddings_batch(ids, embeddings, metadatas)
    except Exception as e:
        logger.warning("Не удалось построить эмбеддинги постов: %s", e)


async def _filter_by_keywords(
    posts: list[Post],
    keywords: list[str],
    channel_ids: list[int],
) -> list[Post]:
    """Фильтрация постов по ключевым словам.

    Умный путь: Ollama эмбеддинг keyword → ChromaDB поиск.
    Fallback: ILIKE по тексту.
    """
    if not keywords:
        return posts

    post_ids_set = {p.id for p in posts}

    try:
        # Умная фильтрация через эмбеддинги
        matched_ids = set()
        for kw in keywords:
            kw_embedding = await embedding_service.get_embedding(kw)
            found = embedding_service.filter_by_keyword_embedding(
                kw_embedding, channel_ids=channel_ids, threshold=0.5, top_k=200
            )
            matched_ids.update(found)
        # Пересечение с нашими постами
        filtered = [p for p in posts if p.id in matched_ids]
        if filtered:
            logger.info("Умная фильтрация: %d → %d постов", len(posts), len(filtered))
            return filtered
    except Exception as e:
        logger.warning("Умная фильтрация не удалась, fallback на текстовый поиск: %s", e)

    # Fallback: текстовый поиск
    keywords_lower = [kw.lower() for kw in keywords]
    filtered = [
        p for p in posts
        if p.text and any(kw in p.text.lower() for kw in keywords_lower)
    ]
    logger.info("Текстовая фильтрация: %d → %d постов", len(posts), len(filtered))
    return filtered


def _deduplicate(posts: list[Post]) -> list[Post]:
    """Дедупликация через ChromaDB. Оставляем пост с MAX(views) из дубликатов."""
    if len(posts) <= 1:
        return posts

    post_ids = [p.id for p in posts]
    try:
        unique_ids = set(embedding_service.deduplicate_by_embeddings(post_ids))
        return [p for p in posts if p.id in unique_ids]
    except Exception as e:
        logger.warning("Дедупликация не удалась: %s", e)
        return posts


def _rank_and_limit(
    posts: list[Post],
    period_days: int,
    ch_stats: dict[int, tuple[float, float, float]],
) -> list[Post]:
    """Двухступенчатое ранжирование.

    1) Из каждого канала берём топ-K постов по нормализованному score,
       где K = max(POSTS_PER_CHANNEL, ceil(top_n / n_channels)).
       Это гарантирует, что ни один канал не заполнит весь пул, и при малом
       числе каналов пула хватит на финальный top-N.
    2) Из общего пула выбираем top-N — лучшие всё равно всплывают выше.
    """
    top_n = TOP_N_MAP.get(period_days, 10)
    if not posts:
        return []

    scores = {p.id: _calc_score(p, ch_stats) for p in posts}

    by_channel: dict[int, list[Post]] = defaultdict(list)
    for p in posts:
        by_channel[p.channel_id].append(p)

    n_channels = len(by_channel)
    per_channel = max(POSTS_PER_CHANNEL, -(-top_n // n_channels))

    pool: list[Post] = []
    for ps in by_channel.values():
        ps_sorted = sorted(ps, key=lambda p: scores[p.id], reverse=True)
        pool.extend(ps_sorted[:per_channel])

    pool.sort(key=lambda p: scores[p.id], reverse=True)
    return pool[:top_n]


def _calc_er(post: Post) -> str:
    """ER поста: (reactions + comments) / views * 100."""
    if not post.views:
        return "0"
    return f"{(post.reactions + post.comments) / post.views * 100:.1f}"


def _group_by_topic(
    top_posts: list[Post],
    classifications: list[dict],
    ch_stats: dict[int, tuple[float, float, float]],
) -> list[tuple[str, str, list[Post]]]:
    """Группировка постов по темам.

    Возвращает [(emoji, label, [posts]), ...] отсортированные по суммарному score.
    """
    topic_map = {}
    for item in classifications:
        topic_map[item["id"]] = (item.get("topic", "Разное"), item.get("emoji", "📌"))

    groups: dict[str, tuple[str, list[Post]]] = {}
    for post in top_posts:
        topic, emoji = topic_map.get(post.id, ("Разное", "📌"))
        if topic not in groups:
            groups[topic] = (emoji, [])
        groups[topic][1].append(post)

    result = [(emoji, label, posts) for label, (emoji, posts) in groups.items()]
    result.sort(key=lambda g: sum(_calc_score(p, ch_stats) for p in g[2]), reverse=True)
    return result


async def _summarize_posts(posts: list[Post]) -> list[str]:
    """Параллельная генерация резюме для каждого поста."""
    tasks = [ai_client.summarize_post(p.text[:1500]) for p in posts if p.text]
    summaries = await asyncio.gather(*tasks, return_exceptions=True)
    result = []
    for s in summaries:
        if isinstance(s, Exception):
            logger.warning("Ошибка суммаризации: %s", s)
            result.append("")
        else:
            result.append(s)
    return result


async def run_digest_pipeline(
    session: AsyncSession,
    channel_ids: list[int],
    channel_names: list[str],
    period_days: int,
    keywords: list[str] | None,
    user_id: int,
    channel_list_id: int | None,
    progress_callback: Callable[[str], Any] | None = None,
) -> dict:
    """Назначение: полный пайплайн команды /digest (сбор, фильтрация, ранжирование, AI).

    Параметры:
        session (AsyncSession): сессия БД.
        channel_ids (list[int]): идентификаторы каналов.
        channel_names (list[str]): username каналов.
        period_days (int): период в днях.
        keywords (list[str] | None): ключевые слова фильтра.
        user_id (int): идентификатор пользователя.
        channel_list_id (int | None): идентификатор списка каналов.
        progress_callback (Callable | None): async-колбэк обновления прогресса.

    Возвращает:
        dict: результат для форматирования (топ-посты, темы, метрики).
    """

    async def _progress(text: str) -> None:
        if progress_callback:
            await progress_callback(text)

    period_from = datetime.now(timezone.utc) - timedelta(days=period_days)
    period_to = datetime.now(timezone.utc)

    # 0. Подтягиваем свежие посты из Telegram для каждого канала списка,
    #    чтобы дайджест работал даже на каналах без /analyze.
    await _progress("📡 Обновляем посты каналов... (1/8)")
    for ch_id, ch_name in zip(channel_ids, channel_names):
        try:
            raw_posts = await telegram_parser.parse_channel_posts(ch_name)
            await batch_upsert_posts(session, ch_id, raw_posts)
        except Exception as e:
            logger.warning("Не удалось обновить посты @%s: %s", ch_name, e)

    # 1. Получаем посты
    await _progress("⏳ Загружаем посты... (2/8)")
    posts = await get_posts_by_channels(session, channel_ids, period_from)
    total_found = len(posts)
    logger.info("Загружено %d постов за %d дней", total_found, period_days)

    if not posts:
        return {
            "empty": True,
            "channel_names": channel_names,
            "period_days": period_days,
            "keywords": keywords,
        }

    # Средние по каналам за весь период — база для нормализации score
    ch_stats = _channel_averages(posts)

    # 2. Строим эмбеддинги (нужны для фильтрации и дедупликации)
    await _progress("🔢 Строим эмбеддинги... (3/8)")
    await _ensure_post_embeddings(posts)

    # 3. Фильтрация по ключевым словам
    await _progress("🔍 Фильтруем по темам... (4/8)")
    if keywords:
        posts = await _filter_by_keywords(posts, keywords, channel_ids)

    # 4. Дедупликация
    await _progress("🔎 Убираем дубликаты... (5/8)")
    posts = _deduplicate(posts)

    # 5. Ранжирование
    await _progress("📊 Ранжируем... (6/8)")
    top_posts = _rank_and_limit(posts, period_days, ch_stats)

    # 6. AI-суммаризация + классификация по темам (параллельно)
    await _progress("✍️ Генерируем резюме и классифицируем... (7/8)")
    posts_for_classify = [
        {"id": p.id, "text": (p.text or "")[:200]} for p in top_posts
    ]
    summaries, classifications = await asyncio.gather(
        _summarize_posts(top_posts),
        ai_client.classify_posts_by_topic(posts_for_classify),
    )

    # 6.5. Группировка по темам
    await _progress("📂 Группируем по темам... (8/8)")
    grouped_posts = _group_by_topic(top_posts, classifications, ch_stats)

    # 7. Собираем данные для сохранения
    posts_data = []
    for i, post in enumerate(top_posts):
        posts_data.append({
            "post_id": post.id,
            "rank": i + 1,
            "summary": summaries[i] if i < len(summaries) else "",
        })

    # 8. Сохраняем дайджест
    digest = await save_digest(
        session,
        user_id=user_id,
        channel_list_id=channel_list_id,
        period_days=period_days,
        period_from=period_from,
        period_to=period_to,
        filter_keywords=keywords,
        posts_data=posts_data,
        status=DigestStatus.sent,
    )

    # Собираем channel username по id из переданных данных (без lazy load)
    channel_map = dict(zip(channel_ids, channel_names))

    # Определяем топ-канал
    channel_counter = Counter(post.channel_id for post in top_posts)
    top_channel_id = channel_counter.most_common(1)[0][0] if channel_counter else None
    top_channel_name = channel_map.get(top_channel_id, "?") if top_channel_id else "?"

    # Главная тема — из channel_topics наиболее частая
    main_topic = ""
    try:
        topic_counter = Counter()
        for ch_id in channel_ids:
            topics = await get_topics_by_channel(session, ch_id, min_percentage=settings.TOPIC_THRESHOLD)
            for t in topics:
                topic_counter[t.label] += 1
        if topic_counter:
            main_topic = topic_counter.most_common(1)[0][0]
    except Exception:
        pass

    return {
        "empty": False,
        "channel_names": channel_names,
        "period_days": period_days,
        "period_label": PERIOD_LABELS.get(period_days, f"{period_days} дн."),
        "keywords": keywords,
        "top_posts": top_posts,
        "summaries": summaries,
        "grouped_posts": grouped_posts,
        "topics_count": len(grouped_posts),
        "channel_map": channel_map,
        "total_found": total_found,
        "top_n": len(top_posts),
        "top_channel": top_channel_name,
        "main_topic": main_topic,
    }


def _fmt_num(n: int | float) -> str:
    """Форматировать число с пробелом-разделителем."""
    n = round(n)
    if n < 10_000:
        return str(n)
    return f"{n:,}".replace(",", " ")


def _truncate_title(text: str, max_len: int = 80) -> str:
    """Обрезать текст поста до заголовка, по границе слова."""
    line = (text or "").replace("\n", " ").strip()[:max_len]
    if len(text or "") > max_len:
        last_space = line.rfind(" ")
        if last_space > 0:
            line = line[:last_space]
        line += "..."
    return line


def format_digest(data: dict) -> str:
    """Назначение: форматировать результат /digest в текст для Telegram (HTML).

    Параметры:
        data (dict): результат run_digest_pipeline.

    Возвращает:
        str: готовое HTML-сообщение.
    """
    if data.get("empty"):
        period_label = PERIOD_LABELS.get(data["period_days"], f"{data['period_days']} дн.")
        channels = " · ".join(f"@{n}" for n in data["channel_names"])
        return (
            f"📰 Дайджест · {period_label}\n"
            f"📋 {channels}\n\n"
            "Постов за этот период не найдено."
        )

    period_label = data["period_label"]
    channels = " · ".join(f"@{n}" for n in data["channel_names"])
    keywords = data.get("keywords")

    lines = [
        f"📰 Дайджест · {period_label}",
        f"📋 {channels}",
    ]
    if keywords:
        lines.append(f"🔑 Фильтр: {' · '.join(keywords)}")
    lines.append("━━━━━━━━━━━━━━━━━━━━")

    summaries = data["summaries"]
    channel_map = data["channel_map"]
    grouped_posts = data["grouped_posts"]

    # Маппинг post.id → summary
    summary_map = {}
    for i, post in enumerate(data["top_posts"]):
        summary_map[post.id] = summaries[i] if i < len(summaries) else ""

    rank = 0
    for emoji, label, posts in grouped_posts:
        n_word = _plural_materials(len(posts))
        lines.append("")
        lines.append(f"{emoji} <b>{html_mod.escape(label)}</b> ({len(posts)} {n_word})")
        lines.append("")

        for post in posts:
            rank += 1
            title = html_mod.escape(_truncate_title(post.text))
            ch_username = channel_map.get(post.channel_id, "?")
            url = f"https://t.me/{ch_username}/{post.tg_id}"
            date_str = f"{post.date.day:02d}.{post.date.month:02d}"
            er = _calc_er(post)
            summary = summary_map.get(post.id, "")

            lines.append(
                f'{rank}️⃣ <a href="{url}">«{title}»</a>'
            )
            lines.append(
                f"📣 @{ch_username} · {date_str} · ER <b>{er}%</b> · 👁 {_fmt_num(post.views)}"
            )
            if summary:
                lines.append(f"▸ {html_mod.escape(summary)}")
            lines.append("")

        lines.append("━━━━━━━━━━━━━━━━━━━━")

    lines.append(
        f"📊 <b>{data['top_n']}</b> материалов · <b>{data['topics_count']}</b> тем · "
        f"из {data['total_found']} найденных"
    )
    lines.append(f"🏆 Топ-канал: <b>@{data['top_channel']}</b>")
    if data.get("main_topic"):
        lines.append(f"🔥 Главная тема: {data['main_topic']}")

    return "\n".join(lines)


def _plural_materials(n: int) -> str:
    """Склонение слова 'материал'."""
    if 11 <= n % 100 <= 19:
        return "материалов"
    last = n % 10
    if last == 1:
        return "материал"
    if 2 <= last <= 4:
        return "материала"
    return "материалов"


class DigestService:
    """Сервис-фасад команды /digest: пайплайн дайджеста и форматирование."""

    run_digest_pipeline = staticmethod(run_digest_pipeline)
    format_digest = staticmethod(format_digest)

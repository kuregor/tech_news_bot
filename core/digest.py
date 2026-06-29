import asyncio
import html as html_mod
import logging
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from core import topics as taxonomy
from core.ai import ai_client
from core.embeddings import embedding_service
from core.formatting import _fmt_num, _plural, _truncate
from core.metrics import _calc_er, _channel_averages
from core.metrics import _viral_score as _calc_score
from core.parser import telegram_parser
from db.models import DigestStatus, Post
from db.repository import (
    batch_upsert_posts,
    get_posts_by_channels,
    save_digest,
)

logger = logging.getLogger(__name__)

# Сколько постов брать в зависимости от периода
TOP_N_MAP = {1: 5, 7: 10, 14: 15}

# Минимум постов с канала в пул кандидатов — чтобы ни один канал не монополизировал топ
POSTS_PER_CHANNEL = 2

PERIOD_LABELS = {1: "24 часа", 7: "за неделю", 14: "за две недели"}

# Порог косинусной близости для семантического фильтра по ключевым словам.
# Выше → строже (меньше «похожих» постов), ниже → шире.
KEYWORD_SIMILARITY_THRESHOLD = 0.65


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
        metadatas = [
            {"post_id": pid, "channel_id": posts[i].channel_id}
            for i, pid in enumerate(ids)
        ]
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

    try:
        # Умная фильтрация через эмбеддинги
        matched_ids = set()
        for kw in keywords:
            kw_embedding = await embedding_service.get_embedding(kw)
            found = embedding_service.filter_by_keyword_embedding(
                kw_embedding,
                channel_ids=channel_ids,
                threshold=KEYWORD_SIMILARITY_THRESHOLD,
                top_k=200,
            )
            matched_ids.update(found)
        # Пересечение с нашими постами
        filtered = [p for p in posts if p.id in matched_ids]
        if filtered:
            logger.info("Умная фильтрация: %d → %d постов", len(posts), len(filtered))
            return filtered
    except Exception as e:
        logger.warning(
            "Умная фильтрация не удалась, fallback на текстовый поиск: %s", e
        )

    # Fallback: текстовый поиск
    keywords_lower = [kw.lower() for kw in keywords]
    filtered = [
        p
        for p in posts
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


def _preselect(
    posts: list[Post],
    cap: int,
    ch_stats: dict[int, tuple[float, float, float]],
) -> list[Post]:
    """Грубый предотбор кандидатов по engagement-score до классификации.

    Берём топ-`cap` постов по нормализованному score, чтобы не гонять LLM
    по сотням постов. Запас (cap = 4 × top_n) — чтобы после отсева other
    осталось ≥ top_n.
    """
    if len(posts) <= cap:
        return posts
    return sorted(posts, key=lambda p: _calc_score(p, ch_stats), reverse=True)[:cap]


def _group_by_topic(
    top_posts: list[Post],
    topic_by_id: dict[int, str],
    ch_stats: dict[int, tuple[float, float, float]],
) -> list[tuple[str, str, list[Post]]]:
    """Группировка постов по slug-темам из закрытого набора.

    Возвращает [(emoji, label, [posts]), ...] отсортированные по суммарному score.
    Пустые темы не появляются (группа создаётся только при наличии постов),
    посты other пропускаются (страховка — они уже отсеяны выше).
    """
    groups: dict[str, list[Post]] = defaultdict(list)
    for post in top_posts:
        slug = topic_by_id.get(post.id, taxonomy.OTHER)
        if slug == taxonomy.OTHER:
            continue
        groups[slug].append(post)

    result = [
        (taxonomy.emoji_for(slug), taxonomy.label_for(slug), posts)
        for slug, posts in groups.items()
    ]
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

    period_from = datetime.now(UTC) - timedelta(days=period_days)
    period_to = datetime.now(UTC)

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

    # 5. Предотбор кандидатов и классификация по закрытому набору тем.
    #    Классифицируем расширенный пул (×4 от top_n), отсекаем other, и только
    #    потом финально ранжируем — чтобы не суммаризировать посты вне тем.
    await _progress("📊 Ранжируем и классифицируем... (6/8)")
    top_n = TOP_N_MAP.get(period_days, 10)
    candidates = _preselect(posts, top_n * 4, ch_stats)
    posts_for_classify = [
        {"id": p.id, "text": (p.text or "")[:200]} for p in candidates
    ]
    classifications = await ai_client.classify_posts_by_topic(posts_for_classify)
    topic_by_id = {c["id"]: c["topic"] for c in classifications if isinstance(c, dict)}

    # Отсекаем посты вне тем (other / без классификации)
    on_topic = [
        p for p in candidates if topic_by_id.get(p.id, taxonomy.OTHER) != taxonomy.OTHER
    ]

    # 6. Финальное ранжирование выживших → top-N
    top_posts = _rank_and_limit(on_topic, period_days, ch_stats)
    if not top_posts:
        return {
            "empty": True,
            "channel_names": channel_names,
            "period_days": period_days,
            "keywords": keywords,
        }

    # 7. Суммаризация финальных постов
    await _progress("✍️ Генерируем резюме... (7/8)")
    summaries = await _summarize_posts(top_posts)

    # 7.5. Группировка по темам
    await _progress("📂 Группируем по темам... (8/8)")
    grouped_posts = _group_by_topic(top_posts, topic_by_id, ch_stats)

    # 7. Собираем данные для сохранения
    posts_data = []
    for i, post in enumerate(top_posts):
        posts_data.append(
            {
                "post_id": post.id,
                "rank": i + 1,
                "summary": summaries[i] if i < len(summaries) else "",
            }
        )

    # 8. Сохраняем дайджест
    await save_digest(
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

    # Главная тема — самая весомая группа самого дайджеста
    main_topic = grouped_posts[0][1] if grouped_posts else ""

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


def format_digest(data: dict) -> str:
    """Назначение: форматировать результат /digest в текст для Telegram (HTML).

    Параметры:
        data (dict): результат run_digest_pipeline.

    Возвращает:
        str: готовое HTML-сообщение.
    """
    if data.get("empty"):
        period_label = PERIOD_LABELS.get(
            data["period_days"], f"{data['period_days']} дн."
        )
        channels = " · ".join(f"@{n}" for n in data["channel_names"])
        keywords = data.get("keywords")
        filter_line = f"🔑 Фильтр: {' · '.join(keywords)}\n" if keywords else ""
        return (
            f"📰 Дайджест · {period_label}\n"
            f"📋 {channels}\n"
            f"{filter_line}\n"
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
        lines.append("")
        lines.append(f"{emoji} <b>{html_mod.escape(label)}</b>")
        lines.append("")

        for post in posts:
            rank += 1
            title = html_mod.escape(_truncate(post.text, 80))
            ch_username = channel_map.get(post.channel_id, "?")
            url = f"https://t.me/{ch_username}/{post.tg_id}"
            date_str = f"{post.date.day:02d}.{post.date.month:02d}"
            er = f"{_calc_er(post.reactions or 0, post.comments or 0, post.views or 0):.1f}"
            summary = summary_map.get(post.id, "")

            lines.append(f'{rank}️⃣ <a href="{url}">«{title}»</a>')
            lines.append(f"📣 @{ch_username}")
            if summary:
                lines.append(f"▸ {html_mod.escape(summary)}")
            lines.append(
                f"🗓 {date_str} · 👁 {_fmt_num(post.views)} · "
                f"❤️ {_fmt_num(post.reactions or 0)} · 💬 {_fmt_num(post.comments or 0)} · "
                f"ER <b>{er}%</b>"
            )
            lines.append("")

        lines.append("━━━━━━━━━━━━━━━━━━━━")

    lines.append(
        f"📊 <b>{data['top_n']}</b> {_plural(data['top_n'], 'материал', 'материала', 'материалов')} · "
        f"<b>{data['topics_count']}</b> {_plural(data['topics_count'], 'тема', 'темы', 'тем')} · "
        f"из {data['total_found']} постов"
    )
    lines.append(f"🏆 Топ-канал: <b>@{data['top_channel']}</b>")
    if data.get("main_topic"):
        lines.append(f"🔥 Главная тема: {data['main_topic']}")

    return "\n".join(lines)


def format_digest_rich(data: dict) -> str:
    """Назначение: форматировать /digest в rich-HTML (структурная страница).

    Запасной вариант — обычный format_digest.
    """
    period_label = PERIOD_LABELS.get(data["period_days"], f"{data['period_days']} дн.")
    channels = " · ".join(f"@{html_mod.escape(n)}" for n in data["channel_names"])

    if data.get("empty"):
        keywords = data.get("keywords")
        filter_line = (
            f"<p>🔑 Фильтр: {' · '.join(html_mod.escape(k) for k in keywords)}</p>"
            if keywords
            else ""
        )
        return (
            f"<h2>📰 Дайджест · {period_label}</h2>"
            f"<p>📋 {channels}</p>"
            f"{filter_line}"
            "<p>Постов за этот период не найдено.</p>"
        )

    parts = [
        f"<h2>📰 Дайджест · {data['period_label']}</h2>",
        f"<p>📋 {channels}</p>",
    ]
    keywords = data.get("keywords")
    if keywords:
        kw = " · ".join(html_mod.escape(k) for k in keywords)
        parts.append(f"<p>🔑 Фильтр: {kw}</p>")
    parts.append("<hr/>")

    channel_map = data["channel_map"]
    summary_map = {}
    for i, post in enumerate(data["top_posts"]):
        summary_map[post.id] = (
            data["summaries"][i] if i < len(data["summaries"]) else ""
        )

    rank = 0
    for emoji, label, posts in data["grouped_posts"]:
        parts.append(f"<h3>{emoji} {html_mod.escape(label)}</h3>")
        # Сквозная нумерация: список каждой темы продолжает счёт предыдущей.
        parts.append(f'<ol start="{rank + 1}">')
        for post in posts:
            rank += 1
            title = html_mod.escape(_truncate(post.text, 80))
            ch_username = channel_map.get(post.channel_id, "?")
            url = f"https://t.me/{html_mod.escape(ch_username)}/{post.tg_id}"
            date_str = f"{post.date.day:02d}.{post.date.month:02d}"
            er = f"{_calc_er(post.reactions or 0, post.comments or 0, post.views or 0):.1f}"
            summary = summary_map.get(post.id, "")
            metrics = (
                f"🗓 {date_str} · 👁 {_fmt_num(post.views)} · "
                f"❤️ {_fmt_num(post.reactions or 0)} · 💬 {_fmt_num(post.comments or 0)} · "
                f"ER <b>{er}%</b>"
            )
            item = (
                f'<li><a href="{url}">«{title}»</a>'
                f"<br>📣 @{html_mod.escape(ch_username)}"
            )
            if summary:
                item += f"<blockquote>{html_mod.escape(summary)}</blockquote>{metrics}"
            else:
                item += f"<br>{metrics}"
            item += "</li>"
            parts.append(item)
        parts.append("</ol>")

    parts.append("<hr/>")
    footer = (
        f"📊 <b>{data['top_n']}</b> {_plural(data['top_n'], 'материал', 'материала', 'материалов')} · "
        f"<b>{data['topics_count']}</b> {_plural(data['topics_count'], 'тема', 'темы', 'тем')} · "
        f"из {data['total_found']} постов"
        f"<br>🏆 Топ-канал: <b>@{html_mod.escape(data['top_channel'])}</b>"
    )
    if data.get("main_topic"):
        footer += f"<br>🔥 Главная тема: {html_mod.escape(data['main_topic'])}"
    parts.append(f"<p>{footer}</p>")

    return "".join(parts)

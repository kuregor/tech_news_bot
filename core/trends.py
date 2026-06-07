import html as html_mod
import logging
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from core.ai import ai_client
from db.models import Post
from db.repository import get_posts_by_channels

logger = logging.getLogger(__name__)

PERIOD_LABELS = {1: "24 часа", 7: "неделю", 14: "две недели"}

# Минимальное суммарное число упоминаний темы, чтобы учитывать её в трендах
MIN_MENTIONS = 3

# Ограничения на размер блоков вывода
TOP_RISING = 5
TOP_DECLINING = 5
TOP_NEW = 5


# ─── Помощники ──────────────────────────────────────────


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


def _viral_score(post: Post, ch_stats: dict[int, tuple[float, float, float]]) -> float:
    """Нормализованный рейтинг вирусности поста относительно среднего канала."""
    avg_v, avg_r, avg_f = ch_stats.get(post.channel_id, (1.0, 1.0, 1.0))
    # Защита от деления на ноль — если среднее = 0, подставляем 1
    avg_v = avg_v if avg_v > 0 else 1.0
    avg_r = avg_r if avg_r > 0 else 1.0
    avg_f = avg_f if avg_f > 0 else 1.0
    return (
        (post.views or 0) / avg_v * 0.4
        + (post.reactions or 0) / avg_r * 0.4
        + (post.forwards or 0) / avg_f * 0.2
    )


# ─── Пайплайн ───────────────────────────────────────────

async def run_trends_pipeline(
    session: AsyncSession,
    channel_ids: list[int],
    channel_names: list[str],
    period_days: int,
    progress_callback: Callable[[str], Any] | None = None,
) -> dict:
    """Назначение: полный пайплайн команды /trends (тренд-радар, AI-классификация тем).

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

    period_to = datetime.now(timezone.utc)
    period_from = period_to - timedelta(days=period_days)

    # 1. Посты за период
    await _progress("⏳ Загружаем посты... (1/4)")
    posts = await get_posts_by_channels(session, channel_ids, period_from)
    if not posts:
        return {
            "empty": True,
            "channel_names": channel_names,
            "period_days": period_days,
            "period_label": PERIOD_LABELS.get(period_days, f"{period_days} дн."),
        }

    # 2. Деление на половины
    await _progress("📊 Делим на половины... (2/4)")
    first, second = _split_halves(posts, period_from, period_to)
    n_first = len(first)
    n_second = len(second)

    first_ids = {p.id for p in first}
    second_ids = {p.id for p in second}
    posts_map = {p.id: p for p in posts}

    # 3. Темы по постам — AI-классификация без зависимости от /analyze
    await _progress("🤖 Классифицируем темы... (3/4)")
    raw_classification = await ai_client.classify_posts_for_trends(posts)
    topics_map: dict[int, list[str]] = {
        pid: [label]
        for pid, label in raw_classification.items()
        if label and pid in posts_map
    }

    # 4. Подсчёт упоминаний и связка тем с постами
    await _progress("🧮 Считаем тренды... (4/4)")
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
            new_topics.append({
                "label": label,
                "posts_count": posts_count,
                "avg_views": avg_views,
                "second_count": c2,
            })
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
    await _progress("🔥 Ищем вирусные посты... (4/4)")
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


# ─── Форматирование ─────────────────────────────────────

def _fmt_num(n: int | float) -> str:
    n = round(n)
    if n < 10_000:
        return str(n)
    return f"{n:,}".replace(",", " ")


_TOPIC_EMOJI: dict[str, str] = {
    "финансы": "💰",
    "технологии": "💻",
    "интернет": "🌐",
    "гаджеты": "📱",
    "игры": "🎮",
    "программирование": "⌨️",
    "разработка": "⚙️",
    "дизайн": "🎨",
    "искусственный интеллект": "🤖",
    "безопасность": "🔐",
    "новости": "📰",
    "события": "📰",
    "обзоры": "📰",
    "автомобил": "🚗",
    "наука": "🔬",
    "бизнес": "📊",
    "данные": "📊",
    "python": "🐍",
    "мотивация": "💡",
    "личность": "💡",
    "разное": "📌",
}


def _topic_emoji(label: str) -> str:
    lower = label.lower()
    for key, emoji in _TOPIC_EMOJI.items():
        if key in lower:
            return emoji
    return "•"


def _truncate(text: str, max_len: int = 140) -> str:
    text = (text or "").replace("\n", " ").strip()
    if len(text) <= max_len:
        return text
    truncated = text[:max_len]
    last_space = truncated.rfind(" ")
    if last_space > 0:
        truncated = truncated[:last_space]
    return truncated + "..."


def _decline_channels(n: int) -> str:
    if 11 <= n % 100 <= 19:
        return f"{n} каналов"
    r = n % 10
    if r == 1:
        return f"{n} канал"
    if 2 <= r <= 4:
        return f"{n} канала"
    return f"{n} каналов"


def _decline_posts(n: int) -> str:
    if 11 <= n % 100 <= 19:
        return f"{n} постов"
    r = n % 10
    if r == 1:
        return f"{n} пост"
    if 2 <= r <= 4:
        return f"{n} поста"
    return f"{n} постов"


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
            emoji = _topic_emoji(t["label"])
            c1 = t.get("first_count", 0)
            c2 = t.get("second_count", 0)
            lines.append(
                f"{emoji} {html_mod.escape(t['label'])} — "
                f"<b>+{round(t['growth'])}%</b> ({c1} → {_decline_posts(c2)})"
            )
            sample = t.get("sample_text")
            if sample:
                url = t.get("sample_url", "")
                ch = t.get("sample_channel", "")
                lines.append(f'   └ <a href="{url}">«{html_mod.escape(sample)}»</a> @{ch}')
        lines.append("")

    if new_topics:
        lines.append("🆕 <b>Появилось впервые:</b>")
        for t in new_topics:
            emoji = _topic_emoji(t["label"])
            lines.append(
                f"  {emoji} {html_mod.escape(t['label'])} · {t['posts_count']} постов"
            )
        lines.append("")

    if declining:
        lines.append("📉 <b>Затихло:</b>")
        for t in declining:
            emoji = _topic_emoji(t["label"])
            if round(abs(t["growth"])) >= 100:
                lines.append(
                    f"  {emoji} {html_mod.escape(t['label'])} — исчезло из эфира"
                )
            else:
                lines.append(
                    f"  {emoji} {html_mod.escape(t['label'])} — "
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
        lines.append(
            f'🔥 <b>Самый виральный:</b> <a href="{url}">«{html_mod.escape(preview)}»</a>'
        )
        lines.append(
            f"   @{ch_username} · {_fmt_num(top.views or 0)} 👁 · "
            f"{_fmt_num(top.reactions or 0)} ❤️"
        )

    return "\n".join(lines)


class TrendsService:
    """Сервис-фасад команды /trends: тренд-радар и форматирование."""

    run_trends_pipeline = staticmethod(run_trends_pipeline)
    format_trends = staticmethod(format_trends)

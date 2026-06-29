"""Числовые метрики, общие для команд /analyze, /digest, /trends.

Средние по каналу, нормализованный engagement-score и ER (engagement rate)
раньше дублировались в core/digest.py и core/trends.py; здесь — единая версия,
чтобы рейтинг постов и ER считались одинаково во всех командах.
"""

from collections import defaultdict

from db.models import Post


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


def _calc_er(
    reactions: int | float, comments: int | float, views: int | float
) -> float:
    """ER (engagement rate) = (реакции + комментарии) / просмотры × 100."""
    if not views:
        return 0.0
    return round(((reactions or 0) + (comments or 0)) / views * 100, 1)

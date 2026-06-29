"""Текстовые форматтеры, общие для команд /analyze, /compare, /digest, /trends.

Раньше эти хелперы были скопированы по модулям core/*; здесь собрана одна
каноничная версия каждого, чтобы числа и обрезка текста выглядели одинаково
во всех командах.
"""

import re


def _fmt_num(n: int | float) -> str:
    """Форматировать число с пробелом-разделителем тысяч (112 731)."""
    n = round(n)
    if n < 10_000:
        return str(n)
    return f"{n:,}".replace(",", " ")


def _fmt_pct(n: float) -> str:
    """Процент с одним знаком после запятой (12.3%)."""
    return f"{n:.1f}%"


def _strip_md(text: str) -> str:
    """Убрать markdown из текста поста: [текст](url) → текст, снять **/~~/`.

    Telegram-каналы пишут разметку прямо в тексте; в заголовках/превью она
    выглядит как лишние звёздочки/тильды и сырые ссылки — вырезаем их.
    """
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    for marker in ("*", "~", "`"):
        text = text.replace(marker, "")
    return text


def _truncate(text: str, max_len: int = 140) -> str:
    """Обрезать текст по границе слова до max_len символов, добавить «...».

    Текст предварительно очищается от markdown и переводов строк. Обрезка идёт
    по последнему пробелу, но только если он не слишком рано — иначе длинный
    первый токен/URL схлопнул бы превью до пары символов.
    """
    text = _strip_md(text or "").replace("\n", " ").strip()
    if len(text) <= max_len:
        return text
    truncated = text[:max_len]
    last_space = truncated.rfind(" ")
    if last_space > max_len // 2:
        truncated = truncated[:last_space]
    return truncated + "..."


def _plural(n: int, one: str, few: str, many: str) -> str:
    """Выбор формы слова по числу: 1→one, 2-4→few, иначе many."""
    if 11 <= n % 100 <= 19:
        return many
    r = n % 10
    if r == 1:
        return one
    if 2 <= r <= 4:
        return few
    return many


def _decline_posts(n: int) -> str:
    """Склонение слова «пост»: 1 пост, 2 поста, 5 постов."""
    return _plural(n, f"{n} пост", f"{n} поста", f"{n} постов")


def _decline_channels(n: int) -> str:
    """Склонение слова «канал»: 1 канал, 2 канала, 5 каналов."""
    return _plural(n, f"{n} канал", f"{n} канала", f"{n} каналов")

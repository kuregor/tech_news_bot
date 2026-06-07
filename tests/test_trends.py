"""Unit-тесты модуля core/trends.py."""
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

from core.trends import (
    _split_halves,
    _channel_averages,
    _viral_score,
    _truncate,
    _fmt_num,
    _decline_posts,
)


def _make_post(days_ago: int, channel_id: int = 1,
               views: int = 100, reactions: int = 10, forwards: int = 5):
    """Фабрика тестовых постов через MagicMock."""
    post = MagicMock()
    post.date = datetime.now(timezone.utc) - timedelta(days=days_ago)
    post.channel_id = channel_id
    post.views = views
    post.reactions = reactions
    post.forwards = forwards
    return post


# ─── _split_halves ──────────────────────────────────────────────────────────

class TestSplitHalves:

    def test_equal_distribution(self):
        """
        - Тестируется _split_halves
        - Сценарий: равномерное распределение постов по половинам 14-дневного периода
        - Результат: по 2 поста в каждой половине
        """
        now = datetime.now(timezone.utc)
        period_from = now - timedelta(days=14)
        posts = [
            _make_post(13),  # первая половина
            _make_post(10),  # первая половина
            _make_post(3),   # вторая половина
            _make_post(1),   # вторая половина
        ]
        first, second = _split_halves(posts, period_from, now)
        assert len(first) == 2
        assert len(second) == 2

    def test_empty_posts(self):
        """
        - Тестируется _split_halves
        - Сценарий: пустой список постов
        - Результат: обе половины пустые
        """
        now = datetime.now(timezone.utc)
        first, second = _split_halves([], now - timedelta(days=7), now)
        assert first == []
        assert second == []

    def test_all_posts_in_first_half(self):
        """
        - Тестируется _split_halves
        - Сценарий: все посты — в начале периода (первая половина)
        - Результат: first содержит все посты, second пуст
        """
        now = datetime.now(timezone.utc)
        posts = [_make_post(13), _make_post(12), _make_post(11)]
        first, second = _split_halves(posts, now - timedelta(days=14), now)
        assert len(first) == 3
        assert len(second) == 0


# ─── _channel_averages ──────────────────────────────────────────────────────

class TestChannelAverages:

    def test_single_channel_averages(self):
        """
        - Тестируется _channel_averages
        - Сценарий: три поста одного канала с разными метриками
        - Результат: корректные средние views/reactions/forwards
        """
        posts = [
            _make_post(1, channel_id=1, views=100, reactions=10, forwards=5),
            _make_post(2, channel_id=1, views=200, reactions=20, forwards=10),
            _make_post(3, channel_id=1, views=300, reactions=30, forwards=15),
        ]
        stats = _channel_averages(posts)
        avg_v, avg_r, avg_f = stats[1]
        assert avg_v == pytest.approx(200.0)
        assert avg_r == pytest.approx(20.0)
        assert avg_f == pytest.approx(10.0)

    def test_zero_metrics_no_error(self):
        """
        - Тестируется _channel_averages
        - Сценарий: посты с нулевыми метриками
        - Результат: нули без исключений
        """
        posts = [_make_post(1, channel_id=2, views=0, reactions=0, forwards=0)]
        stats = _channel_averages(posts)
        assert stats[2] == (0.0, 0.0, 0.0)


# ─── _viral_score ────────────────────────────────────────────────────────────

class TestViralScore:

    def test_above_average_post(self):
        """
        - Тестируется _viral_score
        - Сценарий: пост значительно выше среднего по каналу
        - Результат: score > 1.0
        """
        post = _make_post(1, views=2000, reactions=200, forwards=100)
        ch_stats = {1: (1000.0, 100.0, 50.0)}
        score = _viral_score(post, ch_stats)
        assert score > 1.0

    def test_zero_averages_no_division_error(self):
        """
        - Тестируется _viral_score
        - Сценарий: нулевые средние (потенциальное деление на ноль)
        - Результат: не выбрасывает ZeroDivisionError
        """
        post = _make_post(1, views=500, reactions=50, forwards=25)
        ch_stats = {1: (0.0, 0.0, 0.0)}
        score = _viral_score(post, ch_stats)
        assert isinstance(score, float)


# ─── _truncate ───────────────────────────────────────────────────────────────

class TestTruncate:

    def test_long_text_truncated(self):
        """
        - Тестируется _truncate
        - Сценарий: текст длиннее max_len
        - Результат: обрезается, оканчивается на "..."
        """
        long_text = "слово " * 40  # ~240 символов
        result = _truncate(long_text, max_len=140)
        assert len(result) <= 143
        assert result.endswith("...")

    def test_short_text_unchanged(self):
        """
        - Тестируется _truncate
        - Сценарий: текст короче max_len
        - Результат: возвращается без изменений
        """
        short = "Привет мир"
        assert _truncate(short, max_len=140) == short


# ─── _fmt_num ────────────────────────────────────────────────────────────────

class TestFmtNum:

    def test_small_number(self):
        """
        - Тестируется _fmt_num
        - Сценарий: число < 10 000
        - Результат: строка без разделителей
        """
        assert _fmt_num(9999) == "9999"

    def test_large_number_with_space(self):
        """
        - Тестируется _fmt_num
        - Сценарий: число >= 10 000
        - Результат: строка с пробелом-разделителем тысяч
        """
        assert _fmt_num(15000) == "15 000"


# ─── _decline_posts ──────────────────────────────────────────────────────────

class TestDeclinePosts:

    def test_declension_rules(self):
        """
        - Тестируется _decline_posts
        - Сценарий: все формы склонения (1, 2-4, 5+, 11-19)
        - Результат: корректная форма слова "пост"
        """
        assert _decline_posts(1)  == "1 пост"
        assert _decline_posts(2)  == "2 поста"
        assert _decline_posts(4)  == "4 поста"
        assert _decline_posts(5)  == "5 постов"
        assert _decline_posts(11) == "11 постов"
        assert _decline_posts(21) == "21 пост"

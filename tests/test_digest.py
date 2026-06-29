"""Unit-тесты пайплайна дайджеста (core/digest.py)."""

from unittest.mock import MagicMock

from core.digest import _group_by_topic, _preselect


def _make_post(
    pid: int,
    channel_id: int = 1,
    views: int = 100,
    reactions: int = 10,
    forwards: int = 5,
):
    post = MagicMock()
    post.id = pid
    post.channel_id = channel_id
    post.views = views
    post.reactions = reactions
    post.forwards = forwards
    return post


_CH_STATS = {1: (100.0, 10.0, 5.0)}


class TestGroupByTopic:

    def test_other_posts_excluded(self):
        """
        - Тестируется _group_by_topic
        - Сценарий: среди постов есть тема other
        - Результат: post с other не попадает ни в одну группу
        """
        p1, p2, p3 = _make_post(1), _make_post(2), _make_post(3)
        topic_by_id = {1: "ai", 2: "other", 3: "development"}
        groups = _group_by_topic([p1, p2, p3], topic_by_id, _CH_STATS)

        labels = {label for _, label, _ in groups}
        assert "Не по теме" not in labels
        grouped_ids = {p.id for _, _, posts in groups for p in posts}
        assert grouped_ids == {1, 3}

    def test_labels_and_emoji_from_taxonomy(self):
        """
        - Тестируется _group_by_topic
        - Сценарий: один пост с темой ai
        - Результат: emoji и label берутся из таксономии
        """
        groups = _group_by_topic([_make_post(1)], {1: "ai"}, _CH_STATS)
        emoji, label, posts = groups[0]
        assert label == "ИИ"
        assert emoji == "🤖"

    def test_all_other_gives_no_groups(self):
        """
        - Тестируется _group_by_topic
        - Сценарий: все посты классифицированы как other
        - Результат: пустой список групп (дайджест отдаст «пусто»)
        """
        posts = [_make_post(1), _make_post(2)]
        groups = _group_by_topic(posts, {1: "other", 2: "other"}, _CH_STATS)
        assert groups == []


class TestPreselect:

    def test_caps_to_top_by_score(self):
        """
        - Тестируется _preselect
        - Сценарий: 5 постов, кэп 3
        - Результат: 3 поста с наибольшим engagement-score
        """
        posts = [_make_post(i, views=i * 100) for i in range(1, 6)]
        selected = _preselect(posts, 3, _CH_STATS)
        assert len(selected) == 3
        assert {p.id for p in selected} == {5, 4, 3}

    def test_returns_all_when_under_cap(self):
        """
        - Тестируется _preselect
        - Сценарий: постов меньше кэпа
        - Результат: список возвращается целиком
        """
        posts = [_make_post(1), _make_post(2)]
        assert _preselect(posts, 10, _CH_STATS) == posts

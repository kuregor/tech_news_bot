"""Unit-тесты закрытой таксономии тем (core/topics.py)."""

from core import topics


class TestNormalize:

    def test_all_slugs_pass_through(self):
        """
        - Тестируется normalize
        - Сценарий: каждый из 12 разрешённых slug подаётся как есть
        - Результат: возвращается тот же slug
        """
        for _, slug, _ in topics.TOPICS:
            assert topics.normalize(slug) == slug

    def test_label_form_maps_to_slug(self):
        """
        - Тестируется normalize
        - Сценарий: модель вернула русский label вместо slug
        - Результат: маппится в правильный slug
        """
        assert topics.normalize("Разработка") == "development"
        assert topics.normalize("ИИ") == "ai"
        assert topics.normalize("кибербезопасность") == "security"

    def test_garbage_maps_to_other(self):
        """
        - Тестируется normalize
        - Сценарий: неизвестная/мусорная тема
        - Результат: other
        """
        assert topics.normalize("ИИ и нейросети") == "other"
        assert topics.normalize("случайный текст") == "other"
        assert topics.normalize("") == "other"
        assert topics.normalize(None) == "other"


class TestLookups:

    def test_emoji_and_label_for_every_slug(self):
        """
        - Тестируется emoji_for / label_for
        - Сценарий: проход по всем 12 slug
        - Результат: непустые emoji и label для каждого
        """
        for label, slug, emoji in topics.TOPICS:
            assert topics.emoji_for(slug) == emoji
            assert topics.label_for(slug) == label

    def test_unknown_slug_falls_back_to_other(self):
        """
        - Тестируется emoji_for / label_for
        - Сценарий: неизвестный slug
        - Результат: значения темы other
        """
        assert topics.emoji_for("zzz") == topics.emoji_for("other")
        assert topics.label_for("zzz") == topics.label_for("other")


class TestAllowedBlock:

    def test_contains_all_slugs(self):
        """
        - Тестируется allowed_block
        - Сценарий: рендер списка тем для промпта
        - Результат: содержит все 12 slug
        """
        block = topics.allowed_block()
        for _, slug, _ in topics.TOPICS:
            assert slug in block

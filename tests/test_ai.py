"""Unit-тесты модуля core/ai.py."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from core import topics
from core.ai import AIClient, _parse_json


class TestParseJson:

    def test_valid_json_dict(self):
        """
        - Тестируется _parse_json
        - Сценарий: корректный JSON-объект
        - Результат: возвращается dict с ожидаемыми ключами
        """
        result = _parse_json('{"label": "AI", "percentage": 0.5}')
        assert isinstance(result, dict)
        assert result["label"] == "AI"
        assert result["percentage"] == pytest.approx(0.5)

    def test_json_wrapped_in_markdown_fence(self):
        """
        - Тестируется _parse_json
        - Сценарий: JSON обёрнут в ```json ... ```
        - Результат: блок снимается, JSON парсится корректно
        """
        text = '```json\n{"label": "ML", "percentage": 0.3}\n```'
        result = _parse_json(text)
        assert result["label"] == "ML"

    def test_valid_json_list(self):
        """
        - Тестируется _parse_json
        - Сценарий: корректный JSON-массив
        - Результат: возвращается list
        """
        text = '[{"id": 1, "topic": "Python"}, {"id": 2, "topic": "AI"}]'
        result = _parse_json(text)
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["topic"] == "Python"

    def test_invalid_json_raises(self):
        """
        - Тестируется _parse_json
        - Сценарий: невалидная строка (не JSON)
        - Результат: выбрасывается json.JSONDecodeError
        """
        with pytest.raises(json.JSONDecodeError):
            _parse_json("not a json at all")

    def test_empty_string_raises(self):
        """
        - Тестируется _parse_json
        - Сценарий: пустая строка
        - Результат: выбрасывается json.JSONDecodeError
        """
        with pytest.raises(json.JSONDecodeError):
            _parse_json("")


class TestClassifyClosedSet:

    def test_only_allowed_slugs_returned(self):
        """
        - Тестируется classify_posts_by_topic
        - Сценарий: модель вернула slug, русский label и мусор
        - Результат: slug проходит, label маппится, мусор → other; emoji отсутствует
        """
        client = AIClient()
        client._call_llm = AsyncMock(
            return_value=(
                '[{"id": 1, "topic": "ai"},'
                ' {"id": 2, "topic": "Разработка"},'
                ' {"id": 3, "topic": "какая-то ерунда"}]'
            )
        )
        result = asyncio.run(
            client.classify_posts_by_topic(
                [{"id": 1, "text": "a"}, {"id": 2, "text": "b"}, {"id": 3, "text": "c"}]
            )
        )
        by_id = {r["id"]: r["topic"] for r in result}
        assert by_id == {1: "ai", 2: "development", 3: "other"}
        assert all(r["topic"] in topics.ALLOWED_SLUGS for r in result)
        assert all("emoji" not in r for r in result)

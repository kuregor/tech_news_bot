"""Unit-тесты модуля core/ai.py."""
import json
import pytest

from core.ai import _parse_json


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

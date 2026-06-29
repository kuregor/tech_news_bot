"""Smoke-тест: модули, которые не покрыты другими тестами, должны импортироваться.

Хендлеры команд и планировщик не дёргаются остальным сьютом, поэтому ошибка
импорта (опечатка, неверный уровень импорта после рефактора) в них невидима.
Этот тест ловит такие поломки на этапе сборки сьюта.
"""

import importlib

import pytest

MODULES = [
    "bot.handlers.analyze",
    "bot.handlers.compare",
    "bot.handlers.digest",
    "bot.handlers.trends",
    "bot.handlers.common",
    "core.scheduler",
    "core.analyzer",
    "core.compare",
    "core.digest",
    "core.trends",
]


@pytest.mark.parametrize("module_name", MODULES)
def test_module_imports(module_name: str) -> None:
    """Каждый модуль импортируется без ошибок."""
    assert importlib.import_module(module_name) is not None

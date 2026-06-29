"""Настройка окружения перед запуском тестов."""

import os
import sys
from unittest.mock import MagicMock

# Фиктивные env-переменные, чтобы pydantic-settings не упал
os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("TG_API_ID", "12345")
os.environ.setdefault("TG_API_HASH", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://user:pass@localhost/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")
os.environ.setdefault("LLM_API_KEY", "test")
os.environ.setdefault("LLM_MODEL", "test")
os.environ.setdefault("EMBED_BASE_URL", "http://localhost:1234/v1")
os.environ.setdefault("EMBED_API_KEY", "test")
os.environ.setdefault("EMBED_MODEL", "test")
os.environ.setdefault("CHROMADB_URL", "http://localhost:8000")

# Мокаем внешние SDK, которых нет в тестовой среде
_mocks = [
    "cerebras",
    "cerebras.cloud",
    "cerebras.cloud.sdk",
    "telethon",
    "telethon.tl",
    "telethon.tl.functions",
    "telethon.tl.functions.channels",
    "telethon.tl.types",
    "chromadb",
    "redis",
    "aioredis",
    "apscheduler",
    "apscheduler.schedulers",
    "apscheduler.schedulers.asyncio",
]
for mod in _mocks:
    sys.modules.setdefault(mod, MagicMock())

from pathlib import Path

from pydantic_settings import BaseSettings

BASE_DIR = Path(__file__).resolve().parent


class Settings(BaseSettings):
    # Telegram Bot
    BOT_TOKEN: str

    # Telethon
    TG_API_ID: int
    TG_API_HASH: str
    TG_PHONE: str

    # PostgreSQL
    DATABASE_URL: str

    # Redis
    REDIS_URL: str

    # LLM (OpenAI-совместимый эндпоинт, напр. OpenRouter)
    LLM_API_KEY: str
    LLM_MODEL: str
    LLM_BASE_URL: str = "https://openrouter.ai/api"

    # Ollama (эмбеддинги)
    EMBED_BASE_URL: str
    EMBED_API_KEY: str
    EMBED_MODEL: str

    # ChromaDB
    CHROMADB_URL: str

    # Константы
    CACHE_TTL_HOURS: int
    DEDUP_THRESHOLD: float
    TOPIC_THRESHOLD: float
    PARSE_LIMIT: int

    class Config:
        env_file = BASE_DIR / ".env"


settings = Settings()

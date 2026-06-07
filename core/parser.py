import logging
from datetime import datetime, timezone

from telethon import TelegramClient
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.types import Channel as TgChannel

from config import settings, BASE_DIR

logger = logging.getLogger(__name__)

# Путь к папке сессий
SESSIONS_DIR = BASE_DIR / "sessions"
SESSIONS_DIR.mkdir(exist_ok=True)


class TelegramParser:
    """Сервис чтения Telegram-каналов через Telethon (user-account).

    Хранит ленивый синглтон клиента Telethon в self._client и предоставляет
    методы получения мета-данных и постов канала.
    """

    def __init__(self) -> None:
        self._client: TelegramClient | None = None

    async def get_client(self) -> TelegramClient:
        """Назначение: лениво создать и подключить клиент Telethon.

        Возвращает:
            TelegramClient: подключённый клиент user-account.
        """
        if self._client is None:
            session_path = str(SESSIONS_DIR / "tech_news_bot_session")
            self._client = TelegramClient(
                session_path,
                settings.TG_API_ID,
                settings.TG_API_HASH,
            )
        if not self._client.is_connected():
            await self._client.start(phone=settings.TG_PHONE)
            logger.info("Telethon клиент подключён")
        return self._client

    async def disconnect_client(self) -> None:
        """Назначение: отключить клиент Telethon, если он подключён."""
        if self._client and self._client.is_connected():
            await self._client.disconnect()
            logger.info("Telethon клиент отключён")

    async def parse_channel_info(self, username: str) -> dict:
        """Назначение: получить мета-данные канала.

        Параметры:
            username (str): username канала (с @ или без).

        Возвращает:
            dict: {username, title, description, subscribers_count}.
        """
        client = await self.get_client()
        entity = await client.get_entity(username)

        full = await client(GetFullChannelRequest(entity))
        full_chat = full.full_chat

        return {
            "username": username.lstrip("@"),
            "title": entity.title,
            "description": full_chat.about or "",
            "subscribers_count": full_chat.participants_count or 0,
        }

    async def parse_channel_posts(self, username: str, limit: int | None = None) -> list[dict]:
        """Назначение: спарсить последние посты канала.

        Параметры:
            username (str): username канала.
            limit (int | None): сколько постов брать (по умолчанию settings.PARSE_LIMIT).

        Возвращает:
            list[dict]: [{tg_id, text, views, reactions, comments, forwards, date}].
        """
        if limit is None:
            limit = settings.PARSE_LIMIT

        client = await self.get_client()
        entity = await client.get_entity(username)

        posts = []
        async for message in client.iter_messages(entity, limit=limit):
            if not message.text:
                continue

            # Суммируем все реакции
            reactions_count = 0
            if message.reactions and message.reactions.results:
                reactions_count = sum(r.count for r in message.reactions.results)

            # Количество комментариев
            comments_count = 0
            if message.replies and message.replies.replies:
                comments_count = message.replies.replies

            posts.append({
                "tg_id": message.id,
                "text": message.text,
                "views": message.views or 0,
                "reactions": reactions_count,
                "comments": comments_count,
                "forwards": message.forwards or 0,
                "date": message.date.replace(tzinfo=timezone.utc) if message.date else datetime.now(timezone.utc),
            })

        logger.info("Спарсено %d постов из @%s", len(posts), username.lstrip("@"))
        return posts


# Синглтон-экземпляр сервиса
telegram_parser = TelegramParser()

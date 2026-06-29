import asyncio
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from aiogram import Bot, Dispatcher, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.redis import RedisStorage
from redis.asyncio import Redis

from bot.handlers.analyze import router as analyze_router
from bot.handlers.common import router as common_router
from bot.handlers.compare import router as compare_router
from bot.handlers.digest import router as digest_router
from bot.handlers.trends import router as trends_router
from config import settings
from core.parser import telegram_parser
from core.scheduler import digest_scheduler
from db.models import Base
from db.session import engine

# Создаём папку logs рядом с main.py
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

# Корневой логгер — и в консоль, и в файл
logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(
            os.path.join(LOG_DIR, "bot.log"),
            maxBytes=10 * 1024 * 1024,  # 10 МБ
            backupCount=5,
            encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger(__name__)


async def _init_db(retries: int = 10, delay: float = 3.0) -> None:
    """Создать таблицы, переживая неготовность Postgres на старте контейнера."""
    for attempt in range(1, retries + 1):
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            return
        except Exception as exc:
            if attempt == retries:
                raise
            logger.warning(
                "Postgres недоступен (попытка %d/%d): %s. Повтор через %.0f с",
                attempt,
                retries,
                exc,
                delay,
            )
            await asyncio.sleep(delay)


async def on_startup(bot: Bot) -> None:
    await _init_db()
    await bot.set_my_commands(
        [
            types.BotCommand(command="analyze", description="Анализ канала"),
            types.BotCommand(command="digest", description="Дайджест лучших постов"),
            types.BotCommand(command="trends", description="Тренды тем"),
            types.BotCommand(command="compare", description="Сравнение каналов"),
            types.BotCommand(command="cancel", description="Отменить текущую операцию"),
        ]
    )
    await digest_scheduler.setup_scheduler(bot)
    logger.info("Бот запущен")


async def on_shutdown(bot: Bot) -> None:
    """Действия при остановке бота."""
    await telegram_parser.disconnect_client()
    await engine.dispose()
    logger.info("Бот остановлен")


async def main() -> None:
    # Redis для FSM storage
    redis = Redis.from_url(settings.REDIS_URL)
    storage = RedisStorage(redis=redis)

    bot = Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=storage)

    # Регистрация роутеров
    dp.include_router(common_router)
    dp.include_router(analyze_router)
    dp.include_router(digest_router)
    dp.include_router(trends_router)
    dp.include_router(compare_router)

    # Хуки жизненного цикла
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    logger.info("Запуск polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception:
        logger.exception("Бот аварийно завершился")
        raise

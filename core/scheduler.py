import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from core.digest import format_digest, run_digest_pipeline
from db.repository import get_list_channels, get_scheduled_lists
from db.session import async_session

logger = logging.getLogger(__name__)

PERIOD_BY_SCHEDULE = {"daily": 1, "weekly": 7}


class DigestScheduler:
    """Сервис планировщика автодайджестов (APScheduler).

    Хранит экземпляр AsyncIOScheduler в self._scheduler и управляет
    задачами рассылки дайджестов по расписанию.
    """

    def __init__(self) -> None:
        self._scheduler = AsyncIOScheduler(timezone="Europe/Moscow")

    async def setup_scheduler(self, bot) -> None:
        """Назначение: загрузить расписания из БД и запустить планировщик.

        Параметры:
            bot: экземпляр бота для отправки сообщений.
        """
        async with async_session() as session:
            lists = await get_scheduled_lists(session)
            jobs_data = [
                {
                    "user_id": cl.user_id,
                    "channel_list_id": cl.id,
                    "schedule_type": cl.schedule_type,
                    "schedule_day": cl.schedule_day,
                    "schedule_hour": cl.schedule_hour,
                    "schedule_minute": cl.schedule_minute,
                    "filter_keywords": cl.filter_keywords,
                }
                for cl in lists
            ]

        for job in jobs_data:
            self.add_digest_job(
                user_id=job["user_id"],
                channel_list_id=job["channel_list_id"],
                schedule_type=job["schedule_type"],
                schedule_day=job["schedule_day"],
                schedule_hour=job["schedule_hour"],
                schedule_minute=job["schedule_minute"],
                bot=bot,
                filter_keywords=job["filter_keywords"],
            )

        self._scheduler.start()
        logger.info("APScheduler запущен, загружено %d расписаний", len(jobs_data))

    def add_digest_job(
        self,
        user_id: int,
        channel_list_id: int,
        schedule_type: str,
        schedule_day: int | None,
        schedule_hour: int,
        schedule_minute: int | None = None,
        bot=None,
        filter_keywords: list | None = None,
    ) -> None:
        """Назначение: добавить или обновить задачу автодайджеста.

        Параметры:
            user_id (int): идентификатор пользователя-получателя.
            channel_list_id (int): идентификатор списка каналов.
            schedule_type (str): "daily" или "weekly".
            schedule_day (int | None): день недели для weekly.
            schedule_hour (int): час запуска.
            schedule_minute (int | None): минута запуска.
            bot: экземпляр бота для отправки.
            filter_keywords (list | None): ключевые слова фильтра.
        """
        trigger_kwargs: dict = {"hour": schedule_hour, "minute": schedule_minute or 0}
        if schedule_type == "weekly" and schedule_day is not None:
            trigger_kwargs["day_of_week"] = schedule_day

        period_days = PERIOD_BY_SCHEDULE.get(schedule_type, 7)

        self._scheduler.add_job(
            self._send_scheduled_digest,
            trigger="cron",
            id=f"digest_{channel_list_id}",
            replace_existing=True,
            misfire_grace_time=300,
            kwargs={
                "bot": bot,
                "channel_list_id": channel_list_id,
                "user_id": user_id,
                "period_days": period_days,
                "filter_keywords": filter_keywords,
            },
            **trigger_kwargs,
        )
        logger.info(
            "Расписание добавлено: list_id=%d user_id=%d type=%s time=%d:%02d",
            channel_list_id,
            user_id,
            schedule_type,
            schedule_hour,
            schedule_minute or 0,
        )

    def remove_digest_job(self, channel_list_id: int) -> None:
        """Назначение: удалить задачу расписания.

        Параметры:
            channel_list_id (int): идентификатор списка каналов.
        """
        job_id = f"digest_{channel_list_id}"
        if self._scheduler.get_job(job_id):
            self._scheduler.remove_job(job_id)
            logger.info("Расписание удалено: list_id=%d", channel_list_id)

    async def _send_scheduled_digest(
        self,
        bot,
        channel_list_id: int,
        user_id: int,
        period_days: int,
        filter_keywords: list | None,
    ) -> None:
        """Сформировать и отправить дайджест по расписанию."""
        if bot is None:
            logger.error("Авторасписание list_id=%d: bot=None", channel_list_id)
            return

        try:
            async with async_session() as session:
                channels = await get_list_channels(session, channel_list_id)

            if not channels:
                logger.warning(
                    "Авторасписание list_id=%d: нет каналов", channel_list_id
                )
                return

            channel_ids = [c.id for c in channels]
            channel_names = [c.username for c in channels]

            async with async_session() as session:
                result = await run_digest_pipeline(
                    session=session,
                    channel_ids=channel_ids,
                    channel_names=channel_names,
                    period_days=period_days,
                    keywords=filter_keywords,
                    user_id=user_id,
                    channel_list_id=channel_list_id,
                )

            formatted = format_digest(result)

            MAX_LEN = 4096
            if len(formatted) <= MAX_LEN:
                await bot.send_message(user_id, formatted)
            else:
                parts = []
                current = ""
                for line in formatted.split("\n"):
                    if len(current) + len(line) + 1 > MAX_LEN:
                        parts.append(current)
                        current = line
                    else:
                        current = current + "\n" + line if current else line
                if current:
                    parts.append(current)
                for part in parts:
                    await bot.send_message(user_id, part)

            logger.info(
                "Авторасписание отправлен: list_id=%d user_id=%d",
                channel_list_id,
                user_id,
            )

        except Exception:
            logger.exception("Ошибка авторасписания list_id=%d", channel_list_id)


# Синглтон-экземпляр сервиса
digest_scheduler = DigestScheduler()

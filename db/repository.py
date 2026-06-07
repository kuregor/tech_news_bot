import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from db.models import (
    Analysis, Channel, ChannelList, ChannelListItem,
    ChannelTopic, Digest, DigestStatus, Post,
)

logger = logging.getLogger(__name__)


# ─── Channels ───────────────────────────────────────────

async def upsert_channel(
    session: AsyncSession,
    username: str,
    title: str | None = None,
    description: str | None = None,
    subscribers_count: int = 0,
) -> Channel:
    """Создать или обновить канал по username."""
    stmt = pg_insert(Channel).values(
        username=username,
        title=title,
        description=description,
        subscribers_count=subscribers_count,
        parsed_at=datetime.now(timezone.utc),
    ).on_conflict_do_update(
        index_elements=["username"],
        set_={
            "title": title,
            "description": description,
            "subscribers_count": subscribers_count,
            "parsed_at": datetime.now(timezone.utc),
        },
    ).returning(Channel)
    result = await session.execute(stmt)
    await session.commit()
    return result.scalar_one()


async def get_channel_by_username(session: AsyncSession, username: str) -> Channel | None:
    """Найти канал по username."""
    result = await session.execute(
        select(Channel).where(Channel.username == username)
    )
    return result.scalar_one_or_none()


async def update_channel_embedding_timestamp(session: AsyncSession, channel_id: int) -> None:
    """Обновить время последнего построения вектора канала."""
    await session.execute(
        update(Channel)
        .where(Channel.id == channel_id)
        .values(embedding_updated_at=datetime.now(timezone.utc))
    )
    await session.commit()


# ─── Posts ───────────────────────────────────────────────

async def batch_upsert_posts(
    session: AsyncSession,
    channel_id: int,
    posts_data: list[dict],
) -> list[Post]:
    """Batch insert постов, пропуская дубликаты по (channel_id, tg_id)."""
    if not posts_data:
        return []

    stmt = pg_insert(Post).values([
        {
            "channel_id": channel_id,
            "tg_id": p["tg_id"],
            "text": p["text"],
            "views": p.get("views", 0),
            "reactions": p.get("reactions", 0),
            "comments": p.get("comments", 0),
            "forwards": p.get("forwards", 0),
            "date": p["date"],
        }
        for p in posts_data
    ]).on_conflict_do_nothing(
        index_elements=["channel_id", "tg_id"]
    ).returning(Post)
    result = await session.execute(stmt)
    await session.commit()
    return list(result.scalars().all())


async def get_posts_by_channel(
    session: AsyncSession,
    channel_id: int,
    limit: int | None = None,
    order_by_views: bool = False,
    period_from: datetime | None = None,
) -> list[Post]:
    """Получить посты канала с фильтрами."""
    stmt = select(Post).where(Post.channel_id == channel_id)
    if period_from:
        stmt = stmt.where(Post.date >= period_from)
    if order_by_views:
        stmt = stmt.order_by(Post.views.desc())
    else:
        stmt = stmt.order_by(Post.date.desc())
    if limit:
        stmt = stmt.limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_posts_by_channels(
    session: AsyncSession,
    channel_ids: list[int],
    period_from: datetime,
) -> list[Post]:
    """Получить посты нескольких каналов за период."""
    stmt = (
        select(Post)
        .where(Post.channel_id.in_(channel_ids), Post.date >= period_from)
        .order_by(Post.date.desc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


# ─── Channel Topics ─────────────────────────────────────

async def batch_insert_topics(
    session: AsyncSession,
    topics_data: list[dict],
) -> None:
    """Batch insert тем для постов."""
    if not topics_data:
        return
    session.add_all([
        ChannelTopic(
            post_id=t["post_id"],
            label=t["label"],
            percentage=t["percentage"],
        )
        for t in topics_data
    ])
    await session.commit()


async def delete_channel_topics(session: AsyncSession, channel_id: int) -> None:
    """Удалить все темы постов канала перед перезаписью."""
    await session.execute(
        delete(ChannelTopic).where(
            ChannelTopic.post_id.in_(
                select(Post.id).where(Post.channel_id == channel_id)
            )
        )
    )
    await session.commit()


async def get_topics_by_channel(
    session: AsyncSession,
    channel_id: int,
    min_percentage: float | None = None,
) -> list[ChannelTopic]:
    """Получить темы канала через JOIN с posts."""
    stmt = (
        select(ChannelTopic)
        .join(Post, ChannelTopic.post_id == Post.id)
        .where(Post.channel_id == channel_id)
    )
    if min_percentage is not None:
        stmt = stmt.where(ChannelTopic.percentage >= min_percentage)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_topic_counts_by_channels(
    session: AsyncSession,
    channel_ids: list[int],
    period_from: datetime,
    period_to: datetime,
    min_percentage: float = 0.3,
) -> list[dict]:
    """Подсчёт упоминаний тем за период (для /trends)."""
    stmt = (
        select(
            ChannelTopic.label,
            Post.date,
            func.count().label("cnt"),
            func.avg(Post.views).label("avg_views"),
        )
        .join(Post, ChannelTopic.post_id == Post.id)
        .where(
            Post.channel_id.in_(channel_ids),
            Post.date >= period_from,
            Post.date <= period_to,
            ChannelTopic.percentage >= min_percentage,
        )
        .group_by(ChannelTopic.label, Post.date)
    )
    result = await session.execute(stmt)
    return [dict(row._mapping) for row in result.all()]


# ─── Analyses ───────────────────────────────────────────

async def get_cached_analysis(
    session: AsyncSession, channel_id: int
) -> Analysis | None:
    """Получить кэшированный анализ, если не старше CACHE_TTL_HOURS."""
    ttl = datetime.now(timezone.utc) - timedelta(hours=settings.CACHE_TTL_HOURS)
    result = await session.execute(
        select(Analysis)
        .where(Analysis.channel_id == channel_id, Analysis.created_at >= ttl)
    )
    return result.scalar_one_or_none()


async def save_analysis(
    session: AsyncSession,
    channel_id: int,
    tagline: str,
    about: str,
    audience: str,
    style: str,
    avg_views: float,
    avg_reactions: float,
    avg_comments: float,
    posts_count: int,
) -> Analysis:
    """Сохранить или обновить анализ канала."""
    stmt = pg_insert(Analysis).values(
        channel_id=channel_id,
        tagline=tagline,
        about=about,
        audience=audience,
        style=style,
        avg_views=avg_views,
        avg_reactions=avg_reactions,
        avg_comments=avg_comments,
        posts_count=posts_count,
        created_at=datetime.now(timezone.utc),
    ).on_conflict_do_update(
        index_elements=["channel_id"],
        set_={
            "tagline": tagline,
            "about": about,
            "audience": audience,
            "style": style,
            "avg_views": avg_views,
            "avg_reactions": avg_reactions,
            "avg_comments": avg_comments,
            "posts_count": posts_count,
            "created_at": datetime.now(timezone.utc),
        },
    ).returning(Analysis)
    result = await session.execute(stmt)
    await session.commit()
    return result.scalar_one()


# ─── Channel Lists ──────────────────────────────────────

async def get_default_list(session: AsyncSession, user_id: int) -> ChannelList | None:
    """Получить дефолтный список каналов пользователя."""
    result = await session.execute(
        select(ChannelList)
        .where(ChannelList.user_id == user_id, ChannelList.is_default == True)
    )
    return result.scalar_one_or_none()


async def create_channel_list(
    session: AsyncSession, user_id: int, is_default: bool = False
) -> ChannelList:
    """Создать новый список каналов."""
    cl = ChannelList(user_id=user_id, is_default=is_default)
    session.add(cl)
    await session.commit()
    await session.refresh(cl)
    return cl


async def add_channel_to_list(
    session: AsyncSession, list_id: int, channel_id: int
) -> None:
    """Добавить канал в список."""
    stmt = pg_insert(ChannelListItem).values(
        list_id=list_id, channel_id=channel_id
    ).on_conflict_do_nothing()
    await session.execute(stmt)
    await session.commit()


async def remove_channel_from_list(session: AsyncSession, list_id: int, channel_id: int) -> None:
    """Удалить канал из списка."""
    await session.execute(
        delete(ChannelListItem).where(
            ChannelListItem.list_id == list_id,
            ChannelListItem.channel_id == channel_id,
        )
    )
    await session.commit()


async def get_list_channels(session: AsyncSession, list_id: int) -> list[Channel]:
    """Получить каналы из списка."""
    stmt = (
        select(Channel)
        .join(ChannelListItem, ChannelListItem.channel_id == Channel.id)
        .where(ChannelListItem.list_id == list_id)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_user_lists(session: AsyncSession, user_id: int) -> list[ChannelList]:
    """Получить все списки каналов пользователя."""
    result = await session.execute(
        select(ChannelList).where(ChannelList.user_id == user_id)
    )
    return list(result.scalars().all())


async def update_channel_list_schedule(
    session: AsyncSession,
    list_id: int,
    schedule_type: str | None,
    schedule_day: int | None,
    schedule_hour: int | None,
    schedule_minute: int | None = None,
    filter_keywords: list[str] | None = None,
) -> None:
    """Сохранить настройки расписания и фильтра для списка каналов."""
    await session.execute(
        update(ChannelList)
        .where(ChannelList.id == list_id)
        .values(
            schedule_type=schedule_type,
            schedule_day=schedule_day,
            schedule_hour=schedule_hour,
            schedule_minute=schedule_minute,
            filter_keywords=filter_keywords,
        )
    )
    await session.commit()


async def get_scheduled_lists(session: AsyncSession) -> list[ChannelList]:
    """Получить все списки с активным расписанием."""
    result = await session.execute(
        select(ChannelList).where(ChannelList.schedule_type.isnot(None))
    )
    return list(result.scalars().all())


# ─── Digests ────────────────────────────────────────────

async def save_digest(
    session: AsyncSession,
    user_id: int,
    channel_list_id: int | None,
    period_days: int,
    period_from: datetime,
    period_to: datetime,
    filter_keywords: list[str] | None,
    posts_data: list[dict],
    status: DigestStatus = DigestStatus.pending,
) -> Digest:
    """Сохранить дайджест."""
    digest = Digest(
        user_id=user_id,
        channel_list_id=channel_list_id,
        period_days=period_days,
        period_from=period_from,
        period_to=period_to,
        filter_keywords=filter_keywords,
        posts=posts_data,
        status=status,
    )
    session.add(digest)
    await session.commit()
    await session.refresh(digest)
    return digest


async def update_digest_status(
    session: AsyncSession, digest_id: int, status: DigestStatus
) -> None:
    """Обновить статус дайджеста."""
    values = {"status": status}
    if status == DigestStatus.sent:
        values["sent_at"] = datetime.now(timezone.utc)
    await session.execute(
        update(Digest).where(Digest.id == digest_id).values(**values)
    )
    await session.commit()


async def get_user_digests(
    session: AsyncSession, user_id: int, limit: int = 10
) -> list[Digest]:
    """Получить последние дайджесты пользователя."""
    result = await session.execute(
        select(Digest)
        .where(Digest.user_id == user_id)
        .order_by(Digest.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())

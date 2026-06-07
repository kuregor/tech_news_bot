import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, Enum, Float,
    ForeignKey, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, relationship
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


class DigestStatus(str, enum.Enum):
    pending = "pending"
    sent = "sent"
    failed = "failed"


class Channel(Base):
    __tablename__ = "channels"

    id = Column(Integer, primary_key=True)
    username = Column(String(255), unique=True, nullable=False)
    title = Column(String(512))
    description = Column(Text)
    subscribers_count = Column(Integer, default=0)
    parsed_at = Column(DateTime(timezone=True))
    embedding_updated_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    posts = relationship("Post", back_populates="channel", cascade="all, delete-orphan")
    analyses = relationship("Analysis", back_populates="channel", cascade="all, delete-orphan")
    list_items = relationship("ChannelListItem", back_populates="channel", cascade="all, delete-orphan")


class Post(Base):
    __tablename__ = "posts"
    __table_args__ = (UniqueConstraint("channel_id", "tg_id"),)

    id = Column(Integer, primary_key=True)
    channel_id = Column(Integer, ForeignKey("channels.id", ondelete="CASCADE"), nullable=False)
    tg_id = Column(Integer, nullable=False)
    text = Column(Text)
    views = Column(Integer, default=0)
    reactions = Column(Integer, default=0)
    comments = Column(Integer, default=0)
    forwards = Column(Integer, default=0)
    date = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    channel = relationship("Channel", back_populates="posts")
    topics = relationship("ChannelTopic", back_populates="post", cascade="all, delete-orphan")


class ChannelTopic(Base):
    __tablename__ = "channel_topics"

    id = Column(Integer, primary_key=True)
    post_id = Column(Integer, ForeignKey("posts.id", ondelete="CASCADE"), nullable=False)
    label = Column(String(255), nullable=False)
    percentage = Column(Float, nullable=False)

    post = relationship("Post", back_populates="topics")


class Analysis(Base):
    __tablename__ = "analyses"
    __table_args__ = (UniqueConstraint("channel_id"),)

    id = Column(Integer, primary_key=True)
    channel_id = Column(Integer, ForeignKey("channels.id", ondelete="CASCADE"), nullable=False)
    tagline = Column(String(512))
    about = Column(Text)
    audience = Column(Text)
    style = Column(Text)
    avg_views = Column(Float, default=0)
    avg_reactions = Column(Float, default=0)
    avg_comments = Column(Float, default=0)
    posts_count = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    channel = relationship("Channel", back_populates="analyses")


class ChannelList(Base):
    __tablename__ = "channel_lists"

    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, nullable=False)
    is_default = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Расписание автодайджеста
    schedule_type   = Column(String(16), nullable=True)  # "daily" / "weekly" / None
    schedule_day    = Column(Integer, nullable=True)     # 0-6 (пн-вс), только для weekly
    schedule_hour   = Column(Integer, nullable=True)     # 0-23
    schedule_minute = Column(Integer, nullable=True)     # 0-59
    filter_keywords = Column(JSONB, nullable=True)       # запомненный фильтр

    items = relationship("ChannelListItem", back_populates="channel_list", cascade="all, delete-orphan")
    digests = relationship("Digest", back_populates="channel_list")


class ChannelListItem(Base):
    __tablename__ = "channel_list_items"
    __table_args__ = (UniqueConstraint("list_id", "channel_id"),)

    id = Column(Integer, primary_key=True)
    list_id = Column(Integer, ForeignKey("channel_lists.id", ondelete="CASCADE"), nullable=False)
    channel_id = Column(Integer, ForeignKey("channels.id", ondelete="CASCADE"), nullable=False)

    channel_list = relationship("ChannelList", back_populates="items")
    channel = relationship("Channel", back_populates="list_items")


class Digest(Base):
    __tablename__ = "digests"

    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, nullable=False)
    channel_list_id = Column(Integer, ForeignKey("channel_lists.id", ondelete="SET NULL"), nullable=True)
    period_days = Column(Integer, nullable=False)
    period_from = Column(DateTime(timezone=True), nullable=False)
    period_to = Column(DateTime(timezone=True), nullable=False)
    filter_keywords = Column(JSONB, nullable=True)
    posts = Column(JSONB, nullable=True)
    status = Column(Enum(DigestStatus, name="digest_status", create_type=False), default=DigestStatus.pending)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    sent_at = Column(DateTime(timezone=True), nullable=True)

    channel_list = relationship("ChannelList", back_populates="digests")

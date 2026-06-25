from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

from nonebot import get_plugin_config
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent

from src.storage import get_db

from .config import Config
from .image_store import extract_file_hash, record_image_meta, store_image

SessionType = Literal["group", "private"]

db = get_db()
config = get_plugin_config(Config)


@dataclass(slots=True)
class ArchivedMessage:
    id: int
    session_type: SessionType
    session_id: str
    message_id: int
    event_time: int
    self_id: str
    user_id: str
    group_id: str | None
    sender_name: str
    message_cq: str
    plain_text: str


def ensure_schema() -> None:
    """确保消息归档表结构存在。"""
    db.ensure_schema(
        [
            """
            CREATE TABLE IF NOT EXISTS message_archive (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_type TEXT NOT NULL,
                session_id TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                event_time INTEGER NOT NULL,
                self_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                group_id TEXT,
                sender_name TEXT NOT NULL,
                message_cq TEXT NOT NULL,
                plain_text TEXT NOT NULL,
                UNIQUE (session_type, session_id, message_id)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_message_archive_session_time
            ON message_archive (session_type, session_id, event_time, id)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_message_archive_session_user_time
            ON message_archive (session_type, session_id, user_id, event_time, id)
            """,
            """
            CREATE TABLE IF NOT EXISTS message_archive_image (
                file_hash   TEXT PRIMARY KEY,
                file_path   TEXT NOT NULL,
                archived_at INTEGER NOT NULL,
                expire_at   INTEGER NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_message_archive_image_expire
            ON message_archive_image (expire_at)
            """,
        ]
    )


def get_session_context(event: MessageEvent) -> tuple[SessionType, str]:
    """获取消息所属会话。"""
    if isinstance(event, GroupMessageEvent):
        return "group", str(event.group_id)
    return "private", str(event.user_id)


def resolve_sender_name(event: MessageEvent) -> str:
    """解析消息展示名。"""
    sender = event.sender
    card = sender.card if sender.card is not None else ""
    nickname = sender.nickname if sender.nickname is not None else ""
    return card or nickname or str(event.user_id)


async def archive_message_event(event: MessageEvent) -> None:
    """归档收到的消息事件。"""
    session_type, session_id = get_session_context(event)
    group_id = str(event.group_id) if isinstance(event, GroupMessageEvent) else None

    await db.execute(
        """
        INSERT OR IGNORE INTO message_archive (
            session_type,
            session_id,
            message_id,
            event_time,
            self_id,
            user_id,
            group_id,
            sender_name,
            message_cq,
            plain_text
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_type,
            session_id,
            event.message_id,
            event.time,
            str(event.self_id),
            str(event.user_id),
            group_id,
            resolve_sender_name(event),
            str(event.original_message),
            event.original_message.extract_plain_text(),
        ),
    )

    await _persist_images(event)


async def _persist_images(event: MessageEvent) -> None:
    """归档消息中的图片到本地。失败只 warning，不影响文本归档。"""
    if not config.message_archive_image_enabled:
        return

    image_segments = [seg for seg in event.original_message if seg.type == "image"]
    if not image_segments:
        return

    max_count = config.message_archive_image_max_count
    max_size_bytes = config.message_archive_image_max_size_mb * 1024 * 1024
    expire_at = int(time.time()) + config.message_archive_image_retention_days * 86400

    for seg in image_segments[:max_count]:
        url = seg.data.get("url")
        file_field = str(seg.data.get("file", ""))
        if not url or not file_field:
            continue
        file_hash = extract_file_hash(file_field)
        if not file_hash:
            continue
        path = await store_image(str(url), file_hash, max_size_bytes)
        if path is None:
            continue
        await record_image_meta(file_hash, str(path), expire_at)


async def fetch_session_messages(
    session_type: SessionType,
    session_id: str,
    limit: int,
    exclude_message_id: int | None = None,
    target_user_id: str | None = None,
) -> list[ArchivedMessage]:
    """按会话获取最近归档消息，结果按时间升序返回。"""
    if limit < 1:
        return []

    rows = await db.fetch_all(
        """
        SELECT
            id,
            session_type,
            session_id,
            message_id,
            event_time,
            self_id,
            user_id,
            group_id,
            sender_name,
            message_cq,
            plain_text
        FROM (
            SELECT
                id,
                session_type,
                session_id,
                message_id,
                event_time,
                self_id,
                user_id,
                group_id,
                sender_name,
                message_cq,
                plain_text
            FROM message_archive
            WHERE session_type = ?
              AND session_id = ?
              AND (? IS NULL OR message_id <> ?)
              AND (? IS NULL OR user_id = ?)
            ORDER BY event_time DESC, id DESC
            LIMIT ?
        )
        ORDER BY event_time ASC, id ASC
        """,
        (
            session_type,
            session_id,
            exclude_message_id,
            exclude_message_id,
            target_user_id,
            target_user_id,
            limit,
        ),
    )

    return [
        ArchivedMessage(
            id=row["id"],
            session_type=row["session_type"],
            session_id=row["session_id"],
            message_id=row["message_id"],
            event_time=row["event_time"],
            self_id=row["self_id"],
            user_id=row["user_id"],
            group_id=row["group_id"],
            sender_name=row["sender_name"],
            message_cq=row["message_cq"],
            plain_text=row["plain_text"],
        )
        for row in rows
    ]


async def fetch_group_messages_by_time_range(
    group_id: str,
    start_time: int,
    end_time: int,
    exclude_message_id: int | None = None,
) -> list[ArchivedMessage]:
    """按时间范围获取群聊归档消息，结果按时间升序返回。"""
    if end_time <= start_time:
        return []

    rows = await db.fetch_all(
        """
        SELECT
            id,
            session_type,
            session_id,
            message_id,
            event_time,
            self_id,
            user_id,
            group_id,
            sender_name,
            message_cq,
            plain_text
        FROM message_archive
        WHERE session_type = 'group'
          AND session_id = ?
          AND event_time >= ?
          AND event_time < ?
          AND (? IS NULL OR message_id <> ?)
        ORDER BY event_time ASC, id ASC
        """,
        (
            group_id,
            start_time,
            end_time,
            exclude_message_id,
            exclude_message_id,
        ),
    )

    return [
        ArchivedMessage(
            id=row["id"],
            session_type=row["session_type"],
            session_id=row["session_id"],
            message_id=row["message_id"],
            event_time=row["event_time"],
            self_id=row["self_id"],
            user_id=row["user_id"],
            group_id=row["group_id"],
            sender_name=row["sender_name"],
            message_cq=row["message_cq"],
            plain_text=row["plain_text"],
        )
        for row in rows
    ]

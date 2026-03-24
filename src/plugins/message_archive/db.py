from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent

from src.storage import get_db

SessionType = Literal["group", "private"]

db = get_db()


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


async def fetch_session_messages(
    session_type: SessionType,
    session_id: str,
    limit: int,
    exclude_message_id: int | None = None,
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
            ORDER BY event_time DESC, id DESC
            LIMIT ?
        )
        ORDER BY event_time ASC, id ASC
        """,
        (session_type, session_id, exclude_message_id, exclude_message_id, limit),
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

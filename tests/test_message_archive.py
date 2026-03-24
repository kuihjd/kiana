from datetime import datetime
from unittest.mock import patch

import pytest
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment, PrivateMessageEvent
from nonebot.adapters.onebot.v11.event import Sender


def create_group_message_event(
    message: Message | str,
    *,
    message_id: int = 1,
    user_id: int = 123456,
    group_id: int = 654321,
    self_id: int = 987654321,
    nickname: str = "测试用户",
    card: str = "",
) -> GroupMessageEvent:
    actual_message = message if isinstance(message, Message) else Message(message)
    raw_message = str(actual_message)
    return GroupMessageEvent(
        time=int(datetime.now().timestamp()),
        self_id=self_id,
        post_type="message",
        sub_type="normal",
        user_id=user_id,
        message_type="group",
        group_id=group_id,
        message_id=message_id,
        message=actual_message,
        original_message=actual_message.copy(),
        raw_message=raw_message,
        font=0,
        sender=Sender(user_id=user_id, nickname=nickname, card=card, role="member"),
    )


def create_private_message_event(
    message: Message | str,
    *,
    message_id: int = 1,
    user_id: int = 123456,
    self_id: int = 987654321,
    nickname: str = "测试用户",
) -> PrivateMessageEvent:
    actual_message = message if isinstance(message, Message) else Message(message)
    raw_message = str(actual_message)
    return PrivateMessageEvent(
        time=int(datetime.now().timestamp()),
        self_id=self_id,
        post_type="message",
        sub_type="friend",
        user_id=user_id,
        message_type="private",
        message_id=message_id,
        message=actual_message,
        original_message=actual_message.copy(),
        raw_message=raw_message,
        font=0,
        sender=Sender(user_id=user_id, nickname=nickname, sex="unknown", age=0),
    )


@pytest.mark.asyncio
async def test_archive_group_and_private_messages() -> None:
    """群聊和私聊消息都应被正确归档。"""
    from src.plugins.message_archive.db import archive_message_event, fetch_session_messages

    group_event = create_group_message_event(
        "群消息",
        message_id=101,
        user_id=20001,
        group_id=30001,
        nickname="群昵称",
        card="群名片",
    )
    private_event = create_private_message_event("私聊消息", message_id=202, user_id=40001)

    await archive_message_event(group_event)
    await archive_message_event(private_event)

    group_messages = await fetch_session_messages("group", "30001", 10)
    private_messages = await fetch_session_messages("private", "40001", 10)

    assert len(group_messages) == 1
    assert group_messages[0].sender_name == "群名片"
    assert group_messages[0].group_id == "30001"
    assert group_messages[0].plain_text == "群消息"

    assert len(private_messages) == 1
    assert private_messages[0].sender_name == "测试用户"
    assert private_messages[0].group_id is None
    assert private_messages[0].plain_text == "私聊消息"


@pytest.mark.asyncio
async def test_archive_deduplicates_same_message() -> None:
    """同一消息事件重复归档时只应保留一条记录。"""
    from src.plugins.message_archive.db import archive_message_event, fetch_session_messages

    event = create_group_message_event("重复消息", message_id=88, group_id=10001)

    await archive_message_event(event)
    await archive_message_event(event)

    messages = await fetch_session_messages("group", "10001", 10)
    assert len(messages) == 1
    assert messages[0].message_id == 88


@pytest.mark.asyncio
async def test_archive_preserves_cq_message_for_forward_replay() -> None:
    """包含 CQ 段的消息应能原样读取并用于合并转发。"""
    from src.plugins.chat_forward import build_forward_nodes
    from src.plugins.message_archive.db import archive_message_event, fetch_session_messages

    message = Message([MessageSegment.text("hello "), MessageSegment.at("123456")])
    event = create_group_message_event(message, message_id=66, group_id=20002)

    await archive_message_event(event)

    messages = await fetch_session_messages("group", "20002", 10)
    forward_nodes = build_forward_nodes(messages)

    assert len(messages) == 1
    assert messages[0].message_cq == "hello [CQ:at,qq=123456]"
    assert forward_nodes == [
        {
            "type": "node",
            "data": {
                "name": "测试用户",
                "uin": "123456",
                "content": Message("hello [CQ:at,qq=123456]"),
            },
        }
    ]


@pytest.mark.asyncio
async def test_archive_rebuilds_face_segment_for_forward_replay() -> None:
    """QQ 表情应从归档内容重建，不依赖原消息 ID。"""
    from src.plugins.chat_forward import build_forward_content, build_forward_nodes
    from src.plugins.message_archive.db import archive_message_event, fetch_session_messages

    message = Message([MessageSegment.face(123)])
    event = create_group_message_event(message, message_id=67, group_id=20003)

    await archive_message_event(event)

    messages = await fetch_session_messages("group", "20003", 10)
    forward_nodes = build_forward_nodes(messages)

    assert forward_nodes == [
        {
            "type": "node",
            "data": {
                "name": "测试用户",
                "uin": "123456",
                "content": Message([MessageSegment.face(123)]),
            },
        }
    ]
    assert build_forward_content(messages[0]) == Message([MessageSegment.face(123)])


def test_build_forward_content_strips_face_raw_and_replaces_image_with_placeholder() -> None:
    """回放内容应移除危险字段，并将历史图片降级为占位文本。"""
    from src.plugins.chat_forward import build_forward_content
    from src.plugins.message_archive.db import ArchivedMessage

    face_message = ArchivedMessage(
        id=1,
        session_type="group",
        session_id="1",
        message_id=1,
        event_time=1,
        self_id="1",
        user_id="123456",
        group_id="1",
        sender_name="测试用户",
        message_cq="[CQ:face,id=344,raw={'faceIndex':344}]外面太疯狂了",
        plain_text="外面太疯狂了",
    )
    image_message = ArchivedMessage(
        id=2,
        session_type="group",
        session_id="1",
        message_id=2,
        event_time=2,
        self_id="1",
        user_id="123456",
        group_id="1",
        sender_name="测试用户",
        message_cq=(
            "[CQ:image,summary=,file=87BB4460CDC33BCF4F6441D1AFF5BFC8.png,"
            "sub_type=0,url=https://example.com/test.png,file_size=239885]"
        ),
        plain_text="",
    )

    assert build_forward_content(face_message) == Message(
        [MessageSegment.face(344), MessageSegment.text("外面太疯狂了")]
    )
    assert build_forward_content(image_message) == Message([MessageSegment.text("[图片]")])


def test_build_forward_content_skips_video_and_replaces_image_with_placeholder() -> None:
    """视频应被跳过，图片应降级为占位文本。"""
    from src.plugins.chat_forward import build_forward_content
    from src.plugins.message_archive.db import ArchivedMessage

    mixed_message = ArchivedMessage(
        id=3,
        session_type="group",
        session_id="1",
        message_id=3,
        event_time=3,
        self_id="1",
        user_id="123456",
        group_id="1",
        sender_name="测试用户",
        message_cq=(
            "[CQ:video,file=test.mp4,url=https://example.com/test.mp4]"
            "[CQ:image,file=test.png,url=https://example.com/test.png]"
        ),
        plain_text="",
    )

    assert build_forward_content(mixed_message) == Message([MessageSegment.text("[图片]")])


@pytest.mark.asyncio
async def test_archive_database_failure_is_swallowed() -> None:
    """归档异常只记录日志，不应中断事件处理。"""
    from src.plugins.message_archive import archive_received_message

    event = create_group_message_event("归档失败", group_id=20003)

    with patch("src.plugins.message_archive.archive_message_event", side_effect=RuntimeError("db down")):
        await archive_received_message(event)

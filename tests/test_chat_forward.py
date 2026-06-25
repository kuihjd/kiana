from datetime import datetime

import pytest
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    Message,
    MessageSegment,
    PrivateMessageEvent,
)
from nonebot.adapters.onebot.v11.event import Sender
from nonebug import App


def create_group_event(
    message: Message | str,
    *,
    message_id: int = 1,
    user_id: int = 123456,
    group_id: int = 654321,
    self_id: int = 987654321,
    nickname: str = "测试用户",
) -> GroupMessageEvent:
    actual_message = message if isinstance(message, Message) else Message(message)
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
        raw_message=str(actual_message),
        font=0,
        sender=Sender(user_id=user_id, nickname=nickname, card="", role="member"),
    )


def create_private_event(
    message: str,
    *,
    message_id: int = 1,
    user_id: int = 123456,
    self_id: int = 987654321,
    nickname: str = "测试用户",
) -> PrivateMessageEvent:
    return PrivateMessageEvent(
        time=int(datetime.now().timestamp()),
        self_id=self_id,
        post_type="message",
        sub_type="friend",
        user_id=user_id,
        message_type="private",
        message_id=message_id,
        message=Message(message),
        original_message=Message(message),
        raw_message=message,
        font=0,
        sender=Sender(user_id=user_id, nickname=nickname, sex="unknown", age=0),
    )


def expect_bot_not_muted(ctx, group_id: int, self_id: int = 987654321) -> None:
    """声明预期的禁言检查 API 调用。"""
    ctx.should_call_api(
        "get_group_member_info",
        {"group_id": group_id, "user_id": self_id, "no_cache": True},
        result={"shut_up_timestamp": 0},
    )


def test_chat_forward_failure_messages_are_sanitized() -> None:
    """内部异常提示不应直接暴露给用户。"""
    from src.plugins.chat_forward import _get_send_failure_message, _get_storage_failure_message

    assert _get_storage_failure_message() == "获取消息记录失败，请稍后重试"
    assert _get_send_failure_message() == "发送合并转发失败，请稍后重试"


@pytest.mark.asyncio
async def test_chat_forward_group_replays_archived_messages(app: App) -> None:
    """群聊打包消息应从数据库读取并发送合并转发。"""
    from src.plugins.chat_forward import chat_forward
    from src.plugins.message_archive.db import archive_message_event

    await archive_message_event(create_group_event("第一条", message_id=1, user_id=10001, nickname="用户A"))
    await archive_message_event(create_group_event("第二条", message_id=2, user_id=10002, nickname="用户B"))
    await archive_message_event(create_group_event("第三条", message_id=3, user_id=10003, nickname="用户C"))

    async with app.test_matcher(chat_forward) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        event = create_group_event("打包消息 3", message_id=999, user_id=123456)

        expect_bot_not_muted(ctx, group_id=654321)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_api(
            "send_group_forward_msg",
            {
                "group_id": 654321,
                "messages": [
                    {
                        "type": "node",
                        "data": {
                            "name": "用户A",
                            "uin": "10001",
                            "content": Message("第一条"),
                        },
                    },
                    {
                        "type": "node",
                        "data": {
                            "name": "用户B",
                            "uin": "10002",
                            "content": Message("第二条"),
                        },
                    },
                    {
                        "type": "node",
                        "data": {
                            "name": "用户C",
                            "uin": "10003",
                            "content": Message("第三条"),
                        },
                    },
                ],
            },
            result={"message_id": 1000},
        )


@pytest.mark.asyncio
async def test_chat_forward_private_replays_archived_messages(app: App) -> None:
    """私聊打包消息应走私聊合并转发 API。"""
    from src.plugins.chat_forward import chat_forward
    from src.plugins.message_archive.db import archive_message_event

    await archive_message_event(create_private_event("私聊A", message_id=11, user_id=123456, nickname="用户A"))
    await archive_message_event(create_private_event("私聊B", message_id=12, user_id=123456, nickname="用户A"))
    await archive_message_event(create_private_event("私聊C", message_id=13, user_id=123456, nickname="用户A"))

    async with app.test_matcher(chat_forward) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        event = create_private_event("打包消息 3", message_id=999, user_id=123456, nickname="用户A")

        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_api(
            "send_private_forward_msg",
            {
                "user_id": 123456,
                "messages": [
                    {
                        "type": "node",
                        "data": {
                            "name": "用户A",
                            "uin": "123456",
                            "content": Message("私聊A"),
                        },
                    },
                    {
                        "type": "node",
                        "data": {
                            "name": "用户A",
                            "uin": "123456",
                            "content": Message("私聊B"),
                        },
                    },
                    {
                        "type": "node",
                        "data": {
                            "name": "用户A",
                            "uin": "123456",
                            "content": Message("私聊C"),
                        },
                    },
                ],
            },
            result={"message_id": 1001},
        )


@pytest.mark.asyncio
async def test_chat_forward_excludes_trigger_message() -> None:
    """查询最近消息时应排除当前触发命令本身。"""
    from src.plugins.message_archive.db import archive_message_event, fetch_session_messages

    await archive_message_event(create_group_event("历史消息", message_id=21, group_id=90001))
    await archive_message_event(create_group_event("打包消息 2", message_id=22, group_id=90001))

    messages = await fetch_session_messages("group", "90001", 2, exclude_message_id=22)

    assert [message.message_cq for message in messages] == ["历史消息"]


@pytest.mark.asyncio
async def test_chat_forward_group_replays_target_user_from_middle_mention(app: App) -> None:
    """群聊中在命令中间 @ 某人时，应只打包该成员发言。"""
    from src.plugins.chat_forward import chat_forward
    from src.plugins.message_archive.db import archive_message_event

    await archive_message_event(create_group_event("A-1", message_id=31, user_id=10001, nickname="用户A"))
    await archive_message_event(create_group_event("B-1", message_id=32, user_id=10002, nickname="用户B"))
    await archive_message_event(create_group_event("A-2", message_id=33, user_id=10001, nickname="用户A"))
    await archive_message_event(create_group_event("B-2", message_id=34, user_id=10002, nickname="用户B"))

    async with app.test_matcher(chat_forward) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        event = create_group_event(
            Message(
                [
                    MessageSegment.text("打包消息 "),
                    MessageSegment.at("10001"),
                    MessageSegment.text(" 2"),
                ]
            ),
            message_id=999,
            user_id=123456,
        )

        expect_bot_not_muted(ctx, group_id=654321)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_api(
            "send_group_forward_msg",
            {
                "group_id": 654321,
                "messages": [
                    {
                        "type": "node",
                        "data": {
                            "name": "用户A",
                            "uin": "10001",
                            "content": Message("A-1"),
                        },
                    },
                    {
                        "type": "node",
                        "data": {
                            "name": "用户A",
                            "uin": "10001",
                            "content": Message("A-2"),
                        },
                    },
                ],
            },
            result={"message_id": 1002},
        )


@pytest.mark.asyncio
async def test_chat_forward_group_replays_target_user_from_leading_mention(app: App) -> None:
    """群聊中在命令开头 @ 某人时，也应只打包该成员发言。"""
    from src.plugins.chat_forward import chat_forward
    from src.plugins.message_archive.db import archive_message_event

    await archive_message_event(create_group_event("A-1", message_id=41, user_id=10001, group_id=77777))
    await archive_message_event(create_group_event("B-1", message_id=42, user_id=10002, group_id=77777))
    await archive_message_event(create_group_event("A-2", message_id=43, user_id=10001, group_id=77777))

    async with app.test_matcher(chat_forward) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        event = create_group_event(
            Message(
                [
                    MessageSegment.at("10001"),
                    MessageSegment.text(" 打包记录 2"),
                ]
            ),
            message_id=999,
            user_id=123456,
            group_id=77777,
        )

        expect_bot_not_muted(ctx, group_id=77777)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_api(
            "send_group_forward_msg",
            {
                "group_id": 77777,
                "messages": [
                    {
                        "type": "node",
                        "data": {
                            "name": "测试用户",
                            "uin": "10001",
                            "content": Message("A-1"),
                        },
                    },
                    {
                        "type": "node",
                        "data": {
                            "name": "测试用户",
                            "uin": "10001",
                            "content": Message("A-2"),
                        },
                    },
                ],
            },
            result={"message_id": 1003},
        )


@pytest.mark.asyncio
async def test_chat_forward_replays_from_archived_content_instead_of_message_id(app: App) -> None:
    """回放应使用归档内容，而不是依赖原始消息 ID。"""
    from src.plugins.chat_forward import chat_forward
    from src.plugins.message_archive.db import archive_message_event

    archived_message = Message([MessageSegment.text("hello "), MessageSegment.face(123)])
    await archive_message_event(
        create_group_event(
            archived_message,
            message_id=51,
            user_id=10001,
            group_id=88888,
            nickname="用户A",
        )
    )

    async with app.test_matcher(chat_forward) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        event = create_group_event("打包消息 1", message_id=999, group_id=88888)

        expect_bot_not_muted(ctx, group_id=88888)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_api(
            "send_group_forward_msg",
            {
                "group_id": 88888,
                "messages": [
                    {
                        "type": "node",
                        "data": {
                            "name": "用户A",
                            "uin": "10001",
                            "content": Message([MessageSegment.text("hello "), MessageSegment.face(123)]),
                        },
                    }
                ],
            },
            result={"message_id": 1004},
        )


@pytest.mark.asyncio
async def test_chat_forward_degrades_image_when_not_cached(app: App, tmp_path, monkeypatch) -> None:
    """本地无图片文件时（未持久化或已过期清理），image 段降级为 [图片] 占位。"""
    from src.plugins.chat_forward import chat_forward
    from src.plugins.message_archive import image_store
    from src.plugins.message_archive.db import archive_message_event

    # image_dir 指向空目录，确保回放时查不到本地文件
    monkeypatch.setattr(image_store, "image_dir", tmp_path)
    # 归档时图片下载失败，不落盘
    async def _fail_fetch(url: object) -> bytes:
        raise OSError("download disabled in test")

    monkeypatch.setattr(image_store, "_fetch_bytes", _fail_fetch)

    archived_message = Message(
        [
            MessageSegment.text("看图 "),
            MessageSegment.image("https://example.com/demo.png"),
            MessageSegment.text(" 完毕"),
        ]
    )
    archived_message[1].data["file"] = "NOTCACHED.png"
    archived_message[1].data["url"] = "https://example.com/demo.png"
    await archive_message_event(
        create_group_event(
            archived_message,
            message_id=61,
            user_id=10001,
            group_id=99991,
            nickname="用户A",
        )
    )

    async with app.test_matcher(chat_forward) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        event = create_group_event("打包消息 1", message_id=999, group_id=99991)

        expect_bot_not_muted(ctx, group_id=99991)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_api(
            "send_group_forward_msg",
            {
                "group_id": 99991,
                "messages": [
                    {
                        "type": "node",
                        "data": {
                            "name": "用户A",
                            "uin": "10001",
                            "content": Message(
                                [
                                    MessageSegment.text("看图 "),
                                    MessageSegment.text("[图片]"),
                                    MessageSegment.text(" 完毕"),
                                ]
                            ),
                        },
                    }
                ],
            },
            result={"message_id": 1005},
        )


@pytest.mark.asyncio
async def test_chat_forward_replays_real_image_when_cached(app: App, tmp_path, monkeypatch) -> None:
    """本地有图片文件时，回放出真实 image 段而非 [图片] 占位。"""
    from src.plugins.chat_forward import chat_forward
    from src.plugins.message_archive import image_store
    from src.plugins.message_archive.db import archive_message_event

    monkeypatch.setattr(image_store, "image_dir", tmp_path)

    # 预置本地图片（绕过下载，直接落盘 + 登记元数据）
    target = tmp_path / "ab" / "abcdef.png"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"\x89PNG fake")
    await image_store.record_image_meta("abcdef", str(target), expire_at=9999999999)

    archived = Message(
        [MessageSegment.text("看图 "), MessageSegment.image("https://e.com/a.png")]
    )
    archived[1].data["file"] = "ABCDEF.png"
    await archive_message_event(
        create_group_event(archived, message_id=81, group_id=60001, nickname="用户A")
    )

    async with app.test_matcher(chat_forward) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        event = create_group_event("打包消息 1", message_id=999, group_id=60001)

        expect_bot_not_muted(ctx, group_id=60001)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_api(
            "send_group_forward_msg",
            {
                "group_id": 60001,
                "messages": [
                    {
                        "type": "node",
                        "data": {
                            "name": "用户A",
                            "uin": "123456",
                            "content": Message(
                                [
                                    MessageSegment.text("看图 "),
                                    MessageSegment.image(b"\x89PNG fake"),
                                ]
                            ),
                        },
                    }
                ],
            },
            result={"message_id": 1006},
        )

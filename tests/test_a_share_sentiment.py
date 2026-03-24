import json
from datetime import datetime
from unittest.mock import AsyncMock, patch

import httpx
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
    event_time: int | None = None,
    message_id: int = 1,
    user_id: int = 123456,
    group_id: int = 654321,
    self_id: int = 987654321,
    nickname: str = "测试用户",
) -> GroupMessageEvent:
    actual_message = message if isinstance(message, Message) else Message(message)
    return GroupMessageEvent(
        time=event_time or int(datetime.now().timestamp()),
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
    event_time: int | None = None,
    message_id: int = 1,
    user_id: int = 123456,
    self_id: int = 987654321,
    nickname: str = "测试用户",
) -> PrivateMessageEvent:
    return PrivateMessageEvent(
        time=event_time or int(datetime.now().timestamp()),
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
    ctx.should_call_api(
        "get_group_member_info",
        {"group_id": group_id, "user_id": self_id, "no_cache": True},
        result={"shut_up_timestamp": 0},
    )


def configure_sentiment_plugin() -> None:
    from src.plugins.a_share_sentiment import config

    config.a_share_sentiment_base_url = "https://example.com/v1"
    config.a_share_sentiment_api_key = "test-key"
    config.a_share_sentiment_model = "test-model"
    config.a_share_sentiment_history_days = 5
    config.a_share_sentiment_min_messages = 20
    config.a_share_sentiment_cooldown_seconds = 300
    config.a_share_sentiment_cache_ttl_minutes = 10
    config.a_share_sentiment_max_today_messages = 200
    config.a_share_sentiment_max_history_messages_per_day = 40
    config.a_share_sentiment_max_prompt_chars_today = 12000
    config.a_share_sentiment_max_prompt_chars_history_day = 4000


@pytest.mark.asyncio
async def test_a_share_sentiment_group_command_returns_score(app: App) -> None:
    from src.plugins.a_share_sentiment import a_share_sentiment
    from src.plugins.a_share_sentiment.ai import SentimentAnalysisResult
    from src.plugins.message_archive.db import archive_message_event

    configure_sentiment_plugin()
    base_time = int(datetime(2026, 3, 24, 14, 30).timestamp())
    for index in range(1, 4):
        await archive_message_event(
            create_group_event(
                f"A股今天有点恐慌，{600000 + index} 还在跳水",
                event_time=base_time - 1800 + index,
                message_id=index,
                user_id=10000 + index,
                group_id=654321,
                nickname=f"用户{index}",
            )
        )

    mock_result = {
        "score": 27,
        "label": "偏悲观",
        "confidence": 0.8,
        "summary": "群里整体偏谨慎，讨论集中在跳水和亏钱效应。",
        "reasons": ["多条消息提到跳水和恐慌", "用户关注仓位和止损", "几乎没有明显看多表述"],
        "compare_to_history": "相比近5日基线更悲观。",
    }

    with patch(
        "src.plugins.a_share_sentiment.request_sentiment_analysis",
        new=AsyncMock(return_value=SentimentAnalysisResult.model_validate(mock_result)),
    ):
        async with app.test_matcher(a_share_sentiment) as ctx:
            bot = ctx.create_bot(base=Bot, self_id="987654321")
            event = create_group_event("本群情绪", event_time=base_time, message_id=999, group_id=654321)

            expect_bot_not_muted(ctx, group_id=654321)
            ctx.receive_event(bot, event)
            ctx.should_pass_rule()
            ctx.should_call_send(
                event,
                (
                    "A股情绪指数：27/100（偏悲观）\n"
                    "置信度：35%\n"
                    "今日样本：3 条文本消息，3 位活跃成员\n"
                    "提示：今日样本偏少，仅供参考\n"
                    "总评：群里整体偏谨慎，讨论集中在跳水和亏钱效应。\n"
                    "原因：\n"
                    "1. 多条消息提到跳水和恐慌\n"
                    "2. 用户关注仓位和止损\n"
                    "3. 几乎没有明显看多表述\n"
                    "近5日对比：相比近5日基线更悲观。"
                ),
                result={"message_id": 1000},
            )


@pytest.mark.asyncio
async def test_a_share_sentiment_private_message_is_rejected(app: App) -> None:
    from src.plugins.a_share_sentiment import a_share_sentiment

    configure_sentiment_plugin()

    async with app.test_matcher(a_share_sentiment) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        event = create_private_event("本群情绪")

        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(
            event,
            "仅支持群聊使用",
            result={"message_id": 1001},
        )


@pytest.mark.asyncio
async def test_a_share_sentiment_handles_no_text_messages(app: App) -> None:
    from src.plugins.a_share_sentiment import a_share_sentiment
    from src.plugins.message_archive.db import archive_message_event

    configure_sentiment_plugin()
    base_time = int(datetime(2026, 3, 24, 14, 30).timestamp())
    await archive_message_event(
        create_group_event(
            Message([MessageSegment.face(123)]),
            event_time=base_time - 60,
            message_id=1,
            group_id=654321,
        )
    )

    async with app.test_matcher(a_share_sentiment) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        event = create_group_event("本群情绪", event_time=base_time, message_id=999, group_id=654321)

        expect_bot_not_muted(ctx, group_id=654321)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(
            event,
            "今天还没有可分析的群聊文本消息",
            result={"message_id": 1002},
        )


@pytest.mark.asyncio
async def test_a_share_sentiment_cache_hit_skips_second_ai_call(app: App) -> None:
    from src.plugins.a_share_sentiment import a_share_sentiment
    from src.plugins.a_share_sentiment.ai import SentimentAnalysisResult
    from src.plugins.message_archive.db import archive_message_event

    configure_sentiment_plugin()
    base_time = int(datetime(2026, 3, 24, 14, 30).timestamp())
    for index in range(1, 3):
        await archive_message_event(
            create_group_event(
                f"A股分歧很大，{600000 + index} 今天炸板",
                event_time=base_time - 120 + index,
                message_id=index,
                user_id=20000 + index,
                group_id=654321,
            )
        )

    mock_result = {
        "score": 42,
        "label": "中性",
        "confidence": 0.2,
        "summary": "群里观点分歧较大。",
        "reasons": ["有人看多也有人担忧炸板", "讨论热度一般"],
        "compare_to_history": "和近5日基线接近。",
    }

    with patch(
        "src.plugins.a_share_sentiment.request_sentiment_analysis",
        new=AsyncMock(return_value=SentimentAnalysisResult.model_validate(mock_result)),
    ) as mocked_request:
        async with app.test_matcher(a_share_sentiment) as ctx:
            bot = ctx.create_bot(base=Bot, self_id="987654321")
            first_event = create_group_event("本群情绪", event_time=base_time, message_id=999, group_id=654321)
            second_event = create_group_event("今日情绪", event_time=base_time + 60, message_id=1000, group_id=654321)

            expect_bot_not_muted(ctx, group_id=654321)
            ctx.receive_event(bot, first_event)
            ctx.should_pass_rule()
            ctx.should_call_send(
                first_event,
                (
                    "A股情绪指数：42/100（中性）\n"
                    "置信度：20%\n"
                    "今日样本：2 条文本消息，2 位活跃成员\n"
                    "提示：今日样本偏少，仅供参考\n"
                    "总评：群里观点分歧较大。\n"
                    "原因：\n"
                    "1. 有人看多也有人担忧炸板\n"
                    "2. 讨论热度一般\n"
                    "近5日对比：和近5日基线接近。"
                ),
                result={"message_id": 1003},
            )

            ctx.receive_event(bot, second_event)
            ctx.should_pass_rule()
            ctx.should_call_send(
                second_event,
                (
                    "A股情绪指数：42/100（中性）\n"
                    "置信度：20%\n"
                    "今日样本：2 条文本消息，2 位活跃成员\n"
                    "提示：今日样本偏少，仅供参考\n"
                    "总评：群里观点分歧较大。\n"
                    "原因：\n"
                    "1. 有人看多也有人担忧炸板\n"
                    "2. 讨论热度一般\n"
                    "近5日对比：和近5日基线接近。"
                ),
                result={"message_id": 1004},
            )

    assert mocked_request.await_count == 1


@pytest.mark.asyncio
async def test_a_share_sentiment_cooldown_blocks_cache_miss(app: App) -> None:
    from src.plugins.a_share_sentiment import a_share_sentiment, cooldown_dict

    configure_sentiment_plugin()
    with patch("src.plugins.a_share_sentiment.time.time", return_value=1_000_000.0):
        cooldown_dict["654321"] = 1_000_000.0

        async with app.test_matcher(a_share_sentiment) as ctx:
            bot = ctx.create_bot(base=Bot, self_id="987654321")
            event = create_group_event("本群情绪", group_id=654321)

            expect_bot_not_muted(ctx, group_id=654321)
            ctx.receive_event(bot, event)
            ctx.should_pass_rule()
            ctx.should_call_send(
                event,
                "冷却中，请等待 300 秒",
                result={"message_id": 1005},
            )


@pytest.mark.asyncio
async def test_fetch_group_messages_by_time_range_respects_day_boundaries() -> None:
    from src.plugins.message_archive.db import (
        archive_message_event,
        fetch_group_messages_by_time_range,
    )

    march_23_235959 = int(datetime(2026, 3, 23, 23, 59, 59).timestamp())
    march_24_000000 = int(datetime(2026, 3, 24, 0, 0, 0).timestamp())
    march_24_120000 = int(datetime(2026, 3, 24, 12, 0, 0).timestamp())
    march_25_000000 = int(datetime(2026, 3, 25, 0, 0, 0).timestamp())

    await archive_message_event(
        create_group_event("前一天", event_time=march_23_235959, message_id=1, group_id=654321)
    )
    await archive_message_event(
        create_group_event("当天零点", event_time=march_24_000000, message_id=2, group_id=654321)
    )
    await archive_message_event(
        create_group_event("当天中午", event_time=march_24_120000, message_id=3, group_id=654321)
    )
    await archive_message_event(
        create_group_event("第二天零点", event_time=march_25_000000, message_id=4, group_id=654321)
    )

    messages = await fetch_group_messages_by_time_range(
        group_id="654321",
        start_time=march_24_000000,
        end_time=march_25_000000,
    )

    assert [message.plain_text for message in messages] == ["当天零点", "当天中午"]


def test_build_day_analysis_prioritizes_keywords_and_codes() -> None:
    from src.plugins.a_share_sentiment.analysis import build_day_analysis
    from src.plugins.message_archive.db import ArchivedMessage

    messages = [
        ArchivedMessage(
            id=1,
            session_type="group",
            session_id="654321",
            message_id=1,
            event_time=1,
            self_id="1",
            user_id="10001",
            group_id="654321",
            sender_name="用户A",
            message_cq="普通闲聊1",
            plain_text="普通闲聊1",
        ),
        ArchivedMessage(
            id=2,
            session_type="group",
            session_id="654321",
            message_id=2,
            event_time=2,
            self_id="1",
            user_id="10002",
            group_id="654321",
            sender_name="用户B",
            message_cq="600000 今天炸板了",
            plain_text="600000 今天炸板了",
        ),
        ArchivedMessage(
            id=3,
            session_type="group",
            session_id="654321",
            message_id=3,
            event_time=3,
            self_id="1",
            user_id="10003",
            group_id="654321",
            sender_name="用户C",
            message_cq="普通闲聊2",
            plain_text="普通闲聊2",
        ),
        ArchivedMessage(
            id=4,
            session_type="group",
            session_id="654321",
            message_id=4,
            event_time=4,
            self_id="1",
            user_id="10004",
            group_id="654321",
            sender_name="用户D",
            message_cq="A股今天情绪修复",
            plain_text="A股今天情绪修复",
        ),
        ArchivedMessage(
            id=5,
            session_type="group",
            session_id="654321",
            message_id=5,
            event_time=5,
            self_id="1",
            user_id="10005",
            group_id="654321",
            sender_name="用户E",
            message_cq="普通闲聊3",
            plain_text="普通闲聊3",
        ),
    ]

    analysis = build_day_analysis(messages, "2026-03-24", max_messages=3, char_budget=500)

    assert analysis.total_messages == 5
    assert analysis.keyword_messages == 2
    assert analysis.code_messages == 1
    assert analysis.sampled_messages[0].endswith("600000 今天炸板了")
    assert analysis.sampled_messages[1].endswith("A股今天情绪修复")
    assert len(analysis.sampled_messages) == 3


@pytest.mark.asyncio
async def test_request_sentiment_analysis_success() -> None:
    from src.plugins.a_share_sentiment.ai import request_sentiment_analysis

    response = httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "score": 61,
                                "label": "偏乐观",
                                "confidence": 0.66,
                                "summary": "群里整体偏乐观。",
                                "reasons": ["讨论集中在反弹", "看多措辞明显"],
                                "compare_to_history": "比近5日基线更积极。",
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        },
        request=httpx.Request("POST", "https://example.com/v1/chat/completions"),
    )

    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=response)):
        result = await request_sentiment_analysis(
            base_url="https://example.com/v1",
            api_key="key",
            model="model",
            timeout_seconds=30,
            temperature=0.2,
            prompt_payload="{}",
        )

    assert result.score == 61
    assert result.label == "偏乐观"


@pytest.mark.asyncio
async def test_request_sentiment_analysis_rejects_non_json_content() -> None:
    from src.plugins.a_share_sentiment.ai import (
        SentimentAIResponseError,
        request_sentiment_analysis,
    )

    response = httpx.Response(
        200,
        json={"choices": [{"message": {"content": "不是 JSON"}}]},
        request=httpx.Request("POST", "https://example.com/v1/chat/completions"),
    )

    with (
        patch("httpx.AsyncClient.post", new=AsyncMock(return_value=response)),
        pytest.raises(SentimentAIResponseError),
    ):
        await request_sentiment_analysis(
            base_url="https://example.com/v1",
            api_key="key",
            model="model",
            timeout_seconds=30,
            temperature=0.2,
            prompt_payload="{}",
        )


@pytest.mark.asyncio
async def test_request_sentiment_analysis_rejects_missing_fields() -> None:
    from src.plugins.a_share_sentiment.ai import (
        SentimentAIResponseError,
        request_sentiment_analysis,
    )

    response = httpx.Response(
        200,
        json={"choices": [{"message": {"content": '{"score": 12, "label": "偏悲观"}'}}]},
        request=httpx.Request("POST", "https://example.com/v1/chat/completions"),
    )

    with (
        patch("httpx.AsyncClient.post", new=AsyncMock(return_value=response)),
        pytest.raises(SentimentAIResponseError),
    ):
        await request_sentiment_analysis(
            base_url="https://example.com/v1",
            api_key="key",
            model="model",
            timeout_seconds=30,
            temperature=0.2,
            prompt_payload="{}",
        )


@pytest.mark.asyncio
async def test_request_sentiment_analysis_handles_timeout() -> None:
    from src.plugins.a_share_sentiment.ai import SentimentAITimeoutError, request_sentiment_analysis

    with (
        patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=httpx.ReadTimeout("timeout"))),
        pytest.raises(SentimentAITimeoutError),
    ):
        await request_sentiment_analysis(
            base_url="https://example.com/v1",
            api_key="key",
            model="model",
            timeout_seconds=30,
            temperature=0.2,
            prompt_payload="{}",
        )


@pytest.mark.asyncio
async def test_request_sentiment_analysis_handles_auth_error() -> None:
    from src.plugins.a_share_sentiment.ai import SentimentAIAuthError, request_sentiment_analysis

    response = httpx.Response(
        401,
        request=httpx.Request("POST", "https://example.com/v1/chat/completions"),
    )

    with (
        patch("httpx.AsyncClient.post", new=AsyncMock(return_value=response)),
        pytest.raises(SentimentAIAuthError),
    ):
        await request_sentiment_analysis(
            base_url="https://example.com/v1",
            api_key="key",
            model="model",
            timeout_seconds=30,
            temperature=0.2,
            prompt_payload="{}",
        )


@pytest.mark.asyncio
async def test_request_sentiment_analysis_handles_server_error() -> None:
    from src.plugins.a_share_sentiment.ai import SentimentAIServiceError, request_sentiment_analysis

    response = httpx.Response(
        500,
        request=httpx.Request("POST", "https://example.com/v1/chat/completions"),
    )

    with (
        patch("httpx.AsyncClient.post", new=AsyncMock(return_value=response)),
        pytest.raises(SentimentAIServiceError),
    ):
        await request_sentiment_analysis(
            base_url="https://example.com/v1",
            api_key="key",
            model="model",
            timeout_seconds=30,
            temperature=0.2,
            prompt_payload="{}",
        )

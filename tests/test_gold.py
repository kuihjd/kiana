from datetime import datetime

import pytest
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message
from nonebot.adapters.onebot.v11.event import Sender
from nonebug import App


def create_group_event(message: str, group_id: int = 123456, user_id: int = 111111) -> GroupMessageEvent:
    """创建群消息事件"""
    return GroupMessageEvent(
        time=int(datetime.now().timestamp()),
        self_id=987654321,
        post_type="message",
        sub_type="normal",
        user_id=user_id,
        message_type="group",
        group_id=group_id,
        message_id=1,
        message=Message(message),
        original_message=Message(message),
        raw_message=message,
        font=0,
        sender=Sender(user_id=user_id, nickname="测试用户", card="", role="member"),
    )


def expect_bot_not_muted(ctx, group_id: int = 123456, self_id: int = 987654321) -> None:
    """声明预期的禁言检查 API 调用"""
    ctx.should_call_api(
        "get_group_member_info",
        {"group_id": group_id, "user_id": self_id, "no_cache": True},
        result={"shut_up_timestamp": 0},
    )


@pytest.mark.asyncio
async def test_gold_matcher_rule_matches(app: App):
    """测试 gold matcher 的规则能匹配'金价'消息"""
    from src.plugins.gold import gold

    async with app.test_api() as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        event = create_group_event("金价")

        result = await gold.rule(bot, event, {})
        assert result is True, "金价消息应该匹配 gold matcher 规则"


@pytest.mark.asyncio
async def test_gold_matcher_rule_not_matches(app: App):
    """测试 gold matcher 的规则不匹配其他消息"""
    from src.plugins.gold import gold

    async with app.test_matcher(gold) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        event = create_group_event("hello")

        ctx.receive_event(bot, event)
        ctx.should_not_pass_rule()


@pytest.mark.asyncio
async def test_fetch_gold_price_returns_number():
    """测试获取金价函数能返回有效数字"""
    from src.plugins.gold import _fetch_gold_price_sync

    price = _fetch_gold_price_sync()
    print(f"\n当前金价: {price}")

    assert price is not None, "金价获取失败"
    assert isinstance(price, float), f"金价应该是 float 类型，实际是: {type(price)}"
    assert price > 0, f"金价应该是正数，实际是: {price}"


def test_gold_chart_failure_message_is_sanitized() -> None:
    """图表生成失败时不应把底层异常直接发给用户。"""
    from src.plugins.gold import get_gold_chart_failure_message

    assert get_gold_chart_failure_message() == "生成图表失败，请稍后重试"

"""共享的转发消息工具函数"""

from typing import Any

from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
)


def create_forward_node(bot: Bot, content: str | MessageSegment) -> dict[str, Any]:
    """创建单个转发消息节点

    Args:
        bot: Bot 实例
        content: 消息内容（文本或消息段）

    Returns:
        转发消息节点字典
    """
    msg = Message(content)
    return {"type": "node", "data": {"name": "", "uin": bot.self_id, "content": msg}}


def create_forward_nodes(
    bot: Bot,
    contents: list[str | MessageSegment],
) -> list[dict[str, Any]]:
    """创建多个转发消息节点

    Args:
        bot: Bot 实例
        contents: 消息内容列表

    Returns:
        转发消息节点列表
    """
    return [create_forward_node(bot, content) for content in contents]


async def send_forward_message(
    bot: Bot,
    event: MessageEvent,
    forward_nodes: list[dict[str, Any]],
) -> None:
    """发送合并转发消息

    Args:
        bot: Bot 实例
        event: 消息事件
        forward_nodes: 转发消息节点列表
    """
    if isinstance(event, GroupMessageEvent):
        await bot.call_api(
            "send_group_forward_msg", group_id=event.group_id, messages=forward_nodes
        )
    else:
        await bot.call_api(
            "send_private_forward_msg", user_id=event.user_id, messages=forward_nodes
        )

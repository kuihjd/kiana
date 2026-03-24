import time
from typing import Any

from nonebot import get_plugin_config, on_regex
from nonebot.adapters.onebot.v11 import Bot, MessageEvent
from nonebot.params import RegexGroup
from nonebot.plugin import PluginMetadata

from ..forward_utils import send_forward_message
from ..group_permission import create_sub_feature_rule
from ..message_archive.db import ArchivedMessage, fetch_session_messages, get_session_context
from .config import Config

__plugin_meta__ = PluginMetadata(
    name="chat_forward",
    description="打包当前会话消息并合并转发",
    usage="打包消息 [条数] - 打包当前会话最近n条消息（默认15条）\n打包记录 [条数] - 同上",
    config=Config,
)

config: Config = get_plugin_config(Config)

is_chat_forward_enabled = create_sub_feature_rule(
    config_getter=lambda: config,
    plugin_enabled_attr="chat_forward_plugin_enabled",
    feature_enabled_attr="chat_forward_plugin_enabled",
    prefix="chat_forward_",
)

chat_forward = on_regex(
    r"^打包(消息|记录)\s*(\d*)$",
    rule=is_chat_forward_enabled,
    priority=5,
    block=True,
)

# 冷却字典
cooldown_dict: dict[str, float] = {}


def parse_count(reg_group: tuple) -> int:
    """解析条数参数"""
    count_str = reg_group[1] if len(reg_group) > 1 else ""
    if not count_str:
        return config.chat_forward_default_count
    return int(count_str)


def get_cooldown_key(event: MessageEvent) -> str:
    """获取会话级冷却 key。"""
    session_type, session_id = get_session_context(event)
    return f"{session_type}:{session_id}"


def build_forward_nodes(messages: list[ArchivedMessage]) -> list[dict[str, Any]]:
    """构建合并转发节点。

    优先直接引用原消息 ID，尽量保留 QQ 原生消息类型的渲染能力。
    """
    return [
        {
            "type": "node",
            "data": {
                "id": message.message_id,
            },
        }
        for message in messages
    ]


@chat_forward.handle()
async def handle_chat_forward(bot: Bot, event: MessageEvent, reg_group: tuple = RegexGroup()) -> None:
    cooldown_key = get_cooldown_key(event)
    current_time = time.time()

    if cooldown_key in cooldown_dict:
        elapsed = current_time - cooldown_dict[cooldown_key]
        if elapsed < config.chat_forward_cooldown:
            remaining = int(config.chat_forward_cooldown - elapsed)
            await chat_forward.finish(f"冷却中，请等待 {remaining} 秒")

    count = parse_count(reg_group)
    if count > config.chat_forward_max_count:
        await chat_forward.finish(f"最多只能打包 {config.chat_forward_max_count} 条消息")
    if count < 1:
        await chat_forward.finish("条数必须大于 0")

    # 更新冷却时间
    cooldown_dict[cooldown_key] = current_time

    session_type, session_id = get_session_context(event)
    try:
        messages = await fetch_session_messages(
            session_type=session_type,
            session_id=session_id,
            limit=count,
            exclude_message_id=event.message_id,
        )
    except Exception as e:
        await chat_forward.finish(f"获取消息记录失败: {e}")

    if not messages:
        await chat_forward.finish("没有获取到消息记录")

    forward_nodes = build_forward_nodes(messages)
    if not forward_nodes:
        await chat_forward.finish("没有可打包的消息")

    try:
        await send_forward_message(bot, event, forward_nodes)
    except Exception as e:
        await chat_forward.finish(f"发送合并转发失败: {e}")

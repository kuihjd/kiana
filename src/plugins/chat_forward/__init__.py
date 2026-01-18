import time

from nonebot import get_plugin_config, on_regex
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
from nonebot.params import RegexGroup
from nonebot.plugin import PluginMetadata

from ..group_permission import create_sub_feature_rule
from .config import Config

__plugin_meta__ = PluginMetadata(
    name="chat_forward",
    description="打包群聊消息并合并转发",
    usage="打包消息 [条数] - 打包最近n条消息（默认15条）\n打包记录 [条数] - 同上",
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
cooldown_dict: dict[int, float] = {}


def parse_count(reg_group: tuple) -> int:
    """解析条数参数"""
    count_str = reg_group[1] if len(reg_group) > 1 else ""
    if not count_str:
        return config.chat_forward_default_count
    return int(count_str)


def is_valid_message(msg: dict) -> bool:
    """检查消息是否有效（排除撤回等无效消息）"""
    content = msg.get("message")
    # 消息内容为空
    if not content:
        return False
    # 消息内容是空列表
    return not (isinstance(content, list) and len(content) == 0)


def build_forward_nodes(messages: list, bot_id: str) -> list[dict]:
    """构建合并转发节点"""
    forward_nodes = []
    for msg in messages:
        if not is_valid_message(msg):
            continue
        sender = msg.get("sender", {})
        node = {
            "type": "node",
            "data": {
                "name": sender.get("nickname", "未知"),
                "uin": str(sender.get("user_id", bot_id)),
                "content": msg.get("message", ""),
            },
        }
        forward_nodes.append(node)
    return forward_nodes


@chat_forward.handle()
async def handle_chat_forward(bot: Bot, event: GroupMessageEvent, reg_group: tuple = RegexGroup()):
    group_id = event.group_id
    current_time = time.time()

    if group_id in cooldown_dict:
        elapsed = current_time - cooldown_dict[group_id]
        if elapsed < config.chat_forward_cooldown:
            remaining = int(config.chat_forward_cooldown - elapsed)
            await chat_forward.finish(f"冷却中，请等待 {remaining} 秒")

    count = parse_count(reg_group)
    if count > config.chat_forward_max_count:
        await chat_forward.finish(f"最多只能打包 {config.chat_forward_max_count} 条消息")
    if count < 1:
        await chat_forward.finish("条数必须大于 0")

    # 更新冷却时间
    cooldown_dict[group_id] = current_time

    # 获取群消息历史（多取一条，因为要排除触发消息本身）
    try:
        history = await bot.call_api(
            "get_group_msg_history",
            group_id=group_id,
            count=count + 1,
        )
    except Exception as e:
        await chat_forward.finish(f"获取消息记录失败: {e}")

    messages = history.get("messages", [])
    if not messages:
        await chat_forward.finish("没有获取到消息记录")

    trigger_msg_id = event.message_id
    messages = [msg for msg in messages if msg.get("message_id") != trigger_msg_id]
    messages = messages[-count:]

    if not messages:
        await chat_forward.finish("没有可打包的消息")

    forward_nodes = build_forward_nodes(messages, bot.self_id)
    try:
        await bot.call_api(
            "send_group_forward_msg",
            group_id=group_id,
            messages=forward_nodes,
        )
    except Exception as e:
        await chat_forward.finish(f"发送合并转发失败: {e}")

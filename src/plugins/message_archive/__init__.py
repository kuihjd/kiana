from nonebot import get_driver, get_plugin_config, logger
from nonebot.adapters.onebot.v11 import Event, MessageEvent
from nonebot.message import event_preprocessor
from nonebot.plugin import PluginMetadata

from ..group_permission import check_group_permission
from .config import Config
from .db import archive_message_event, ensure_schema

__plugin_meta__ = PluginMetadata(
    name="message_archive",
    description="归档收到的消息，供后续合并转发回放",
    usage="无命令。插件会自动归档群聊和私聊消息。",
    config=Config,
)

config: Config = get_plugin_config(Config)
driver = get_driver()


def _is_archive_enabled(event: Event) -> bool:
    return check_group_permission(
        event=event,
        enabled=config.message_archive_plugin_enabled,
        group_mode=config.message_archive_group_mode,
        group_whitelist=config.message_archive_group_whitelist,
        group_blacklist=config.message_archive_group_blacklist,
    )


@driver.on_startup
async def init_message_archive_schema() -> None:
    """在 NoneBot 启动时初始化消息归档表。"""
    ensure_schema()


@event_preprocessor
async def archive_received_message(event: Event) -> None:
    """归档每一条收到的消息事件。"""
    if not isinstance(event, MessageEvent):
        return
    if not _is_archive_enabled(event):
        return

    try:
        await archive_message_event(event)
    except Exception as e:
        logger.error(f"归档消息失败: {e}", exc_info=True)

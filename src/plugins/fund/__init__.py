"""基金/股票/指数查询插件"""

import re

from nonebot import get_plugin_config, logger, on_regex
from nonebot.adapters.onebot.v11 import Bot, MessageEvent, MessageSegment
from nonebot.exception import MatcherException
from nonebot.plugin import PluginMetadata

from ..forward_utils import create_forward_nodes, send_forward_message
from ..group_permission import create_group_rule
from .cache import FundDataCacheManager
from .code_identifier import CodeType, identify_code_type
from .config import Config
from .queries import query_by_code_type

__plugin_meta__ = PluginMetadata(
    name="fund",
    description="基金查询插件",
    usage=(
        "发送代码查询信息:\n"
        "- 场外基金: 018957、002170\n"
        "- 场内ETF: 510300、159915\n"
        "- 场内LOF: 163406、501018\n"
        "- 个股(需带交易所): 000001.SZ、600000.SH、43123456.BJ\n"
        "- 指数: 000001.SH、399001.SZ"
    ),
    config=Config,
)


# 延迟加载插件配置（避免在模块导入时初始化）
_cached_plugin_config: Config | None = None


def _get_plugin_config() -> Config:
    """获取插件配置，使用缓存避免重复获取"""
    global _cached_plugin_config  # noqa: PLW0603
    if _cached_plugin_config is None:
        _cached_plugin_config = get_plugin_config(Config)
    return _cached_plugin_config


# 延迟初始化缓存管理器
_cached_cache_manager: FundDataCacheManager | None = None


def _get_cache_manager() -> FundDataCacheManager:
    """获取缓存管理器实例"""
    global _cached_cache_manager  # noqa: PLW0603
    if _cached_cache_manager is None:
        try:
            config = _get_plugin_config()
            max_size = getattr(config, "fund_max_cache_size", 100)
        except ValueError:
            max_size = 100
        _cached_cache_manager = FundDataCacheManager(max_size=max_size)
    return _cached_cache_manager


# 创建群组规则检查函数
fund_group_rule = create_group_rule(
    config_getter=_get_plugin_config,
    plugin_enabled_attr="fund_plugin_enabled",
    prefix="fund_",
)


fund_query = on_regex(
    r"^(\d{6}|\d{6}\.(SZ|SH)|\d{8}\.BJ)$",
    rule=fund_group_rule,
    flags=re.IGNORECASE,
)


@fund_query.handle()
async def handle_fund_query(bot: Bot, event: MessageEvent) -> None:
    """处理基金/股票查询请求

    Args:
        bot: Bot 实例
        event: 消息事件

    Returns:
        None
    """
    code = str(event.message).strip()

    try:
        # 识别代码类型
        code_type = identify_code_type(code)

        if code_type == CodeType.UNKNOWN:
            logger.info(f"未识别的代码类型: {code}")
            return  # 静默失败

        # 查询数据
        info_text, holdings_text = await query_by_code_type(code, code_type)

        # 创建并发送合并转发消息
        if info_text:
            contents: list[str | MessageSegment] = [info_text]
            if holdings_text:
                contents.append(holdings_text)
            forward_nodes = create_forward_nodes(bot, contents)
            await send_forward_message(bot, event, forward_nodes)
        else:
            logger.warning(f"未能获取到数据: {code}")
            # 静默失败，不发送任何消息

    except MatcherException:
        return
    except Exception as e:
        logger.error(f"处理查询请求失败 [{code}]: {e}", exc_info=True)
        # 静默失败，不发送任何消息

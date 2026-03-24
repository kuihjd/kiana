"""基金/股票/指数查询插件"""

import re

from nonebot import logger, on_regex
from nonebot.adapters.onebot.v11 import Bot, MessageEvent, MessageSegment
from nonebot.exception import MatcherException
from nonebot.plugin import PluginMetadata

from ..forward_utils import create_forward_nodes, send_forward_message
from ..group_permission import create_group_rule
from .code_identifier import CodeType, identify_code_type
from .config import Config
from .queries import query_by_code_type
from .runtime import get_plugin_config_cached

__plugin_meta__ = PluginMetadata(
    name="fund",
    description="基金查询插件",
    usage=(
        "发送代码查询信息:\n"
        "- 场外基金: 018957、002170\n"
        "- 场内ETF: 510300、159915\n"
        "- 场内LOF: 163406、501018\n"
        "- 个股(需带交易所): 000001.SZ、600000.SH、920029.BJ\n"
        "- 指数: 000001.SH、399001.SZ"
    ),
    config=Config,
)

# 创建群组规则检查函数
fund_group_rule = create_group_rule(
    config_getter=get_plugin_config_cached,
    plugin_enabled_attr="fund_plugin_enabled",
    prefix="fund_",
)


fund_query = on_regex(
    r"^(\d{6}|\d{6}\.(SZ|SH|BJ))$",
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
            logger.info(f"未能获取到数据: {code}")
            # 静默失败，不发送任何消息

    except MatcherException:
        return
    except Exception as e:
        logger.error(f"处理查询请求失败 [{code}]: {e}", exc_info=True)
        # 静默失败，不发送任何消息

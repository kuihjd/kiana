"""基金查询协调模块"""

from typing import Literal

from nonebot import logger

from .code_identifier import CodeType
from .data_fetcher import (
    get_fund_data,
    get_fund_holdings,
    get_index_data,
    get_market_fund_data,
    get_stock_data,
)
from .formatters import (
    format_etf_info,
    format_fund_holdings,
    format_fund_info,
    format_index_info,
    format_stock_info,
)
from .runtime import get_plugin_config_cached


def _get_config_value(attr_name: str, default_value: bool) -> bool:
    """安全地获取配置值，如果 NoneBot 未初始化则使用默认值"""
    try:
        config = get_plugin_config_cached()
        return getattr(config, attr_name, default_value)
    except ValueError:
        # NoneBot has not been initialized
        return default_value


async def _query_off_market_fund(code: str) -> tuple[str | None, str | None]:
    """查询场外基金数据

    Args:
        code: 基金代码

    Returns:
        (info_text, holdings_text) 元组
    """
    if not _get_config_value("fund_enable_off_market", True):
        logger.debug(f"场外基金查询已禁用: {code}")
        return None, None

    fund_data = await get_fund_data(code)
    if not fund_data["success"]:
        logger.info(f"场外基金数据获取失败: {code}")
        return None, None

    info_text = format_fund_info(code, fund_data)

    # 获取持仓数据
    holdings_data = await get_fund_holdings(code)
    holdings_text = None
    if holdings_data["success"]:
        holdings_text = format_fund_holdings(code, holdings_data)
    else:
        logger.info(f"获取基金持仓数据失败: {holdings_data.get('error', '未知错误')}")

    return info_text, holdings_text


async def _query_market_fund(
    code: str, fund_type: Literal["etf", "lof"], type_name: str
) -> tuple[str | None, str | None]:
    """查询场内基金数据（ETF/LOF）

    Args:
        code: 基金代码
        fund_type: 基金类型
        type_name: 类型名称（用于日志）

    Returns:
        (info_text, None) 元组
    """
    config_key = f"fund_enable_{fund_type}"
    if not _get_config_value(config_key, True):
        logger.debug(f"{type_name}查询已禁用: {code}")
        return None, None

    fund_data = await get_market_fund_data(code, fund_type)
    if not fund_data["success"]:
        logger.info(f"{type_name}数据获取失败: {code}")
        return None, None

    info_text = format_etf_info(code, fund_data)
    return info_text, None


async def _query_stock(code: str) -> tuple[str | None, str | None]:
    """查询股票数据

    Args:
        code: 股票代码

    Returns:
        (info_text, None) 元组
    """
    if not _get_config_value("fund_enable_stocks", True):
        logger.debug(f"股票查询已禁用: {code}")
        return None, None

    stock_data = await get_stock_data(code)
    if not stock_data["success"]:
        logger.info(f"股票数据获取失败: {code}")
        return None, None

    info_text = await format_stock_info(code, stock_data)
    return info_text, None


async def _query_index(code: str) -> tuple[str | None, str | None]:
    """查询指数数据

    Args:
        code: 指数代码

    Returns:
        (info_text, None) 元组
    """
    if not _get_config_value("fund_enable_index", True):
        logger.debug(f"指数查询已禁用: {code}")
        return None, None

    index_data = await get_index_data(code)
    if not index_data["success"]:
        logger.info(f"指数数据获取失败: {code}")
        return None, None

    info_text = format_index_info(code, index_data)
    return info_text, None


async def query_by_code_type(code: str, code_type: CodeType) -> tuple[str | None, str | None]:
    """根据代码类型查询数据并格式化

    Args:
        code: 代码字符串
        code_type: 代码类型

    Returns:
        (info_text, holdings_text) 元组，失败时返回 (None, None)
    """
    try:
        if code_type == CodeType.OFF_MARKET_FUND:
            return await _query_off_market_fund(code)
        if code_type == CodeType.ETF:
            return await _query_market_fund(code, "etf", "ETF")
        if code_type == CodeType.LOF:
            return await _query_market_fund(code, "lof", "LOF")
        if code_type == CodeType.STOCK:
            return await _query_stock(code)
        if code_type == CodeType.INDEX:
            return await _query_index(code)
        return None, None

    except Exception as e:
        logger.error(f"查询{code_type.value}数据时发生错误 [{code}]: {e}", exc_info=True)
        return None, None

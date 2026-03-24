"""基金数据获取模块"""

import asyncio
from datetime import datetime, timedelta
from typing import Any, Literal

import akshare as ak
import pandas as pd
from nonebot import logger

from .cache import FundDataCacheManager
from .market_rules import (
    infer_stock_market,
    is_shanghai_index,
    is_shenzhen_index,
    validate_market_code,
)
from .runtime import get_cache_manager, get_plugin_config_cached


def _get_config_value(attr_name: str, default_value: Any) -> Any:
    """安全地获取配置值，如果 NoneBot 未初始化则使用默认值"""
    try:
        config = get_plugin_config_cached()
        return getattr(config, attr_name, default_value)
    except ValueError:
        # NoneBot has not been initialized
        return default_value


def _get_cache_manager() -> FundDataCacheManager:
    """获取缓存管理器实例"""
    return get_cache_manager()


# 默认配置常量
DEFAULT_HISTORY_DAYS = 30
DEFAULT_CACHE_TTL_MINUTES = 5


def _normalize_etf_data_from_ths(df: pd.DataFrame) -> pd.DataFrame:
    """将同花顺 ETF 数据格式转换为东方财富格式

    Args:
        df: 同花顺 ETF 数据 DataFrame

    Returns:
        转换后的 DataFrame（兼容东方财富格式）
    """
    # 创建列名映射
    column_mapping = {
        "基金代码": "代码",
        "基金名称": "名称",
        "当前-单位净值": "最新价",
        "增长率": "涨跌幅",
        "增长值": "涨跌额",
    }

    # 只保留和重命名需要的列
    new_df = df.copy()

    # 重命名列
    for old_col, new_col in column_mapping.items():
        if old_col in new_df.columns:
            new_df = new_df.rename(columns={old_col: new_col})

    # 添加缺失的列（同花顺没有成交量和成交额）
    if "成交量" not in new_df.columns:
        new_df["成交量"] = "N/A"
    if "成交额" not in new_df.columns:
        new_df["成交额"] = "N/A"

    # 确保必需的列存在
    required_columns = ["代码", "名称", "最新价", "涨跌幅", "涨跌额"]
    for col in required_columns:
        if col not in new_df.columns:
            logger.warning(f"同花顺数据缺少列: {col}")
            new_df[col] = "N/A"

    return new_df


async def get_etf_spot_data_cached() -> pd.DataFrame | None:
    """获取ETF实时数据（带线程安全缓存）

    实现了智能缓存和多数据源切换策略：
    - 使用线程安全的异步缓存防止竞态条件
    - 缓存未过期时直接返回缓存数据
    - 缓存过期时尝试获取新数据
    - 优先使用东方财富接口，失败后自动切换到同花顺接口
    - 新数据获取失败但有旧缓存时，使用旧缓存并警告

    Returns:
        ETF 实时数据 DataFrame
    """

    async def fetch_etf_data_em() -> pd.DataFrame:
        """获取东方财富ETF数据"""
        return await asyncio.to_thread(ak.fund_etf_spot_em)

    async def fetch_etf_data_ths() -> pd.DataFrame:
        """获取同花顺ETF数据并规范化格式"""
        data = await asyncio.to_thread(ak.fund_etf_spot_ths)
        return _normalize_etf_data_from_ths(data)

    async def primary_fetch() -> pd.DataFrame:
        """主数据源获取函数"""
        return await fetch_etf_data_em()

    async def fallback_fetch() -> pd.DataFrame:
        """备用数据源获取函数"""
        return await fetch_etf_data_ths()

    # 使用新的线程安全缓存，支持主备数据源切换
    cache_manager = _get_cache_manager()
    ttl_minutes = _get_config_value("fund_cache_ttl_minutes", DEFAULT_CACHE_TTL_MINUTES)

    try:
        return await cache_manager.etf_cache.get_or_update(
            key="etf_spot_data",
            fetch_func=primary_fetch,
            ttl_minutes=ttl_minutes,
            data_type="ETF(东方财富)",
        )
    except Exception as e:
        # 检查是否启用数据源切换
        if not _get_config_value("fund_enable_data_source_fallback", True):
            logger.error(f"东方财富 ETF 接口失败且数据源切换已禁用: {e}", exc_info=True)
            return None

        # 主数据源失败，尝试备用数据源
        logger.warning("东方财富 ETF 接口失败，尝试备用数据源")
        return await cache_manager.etf_cache.get_or_update(
            key="etf_spot_data_ths",
            fetch_func=fallback_fetch,
            ttl_minutes=ttl_minutes,
            data_type="ETF(同花顺备用)",
        )


async def get_lof_spot_data_cached() -> pd.DataFrame:
    """获取LOF实时数据（带线程安全缓存）

    实现了智能缓存策略：
    - 使用线程安全的异步缓存防止竞态条件
    - 缓存未过期时直接返回缓存数据
    - 缓存过期时尝试获取新数据
    - 新数据获取失败但有旧缓存时，使用旧缓存并警告

    Returns:
        LOF 实时数据 DataFrame
    """

    async def fetch_lof_data() -> pd.DataFrame:
        """获取LOF数据"""
        return await asyncio.to_thread(ak.fund_lof_spot_em)

    cache_manager = _get_cache_manager()
    ttl_minutes = _get_config_value("fund_cache_ttl_minutes", DEFAULT_CACHE_TTL_MINUTES)

    return await cache_manager.lof_cache.get_or_update(
        key="lof_spot_data", fetch_func=fetch_lof_data, ttl_minutes=ttl_minutes, data_type="LOF"
    )


async def get_fund_data(fund_code: str) -> dict:
    """获取基金数据,包括基本信息、业绩和净值信息"""
    try:
        # 获取基金基本信息
        basic_info_df = await asyncio.to_thread(
            ak.fund_individual_basic_info_xq, symbol=fund_code
        )

        if basic_info_df.empty or len(basic_info_df) == 0:
            logger.debug(f"未找到场外基金 {fund_code} 的基本信息")
            return {"success": False, "error": "未找到基金信息"}

        # 获取基金业绩数据
        achievement_df = await asyncio.to_thread(
            ak.fund_individual_achievement_xq, symbol=fund_code
        )

        # 获取基金净值数据
        nav_df = await asyncio.to_thread(
            ak.fund_open_fund_info_em, symbol=fund_code, indicator="单位净值走势"
        )

        # 检查净值数据是否有效
        if nav_df.empty or len(nav_df) == 0:
            logger.debug(f"基金 {fund_code} 净值数据为空")
            return {"success": False, "error": "净值数据不可用"}

        return {
            "basic_info": basic_info_df,
            "achievement": achievement_df,
            "nav": nav_df,
            "success": True,
        }
    except Exception as e:
        logger.error(f"获取场外基金数据失败 [{fund_code}]: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def get_market_fund_data(fund_code: str, fund_type: Literal["etf", "lof"]) -> dict:
    """获取场内基金数据（ETF/LOF通用）

    Args:
        fund_code: 基金代码
        fund_type: 基金类型（etf或lof）

    Returns:
        包含实时行情和历史数据的字典
    """
    try:
        # 根据类型获取实时数据
        if fund_type == "etf":
            spot_df = await get_etf_spot_data_cached()
            hist_func = ak.fund_etf_hist_em
        else:
            spot_df = await get_lof_spot_data_cached()
            hist_func = ak.fund_lof_hist_em

        if spot_df is None:
            return {"success": False, "error": "无法获取实时数据"}

        # 查找指定基金
        fund_info = spot_df[spot_df["代码"] == fund_code]
        if fund_info.empty:
            logger.debug(f"未找到{fund_type.upper()}基金 {fund_code}")
            return {"success": False, "error": f"未找到{fund_type.upper()}基金代码"}

        # 获取历史数据
        history_days = _get_config_value("fund_history_days", DEFAULT_HISTORY_DAYS)
        end_date = datetime.now().strftime("%Y%m%d")
        start_date = (datetime.now() - timedelta(days=history_days)).strftime("%Y%m%d")

        hist_df = await asyncio.to_thread(
            hist_func,
            symbol=fund_code,
            period="daily",
            start_date=start_date,
            end_date=end_date,
            adjust="",
        )

        if hist_df.empty or len(hist_df) < 2:
            logger.debug(f"{fund_type.upper()}基金 {fund_code} 历史数据不足")
            return {"success": False, "error": "历史数据不足"}

        return {"spot_info": fund_info.iloc[0], "hist_data": hist_df, "success": True}
    except Exception as e:
        logger.error(f"获取{fund_type.upper()}数据失败 [{fund_code}]: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def get_fund_holdings(fund_code: str) -> dict:
    """获取基金十大重仓股信息"""
    try:
        current_year = datetime.now().year
        holdings_df = pd.DataFrame()

        # 年初时最新披露季度通常仍属于上一年，当前年份为空时回退到上一年。
        for year in (current_year, current_year - 1):
            holdings_df = await asyncio.to_thread(
                ak.fund_portfolio_hold_em, symbol=fund_code, date=str(year)
            )
            if not holdings_df.empty:
                if year != current_year:
                    logger.info(f"基金 {fund_code} 持仓数据回退到 {year} 年")
                break

        return {
            "holdings": holdings_df,
            "success": True,
        }
    except Exception as e:
        logger.error(f"获取基金持仓数据失败 [{fund_code}]: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def get_stock_data(stock_code: str) -> dict:
    """获取个股数据

    Args:
        stock_code: 股票代码(如 000001.SZ 或 000001)

    Returns:
        包含股票历史数据的字典
    """
    try:
        # 处理股票代码格式
        if "." in stock_code:
            # 格式: 000001.SZ -> symbol=000001, market=sz
            code, exchange = stock_code.split(".")
            market = exchange.lower()
        else:
            # 根据代码前缀推断市场
            code = stock_code
            market = infer_stock_market(code)

        # 验证市场和代码的匹配性
        is_valid, error_msg = validate_market_code(code, market)
        if not is_valid:
            logger.info(error_msg)
            return {"success": False, "error": error_msg}

        # 获取历史数据
        history_days = _get_config_value("fund_history_days", DEFAULT_HISTORY_DAYS)
        end_date = datetime.now().strftime("%Y%m%d")
        start_date = (datetime.now() - timedelta(days=history_days)).strftime("%Y%m%d")

        hist_df = await asyncio.to_thread(
            ak.stock_zh_a_hist,
            symbol=code,
            period="daily",
            start_date=start_date,
            end_date=end_date,
            adjust="qfq",
        )

        if hist_df.empty or len(hist_df) < 2:
            logger.debug(f"股票 {stock_code} 历史数据不足")
            return {"success": False, "error": "历史数据不足"}

        return {"hist_data": hist_df, "code": code, "market": market, "success": True}
    except Exception as e:
        logger.error(f"获取股票数据失败 [{stock_code}]: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def get_stock_name(stock_code: str) -> str:
    """获取股票名称

    Args:
        stock_code: 股票代码 (如 "002170" 或 "002170.SZ")

    Returns:
        股票名称，获取失败时返回默认格式
    """
    # 输入验证
    if not stock_code or not stock_code.strip():
        logger.debug("股票代码为空")
        return "未知股票"

    try:
        # 提取纯代码部分
        code = stock_code.split(".", maxsplit=1)[0] if "." in stock_code else stock_code

        # 验证代码格式（6位数字）
        if not code.isdigit() or len(code) != 6:
            logger.debug(f"无效的股票代码格式: {stock_code}")
            return f"股票 {stock_code}"

        # 使用现代的异步API（Python 3.9+）
        stock_info_df = await asyncio.to_thread(ak.stock_individual_info_em, symbol=code)

        if not stock_info_df.empty:
            # 查找股票简称
            name_row = stock_info_df[stock_info_df["item"] == "股票简称"]
            if not name_row.empty:
                stock_name = name_row.iloc[0]["value"]
                logger.debug(f"获取股票名称成功: {code} -> {stock_name}")
                return str(stock_name)

        logger.debug(f"未找到股票 {code} 的名称信息")
        return f"股票 {stock_code}"

    except Exception as e:
        logger.debug(f"获取股票名称失败 [{stock_code}]: {e}")
        return f"股票 {stock_code}"


async def get_index_data(index_code: str) -> dict:
    """获取指数数据

    Args:
        index_code: 指数代码（如 000001.SH 或 sh000001）

    Returns:
        包含指数历史数据的字典
    """
    try:
        # 处理指数代码格式
        if "." in index_code:
            # 格式: 000001.SH -> sh000001
            code, exchange = index_code.split(".")
            market = exchange.lower()
            symbol = f"{market}{code}"
        # 推断市场
        elif is_shanghai_index(index_code):
            symbol = f"sh{index_code}"
        elif is_shenzhen_index(index_code):
            symbol = f"sz{index_code}"
        else:
            return {"success": False, "error": "无法识别的指数代码"}

        # 获取历史数据（使用 stock_zh_index_daily_em API）
        hist_df = await asyncio.to_thread(
            ak.stock_zh_index_daily_em,
            symbol=symbol,
        )

        if hist_df.empty or len(hist_df) < 2:
            logger.debug(f"指数 {index_code} 历史数据不足")
            return {"success": False, "error": "历史数据不足"}

        return {"hist_data": hist_df, "symbol": symbol, "code": index_code, "success": True}
    except Exception as e:
        logger.error(f"获取指数数据失败 [{index_code}]: {e}", exc_info=True)
        return {"success": False, "error": str(e)}

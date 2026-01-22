import asyncio
import re
from datetime import datetime, timedelta
from enum import Enum
from functools import partial
from typing import Any, Literal

import akshare as ak
import pandas as pd
from nonebot import get_plugin_config, logger, on_regex
from nonebot.adapters.onebot.v11 import Bot, Event, GroupMessageEvent, MessageEvent, MessageSegment
from nonebot.exception import MatcherException
from nonebot.plugin import PluginMetadata

from ..group_permission import create_group_rule
from .cache import FundDataCacheManager
from .config import Config
from .market_rules import (
    infer_stock_market,
    is_beijing_stock,
    is_etf,
    is_index,
    is_lof,
    is_off_market_fund,
    is_shanghai_index,
    is_shenzhen_index,
    validate_market_code,
)

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


# 默认配置常量（当 NoneBot 未初始化时使用）
DEFAULT_HISTORY_DAYS = 30
DEFAULT_DISPLAY_RECENT_DAYS = 7
DEFAULT_CACHE_TTL_MINUTES = 5


# 获取配置值的辅助函数
def _get_config_value(attr_name: str, default_value: Any) -> Any:
    """安全地获取配置值，如果 NoneBot 未初始化则使用默认值"""
    try:
        config = _get_plugin_config()
        return getattr(config, attr_name, default_value)
    except ValueError:
        # NoneBot has not been initialized
        return default_value


# 延迟初始化缓存管理器
_cached_cache_manager: FundDataCacheManager | None = None


def _get_cache_manager() -> FundDataCacheManager:
    """获取缓存管理器实例"""
    global _cached_cache_manager  # noqa: PLW0603
    if _cached_cache_manager is None:
        max_size = _get_config_value("fund_max_cache_size", 100)
        _cached_cache_manager = FundDataCacheManager(max_size=max_size)
    return _cached_cache_manager


# ==================== Rule 检查函数 ====================


# 创建群组规则检查函数
fund_group_rule = create_group_rule(
    config_getter=_get_plugin_config,
    plugin_enabled_attr="fund_plugin_enabled",
    prefix="fund_",
)


class CodeType(Enum):
    """代码类型枚举"""

    OFF_MARKET_FUND = "off_market_fund"  # 场外基金
    ETF = "etf"  # 场内ETF基金
    LOF = "lof"  # 场内LOF基金
    STOCK = "stock"  # 股票
    INDEX = "index"  # 指数
    UNKNOWN = "unknown"  # 未知类型


def _identify_with_exchange_suffix(code: str, pure_code: str, exchange: str) -> CodeType:
    """识别带交易所后缀的代码类型"""
    # 北交所股票：8位代码 + .BJ
    if exchange == "BJ":
        return CodeType.STOCK

    # 6位代码：根据交易所判断是否为指数
    if (exchange == "SH" and is_shanghai_index(pure_code)) or (
        exchange == "SZ" and is_shenzhen_index(pure_code)
    ):
        return CodeType.INDEX

    # 其他带交易所后缀的都是股票
    return CodeType.STOCK


def _identify_six_digit_code(code: str) -> CodeType:
    """识别6位数字代码类型"""
    # 按优先级检查：指数 > ETF > LOF > 场外基金
    type_checkers = [
        (is_index, CodeType.INDEX),
        (is_etf, CodeType.ETF),
        (is_lof, CodeType.LOF),
        (is_off_market_fund, CodeType.OFF_MARKET_FUND),
    ]

    for checker_func, code_type in type_checkers:
        if checker_func(code):
            return code_type

    return CodeType.UNKNOWN


def identify_code_type(code: str) -> CodeType:
    """识别代码类型

    规则说明:
    - 股票必须带交易所后缀 (.SZ/.SH/.BJ)
    - 纯6位数字按前缀分类:
      * 000/999 (上海) 或 399 (深圳): 指数
      * 51x/58x/56x/55x (上海) 或 15x (深圳): 场内ETF
      * 50x/16x: 场内LOF
      * 00-09: 场外基金 (开放式基金)
      * 其他: 未知类型，需要权威查询
    - 纯8位数字: 北交所股票（需带 .BJ 后缀）

    Args:
        code: 代码字符串

    Returns:
        代码类型枚举
    """
    # 移除可能的空格
    code = code.strip().upper()

    # 带交易所后缀的格式 (如 000001.SZ, 600000.SH, 43123456.BJ)
    if re.match(r"^\d{6,8}\.(SZ|SH|BJ)$", code):
        pure_code, exchange = code.split(".")
        return _identify_with_exchange_suffix(code, pure_code, exchange)

    # 纯8位数字可能是北交所股票（但需要后缀确认）
    if re.match(r"^\d{8}$", code):
        return CodeType.UNKNOWN

    # 纯6位数字 - 按优先级判断类型
    if re.match(r"^\d{6}$", code):
        return _identify_six_digit_code(code)

    # 未知格式
    return CodeType.UNKNOWN


fund_query = on_regex(
    r"^(\d{6}|\d{6}\.(SZ|SH)|\d{8}\.BJ)$",
    rule=fund_group_rule,
    flags=re.IGNORECASE,
)


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
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, ak.fund_etf_spot_em)

    async def fetch_etf_data_ths() -> pd.DataFrame:
        """获取同花顺ETF数据并规范化格式"""
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, ak.fund_etf_spot_ths)
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
            logger.error(f"东方财富 ETF 接口失败且数据源切换已禁用: {e}")
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
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, ak.fund_lof_spot_em)

    cache_manager = _get_cache_manager()
    ttl_minutes = _get_config_value("fund_cache_ttl_minutes", DEFAULT_CACHE_TTL_MINUTES)

    return await cache_manager.lof_cache.get_or_update(
        key="lof_spot_data", fetch_func=fetch_lof_data, ttl_minutes=ttl_minutes, data_type="LOF"
    )


async def get_fund_data(fund_code: str) -> dict:
    """获取基金数据,包括基本信息、业绩和净值信息"""
    try:
        loop = asyncio.get_event_loop()

        # 获取基金基本信息
        basic_info_df = await loop.run_in_executor(
            None, partial(ak.fund_individual_basic_info_xq, symbol=fund_code)
        )

        if basic_info_df.empty or len(basic_info_df) == 0:
            logger.warning(f"未找到场外基金 {fund_code} 的基本信息")
            return {"success": False, "error": "未找到基金信息"}

        # 获取基金业绩数据
        achievement_df = await loop.run_in_executor(
            None, partial(ak.fund_individual_achievement_xq, symbol=fund_code)
        )

        # 获取基金净值数据
        nav_df = await loop.run_in_executor(
            None, partial(ak.fund_open_fund_info_em, symbol=fund_code, indicator="单位净值走势")
        )

        # 检查净值数据是否有效
        if nav_df.empty or len(nav_df) == 0:
            logger.warning(f"基金 {fund_code} 净值数据为空")
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
            logger.warning(f"未找到{fund_type.upper()}基金 {fund_code}")
            return {"success": False, "error": f"未找到{fund_type.upper()}基金代码"}

        # 获取历史数据
        history_days = _get_config_value("fund_history_days", DEFAULT_HISTORY_DAYS)
        end_date = datetime.now().strftime("%Y%m%d")
        start_date = (datetime.now() - timedelta(days=history_days)).strftime("%Y%m%d")

        loop = asyncio.get_event_loop()
        hist_df = await loop.run_in_executor(
            None,
            partial(
                hist_func,
                symbol=fund_code,
                period="daily",
                start_date=start_date,
                end_date=end_date,
                adjust="",
            ),
        )

        if hist_df.empty or len(hist_df) < 2:
            logger.warning(f"{fund_type.upper()}基金 {fund_code} 历史数据不足")
            return {"success": False, "error": "历史数据不足"}

        return {"spot_info": fund_info.iloc[0], "hist_data": hist_df, "success": True}
    except Exception as e:
        logger.error(f"获取{fund_type.upper()}数据失败 [{fund_code}]: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def get_fund_holdings(fund_code: str) -> dict:
    """获取基金十大重仓股信息"""
    try:
        current_year = datetime.now().year

        # 获取基金持仓数据
        loop = asyncio.get_event_loop()
        holdings_df = await loop.run_in_executor(
            None, partial(ak.fund_portfolio_hold_em, symbol=fund_code, date=str(current_year))
        )

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
            logger.warning(error_msg)
            return {"success": False, "error": error_msg}

        # 获取历史数据
        history_days = _get_config_value("fund_history_days", DEFAULT_HISTORY_DAYS)
        end_date = datetime.now().strftime("%Y%m%d")
        start_date = (datetime.now() - timedelta(days=history_days)).strftime("%Y%m%d")

        loop = asyncio.get_event_loop()
        hist_df = await loop.run_in_executor(
            None,
            partial(
                ak.stock_zh_a_hist,
                symbol=code,
                period="daily",
                start_date=start_date,
                end_date=end_date,
                adjust="qfq",
            ),
        )

        if hist_df.empty or len(hist_df) < 2:
            logger.warning(f"股票 {stock_code} 历史数据不足")
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
        logger.warning("股票代码为空")
        return "未知股票"

    try:
        # 提取纯代码部分
        code = stock_code.split(".")[0] if "." in stock_code else stock_code

        # 验证代码格式（6位数字）
        if not code.isdigit() or len(code) != 6:
            logger.warning(f"无效的股票代码格式: {stock_code}")
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

        logger.warning(f"未找到股票 {code} 的名称信息")
        return f"股票 {stock_code}"

    except Exception as e:
        logger.warning(f"获取股票名称失败 [{stock_code}]: {e}")
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
        loop = asyncio.get_event_loop()
        hist_df = await loop.run_in_executor(
            None,
            partial(
                ak.stock_zh_index_daily_em,
                symbol=symbol,
            ),
        )

        if hist_df.empty or len(hist_df) < 2:
            logger.warning(f"指数 {index_code} 历史数据不足")
            return {"success": False, "error": "历史数据不足"}

        return {"hist_data": hist_df, "symbol": symbol, "code": index_code, "success": True}
    except Exception as e:
        logger.error(f"获取指数数据失败 [{index_code}]: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


def _extract_price_data(latest: pd.Series, previous: pd.Series | None) -> dict | None:
    """提取并计算价格数据

    Args:
        latest: 最新交易日数据
        previous: 前一交易日数据

    Returns:
        包含价格信息的字典，如果数据异常则返回 None
    """
    try:
        latest_price = float(latest.get("close") or 0)
        open_price = float(latest.get("open") or 0)
        high = float(latest.get("high") or 0)
        low = float(latest.get("low") or 0)

        # 计算涨跌幅和涨跌额
        if previous is not None:
            previous_close = float(previous.get("close") or 0)
            change_amount = latest_price - previous_close
            change_pct = (change_amount / previous_close * 100) if previous_close != 0 else 0
        else:
            change_amount = 0
            change_pct = 0

        return {
            "latest_price": latest_price,
            "open_price": open_price,
            "high": high,
            "low": low,
            "change_amount": change_amount,
            "change_pct": change_pct,
        }
    except (ValueError, TypeError) as e:
        logger.error(f"数据转换失败: {e}")
        return None


def _build_index_info_header(
    index_code: str, index_data: dict, price_data: dict, latest: pd.Series
) -> list[str]:
    """构建指数信息头部

    Args:
        index_code: 指数代码
        index_data: 指数数据字典
        price_data: 价格数据字典
        latest: 最新交易日数据

    Returns:
        信息行列表
    """
    # 指数名称映射
    index_names = {
        "sh000001": "上证指数",
        "sz399001": "深证成指",
        "sh000300": "沪深300",
        "sz399006": "创业板指",
        "sz399005": "中小板指",
        "sh000016": "上证50",
        "sz399102": "创业板综",
    }
    symbol = index_data.get("symbol", "")
    index_name = index_names.get(symbol, f"指数 {index_code}")

    volume = latest.get("volume", "N/A")
    amount = latest.get("amount", "N/A")
    date_str = latest.get("date", "")

    # 构建信息文本
    info_lines = [
        index_name,
        f"代码: {index_code}",
        "",
        f"最新点位: {price_data['latest_price']:.2f}",
    ]

    # 添加涨跌幅信息
    change_pct = price_data["change_pct"]
    change_amount = price_data["change_amount"]
    if change_pct > 0:
        info_lines.append(f"涨跌幅: +{change_pct:.2f}% (+{change_amount:.2f})")
    else:
        info_lines.append(f"涨跌幅: {change_pct:.2f}% ({change_amount:.2f})")

    info_lines.extend(
        [
            f"今开: {price_data['open_price']:.2f}  最高: {price_data['high']:.2f}  最低: {price_data['low']:.2f}",
            f"成交量: {volume}",
            f"成交额: {amount}",
            f"日期: {date_str}",
            "",
            "最近交易日涨跌:",
        ]
    )

    return info_lines


def _add_recent_changes(info_lines: list[str], hist_df: pd.DataFrame) -> None:
    """添加最近交易日涨跌幅信息

    Args:
        info_lines: 信息行列表（就地修改）
        hist_df: 历史数据
    """
    display_days = _get_config_value("fund_display_recent_days", DEFAULT_DISPLAY_RECENT_DAYS)
    recent_hist = hist_df.tail(display_days).iloc[::-1]

    for i, (_, row) in enumerate(recent_hist.iterrows()):
        try:
            date_str = row.get("date", "")
            close_price = float(row.get("close", 0))

            # 计算当日涨跌幅
            if i < len(recent_hist) - 1:
                prev_close = float(recent_hist.iloc[i + 1].get("close", 0))
                daily_change = (
                    (close_price - prev_close) / prev_close * 100 if prev_close != 0 else 0
                )
            else:
                daily_change = 0

            change_sign = "+" if daily_change > 0 else ""
            info_lines.append(f"{date_str}: {change_sign}{daily_change:.2f}% ({close_price:.2f})")
        except (ValueError, TypeError):
            continue


def format_index_info(index_code: str, index_data: dict) -> str:
    """格式化指数信息文本

    Args:
        index_code: 指数代码
        index_data: 指数数据字典

    Returns:
        格式化后的信息文本
    """
    try:
        hist_df = index_data["hist_data"]

        if hist_df.empty or len(hist_df) == 0:
            return f"指数 {index_code}\n暂无数据"

        # 获取最新交易日数据
        latest = hist_df.iloc[-1]
        previous = hist_df.iloc[-2] if len(hist_df) > 1 else None

        # 提取价格数据
        price_data = _extract_price_data(latest, previous)
        if price_data is None:
            return f"指数 {index_code}\n数据格式异常"

        # 构建信息头部
        info_lines = _build_index_info_header(index_code, index_data, price_data, latest)

        # 添加最近交易日涨跌幅
        _add_recent_changes(info_lines, hist_df)

        return "\n".join(info_lines)

    except Exception as e:
        logger.error(f"格式化指数信息失败 [{index_code}]: {e}", exc_info=True)
        return f"指数 {index_code}\n数据格式化失败: {e!s}"


async def format_stock_info(stock_code: str, stock_data: dict) -> str:
    """格式化股票信息文本

    Args:
        stock_code: 股票代码
        stock_data: 股票数据字典

    Returns:
        格式化后的信息文本
    """
    try:
        hist_df = stock_data["hist_data"]

        if hist_df.empty or len(hist_df) == 0:
            return f"股票 {stock_code}\n暂无数据"

        # 获取最新交易日数据
        latest = hist_df.iloc[-1]

        # 获取股票名称
        stock_name = await get_stock_name(stock_code)

        # 安全地获取数值数据
        try:
            latest_price = float(latest.get("收盘", 0))
            change_pct = float(latest.get("涨跌幅", 0))
            change_amount = float(latest.get("涨跌额", 0))
            high = float(latest.get("最高", 0))
            low = float(latest.get("最低", 0))
            open_price = float(latest.get("开盘", 0))
        except (ValueError, TypeError) as e:
            logger.error(f"股票 {stock_code} 数据转换失败: {e}")
            return f"股票 {stock_code}\n数据格式异常"

        volume = latest.get("成交量", "N/A")
        turnover = latest.get("成交额", "N/A")

        # 构建信息文本
        info_lines = [
            stock_name,
            f"代码: {stock_code}",
            "",
            f"最新价: {latest_price:.2f}",
        ]

        if change_pct > 0:
            info_lines.append(f"涨跌幅: +{change_pct:.2f}% (+{change_amount:.2f})")
        else:
            info_lines.append(f"涨跌幅: {change_pct:.2f}% ({change_amount:.2f})")

        info_lines.extend(
            [
                f"今开: {open_price:.2f}  最高: {high:.2f}  最低: {low:.2f}",
                f"成交量: {volume}",
                f"成交额: {turnover}",
                "",
                "最近交易日涨跌:",
            ]
        )

        # 添加最近交易日的涨跌幅
        display_days = _get_config_value("fund_display_recent_days", DEFAULT_DISPLAY_RECENT_DAYS)
        recent_hist = hist_df.tail(display_days).iloc[::-1]
        for _, row in recent_hist.iterrows():
            try:
                date_str = row.get("日期", "")
                daily_change = float(row.get("涨跌幅", 0))
                close_price = float(row.get("收盘", 0))

                if daily_change > 0:
                    info_lines.append(f"{date_str}: +{daily_change:.2f}% ({close_price:.2f})")
                else:
                    info_lines.append(f"{date_str}: {daily_change:.2f}% ({close_price:.2f})")
            except (ValueError, TypeError):
                continue

        return "\n".join(info_lines)

    except Exception as e:
        logger.error(f"格式化股票信息失败 [{stock_code}]: {e}", exc_info=True)
        return f"股票 {stock_code}\n数据格式化失败: {e!s}"


def format_etf_info(fund_code: str, etf_data: dict) -> str:
    """格式化场内ETF/LOF基金信息文本

    Args:
        fund_code: 基金代码
        etf_data: ETF数据字典

    Returns:
        格式化后的信息文本
    """
    try:
        spot_info = etf_data["spot_info"]
        hist_df = etf_data["hist_data"]

        # 安全地获取数据
        fund_name = spot_info.get("名称", f"基金 {fund_code}")

        try:
            latest_price = float(spot_info.get("最新价", 0))
            change_pct = float(spot_info.get("涨跌幅", 0))
            change_amount = float(spot_info.get("涨跌额", 0))
        except (ValueError, TypeError) as e:
            logger.error(f"ETF {fund_code} 数据转换失败: {e}")
            return f"基金 {fund_code}\n数据格式异常"

        volume = spot_info.get("成交量", "N/A")
        turnover = spot_info.get("成交额", "N/A")

        # 构建信息文本
        info_lines = [
            fund_name,
            f"代码: {fund_code}",
            "",
            f"最新价: {latest_price:.3f}",
        ]

        if change_pct > 0:
            info_lines.append(f"涨跌幅: +{change_pct:.2f}% (+{change_amount:.3f})")
        else:
            info_lines.append(f"涨跌幅: {change_pct:.2f}% ({change_amount:.3f})")

        info_lines.extend(
            [
                f"成交量: {volume}",
                f"成交额: {turnover}",
                "",
                "最近交易日涨跌:",
            ]
        )

        # 添加最近交易日的涨跌幅
        display_days = _get_config_value("fund_display_recent_days", DEFAULT_DISPLAY_RECENT_DAYS)
        recent_hist = hist_df.tail(display_days).iloc[::-1]
        for _, row in recent_hist.iterrows():
            try:
                date_str = row.get("日期", "")
                daily_change = float(row.get("涨跌幅", 0))
                close_price = float(row.get("收盘", 0))

                if daily_change > 0:
                    info_lines.append(f"{date_str}: +{daily_change:.2f}% ({close_price:.3f})")
                else:
                    info_lines.append(f"{date_str}: {daily_change:.2f}% ({close_price:.3f})")
            except (ValueError, TypeError):
                continue

        return "\n".join(info_lines)

    except Exception as e:
        logger.error(f"格式化ETF信息失败 [{fund_code}]: {e}", exc_info=True)
        return f"基金 {fund_code}\n数据格式化失败: {e!s}"


def format_fund_info(fund_code: str, fund_data: dict) -> str:
    """格式化基金信息文本"""
    try:
        basic_info_df = fund_data["basic_info"]
        achievement_df = fund_data["achievement"]
        nav_df = fund_data["nav"]

        # 从基本信息中获取基金名称
        fund_name_row = basic_info_df[basic_info_df["item"] == "基金名称"]
        if not fund_name_row.empty:
            fund_name = fund_name_row.iloc[0]["value"]
        else:
            fund_name = f"基金 {fund_code}"

        # 获取最近交易日的数据
        display_days = _get_config_value("fund_display_recent_days", DEFAULT_DISPLAY_RECENT_DAYS)
        recent_nav = nav_df.tail(display_days).iloc[::-1]

        # 构建信息文本
        info_lines = [
            fund_name,
            f"代码: {fund_code}",
            "",
            "最近交易日收益:",
        ]

        for _, row in recent_nav.iterrows():
            try:
                date_str = row.get("净值日期", "")
                daily_return = float(row.get("日增长率", 0))
                if daily_return > 0:
                    info_lines.append(f"{date_str}: +{daily_return:.2f}%")
                else:
                    info_lines.append(f"{date_str}: {daily_return:.2f}%")
            except (ValueError, TypeError):
                continue

        info_lines.extend(["", "阶段收益:"])

        # 添加阶段收益数据
        stage_periods = ["近1月", "近3月", "近6月", "近1年", "近3年", "近5年"]
        for period in stage_periods:
            try:
                period_data = achievement_df[achievement_df["周期"] == period]
                if not period_data.empty:
                    return_rate = float(period_data.iloc[0]["本产品区间收益"])
                    info_lines.append(f"{period}: {return_rate:.2f}%")
            except (KeyError, ValueError, IndexError) as e:
                # 如果某个周期的数据不存在或格式错误,跳过该周期
                logger.debug(f"跳过周期 {period} 的数据: {e}")
                continue

        return "\n".join(info_lines)

    except Exception as e:
        logger.error(f"格式化基金信息失败 [{fund_code}]: {e}", exc_info=True)
        return f"基金 {fund_code}\n数据格式化失败: {e!s}"


def format_fund_holdings(fund_code: str, holdings_data: dict) -> str:
    """格式化基金十大重仓股信息"""
    try:
        holdings_df = holdings_data["holdings"]

        if holdings_df.empty:
            return f"基金 {fund_code}\n暂无持仓数据"

        # 获取最新季度的数据
        unique_quarters = holdings_df["季度"].unique()
        latest_quarter = sorted(unique_quarters, reverse=True)[0]
        latest_holdings = holdings_df[holdings_df["季度"] == latest_quarter].head(10)

        info_lines = [
            f"十大重仓股 ({latest_quarter})",
            "",
        ]

        for idx, (_, row) in enumerate(latest_holdings.iterrows(), 1):
            try:
                stock_code = row.get("股票代码", "")
                stock_name = row.get("股票名称", "")
                ratio = float(row.get("占净值比例", 0))
                info_lines.append(f"{idx}. {stock_name}({stock_code}) {ratio:.2f}%")
            except (ValueError, TypeError):
                continue

        return "\n".join(info_lines)

    except Exception as e:
        logger.error(f"格式化基金持仓信息失败 [{fund_code}]: {e}", exc_info=True)
        return f"基金 {fund_code}\n持仓数据格式化失败: {e!s}"


def create_forward_nodes(
    bot: Bot,
    info_text: str,
    holdings_text: str | None = None,
) -> list[dict]:
    """创建合并转发消息节点"""
    forward_nodes = []

    # 基金基本信息节点
    text_node = {
        "type": "node",
        "data": {"name": "", "uin": bot.self_id, "content": info_text},
    }
    forward_nodes.append(text_node)

    # 十大重仓股信息节点
    if holdings_text:
        holdings_node = {
            "type": "node",
            "data": {"name": "", "uin": bot.self_id, "content": holdings_text},
        }
        forward_nodes.append(holdings_node)

    return forward_nodes


async def send_forward_message(bot: Bot, event: MessageEvent, forward_nodes: list):
    """发送合并转发消息"""
    if isinstance(event, GroupMessageEvent):
        await bot.call_api(
            "send_group_forward_msg",
            group_id=event.group_id,
            messages=forward_nodes,
        )
    else:
        await bot.call_api(
            "send_private_forward_msg",
            user_id=event.user_id,
            messages=forward_nodes,
        )


async def _query_off_market_fund(code: str) -> tuple[str | None, str | None]:
    """查询场外基金数据

    Args:
        code: 基金代码

    Returns:
        (info_text, holdings_text) 元组
    """
    if not _get_config_value("fund_enable_off_market", True):
        logger.warning(f"场外基金查询已禁用: {code}")
        return None, None

    fund_data = await get_fund_data(code)
    if not fund_data["success"]:
        logger.warning(f"场外基金数据获取失败: {code}")
        return None, None

    info_text = format_fund_info(code, fund_data)

    # 获取持仓数据
    holdings_data = await get_fund_holdings(code)
    holdings_text = None
    if holdings_data["success"]:
        holdings_text = format_fund_holdings(code, holdings_data)
    else:
        logger.warning(f"获取基金持仓数据失败: {holdings_data.get('error', '未知错误')}")

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
        logger.warning(f"{type_name}查询已禁用: {code}")
        return None, None

    fund_data = await get_market_fund_data(code, fund_type)
    if not fund_data["success"]:
        logger.warning(f"{type_name}数据获取失败: {code}")
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
        logger.warning(f"股票查询已禁用: {code}")
        return None, None

    stock_data = await get_stock_data(code)
    if not stock_data["success"]:
        logger.warning(f"股票数据获取失败: {code}")
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
        logger.warning(f"指数查询已禁用: {code}")
        return None, None

    index_data = await get_index_data(code)
    if not index_data["success"]:
        logger.warning(f"指数数据获取失败: {code}")
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
            forward_nodes = create_forward_nodes(bot, info_text, holdings_text)
            await send_forward_message(bot, event, forward_nodes)
        else:
            logger.warning(f"未能获取到数据: {code}")
            # 静默失败，不发送任何消息

    except MatcherException:
        return
    except Exception as e:
        logger.error(f"处理查询请求失败 [{code}]: {e}", exc_info=True)
        # 静默失败，不发送任何消息

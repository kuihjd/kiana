"""基金代码类型识别模块"""

import re
from enum import Enum

from .market_rules import (
    is_etf,
    is_index,
    is_lof,
    is_off_market_fund,
    is_shanghai_index,
    is_shenzhen_index,
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
    # 北交所股票：6位代码 + .BJ
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
    - 北交所股票使用 6 位代码（需带 .BJ 后缀）

    Args:
        code: 代码字符串

    Returns:
        代码类型枚举
    """
    # 移除可能的空格
    code = code.strip().upper()

    # 带交易所后缀的格式 (如 000001.SZ, 600000.SH, 920029.BJ)
    if re.match(r"^\d{6}\.(SZ|SH|BJ)$", code):
        pure_code, exchange = code.split(".")
        return _identify_with_exchange_suffix(code, pure_code, exchange)

    # 纯8位数字不属于当前支持的证券代码格式
    if re.match(r"^\d{8}$", code):
        return CodeType.UNKNOWN

    # 纯6位数字 - 按优先级判断类型
    if re.match(r"^\d{6}$", code):
        return _identify_six_digit_code(code)

    # 未知格式
    return CodeType.UNKNOWN

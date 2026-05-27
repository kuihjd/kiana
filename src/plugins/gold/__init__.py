import asyncio
import io
import json
import re
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import httpx
import matplotlib.pyplot as plt
from nonebot import get_driver, get_plugin_config, logger, on_fullmatch, on_regex, require
from nonebot.adapters.onebot.v11 import (
    Bot,
    Event,
    GroupMessageEvent,
    MessageSegment,
    PrivateMessageEvent,
)
from nonebot.params import RegexGroup
from nonebot.plugin import PluginMetadata

from src.storage import get_db

from ..group_permission import create_sub_feature_rule
from .config import Config

__plugin_meta__ = PluginMetadata(
    name="gold",
    description="实时黄金价格查询和走势图生成",
    usage=(
        "金价 - 查询当前金价\n金价走势 [时间] - 查看金价走势图\n时间格式: 1小时、24小时、7天、1月等"
    ),
    config=Config,
)

config: Config = get_plugin_config(Config)


# ==================== Rule 检查函数 ====================


# 创建金价查询和走势图的群组规则检查函数
is_price_query_enabled = create_sub_feature_rule(
    config_getter=lambda: config,
    plugin_enabled_attr="gold_plugin_enabled",
    feature_enabled_attr="gold_enable_price_query",
    prefix="gold_",
)

is_chart_enabled = create_sub_feature_rule(
    config_getter=lambda: config,
    plugin_enabled_attr="gold_plugin_enabled",
    feature_enabled_attr="gold_enable_chart",
    prefix="gold_",
)


# ==================== 事件响应器 ====================

gold = on_fullmatch("金价", rule=is_price_query_enabled)
gold_chart = on_regex(
    r"^(金价走势|金价趋势|黄金走势|黄金趋势|金价图|黄金图)\s*(.*)$",
    rule=is_chart_enabled,
    priority=5,
    block=True,
)


def get_gold_chart_failure_message() -> str:
    return "生成图表失败，请稍后重试"


class CooldownManager:
    """冷却管理器"""

    def __init__(self, cleanup_interval: int = 3600, max_age: int = 86400):
        """初始化冷却管理器

        Args:
            cleanup_interval: 清理检查间隔（秒），默认1小时
            max_age: 冷却记录最大保留时间（秒），默认24小时
        """
        self._data: dict[int, float] = {}  # group_id -> last_call_time
        self._cleanup_interval = cleanup_interval
        self._max_age = max_age
        self._last_cleanup = time.time()

    def _maybe_cleanup(self) -> None:
        """检查并执行过期记录清理"""
        current_time = time.time()
        if current_time - self._last_cleanup < self._cleanup_interval:
            return

        # 清理超过 max_age 的记录
        expired_keys = [k for k, v in self._data.items() if current_time - v > self._max_age]
        for key in expired_keys:
            del self._data[key]

        if expired_keys:
            logger.debug(f"冷却管理器清理了 {len(expired_keys)} 条过期记录")

        self._last_cleanup = current_time

    def get_remaining_cooldown(self, group_id: int, cooldown_time: int) -> int:
        """获取剩余冷却时间

        Args:
            group_id: 群组ID
            cooldown_time: 冷却时间（秒）

        Returns:
            剩余冷却秒数，0 表示不在冷却中
        """
        self._maybe_cleanup()
        current_time = time.time()
        last_call = self._data.get(group_id, 0)
        remaining = int(last_call + cooldown_time - current_time)
        return max(0, remaining)

    def update(self, group_id: int) -> None:
        """更新群组的最后调用时间"""
        self._data[group_id] = time.time()


# 使用带自动清理的冷却管理器
cooldown_manager = CooldownManager()

PRICE_HISTORY_LIMIT = max(86400, config.price_history_limit)
MIN_WINDOW_SECONDS = config.min_window_seconds
CHART_WINDOW_SECONDS = max(MIN_WINDOW_SECONDS, config.chart_window_hours * 3600)
price_history: deque[tuple[float, float]] = deque(maxlen=PRICE_HISTORY_LIMIT)

_chart_executor = ThreadPoolExecutor(max_workers=1)

scheduler = require("nonebot_plugin_apscheduler").scheduler
driver = get_driver()

db = get_db()


def _init_gold_schema() -> None:
    """初始化金价历史表 schema"""
    db.ensure_schema(
        [
            """
            CREATE TABLE IF NOT EXISTS gold_price_history (
                timestamp REAL PRIMARY KEY,
                price REAL NOT NULL
            )
            """
        ]
    )


async def load_price_history() -> None:
    """从数据库加载最近的价格历史"""
    rows = await db.fetch_all(
        """
        SELECT timestamp, price
        FROM gold_price_history
        ORDER BY timestamp DESC
        LIMIT ?
        """,
        (PRICE_HISTORY_LIMIT,),
    )

    price_history.clear()
    for row in reversed(rows):
        price_history.append((row["timestamp"], row["price"]))

    if rows:
        logger.info(f"已从数据库加载 {len(rows)} 条历史金价数据")


async def persist_price(timestamp: float, price: float) -> None:
    """写入数据库并维护内存中的价格历史"""
    if len(price_history) == PRICE_HISTORY_LIMIT:
        oldest_timestamp, _ = price_history.popleft()
        await db.execute(
            "DELETE FROM gold_price_history WHERE timestamp = ?",
            (oldest_timestamp,),
        )

    price_history.append((timestamp, price))
    await db.execute(
        """
        INSERT OR REPLACE INTO gold_price_history (timestamp, price)
        VALUES (?, ?)
        """,
        (timestamp, price),
    )


def _fetch_gold_price_sync() -> float | None:
    """同步获取金价

    Returns:
        float | None: 金价，失败时返回 None
    """
    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.post(
                config.API_URL, content=config.API_PAYLOAD, headers=config.API_HEADERS
            )
            response.raise_for_status()
            json_data = response.json()

            # 解析金价数据
            if json_data.get("success"):
                return float(json_data["data"]["FQAMBPRCZ1"]["zBuyPrc"])

            logger.warning("API 返回 success=False")
            return None

    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP 状态错误: {e}", exc_info=True)
        return None
    except httpx.RequestError as e:
        logger.error(f"HTTP 请求失败: {e}", exc_info=True)
        return None
    except (KeyError, ValueError) as e:
        logger.error(f"解析金价数据失败: {e}", exc_info=True)
        return None
    except Exception as e:
        logger.error(f"获取金价失败（未知错误）: {e}", exc_info=True)
        return None


async def fetch_gold_price() -> float | None:
    """获取金价

    使用 asyncio.to_thread 包装同步请求，避免阻塞事件循环

    Returns:
        float | None: 金价，失败时返回 None
    """
    return await asyncio.to_thread(_fetch_gold_price_sync)


@scheduler.scheduled_job("interval", seconds=config.price_fetch_interval)
async def record_price():
    """定时记录金价"""
    # 检查插件是否启用
    if not config.gold_plugin_enabled:
        return

    price = await fetch_gold_price()
    if price is None:
        return

    if price_history:
        last_price = price_history[-1][1]
        if last_price == price:
            logger.debug(f"金价未变化 ({price})，跳过记录")
            return

    current_time = time.time()
    await persist_price(current_time, price)


def _cluster_segments(
    data: list[tuple[float, float]], gap_threshold: int = 1800
) -> list[list[tuple[float, float]]]:
    """将 (timestamp, price) 数据按时间间隔聚类为交易时段。

    Args:
        data: 按时间升序排列的 (timestamp, price) 列表。
        gap_threshold: 间隔阈值（秒），超过此值认为是不同交易时段。默认 1800 (30 分钟)。

    Returns:
        交易时段列表，每个时段为一个点列表。
    """
    segments: list[list[tuple[float, float]]] = []
    if not data:
        return segments

    current: list[tuple[float, float]] = [data[0]]
    for i in range(1, len(data)):
        if data[i][0] - data[i-1][0] > gap_threshold:
            segments.append(current)
            current = []
        current.append(data[i])
    segments.append(current)
    return segments


def generate_chart(window_seconds: int | None = None) -> bytes:
    """生成金价走势图（压缩非连续时间轴）"""
    fig = None
    try:
        plt.style.use("bmh")
        fig = plt.figure(figsize=(12, 6))

        effective_window = (
            CHART_WINDOW_SECONDS
            if window_seconds is None
            else max(MIN_WINDOW_SECONDS, window_seconds)
        )
        cutoff = time.time() - effective_window
        window_data = [(t, p) for t, p in price_history if t >= cutoff]
        if len(window_data) < 2:
            window_data = list(price_history)

        # 按交易时段聚类
        segments = _cluster_segments(window_data, gap_threshold=1800)

        # 展平为连续 x 轴
        xs: list[int] = []
        ys: list[float] = []
        tick_positions: list[int] = []
        tick_labels: list[str] = []
        x = 0
        for seg in segments:
            seg_xs = list(range(x, x + len(seg)))
            seg_ys = [p for _, p in seg]
            xs.extend(seg_xs)
            ys.extend(seg_ys)

            # 记录 segment 起始时间标签
            dt = datetime.fromtimestamp(seg[0][0]).astimezone()
            tick_positions.append(seg_xs[0])
            tick_labels.append(dt.strftime("%m/%d %H:%M"))

            x += len(seg)

        # 记录最后一个 segment 的结束标签
        if segments:
            last_seg = segments[-1]
            dt = datetime.fromtimestamp(last_seg[-1][0]).astimezone()
            tick_positions.append(x - 1)
            tick_labels.append(dt.strftime("%m/%d %H:%M"))

        plt.plot(xs, ys)
        axis = plt.gca()
        axis.set_xticks(tick_positions)
        axis.set_xticklabels(tick_labels, rotation=30, ha="right")
        plt.grid(True)

        buf = io.BytesIO()
        plt.savefig(buf, format="PNG")
        buf.seek(0)
        return buf.getvalue()
    finally:
        if fig is not None:
            plt.close(fig)


# 群聊处理器（带冷却）
@gold.handle()
async def handle_group_gold_query(bot: Bot, event: GroupMessageEvent) -> None:
    """处理群聊金价查询（带冷却机制）"""
    group_id = event.group_id  # 类型安全：GroupMessageEvent 一定有 group_id

    # 检查是否在冷却时间内
    remaining_time = cooldown_manager.get_remaining_cooldown(group_id, config.cooldown_time)
    if remaining_time > 0:
        await gold.finish(f"冷却中，请等待 {remaining_time} 秒后再试")

    price = await fetch_gold_price()
    if price is not None:
        # 更新冷却时间
        cooldown_manager.update(group_id)
        await gold.finish(f"{price}")
    else:
        await gold.finish("获取金价失败")


# 私聊处理器（无冷却）
@gold.handle()
async def handle_private_gold_query(bot: Bot, event: PrivateMessageEvent) -> None:
    """处理私聊金价查询（无冷却限制）"""
    price = await fetch_gold_price()
    if price is not None:
        await gold.finish(f"{price}")
    else:
        await gold.finish("获取金价失败")


@gold_chart.handle()
async def _(bot: Bot, event: Event, matches: tuple[str, str] = RegexGroup()):
    """处理金价走势图请求"""
    if len(price_history) < 2:
        await gold_chart.finish("数据收集中，请稍后再试")
        return

    suffix = matches[1].strip() if len(matches) > 1 else ""
    custom_window: int | None = None

    if suffix:
        parsed_window = parse_window_spec(suffix)
        if parsed_window is None:
            await gold_chart.finish("我听不懂哦")
            return
        custom_window = parsed_window

    try:
        loop = asyncio.get_running_loop()
        image_data = await loop.run_in_executor(_chart_executor, generate_chart, custom_window)
        await gold_chart.send(MessageSegment.image(image_data))
    except Exception as e:
        logger.error(f"生成金价走势图失败: {e}", exc_info=True)
        await gold_chart.send(get_gold_chart_failure_message())


@driver.on_startup
async def init_gold_plugin():
    """初始化金价插件：创建表并加载历史数据"""
    _init_gold_schema()
    await load_price_history()


WINDOW_PATTERN = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>分钟|分|min|m|小时|时|h|天|日|d|周|星期|w|月)",
    re.IGNORECASE,
)


def parse_window_spec(spec: str) -> int | None:
    match = WINDOW_PATTERN.search(spec)
    if not match:
        return None

    value = float(match.group("value"))
    unit = match.group("unit").lower()

    if unit in {"分钟", "分", "min", "m"}:
        base = 60
    elif unit in {"小时", "时", "h"}:
        base = 3600
    elif unit in {"天", "日", "d"}:
        base = 86400
    elif unit in {"周", "星期", "w"}:
        base = 7 * 86400
    elif unit == "月":
        base = 30 * 86400
    else:
        return None

    seconds = int(value * base)
    if seconds <= 0:
        return None
    return max(MIN_WINDOW_SECONDS, seconds)

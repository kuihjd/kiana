"""Fund 插件缓存管理模块

提供线程安全的缓存管理，避免竞态条件
"""

import asyncio
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from nonebot import logger


class CacheEntry[T]:
    """缓存条目"""

    def __init__(self, data: T, timestamp: datetime):
        self.data = data
        self.timestamp = timestamp

    def is_expired(self, ttl_minutes: int) -> bool:
        """检查是否已过期"""
        if self.timestamp is None:
            return True
        return datetime.now() - self.timestamp > timedelta(minutes=ttl_minutes)

    @property
    def age_minutes(self) -> float:
        """获取缓存年龄（分钟）"""
        if self.timestamp is None:
            return float("inf")
        return (datetime.now() - self.timestamp).total_seconds() / 60


class SafeCache[T]:
    """线程安全的缓存类

    使用异步锁防止竞态条件
    """

    def __init__(self, max_size: int = 100):
        self._cache: OrderedDict[str, CacheEntry[T]] = OrderedDict()
        self._lock = asyncio.Lock()
        self._max_size = max_size

    async def get_or_update(
        self,
        key: str,
        fetch_func: Callable[[], Any],
        ttl_minutes: int,
        data_type: str = "data",
    ) -> T:
        """获取缓存数据或更新缓存

        Args:
            key: 缓存键
            fetch_func: 数据获取函数
            ttl_minutes: 缓存过期时间（分钟）
            data_type: 数据类型描述（用于日志）

        Returns:
            缓存的数据

        Raises:
            Exception: 数据获取失败且无可用缓存时抛出异常
        """
        async with self._lock:
            # 检查缓存
            entry = self._cache.get(key)
            if entry and not entry.is_expired(ttl_minutes):
                # 更新访问顺序（LRU）
                self._cache.move_to_end(key)
                logger.debug(f"缓存命中: {key}")
                return entry.data

            # 缓存过期或不存在，尝试获取新数据
            logger.debug(f"{data_type}缓存过期，重新获取数据 (key: {key})")
            last_error = None

            try:
                new_data = await fetch_func()
                new_entry = CacheEntry(new_data, datetime.now())
                self._cache[key] = new_entry

                # 维护缓存大小
                self._cleanup_cache()

                logger.debug(f"{data_type}数据已缓存 (key: {key})")
                return new_data

            except Exception as e:
                last_error = e
                logger.warning(f"{data_type}数据获取失败: {e}")

                # 如果有旧缓存，使用旧缓存
                if entry and entry.data is not None:
                    age = entry.age_minutes
                    logger.warning(f"使用旧缓存数据（已过期 {age:.1f} 分钟）: {key}")
                    return entry.data

                # 无缓存时抛出异常
                logger.error(f"{data_type}数据获取失败且无可用缓存: {key}")
                raise last_error from None

    async def get(self, key: str) -> T | None:
        """获取缓存数据（不更新）

        Args:
            key: 缓存键

        Returns:
            缓存数据或None
        """
        async with self._lock:
            entry = self._cache.get(key)
            if entry:
                # 更新访问顺序（LRU）
                self._cache.move_to_end(key)
                return entry.data
            return None

    async def set(self, key: str, data: T) -> None:
        """设置缓存数据

        Args:
            key: 缓存键
            data: 数据
        """
        async with self._lock:
            entry = CacheEntry(data, datetime.now())
            self._cache[key] = entry
            self._cleanup_cache()

    async def invalidate(self, key: str) -> bool:
        """使缓存失效

        Args:
            key: 缓存键

        Returns:
            是否成功删除
        """
        async with self._lock:
            if key in self._cache:
                del self._cache[key]
                logger.debug(f"缓存已失效: {key}")
                return True
            return False

    async def clear(self) -> None:
        """清空所有缓存"""
        async with self._lock:
            self._cache.clear()
            logger.info("所有缓存已清空")

    async def get_stats(self) -> dict[str, Any]:
        """获取缓存统计信息

        Returns:
            缓存统计字典
        """
        async with self._lock:
            total_entries = len(self._cache)
            if total_entries == 0:
                return {
                    "total_entries": 0,
                    "memory_usage_estimate": 0,
                    "oldest_entry_age_minutes": 0,
                    "newest_entry_age_minutes": 0,
                }

            ages = [entry.age_minutes for entry in self._cache.values()]
            memory_estimate = total_entries * 1024  # 粗略估计每条目1KB

            return {
                "total_entries": total_entries,
                "memory_usage_estimate": memory_estimate,
                "oldest_entry_age_minutes": max(ages),
                "newest_entry_age_minutes": min(ages),
                "average_age_minutes": sum(ages) / len(ages),
            }

    def _cleanup_cache(self) -> None:
        """清理缓存，维护最大大小限制（LRU策略）"""
        while len(self._cache) > self._max_size:
            # 删除最旧的条目
            oldest_key = next(iter(self._cache))
            del self._cache[oldest_key]
            logger.debug(f"缓存已满，删除最旧条目: {oldest_key}")


class FundDataCacheManager:
    """基金数据缓存管理器

    统一管理所有类型的基金数据缓存
    """

    def __init__(self, max_size: int = 100):
        self.etf_cache = SafeCache[Any](max_size)
        self.lof_cache = SafeCache[Any](max_size)
        self.fund_cache = SafeCache[Any](max_size)
        self.stock_cache = SafeCache[Any](max_size)
        self.index_cache = SafeCache[Any](max_size)

    async def get_cache_stats(self) -> dict[str, dict]:
        """获取所有缓存的统计信息"""
        return {
            "etf": await self.etf_cache.get_stats(),
            "lof": await self.lof_cache.get_stats(),
            "fund": await self.fund_cache.get_stats(),
            "stock": await self.stock_cache.get_stats(),
            "index": await self.index_cache.get_stats(),
        }

    async def clear_all_caches(self) -> None:
        """清空所有缓存"""
        await asyncio.gather(
            self.etf_cache.clear(),
            self.lof_cache.clear(),
            self.fund_cache.clear(),
            self.stock_cache.clear(),
            self.index_cache.clear(),
        )
        logger.info("所有基金数据缓存已清空")

import asyncio
from collections import defaultdict
from time import time

from nonebot import logger

from src.storage import get_db

db = get_db()

# 昵称映射缓存: {group_id: (expires_at, {nickname: user_id})}
_nickname_cache: dict[str, tuple[float, dict[str, str]]] = {}
# 集合缓存: {group_id: (expires_at, {collection_name: list[user_id]})}
_collection_cache: dict[str, tuple[float, dict[str, list[str]]]] = {}
# 分群锁，避免跨群阻塞
# 注意：锁一旦创建就不会被删除，因为清理锁会引入复杂的竞态条件问题。
# 锁对象非常轻量，且实际使用中群组数量通常是有限的，内存开销可忽略不计。
_cache_locks: dict[str, asyncio.Lock] = {}
# 全局锁，用于保护 _cache_locks 字典的创建操作，避免竞态条件
_global_lock = asyncio.Lock()
CACHE_TTL = 300
EMPTY_CACHE_TTL = 30


async def _get_group_lock(group_id: str) -> asyncio.Lock:
    """获取指定群的缓存锁。

    使用全局锁保护锁的创建，避免竞态条件下为同一 group_id 创建多个锁实例。
    锁一旦创建就会永久保留，不会被清理。
    """
    lock = _cache_locks.get(group_id)
    if lock is not None:
        return lock

    async with _global_lock:
        # 双重检查，避免重复创建
        lock = _cache_locks.get(group_id)
        if lock is None:
            lock = asyncio.Lock()
            _cache_locks[group_id] = lock
        return lock


async def invalidate_cache(group_id: str) -> None:
    """清除指定群组的昵称映射缓存

    注意：不能在获取锁之前检查缓存是否存在并提前返回，因为这会与并发的缓存写入
    产生竞态条件。必须始终获取锁，确保检查/删除相对于缓存写入操作保持原子性。
    """
    lock = await _get_group_lock(group_id)
    async with lock:
        if group_id in _nickname_cache:
            del _nickname_cache[group_id]
            logger.debug(f"已清除群组 {group_id} 的昵称缓存")
        if group_id in _collection_cache:
            del _collection_cache[group_id]
            logger.debug(f"已清除群组 {group_id} 的集合缓存")


async def _fetch_group_nickname_map(group_id: str) -> dict[str, list[str]]:
    """从数据库获取群组的昵称映射（仅供缓存内部使用）"""
    rows = await db.fetch_all(
        """
        SELECT user_id, nickname
        FROM nicknames
        WHERE group_id = ?
        """,
        (group_id,),
    )
    mapping: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        mapping[row["user_id"]].append(row["nickname"])
    return mapping


async def _fetch_all_collections_map(group_id: str) -> dict[str, list[str]]:
    """从数据库获取群组的所有集合映射（仅供缓存内部使用）"""
    rows = await db.fetch_all(
        """
        SELECT collection_name, user_id
        FROM nickname_collections
        WHERE group_id = ?
        ORDER BY collection_name
        """,
        (group_id,),
    )
    mapping: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        mapping[row["collection_name"]].append(row["user_id"])
    return mapping


async def get_cached_nickname_map(group_id: str) -> dict[str, str]:
    """获取群组的昵称映射（带缓存）"""
    # 快速路径：无锁检查缓存是否有效
    cached = _nickname_cache.get(group_id)
    if cached and time() < cached[0]:
        logger.debug(f"使用群组 {group_id} 的昵称缓存")
        return cached[1]

    lock = await _get_group_lock(group_id)
    async with lock:
        # 双重检查，避免重复回源；重新获取时间戳避免跨 await 的过期判断误差
        cached = _nickname_cache.get(group_id)
        if cached and time() < cached[0]:
            logger.debug(f"使用群组 {group_id} 的昵称缓存（锁内）")
            return cached[1]

        logger.debug(f"从数据库查询群组 {group_id} 的昵称映射")
        group_data = await _fetch_group_nickname_map(group_id)

        # 将 {user_id: [nicknames]} 转换为 {nickname: user_id}
        nickname_to_qq: dict[str, str] = {}
        for user_id, nicknames in group_data.items():
            for nickname in nicknames:
                nickname_to_qq[nickname] = user_id

        ttl = CACHE_TTL if nickname_to_qq else EMPTY_CACHE_TTL
        # DB 查询后使用新的时间戳计算过期时间，确保 TTL 准确
        _nickname_cache[group_id] = (time() + ttl, nickname_to_qq)
        logger.debug(f"已缓存群组 {group_id} 的 {len(nickname_to_qq)} 个昵称映射，TTL={ttl}s")

        return nickname_to_qq


async def get_cached_collection_map(group_id: str) -> dict[str, list[str]]:
    """获取群组的集合映射（带缓存）"""
    cached = _collection_cache.get(group_id)
    if cached and time() < cached[0]:
        logger.debug(f"使用群组 {group_id} 的集合缓存")
        return cached[1]

    lock = await _get_group_lock(group_id)
    async with lock:
        cached = _collection_cache.get(group_id)
        if cached and time() < cached[0]:
            logger.debug(f"使用群组 {group_id} 的集合缓存（锁内）")
            return cached[1]

        logger.debug(f"从数据库查询群组 {group_id} 的集合映射")
        collection_map = await _fetch_all_collections_map(group_id)

        ttl = CACHE_TTL if collection_map else EMPTY_CACHE_TTL
        _collection_cache[group_id] = (time() + ttl, collection_map)
        logger.debug(f"已缓存群组 {group_id} 的 {len(collection_map)} 个集合映射，TTL={ttl}s")

        return collection_map

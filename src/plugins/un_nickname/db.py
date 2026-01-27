import sqlite3

from nonebot import get_plugin_config

from src.storage import get_db

from .cache import invalidate_cache
from .config import Config

config: Config = get_plugin_config(Config)
db = get_db()


def ensure_schema() -> None:
    """确保数据库表结构存在"""
    db.ensure_schema(
        [
            """
            CREATE TABLE IF NOT EXISTS nicknames (
                group_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                nickname TEXT NOT NULL,
                PRIMARY KEY (group_id, user_id, nickname)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_nicknames_group_nickname
            ON nicknames (group_id, nickname)
            """,
            """
            CREATE TABLE IF NOT EXISTS nickname_collections (
                group_id TEXT NOT NULL,
                collection_name TEXT NOT NULL,
                user_id TEXT NOT NULL,
                PRIMARY KEY (group_id, collection_name, user_id)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_collections_group_name
            ON nickname_collections (group_id, collection_name)
            """,
        ]
    )


# ============== 昵称 CRUD 操作 ==============


async def nickname_occupied(group_id: str, nickname: str, user_id: str) -> str | None:
    """检查昵称是否被其他用户占用,返回占用者的 user_id,如果未被占用则返回 None"""
    row = await db.fetch_one(
        """
        SELECT user_id
        FROM nicknames
        WHERE group_id = ? AND nickname = ? AND user_id <> ?
        LIMIT 1
        """,
        (group_id, nickname, user_id),
    )
    return row["user_id"] if row else None


async def add_nickname_record(group_id: str, user_id: str, nickname: str) -> bool:
    """添加昵称记录，返回是否成功（False 表示已存在）"""
    try:
        await db.execute(
            """
            INSERT INTO nicknames (group_id, user_id, nickname)
            VALUES (?, ?, ?)
            """,
            (group_id, user_id, nickname),
        )
        await invalidate_cache(group_id)
        return True
    except sqlite3.IntegrityError:
        return False


async def fetch_user_nicknames(group_id: str, user_id: str) -> list[str]:
    """获取用户的所有昵称"""
    rows = await db.fetch_all(
        """
        SELECT nickname
        FROM nicknames
        WHERE group_id = ? AND user_id = ?
        ORDER BY nickname
        """,
        (group_id, user_id),
    )
    return [row["nickname"] for row in rows]


async def delete_single_nickname(group_id: str, user_id: str, nickname: str) -> bool:
    """删除单个昵称，返回是否成功"""
    existing = await db.fetch_one(
        """
        SELECT 1 FROM nicknames
        WHERE group_id = ? AND user_id = ? AND nickname = ?
        LIMIT 1
        """,
        (group_id, user_id, nickname),
    )
    if not existing:
        return False

    await db.execute(
        """
        DELETE FROM nicknames
        WHERE group_id = ? AND user_id = ? AND nickname = ?
        """,
        (group_id, user_id, nickname),
    )
    await invalidate_cache(group_id)
    return True


async def clear_user_nicknames(group_id: str, user_id: str) -> list[str]:
    """清空用户的所有昵称，返回被清空的昵称列表"""
    nicknames = await fetch_user_nicknames(group_id, user_id)
    if not nicknames:
        return []

    await db.execute(
        """
        DELETE FROM nicknames
        WHERE group_id = ? AND user_id = ?
        """,
        (group_id, user_id),
    )
    await invalidate_cache(group_id)
    return nicknames


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    """去重并保持原有顺序"""
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


async def delete_nicknames_from_data(
    group_id: str, at_qq: str, nicknames: list[str]
) -> tuple[list[str], list[str]]:
    """批量删除昵称，返回 (成功列表, 不存在列表)"""
    if not nicknames:
        return [], []

    # 去重并保持原有顺序，避免冗余 SQL 和重复结果
    unique_nicknames = _dedupe_preserve_order(nicknames)

    placeholders = ",".join("?" * len(unique_nicknames))
    rows = await db.fetch_all(
        f"SELECT nickname FROM nicknames "  # noqa: S608
        f"WHERE group_id = ? AND user_id = ? AND nickname IN ({placeholders})",
        (group_id, at_qq, *unique_nicknames),
    )
    existing_set = {row["nickname"] for row in rows}

    success = [n for n in unique_nicknames if n in existing_set]
    not_found = [n for n in unique_nicknames if n not in existing_set]

    if success:
        delete_placeholders = ",".join("?" * len(success))
        await db.execute(
            f"DELETE FROM nicknames "  # noqa: S608
            f"WHERE group_id = ? AND user_id = ? AND nickname IN ({delete_placeholders})",
            (group_id, at_qq, *success),
        )
        await invalidate_cache(group_id)

    return success, not_found


async def name_exists_as_nickname(group_id: str, name: str) -> bool:
    """检查名称是否已被昵称占用"""
    row = await db.fetch_one(
        "SELECT 1 FROM nicknames WHERE group_id = ? AND nickname = ? LIMIT 1",
        (group_id, name),
    )
    return row is not None


async def name_exists_as_collection(group_id: str, name: str) -> bool:
    """检查名称是否已被集合占用"""
    row = await db.fetch_one(
        "SELECT 1 FROM nickname_collections WHERE group_id = ? AND collection_name = ? LIMIT 1",
        (group_id, name),
    )
    return row is not None


# ============== 集合 CRUD 操作 ==============


async def fetch_collection_members(group_id: str, collection_name: str) -> list[str]:
    """获取指定集合的成员列表"""
    rows = await db.fetch_all(
        """
        SELECT user_id
        FROM nickname_collections
        WHERE group_id = ? AND collection_name = ?
        """,
        (group_id, collection_name),
    )
    return [row["user_id"] for row in rows]


async def fetch_all_collections_summary(group_id: str) -> list[tuple[str, int]]:
    """获取群组所有集合的名称和成员数"""
    rows = await db.fetch_all(
        """
        SELECT collection_name, COUNT(*) as member_count
        FROM nickname_collections
        WHERE group_id = ?
        GROUP BY collection_name
        ORDER BY collection_name
        """,
        (group_id,),
    )
    return [(row["collection_name"], row["member_count"]) for row in rows]


async def add_collection_members(
    group_id: str, collection_name: str, user_ids: list[str]
) -> tuple[list[str], list[str]]:
    """向集合添加成员，返回 (新增成功列表, 已存在列表)"""
    added: list[str] = []
    already_exists: list[str] = []

    for user_id in user_ids:
        try:
            await db.execute(
                """
                INSERT INTO nickname_collections (group_id, collection_name, user_id)
                VALUES (?, ?, ?)
                """,
                (group_id, collection_name, user_id),
            )
            added.append(user_id)
        except sqlite3.IntegrityError:
            already_exists.append(user_id)

    if added:
        await invalidate_cache(group_id)

    return added, already_exists


async def remove_collection_members(
    group_id: str, collection_name: str, user_ids: list[str]
) -> tuple[list[str], list[str], bool]:
    """从集合移除成员，返回 (成功移除列表, 不存在列表, 集合是否已删除)"""
    removed: list[str] = []
    not_found: list[str] = []

    for user_id in user_ids:
        existing = await db.fetch_one(
            """
            SELECT 1 FROM nickname_collections
            WHERE group_id = ? AND collection_name = ? AND user_id = ?
            LIMIT 1
            """,
            (group_id, collection_name, user_id),
        )
        if existing:
            await db.execute(
                """
                DELETE FROM nickname_collections
                WHERE group_id = ? AND collection_name = ? AND user_id = ?
                """,
                (group_id, collection_name, user_id),
            )
            removed.append(user_id)
        else:
            not_found.append(user_id)

    collection_deleted = False
    if removed:
        remaining = await db.fetch_one(
            """
            SELECT 1 FROM nickname_collections
            WHERE group_id = ? AND collection_name = ?
            LIMIT 1
            """,
            (group_id, collection_name),
        )
        if not remaining:
            collection_deleted = True
        await invalidate_cache(group_id)

    return removed, not_found, collection_deleted


async def delete_collection(group_id: str, collection_name: str) -> list[str]:
    """删除整个集合，返回被删除的成员列表"""
    members = await fetch_collection_members(group_id, collection_name)
    if not members:
        return []

    await db.execute(
        """
        DELETE FROM nickname_collections
        WHERE group_id = ? AND collection_name = ?
        """,
        (group_id, collection_name),
    )
    await invalidate_cache(group_id)
    return members


async def remove_user_from_all_collections(group_id: str, user_id: str) -> list[str]:
    """从群组所有集合中移除用户，返回受影响的集合名列表，并自动删除空集合"""
    rows = await db.fetch_all(
        """
        SELECT collection_name
        FROM nickname_collections
        WHERE group_id = ? AND user_id = ?
        """,
        (group_id, user_id),
    )
    affected_collections = [row["collection_name"] for row in rows]

    if not affected_collections:
        return []

    await db.execute(
        """
        DELETE FROM nickname_collections
        WHERE group_id = ? AND user_id = ?
        """,
        (group_id, user_id),
    )

    deleted_collections: list[str] = []
    for collection_name in affected_collections:
        remaining = await db.fetch_one(
            """
            SELECT 1 FROM nickname_collections
            WHERE group_id = ? AND collection_name = ?
            LIMIT 1
            """,
            (group_id, collection_name),
        )
        if not remaining:
            deleted_collections.append(collection_name)

    if affected_collections:
        await invalidate_cache(group_id)

    return deleted_collections


# ============== 升级操作 ==============


async def _add_to_existing_collection(
    group_id: str, collection_name: str, user_id: str
) -> tuple[bool, str]:
    """将用户添加到已存在的集合

    Returns:
        (success, message): 成功返回 (True, "已加入集合，共N人")，失败返回 (False, "错误消息")
    """
    existing_members = await fetch_collection_members(group_id, collection_name)
    member_count = len(existing_members)

    if user_id in existing_members:
        return False, "你已在集合中!"

    if member_count >= config.max_collection_members:
        return (
            False,
            f"集合成员数超过上限（最多{config.max_collection_members}人）",
        )

    await add_collection_members(group_id, collection_name, [user_id])
    members = await fetch_collection_members(group_id, collection_name)
    return True, f"已加入集合，共{len(members)}人"


async def upgrade_nickname_to_collection(
    group_id: str, nickname: str, new_user_id: str, occupied_user_id: str
) -> tuple[bool, str | None]:
    """将重复昵称升级为集合，添加两个用户并删除昵称记录

    Args:
        group_id: 群组ID
        nickname: 昵称名称（将作为集合名）
        new_user_id: 新添加昵称的用户ID
        occupied_user_id: 已占用昵称的用户ID

    Returns:
        (success, error_message): 成功时返回 (True, None)，失败时返回 (False, "错误消息")
    """
    existing_members = await fetch_collection_members(group_id, nickname)
    existing_set = set(existing_members)

    users_to_add = []
    if occupied_user_id not in existing_set:
        users_to_add.append(occupied_user_id)
    if new_user_id not in existing_set:
        users_to_add.append(new_user_id)

    total_members = len(existing_members) + len(users_to_add)
    if total_members > config.max_collection_members:
        return (
            False,
            f"集合成员数超过上限（最多{config.max_collection_members}人）",
        )

    if users_to_add:
        await add_collection_members(group_id, nickname, users_to_add)

    await delete_single_nickname(group_id, occupied_user_id, nickname)
    await delete_single_nickname(group_id, new_user_id, nickname)

    return True, None

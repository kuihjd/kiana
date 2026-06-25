"""消息图片持久化与查询的纯 IO 层。

归档时下载图片落盘并登记元数据；回放时按 file 哈希查本地文件读 bytes。
任何失败都只降级，不抛到主消息流。
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from httpx import AsyncClient
from nonebot import logger

from src.storage import get_db

db = get_db()
image_dir: Path = Path("data") / "message_archive_images"

_DOWNLOAD_TIMEOUT = 5.0  # 单图下载超时（秒）


async def _fetch_bytes(url: str) -> bytes:
    """下载图片 bytes。失败抛异常，由调用方捕获降级。"""
    async with AsyncClient(follow_redirects=True, timeout=_DOWNLOAD_TIMEOUT) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.content


async def store_image(
    url: str,
    file_hash: str,
    max_size_bytes: int,
) -> Path | None:
    """下载图片并落盘，按 file_hash 去重。

    已存在则直接返回原路径（不重复下载）。
    下载失败 / 超大 / 落盘失败均返回 None，不抛异常。
    """
    hash_lower = extract_file_hash(file_hash) if file_hash else ""
    if not hash_lower:
        logger.warning("图片归档跳过：file 字段为空")
        return None

    existing = get_image_path(hash_lower)
    if existing is not None:
        return existing

    try:
        content = await _fetch_bytes(url)
    except Exception as e:
        logger.warning(f"图片下载失败 {url}: {e}")
        return None

    if len(content) > max_size_bytes:
        logger.warning(f"图片过大跳过：{len(content)} > {max_size_bytes} bytes ({url})")
        return None

    ext = resolve_ext(url, "")
    target = _storage_path(hash_lower, ext)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    except OSError as e:
        logger.error(f"图片落盘失败 {target}: {e}", exc_info=True)
        return None

    return target


def extract_file_hash(file_field: str) -> str:
    """从 CQ image 的 file 字段提取归档哈希。

    file 字段形如 ``HASH.ext`` 或 ``{GUID}.ext``，统一取主名、去花括号、小写。
    """
    name = file_field.split("?", 1)[0].split("/", 1)[-1]
    stem = name.rsplit(".", 1)[0] if "." in name else name
    return stem.strip().lower().strip("{}")


def resolve_ext(url: str, content_type: str) -> str:
    """推断图片扩展名：url 后缀 → Content-Type → 兜底 .jpg。"""
    lower_url = url.lower().split("?", 1)[0]
    for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
        if lower_url.endswith(ext):
            return ".jpg" if ext == ".jpeg" else ext
    mapping = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/bmp": ".bmp",
    }
    return mapping.get(content_type.lower(), ".jpg")


def _storage_path(file_hash: str, ext: str) -> Path:
    """计算散列存储路径：<dir>/<hash前2位>/<hash><ext>。"""
    return image_dir / file_hash[:2] / f"{file_hash}{ext}"


def get_image_path(file_hash: str) -> Path | None:
    """查本地是否存在该哈希的图片，存在返回路径，否则 None。"""
    hash_lower = file_hash.lower()
    candidate_dir = image_dir / hash_lower[:2]
    if not candidate_dir.is_dir():
        return None
    for entry in candidate_dir.iterdir():
        if entry.stem.lower() == hash_lower:
            return entry
    return None


async def record_image_meta(file_hash: str, file_path: str, expire_at: int) -> None:
    """登记图片元数据（INSERT OR REPLACE 刷新 expire_at）。失败只 warning。"""
    try:
        await db.execute(
            """
            INSERT OR REPLACE INTO message_archive_image
                (file_hash, file_path, archived_at, expire_at)
            VALUES (?, ?, ?, ?)
            """,
            (file_hash, file_path, int(time.time()), expire_at),
        )
    except Exception as e:
        logger.warning(f"图片元数据登记失败 {file_hash}: {e}")


async def get_image_meta(file_hash: str) -> sqlite3.Row | None:
    """查询单条图片元数据。"""
    return await db.fetch_one(
        "SELECT file_hash, file_path, archived_at, expire_at "
        "FROM message_archive_image WHERE file_hash = ?",
        (file_hash,),
    )


async def purge_expired() -> int:
    """删除已过期图片文件 + 元数据。返回清理数量。失败只 warning。"""
    now = int(time.time())
    try:
        rows = await db.fetch_all(
            "SELECT file_hash, file_path FROM message_archive_image WHERE expire_at < ?",
            (now,),
        )
    except Exception as e:
        logger.warning(f"查询过期图片元数据失败: {e}")
        return 0

    removed = 0
    for row in rows:
        path = Path(row["file_path"])
        try:
            path.unlink(missing_ok=True)
        except OSError as e:
            logger.warning(f"删除过期图片失败 {path}: {e}")
        removed += 1

    if rows:
        try:
            await db.execute(
                "DELETE FROM message_archive_image WHERE expire_at < ?", (now,)
            )
        except Exception as e:
            logger.warning(f"删除过期图片元数据失败: {e}")

    return removed


async def purge_orphans() -> int:
    """删除无元数据引用的孤儿文件。返回清理数量。"""
    try:
        legit_hashes = {
            row["file_hash"].lower()
            for row in await db.fetch_all("SELECT file_hash FROM message_archive_image")
        }
    except Exception as e:
        logger.warning(f"查询图片元数据失败: {e}")
        return 0

    removed = 0
    if not image_dir.is_dir():
        return 0
    for entry in image_dir.rglob("*"):
        if not entry.is_file():
            continue
        if entry.stem.lower() not in legit_hashes:
            try:
                entry.unlink()
                removed += 1
            except OSError as e:
                logger.warning(f"删除孤儿图片失败 {entry}: {e}")
    return removed

"""共享 HTTP 客户端模块

提供复用连接的 HTTP 客户端，减少每次请求创建新连接的开销。
"""

from httpx import AsyncClient
from nonebot import get_driver, get_plugin_config

from .config import Config

_client: AsyncClient | None = None

config = get_plugin_config(Config)


async def get_client() -> AsyncClient:
    """获取共享客户端（复用连接）

    Returns:
        AsyncClient 实例

    Note:
        客户端会在应用关闭时自动清理
    """
    global _client  # noqa: PLW0603
    if _client is None or _client.is_closed:
        _client = AsyncClient(
            follow_redirects=True,
            timeout=config.HTTP_TIMEOUT,
        )
    return _client


@get_driver().on_shutdown
async def _close_client() -> None:
    """关闭共享客户端"""
    global _client  # noqa: PLW0603
    if _client is not None and not _client.is_closed:
        await _client.aclose()
        _client = None

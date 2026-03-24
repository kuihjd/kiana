"""基金插件运行时辅助模块。"""

from nonebot import get_plugin_config

from .cache import FundDataCacheManager
from .config import Config

_cached_plugin_config: Config | None = None
_cached_cache_manager: FundDataCacheManager | None = None


def get_plugin_config_cached() -> Config:
    """获取插件配置，使用缓存避免重复获取。"""
    global _cached_plugin_config  # noqa: PLW0603
    if _cached_plugin_config is None:
        _cached_plugin_config = get_plugin_config(Config)
    return _cached_plugin_config


def get_cache_manager() -> FundDataCacheManager:
    """获取缓存管理器实例。"""
    global _cached_cache_manager  # noqa: PLW0603
    if _cached_cache_manager is None:
        try:
            config = get_plugin_config_cached()
            max_size = getattr(config, "fund_max_cache_size", 100)
        except ValueError:
            max_size = 100
        _cached_cache_manager = FundDataCacheManager(max_size=max_size)
    return _cached_cache_manager

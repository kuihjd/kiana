import pytest
import pytest_asyncio
from nonebug import NONEBOT_INIT_KWARGS


def pytest_configure(config: pytest.Config) -> None:
    """配置 NoneBot 初始化参数"""
    config.stash[NONEBOT_INIT_KWARGS] = {
        "driver": "~fastapi",
    }


@pytest.fixture(scope="session", autouse=True)
async def load_plugins(_nonebot_init: None):
    """在 NoneBot 初始化后自动加载插件"""
    from nonebot import load_plugin

    load_plugin("src.plugins.fund")


@pytest.fixture(autouse=True)
def reset_global_mute_cache() -> None:
    """每个用例前重置全局禁言缓存，避免用例间状态污染。"""
    from src import plugins as global_plugins

    global_plugins._mute_cache.clear()


@pytest_asyncio.fixture
async def fund_plugin():
    """获取 fund 插件实例"""
    from nonebot import get_plugin

    plugin = get_plugin("fund")
    if plugin is None:
        pytest.skip("fund 插件未加载")

    return plugin

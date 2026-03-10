import asyncio
import contextlib
import json

import websockets
from websockets.exceptions import InvalidStatus, InvalidURI
from nonebot import get_bot, get_driver, get_plugin_config, logger
from nonebot.plugin import PluginMetadata

from .config import Config

__plugin_meta__ = PluginMetadata(
    name="gotify",
    description="将 Gotify 推送消息实时转发到 QQ",
    usage="在 .env 中配置 GOTIFY_URL、GOTIFY_CLIENT_TOKEN 和转发目标",
    config=Config,
)

config: Config = get_plugin_config(Config)
driver = get_driver()

_state: dict[str, asyncio.Task | None] = {"ws_task": None}


def _build_ws_url() -> str:
    url = config.gotify_url.rstrip("/")
    if url.startswith("https://"):
        url = "wss://" + url[len("https://"):]
    elif url.startswith("http://"):
        url = "ws://" + url[len("http://"):]
    return f"{url}/stream?token={config.gotify_client_token}"


def _format_message(data: dict) -> str:
    title = data.get("title", "")
    message = data.get("message", "")
    if title:
        return f"📨 Gotify 通知\n标题: {title}\n{message}"
    return f"📨 Gotify 通知\n{message}"


async def _forward_message(text: str) -> None:
    try:
        bot = get_bot()
    except ValueError:
        logger.warning("[Gotify] 没有可用的 Bot 实例，跳过转发")
        return

    for user_id in config.gotify_forward_users:
        try:
            await bot.send_private_msg(user_id=int(user_id), message=text)
        except Exception as e:
            logger.error(f"[Gotify] 转发到用户 {user_id} 失败: {e}")

    for group_id in config.gotify_forward_groups:
        try:
            await bot.send_group_msg(group_id=int(group_id), message=text)
        except Exception as e:
            logger.error(f"[Gotify] 转发到群 {group_id} 失败: {e}")


async def _ws_loop() -> None:
    ws_url = _build_ws_url()
    reconnect_interval = config.gotify_reconnect_interval
    consecutive_failures = 0

    while True:
        try:
            logger.info(f"[Gotify] 正在连接 WebSocket: {ws_url.split('?')[0]}")
            async with websockets.connect(ws_url) as ws:
                logger.info("[Gotify] WebSocket 连接成功")
                consecutive_failures = 0

                async for raw in ws:
                    try:
                        data = json.loads(raw)
                        text = _format_message(data)
                        await _forward_message(text)
                    except json.JSONDecodeError:
                        logger.warning(f"[Gotify] 收到非 JSON 消息: {raw!r}")
                    except Exception as e:
                        logger.error(f"[Gotify] 处理消息失败: {e}")

        except asyncio.CancelledError:
            logger.info("[Gotify] WebSocket 任务被取消")
            return
        except InvalidURI as e:
            logger.error(f"[Gotify] WebSocket 地址无效，停止重连: {e}")
            return
        except InvalidStatus as e:
            status_code = e.response.status_code
            if status_code in {401, 403, 404}:
                logger.error(
                    f"[Gotify] WebSocket 握手失败，状态码 {status_code}，请检查 gotify_url 或 gotify_client_token，停止重连"
                )
                return
            consecutive_failures += 1
            delay = min(reconnect_interval * (2 ** (consecutive_failures - 1)), 300)
            logger.warning(
                f"[Gotify] WebSocket 握手失败，状态码 {status_code}，{delay}s 后重连"
            )
            await asyncio.sleep(delay)
        except Exception as e:
            consecutive_failures += 1
            delay = min(reconnect_interval * (2 ** (consecutive_failures - 1)), 300)
            logger.warning(f"[Gotify] WebSocket 断开: {e}，{delay}s 后重连")
            await asyncio.sleep(delay)


@driver.on_startup
async def _start_gotify() -> None:
    if not config.gotify_plugin_enabled:
        return

    if not config.gotify_url or not config.gotify_client_token:
        logger.warning("[Gotify] 未配置 gotify_url 或 gotify_client_token，插件不启动")
        return

    if not config.gotify_forward_users and not config.gotify_forward_groups:
        logger.warning("[Gotify] 未配置转发目标，插件不启动")
        return

    _state["ws_task"] = asyncio.create_task(_ws_loop())
    logger.info("[Gotify] 后台任务已启动")


@driver.on_shutdown
async def _stop_gotify() -> None:
    task = _state["ws_task"]
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        logger.info("[Gotify] 后台任务已停止")
    _state["ws_task"] = None

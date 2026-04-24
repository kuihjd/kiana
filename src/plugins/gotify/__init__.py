import asyncio
import contextlib
import json

import httpx
import websockets
from nonebot import get_bot, get_driver, get_plugin_config, logger, on_command
from nonebot.permission import SUPERUSER
from nonebot.plugin import PluginMetadata
from websockets.exceptions import InvalidStatus, InvalidURI

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
        url = "wss://" + url[len("https://") :]
    elif url.startswith("http://"):
        url = "ws://" + url[len("http://") :]
    return f"{url}/stream?token={config.gotify_client_token}"


def _format_message(data: dict) -> str:
    title = data.get("title", "")
    message = data.get("message", "")

    if title:
        return f"标题: {title}\n{message}"
    return message


async def _send_to_targets(text: str, users: list[str], groups: list[str]) -> None:
    """发送消息到指定的用户和群聊"""
    try:
        bot = get_bot()
    except ValueError:
        logger.warning("[Gotify] 没有可用的 Bot 实例，跳过转发")
        return

    for user_id in users:
        try:
            await bot.send_private_msg(user_id=int(user_id), message=text)
        except Exception as e:
            logger.error(f"[Gotify] 转发到用户 {user_id} 失败: {e}")

    for group_id in groups:
        try:
            await bot.send_group_msg(group_id=int(group_id), message=text)
        except Exception as e:
            logger.error(f"[Gotify] 转发到群 {group_id} 失败: {e}")


async def _forward_message(data: dict) -> None:
    """根据 appid 规则转发消息"""
    app_id = str(data.get("appid", ""))

    target_users: list[str] = []
    target_groups: list[str] = []

    if app_id:
        for rule in config.gotify_app_rules:
            if rule.app_id == app_id:
                target_users = rule.forward_users
                target_groups = rule.forward_groups
                break

    if not target_users and not target_groups:
        target_users = config.gotify_forward_users
        target_groups = config.gotify_forward_groups

    if not target_users and not target_groups:
        logger.debug(f"[Gotify] 消息 appid={app_id} 没有匹配的转发目标，跳过")
        return

    logger.debug(f"[Gotify] 消息 appid={app_id} 将转发到用户: {target_users}, 群: {target_groups}")
    text = _format_message(data)
    await _send_to_targets(text, target_users, target_groups)


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
                        await _forward_message(data)
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
            logger.warning(f"[Gotify] WebSocket 握手失败，状态码 {status_code}，{delay}s 后重连")
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

    has_default_targets = bool(config.gotify_forward_users or config.gotify_forward_groups)
    has_app_rules = bool(config.gotify_app_rules)

    if not has_default_targets and not has_app_rules:
        logger.warning("[Gotify] 未配置任何转发目标（默认或按 appid），插件不启动")
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


gotify_list = on_command("gotify列表", permission=SUPERUSER)


@gotify_list.handle()
async def _list_applications() -> None:
    if not config.gotify_url or not config.gotify_client_token:
        await gotify_list.finish("[Gotify] 未配置 gotify_url 或 gotify_client_token")

    url = f"{config.gotify_url.rstrip('/')}/application"
    headers = {"X-Gotify-Key": config.gotify_client_token}

    try:
        async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            apps: list[dict] = response.json()
    except httpx.HTTPStatusError as e:
        if e.response.status_code in {401, 403}:
            await gotify_list.finish("[Gotify] Token 无效或权限不足")
        logger.error(f"[Gotify] 获取应用列表失败: {e}")
        await gotify_list.finish(f"[Gotify] 请求失败，状态码: {e.response.status_code}")
    except httpx.RequestError as e:
        logger.error(f"[Gotify] 无法连接到 Gotify 服务器: {e}")
        await gotify_list.finish("[Gotify] 无法连接到 Gotify 服务器，请检查地址和网络")
    except json.JSONDecodeError as e:
        logger.error(f"[Gotify] 解析应用列表响应失败: {e}")
        await gotify_list.finish("[Gotify] 服务器返回的数据格式异常")

    if not apps:
        await gotify_list.finish("[Gotify] 没有应用")

    lines = ["[Gotify 应用列表]"]
    for app in apps:
        app_id = app.get("id", "?")
        name = app.get("name", "未命名")
        description = app.get("description", "")
        last_used = app.get("lastUsed")

        lines.append(f"[{app_id}] {name}")
        lines.append(f"    描述: {description or '(无)'}")

        if last_used:
            short = last_used[:16].replace("T", " ")
            lines.append(f"    最后活跃: {short}")
        else:
            lines.append("    最后活跃: 从未活跃")

    await gotify_list.finish("\n".join(lines))

import json
import ssl

import langid
from httpx import AsyncClient, HTTPStatusError, RequestError
from nonebot import get_plugin_config, logger, on_command
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent, MessageSegment
from nonebot.params import CommandArg
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule
from nonebot.typing import T_State

from .config import Config
from .exceptions import AnimeTraceError, APIRequestError, APIResponseError, ImageDownloadError

__plugin_meta__ = PluginMetadata(
    name="nonebot-anime-trace",
    description="通过图片搜索动漫",
    usage="发送命令 '搜番' 或 '以图搜番' 并附带图片",
    config=Config,
)

config: Config = get_plugin_config(Config)


def has_image() -> Rule:
    def _has_image(event: MessageEvent) -> bool:
        if event.reply:
            # 检查回复的消息中是否包含图片
            for seg in event.reply.message:
                if seg.type == "image":
                    return True
        return False

    return Rule(_has_image)


anime_trace = on_command("搜番", aliases={"以图搜番"}, priority=1, block=True)


@anime_trace.handle()
async def handle_anime_trace(
    bot: Bot,
    event: MessageEvent,
    state: T_State,
    msg: Message = CommandArg(),
):
    # 首先检查是否是回复消息
    if event.reply:
        for seg in event.reply.message:
            if seg.type == "image":
                state["image"] = seg.data["url"]
                break

    # 如果不是回复消息，检查命令中是否包含图片
    if "image" not in state and msg["image"]:
        state["image"] = msg["image"][0].data["url"]

    # 如果没有找到图片，提示用户
    if "image" not in state:
        await anime_trace.finish("请发送图片或回复包含图片的消息")
        return

    try:
        result = await process_image(state["image"])
        await bot.send(event, result["message"])

        if result.get("video_url"):
            logger.info(f"尝试发送视频: {result['video_url']}")
            await bot.send(event, MessageSegment.video(result["video_url"]))
    except Exception as e:
        logger.error(f"处理图片时发生错误: {e}", exc_info=True)
        await bot.send(event, f"处理图片时发生错误: {e!s}")


async def process_image(image_url: str) -> dict:
    """处理图片并搜索动漫

    Args:
        image_url: 图片 URL

    Returns:
        包含 message, video_url, image_url 的字典

    Raises:
        ImageDownloadError: 图片下载失败
        APIRequestError: API 请求失败
        APIResponseError: API 响应错误
    """
    if not image_url:
        raise ImageDownloadError("无法获取图片 URL")

    try:
        # 下载图片
        image_content = await download_image(image_url)

        # 上传图片到接口
        result = await upload_image_to_api(image_content)

        # 解析结果
        return parse_api_result(result)

    except HTTPStatusError as e:
        logger.error(f"HTTP错误: {e}", exc_info=True)
        raise APIRequestError(f"API请求失败 (HTTP {e.response.status_code})") from e
    except RequestError as e:
        logger.error(f"请求错误: {e}", exc_info=True)
        raise APIRequestError("网络请求失败，请检查网络连接") from e
    except json.JSONDecodeError as e:
        logger.error(f"JSON解析错误: {e}", exc_info=True)
        raise APIResponseError("API返回的数据格式不正确") from e
    except AnimeTraceError:
        raise
    except Exception as e:
        logger.error(f"处理图片时发生未知错误: {e}", exc_info=True)
        raise APIResponseError(f"处理图片时发生未知错误: {e!s}") from e


async def download_image(image_url: str) -> bytes:
    """下载图片

    Args:
        image_url: 图片 URL

    Returns:
        图片内容的字节数据

    Raises:
        ImageDownloadError: 图片下载失败
    """
    logger.info(f"开始下载图片: {image_url}")
    try:
        # 创建自定义 SSL 上下文（支持 TLS 1.2+）
        ssl_context = ssl.create_default_context()
        ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
        async with AsyncClient(
            verify=ssl_context,
            trust_env=False,
            timeout=config.download_timeout,
        ) as client:
            response = await client.get(image_url)
            response.raise_for_status()
            return response.content
    except Exception as e:
        logger.error(f"下载图片失败: {e}", exc_info=True)
        raise ImageDownloadError(f"下载图片失败: {e!s}") from e


async def upload_image_to_api(image_content: bytes) -> dict:
    """上传图片到 trace.moe API

    Args:
        image_content: 图片内容的字节数据

    Returns:
        API 响应的 JSON 数据

    Raises:
        APIRequestError: API 请求失败
    """
    logger.info(f"开始上传图片到接口: {config.trace_moe_api_url}")

    try:
        async with AsyncClient(trust_env=False, timeout=config.api_request_timeout) as client:
            response = await client.post(
                config.trace_moe_api_url,
                headers={"User-Agent": "okhttp/4.9.3"},
                files={"image": ("image.png", image_content, "image/png")},
            )
            response.raise_for_status()

        return response.json()
    except Exception as e:
        logger.error(f"上传图片失败: {e}", exc_info=True)
        raise APIRequestError(f"上传图片失败: {e!s}") from e


def parse_api_result(result: dict) -> dict:
    """解析 API 返回结果

    Args:
        result: API 返回的 JSON 数据

    Returns:
        包含 message, video_url, image_url 的字典

    Raises:
        APIResponseError: API 响应错误
    """
    logger.info(f"接口返回结果: {result}")

    if not result.get("result"):
        raise APIResponseError("API返回结果中没有找到 'result' 字段")

    first_result = result["result"][0]
    first_anilist = first_result["anilist"]

    name = detect_simplified_chinese(first_anilist.get("synonyms", []))
    name = "、".join(name) if name else first_anilist["title"].get("native", "未知番名")

    time_string = convert_seconds_to_time(first_result.get("from", 0))

    message = "识别结果：\n"
    message += f"番名：{name}\n"
    message += f"第 {first_result.get('episode', '未知')} 集 {time_string}\n"
    message += f"置信度：{first_result['similarity'] * 100:.2f}%\n"

    if first_anilist.get("isAdult", False):
        message += "（注意：该内容可能不适合所有年龄段）"
        return {"message": message, "video_url": None, "image_url": None}

    video_url = first_result.get("video", "")
    image_url = first_result.get("image", "")

    return {"message": message, "video_url": video_url, "image_url": image_url}


def convert_seconds_to_time(seconds):
    if seconds < 60:
        return f"{seconds} 秒"
    minutes = int(seconds // 60)
    remaining_seconds = int(seconds % 60)
    return f"{minutes} 分 {remaining_seconds} 秒"


def detect_simplified_chinese(synonyms):
    simplified_chinese_synonyms = []
    for synonym in synonyms:
        lang, _confidence = langid.classify(synonym)
        if lang == "zh":
            simplified_chinese_synonyms.append(synonym)
    return simplified_chinese_synonyms

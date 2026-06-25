import re
import time
from dataclasses import dataclass
from typing import Any

from nonebot import get_plugin_config, logger, on_message
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
)
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule

from ..forward_utils import send_forward_message
from ..group_permission import create_sub_feature_rule
from ..message_archive.db import ArchivedMessage, fetch_session_messages, get_session_context
from ..message_archive.image_store import extract_file_hash, get_image_path
from .config import Config

__plugin_meta__ = PluginMetadata(
    name="chat_forward",
    description="打包当前会话消息并合并转发",
    usage=(
        "打包消息 [条数] - 打包当前会话最近n条消息（默认15条）\n"
        "打包记录 [条数] - 同上\n"
        "打包消息 @某人 [条数] - 打包指定成员最近n条发言（仅群聊）"
    ),
    config=Config,
)

config: Config = get_plugin_config(Config)

is_chat_forward_enabled = create_sub_feature_rule(
    config_getter=lambda: config,
    plugin_enabled_attr="chat_forward_plugin_enabled",
    feature_enabled_attr="chat_forward_plugin_enabled",
    prefix="chat_forward_",
)

_COMMAND_PATTERN = re.compile(r"^打包(?:消息|记录)(?:\s*(\d+))?$")


@dataclass(slots=True)
class ChatForwardRequest:
    count: int
    target_user_id: str | None = None
    error_message: str | None = None


def _normalize_command_text(message: Message) -> str:
    """提取命令纯文本并规范空白。"""
    return re.sub(r"\s+", " ", message.extract_plain_text()).strip()


def _extract_at_targets(message: Message) -> list[str]:
    """提取消息中被 @ 的用户。"""
    return [
        str(segment.data["qq"])
        for segment in message
        if segment.type == "at" and segment.data.get("qq") is not None
    ]


def parse_request(event: MessageEvent) -> ChatForwardRequest | None:
    """解析打包命令。"""
    command_text = _normalize_command_text(event.message)
    matched = _COMMAND_PATTERN.fullmatch(command_text)
    if matched is None:
        return None

    at_targets = _extract_at_targets(event.message)
    if "all" in at_targets:
        return ChatForwardRequest(
            count=config.chat_forward_default_count,
            error_message="不支持 @全体成员",
        )
    if len(at_targets) > 1:
        return ChatForwardRequest(
            count=config.chat_forward_default_count,
            error_message="一次只能指定一个用户",
        )
    if at_targets and not isinstance(event, GroupMessageEvent):
        return ChatForwardRequest(
            count=config.chat_forward_default_count,
            error_message="私聊中不能指定打包其他用户的消息",
        )

    count_str = matched.group(1)
    count = config.chat_forward_default_count if count_str is None else int(count_str)
    target_user_id = at_targets[0] if at_targets else None
    return ChatForwardRequest(count=count, target_user_id=target_user_id)


async def is_chat_forward_message(event: MessageEvent) -> bool:
    return parse_request(event) is not None


chat_forward = on_message(
    rule=Rule(is_chat_forward_enabled, is_chat_forward_message),
    priority=5,
    block=True,
)

# 冷却字典
cooldown_dict: dict[str, float] = {}


def get_cooldown_key(event: MessageEvent) -> str:
    """获取会话级冷却 key。"""
    session_type, session_id = get_session_context(event)
    return f"{session_type}:{session_id}"


def _build_face_segment(segment: MessageSegment) -> MessageSegment | None:
    face_id = segment.data.get("id")
    if face_id is None:
        return None

    try:
        return MessageSegment.face(int(str(face_id)))
    except ValueError:
        return None


async def _build_image_segment(segment: MessageSegment) -> MessageSegment | None:
    """从本地读取图片 bytes 构造图片段；未命中则降级 [图片]。"""
    file_field = str(segment.data.get("file", ""))
    file_hash = extract_file_hash(file_field)
    path = get_image_path(file_hash) if file_hash else None
    if path is None:
        return MessageSegment.text("[图片]")
    try:
        return MessageSegment.image(path.read_bytes())
    except OSError as e:
        logger.warning(f"读取本地图片失败 {path}: {e}")
        return MessageSegment.text("[图片]")


def _build_data_segment(segment_type: str, segment: MessageSegment) -> MessageSegment | None:
    data = segment.data.get("data")
    if not isinstance(data, str):
        return None

    if segment_type == "json":
        return MessageSegment.json(data)
    return MessageSegment.xml(data)


def _sanitize_unreplayable_media(segment_type: str) -> MessageSegment | None:
    """处理无法稳定回放的媒体段：视频/语音丢弃，合并转发降级为占位。"""
    if segment_type == "forward":
        return MessageSegment.text("[合并转发]")
    return None  # video / record


async def _sanitize_archived_segment(segment: MessageSegment) -> Message | MessageSegment | None:
    """清洗归档消息段，去掉会导致历史回放失败的冗余字段。"""
    segment_type = segment.type

    if segment_type == "reply":
        return None
    if segment_type == "text":
        return MessageSegment.text(segment.data.get("text", ""))
    if segment_type == "at":
        qq = segment.data.get("qq")
        return MessageSegment.at(qq) if qq is not None else None
    if segment_type == "face":
        return _build_face_segment(segment)
    if segment_type == "image":
        return await _build_image_segment(segment)
    if segment_type in {"video", "record", "forward"}:
        return _sanitize_unreplayable_media(segment_type)
    if segment_type in {"json", "xml"}:
        return _build_data_segment(segment_type, segment)
    return segment


async def build_forward_content(message: ArchivedMessage) -> Message:
    """根据归档内容重建可稳定发送的消息。"""
    content = Message()
    for segment in Message(message.message_cq):
        sanitized = await _sanitize_archived_segment(segment)
        if sanitized is None:
            continue
        if isinstance(sanitized, Message):
            content.extend(sanitized)
        else:
            content.append(sanitized)

    if content:
        return content
    if message.plain_text:
        return Message(message.plain_text)
    return Message("[暂不支持回放的消息]")


async def build_forward_nodes(messages: list[ArchivedMessage]) -> list[dict[str, Any]]:
    """构建合并转发节点。

    使用归档内容重建转发节点，确保重启后仍可回放历史消息。
    """
    nodes: list[dict[str, Any]] = []
    for message in messages:
        if not message.message_cq:
            continue
        nodes.append(
            {
                "type": "node",
                "data": {
                    "name": message.sender_name,
                    "uin": message.user_id,
                    "content": await build_forward_content(message),
                },
            }
        )
    return nodes


async def _parse_or_finish(event: MessageEvent) -> ChatForwardRequest:
    """解析命令并在错误时结束 matcher。"""
    request = parse_request(event)
    if request is None:
        await chat_forward.finish()
    if request.error_message is not None:
        await chat_forward.finish(request.error_message)
    return request


async def _check_cooldown_or_finish(event: MessageEvent) -> None:
    """检查当前会话冷却。"""
    cooldown_key = get_cooldown_key(event)
    current_time = time.time()

    if cooldown_key not in cooldown_dict:
        return

    elapsed = current_time - cooldown_dict[cooldown_key]
    if elapsed < config.chat_forward_cooldown:
        remaining = int(config.chat_forward_cooldown - elapsed)
        await chat_forward.finish(f"冷却中，请等待 {remaining} 秒")


def _validate_count_or_raise(count: int) -> None:
    """校验打包条数。"""
    if count > config.chat_forward_max_count:
        raise ValueError(f"最多只能打包 {config.chat_forward_max_count} 条消息")
    if count < 1:
        raise ValueError("条数必须大于 0")


def _get_storage_failure_message() -> str:
    return "获取消息记录失败，请稍后重试"


def _get_send_failure_message() -> str:
    return "发送合并转发失败，请稍后重试"


@chat_forward.handle()
async def handle_chat_forward(bot: Bot, event: MessageEvent) -> None:
    request = await _parse_or_finish(event)
    await _check_cooldown_or_finish(event)

    try:
        _validate_count_or_raise(request.count)
    except ValueError as e:
        await chat_forward.finish(str(e))

    # 更新冷却时间
    cooldown_dict[get_cooldown_key(event)] = time.time()

    session_type, session_id = get_session_context(event)
    try:
        messages = await fetch_session_messages(
            session_type=session_type,
            session_id=session_id,
            limit=request.count,
            exclude_message_id=event.message_id,
            target_user_id=request.target_user_id,
        )
    except Exception as e:
        logger.error(f"获取消息记录失败: {e}", exc_info=True)
        await chat_forward.finish(_get_storage_failure_message())

    if not messages:
        await chat_forward.finish("没有获取到消息记录")

    forward_nodes = await build_forward_nodes(messages)
    if not forward_nodes:
        await chat_forward.finish("没有可打包的消息")

    try:
        await send_forward_message(bot, event, forward_nodes)
    except Exception as e:
        logger.error(f"发送合并转发失败: {e}", exc_info=True)
        await chat_forward.finish(_get_send_failure_message())

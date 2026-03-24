from __future__ import annotations

import json
import re
from typing import Literal

import httpx
from nonebot import logger
from pydantic import BaseModel, Field, ValidationError, field_validator


class SentimentAIError(Exception):
    """AI 分析失败基类。"""


class SentimentAITimeoutError(SentimentAIError):
    """AI 请求超时。"""


class SentimentAIAuthError(SentimentAIError):
    """AI 鉴权失败。"""


class SentimentAIServiceError(SentimentAIError):
    """AI 服务异常。"""


class SentimentAIResponseError(SentimentAIError):
    """AI 返回格式异常。"""


class SentimentAnalysisResult(BaseModel):
    score: int = Field(ge=0, le=100)
    label: Literal["极度悲观", "偏悲观", "中性", "偏乐观", "极度乐观"]
    confidence: float = Field(ge=0, le=1)
    summary: str
    reasons: list[str]
    compare_to_history: str

    @field_validator("summary", "compare_to_history")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("字段不能为空")
        return normalized

    @field_validator("reasons")
    @classmethod
    def validate_reasons(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value if item.strip()]
        if len(normalized) < 2 or len(normalized) > 4:
            raise ValueError("reasons 长度必须在 2 到 4 之间")
        return normalized


def build_chat_completions_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/chat/completions"


def build_prompt(prompt_payload: str) -> str:
    return (
        "请你根据给定的群聊数据评估群内 A 股情绪，只能基于群聊内容本身判断，不要引入外部市场数据。"
        "输出必须是严格 JSON 对象，不要输出 Markdown，不要补充解释。\n"
        'JSON 字段固定为: {"score":0-100整数,"label":"极度悲观|偏悲观|中性|偏乐观|极度乐观",'
        '"confidence":0-1小数,"summary":"一句总评","reasons":["原因1","原因2"],'
        '"compare_to_history":"相对近5日基线的简短描述"}。\n'
        "请重点关注聊天里的看多/看空措辞、追涨杀跌、连板/炸板、仓位变化、亏钱效应/赚钱效应、"
        "以及是否出现明显的 FOMO、恐慌或冷淡。\n"
        f"群聊数据如下：\n{prompt_payload}"
    )


def extract_json_text(content: str) -> str:
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or start > end:
        raise SentimentAIResponseError("模型输出中没有 JSON 对象")
    return stripped[start : end + 1]


def extract_response_content(payload: dict) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise SentimentAIResponseError("响应中缺少 choices")

    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        raise SentimentAIResponseError("响应中的 choice 格式不正确")

    message = first_choice.get("message")
    if not isinstance(message, dict):
        raise SentimentAIResponseError("响应中缺少 message")

    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts = [
            item["text"]
            for item in content
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)
        ]
        if text_parts:
            return "".join(text_parts)

    raise SentimentAIResponseError("响应中缺少可解析的 content")


def parse_analysis_result(raw_content: str) -> SentimentAnalysisResult:
    try:
        parsed = json.loads(extract_json_text(raw_content))
    except json.JSONDecodeError as e:
        raise SentimentAIResponseError("模型输出不是合法 JSON") from e

    try:
        return SentimentAnalysisResult.model_validate(parsed)
    except ValidationError as e:
        raise SentimentAIResponseError("模型输出字段不完整或格式不正确") from e


async def request_sentiment_analysis(
    *,
    base_url: str,
    api_key: str,
    model: str,
    timeout_seconds: float,
    temperature: float,
    prompt_payload: str,
) -> SentimentAnalysisResult:
    request_url = build_chat_completions_url(base_url)
    request_body = {
        "model": model,
        "temperature": temperature,
        "messages": [
            {
                "role": "system",
                "content": "你是审慎的 A 股群聊情绪分析助手，只返回严格 JSON。",
            },
            {
                "role": "user",
                "content": build_prompt(prompt_payload),
            },
        ],
    }

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds, trust_env=False) as client:
            response = await client.post(
                request_url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=request_body,
            )
            response.raise_for_status()
    except httpx.TimeoutException as e:
        raise SentimentAITimeoutError("AI 请求超时") from e
    except httpx.HTTPStatusError as e:
        status_code = e.response.status_code
        if status_code in {401, 403}:
            raise SentimentAIAuthError("AI 鉴权失败") from e
        raise SentimentAIServiceError(f"AI 服务返回 HTTP {status_code}") from e
    except httpx.RequestError as e:
        raise SentimentAIServiceError("AI 请求失败") from e

    try:
        payload = response.json()
    except json.JSONDecodeError as e:
        raise SentimentAIResponseError("AI 接口返回的不是合法 JSON") from e

    if not isinstance(payload, dict):
        raise SentimentAIResponseError("AI 接口响应格式不正确")

    content = extract_response_content(payload)
    logger.debug(f"[A股情绪] AI 原始输出: {content}")
    return parse_analysis_result(content)

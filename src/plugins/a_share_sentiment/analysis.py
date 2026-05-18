from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from ..message_archive.db import ArchivedMessage

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
MAX_LINE_CHARS = 180
CODE_PATTERN = re.compile(r"(?<!\d)\d{6}(?:\.(?:SZ|SH|BJ))?(?!\d)", re.IGNORECASE)
A_SHARE_KEYWORDS = (
    "A股",
    "大盘",
    "沪指",
    "深成指",
    "创业板",
    "北证",
    "科创板",
    "涨停",
    "跌停",
    "反弹",
    "跳水",
    "抄底",
    "追高",
    "红盘",
    "绿盘",
    "缩量",
    "放量",
    "主力",
    "游资",
    "牛市",
    "熊市",
    "龙头",
    "高标",
    "连板",
    "炸板",
    "回封",
    "题材",
    "板块",
    "仓位",
    "加仓",
    "减仓",
    "止盈",
    "止损",
    "踏空",
    "吃面",
    "赚钱效应",
    "亏钱效应",
)


@dataclass(slots=True)
class PromptMessage:
    event_time: int
    sender_name: str
    text: str
    matched_keywords: tuple[str, ...]
    matched_codes: tuple[str, ...]


@dataclass(slots=True)
class DayAnalysis:
    date_label: str
    total_messages: int
    active_users: int
    keyword_messages: int
    code_messages: int
    average_length: float
    top_keywords: list[str]
    sampled_messages: list[str]


@dataclass(slots=True)
class DayWindow:
    date_label: str
    start_time: int
    end_time: int


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def trim_message_text(text: str, limit: int = MAX_LINE_CHARS) -> str:
    normalized = normalize_text(text)
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: limit - 3]}..."


def extract_keywords(text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    matched_keywords = tuple(keyword for keyword in A_SHARE_KEYWORDS if keyword in text)
    matched_codes = tuple(CODE_PATTERN.findall(text))
    return matched_keywords, matched_codes


def get_today_window(reference_timestamp: int) -> DayWindow:
    reference_dt = datetime.fromtimestamp(reference_timestamp, SHANGHAI_TZ)
    day_start = datetime.combine(reference_dt.date(), time.min, SHANGHAI_TZ)
    return DayWindow(
        date_label=reference_dt.strftime("%Y-%m-%d"),
        start_time=int(day_start.timestamp()),
        end_time=reference_timestamp + 1,
    )


def get_history_day_windows(reference_timestamp: int, history_days: int) -> list[DayWindow]:
    reference_dt = datetime.fromtimestamp(reference_timestamp, SHANGHAI_TZ)
    today_start = datetime.combine(reference_dt.date(), time.min, SHANGHAI_TZ)

    windows: list[DayWindow] = []
    for offset in range(history_days, 0, -1):
        day_start = today_start - timedelta(days=offset)
        day_end = day_start + timedelta(days=1)
        windows.append(
            DayWindow(
                date_label=day_start.strftime("%Y-%m-%d"),
                start_time=int(day_start.timestamp()),
                end_time=int(day_end.timestamp()),
            )
        )
    return windows


def pick_evenly[T](items: Sequence[T], count: int) -> list[T]:
    if count <= 0 or not items:
        return []
    if count >= len(items):
        return list(items)
    if count == 1:
        return [items[len(items) // 2]]

    indexes = []
    last_index = len(items) - 1
    for index in range(count):
        candidate = round(index * last_index / (count - 1))
        if candidate not in indexes:
            indexes.append(candidate)

    next_index = 0
    while len(indexes) < count:
        if next_index not in indexes:
            indexes.append(next_index)
        next_index += 1

    return [items[index] for index in sorted(indexes)]


def format_sample_line(message: PromptMessage) -> str:
    event_dt = datetime.fromtimestamp(message.event_time, SHANGHAI_TZ)
    return f"{event_dt.strftime('%H:%M')} {message.sender_name}: {trim_message_text(message.text)}"


def select_sample_messages(
    messages: Sequence[PromptMessage],
    max_messages: int,
    char_budget: int,
) -> list[str]:
    prioritized = [
        message for message in messages if message.matched_keywords or message.matched_codes
    ]
    regular = [
        message
        for message in messages
        if not message.matched_keywords and not message.matched_codes
    ]

    selected_lines: list[str] = []
    used_chars = 0

    def try_append(message: PromptMessage) -> bool:
        nonlocal used_chars

        line = format_sample_line(message)
        additional_chars = len(line) + (1 if selected_lines else 0)
        if selected_lines and used_chars + additional_chars > char_budget:
            return False
        if not selected_lines and len(line) > char_budget:
            return False
        selected_lines.append(line)
        used_chars += additional_chars
        return True

    for message in prioritized:
        if len(selected_lines) >= max_messages:
            break
        if not try_append(message):
            break

    remaining_slots = max_messages - len(selected_lines)
    if remaining_slots <= 0:
        return selected_lines

    sampled_regular = pick_evenly(regular, remaining_slots)
    for message in sampled_regular:
        if len(selected_lines) >= max_messages:
            break
        try_append(message)

    return selected_lines


def build_day_analysis(
    messages: Sequence[ArchivedMessage],
    date_label: str,
    max_messages: int,
    char_budget: int,
) -> DayAnalysis:
    prompt_messages: list[PromptMessage] = []
    keyword_counter: Counter[str] = Counter()
    active_users: set[str] = set()
    keyword_messages = 0
    code_messages = 0
    text_lengths: list[int] = []

    for message in messages:
        text = normalize_text(message.plain_text)
        if not text:
            continue

        matched_keywords, matched_codes = extract_keywords(text)
        if matched_keywords:
            keyword_counter.update(matched_keywords)
            keyword_messages += 1
        if matched_codes:
            code_messages += 1

        prompt_messages.append(
            PromptMessage(
                event_time=message.event_time,
                sender_name=normalize_text(message.sender_name) or message.user_id,
                text=text,
                matched_keywords=matched_keywords,
                matched_codes=matched_codes,
            )
        )
        active_users.add(message.user_id)
        text_lengths.append(len(text))

    average_length = 0.0
    if text_lengths:
        average_length = round(sum(text_lengths) / len(text_lengths), 1)

    return DayAnalysis(
        date_label=date_label,
        total_messages=len(prompt_messages),
        active_users=len(active_users),
        keyword_messages=keyword_messages,
        code_messages=code_messages,
        average_length=average_length,
        top_keywords=[keyword for keyword, _ in keyword_counter.most_common(8)],
        sampled_messages=select_sample_messages(prompt_messages, max_messages, char_budget),
    )


def build_prompt_payload(
    today_analysis: DayAnalysis,
    history_analyses: Sequence[DayAnalysis],
    *,
    sample_insufficient: bool,
) -> str:
    payload = {
        "market": "A股",
        "today": {
            "date": today_analysis.date_label,
            "message_count": today_analysis.total_messages,
            "active_users": today_analysis.active_users,
            "keyword_message_count": today_analysis.keyword_messages,
            "code_message_count": today_analysis.code_messages,
            "average_message_length": today_analysis.average_length,
            "top_keywords": today_analysis.top_keywords,
            "sample_messages": today_analysis.sampled_messages,
            "sample_insufficient": sample_insufficient,
        },
        "history": [
            {
                "date": history_analysis.date_label,
                "message_count": history_analysis.total_messages,
                "active_users": history_analysis.active_users,
                "keyword_message_count": history_analysis.keyword_messages,
                "code_message_count": history_analysis.code_messages,
                "average_message_length": history_analysis.average_length,
                "top_keywords": history_analysis.top_keywords,
                "sample_messages": history_analysis.sampled_messages,
            }
            for history_analysis in history_analyses
        ],
    }
    return json.dumps(payload, ensure_ascii=False)

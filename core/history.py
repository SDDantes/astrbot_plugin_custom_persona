from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from .models import ChatHistoryConfig


def content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "text":
                parts.append(str(item.get("text", "")))
            elif item_type == "image_url":
                image = item.get("image_url") or {}
                if isinstance(image, dict):
                    ref = image.get("url") or image.get("id") or ""
                else:
                    ref = str(image)
                parts.append(f"[Image: {ref}]")
            elif item_type == "audio_url":
                parts.append("[Audio]")
            elif item_type == "think":
                continue
        return "\n".join(part for part in parts if part)
    try:
        return json.dumps(content, ensure_ascii=False)
    except TypeError:
        return str(content)


def strip_tool_records(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cleaned = []
    for item in history:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").lower()
        if role == "tool":
            continue
        if role == "assistant" and item.get("tool_calls"):
            continue
        if role not in {"user", "assistant"}:
            continue
        if not content_to_text(item.get("content")).strip():
            continue
        if item.get("role") != role:
            item = {**item, "role": role}
        cleaned.append(item)
    return cleaned


def latest_user_assistant_messages(
    history: list[dict[str, Any]], max_turns: int
) -> list[dict[str, Any]]:
    cleaned = strip_tool_records(history)
    if max_turns <= 0:
        return []
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    pending_user: dict[str, Any] | None = None
    for item in cleaned:
        role = item.get("role")
        if role == "user":
            pending_user = item
        elif role == "assistant" and pending_user is not None:
            pairs.append((pending_user, item))
            pending_user = None
    selected = pairs[-max_turns:]
    return [message for pair in selected for message in pair]


def render_chat_history_text(
    history: list[dict[str, Any]],
    config: ChatHistoryConfig,
    *,
    use_preset_if_empty: bool,
) -> tuple[str, bool]:
    messages = latest_user_assistant_messages(history, config.max_turns)
    if not messages:
        preset = config.preset_dialogs.strip()
        if preset and use_preset_if_empty:
            return _truncate_history_text(preset, config.max_tokens), True
        return "", False

    lines: list[str] = []
    for item in messages:
        lines.append(_format_history_item(item, config))
    return _truncate_history_text("\n".join(lines), config.max_tokens), False


def render_ledger_history_text(
    history: list[dict[str, Any]],
    config: ChatHistoryConfig,
    *,
    max_messages: int,
) -> tuple[str, bool]:
    messages = strip_tool_records(history)
    if max_messages > 0:
        messages = messages[-max_messages:]
    if not messages:
        return "", False
    return _truncate_history_text(
        "\n".join(_format_history_item(item, config) for item in messages),
        config.max_tokens,
    ), False


def combine_history_texts(parts: list[str], max_tokens: int) -> str:
    text = "\n\n".join(part.strip() for part in parts if part and part.strip())
    return _truncate_history_text(text, max_tokens)


def _format_history_item(item: dict[str, Any], config: ChatHistoryConfig) -> str:
    role = str(item.get("role") or "")
    sender_name = (
        item.get("sender_name")
        or item.get("user_name")
        or ("User" if role == "user" else "Assistant")
    )
    timestamp = item.get("timestamp") or item.get("created_at") or ""
    if not timestamp:
        timestamp = ""
    elif isinstance(timestamp, (int, float)):
        timestamp = datetime.fromtimestamp(float(timestamp)).isoformat(timespec="seconds")
    content = content_to_text(item.get("content")).strip()
    try:
        return config.format_template.format(
            sender_name=sender_name,
            timestamp=timestamp,
            content=content,
            role=role,
        )
    except Exception:
        return f"[{sender_name}/{timestamp}]: {content}"


def _truncate_history_text(text: str, max_tokens: int) -> str:
    if max_tokens <= 0:
        return text
    # AstrBot 的精确 tokenizer 取决于具体提供方；每 token 四个字符
    # 是一个保守的粗略上界，使此格式化辅助函数无外部依赖。
    max_chars = max_tokens * 4
    if len(text) <= max_chars:
        return text
    return "...[truncated]\n" + text[-max_chars:]


def exclude_l2_overlap(
    history: list[dict[str, Any]], l2_messages: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """移除 L1 历史中已存在于 L2 的条目，避免重复。

    从两个列表的尾部开始按 (role, content) 进行匹配，确保 LLM 不会
    在渲染的聊天记录和内存 L2 上下文窗口中看到相同的轮次。
    """
    overlap = [
        (item.get("role"), content_to_text(item.get("content")).strip())
        for item in l2_messages
        if item.get("role") in {"user", "assistant"} and not item.get("tool_calls")
    ]
    if not overlap:
        return history
    remaining = overlap[:]
    kept: list[dict[str, Any]] = []
    for item in reversed(history):
        role = item.get("role")
        text = content_to_text(item.get("content")).strip()
        if remaining and (role, text) == remaining[-1]:
            remaining.pop()
            continue
        kept.append(item)
    kept.reverse()
    return kept

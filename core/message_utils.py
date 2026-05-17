"""插件内共享的消息序列化辅助函数。"""

from __future__ import annotations

import copy
import json
from typing import Any

from .history import content_to_text


def build_current_user_message(req: Any) -> dict[str, Any]:
    """从 *req*（ProviderRequest）构建 OpenAI 格式的用户消息。"""
    parts: list[dict[str, Any]] = []
    prompt = getattr(req, "prompt", None)
    if prompt and str(prompt).strip():
        parts.append({"type": "text", "text": str(prompt)})
    for part in getattr(req, "extra_user_content_parts", None) or []:
        try:
            dumped = part.model_dump_for_context()
        except Exception:
            dumped = {"type": "text", "text": str(part)}
        if dumped.get("type") == "text":
            parts.append(dumped)
    for image_url in getattr(req, "image_urls", None) or []:
        parts.append({"type": "text", "text": f"[Image Attachment: {image_url}]"})
    for audio_url in getattr(req, "audio_urls", None) or []:
        parts.append({"type": "text", "text": f"[Audio Attachment: {audio_url}]"})
    if not parts:
        return {"role": "user", "content": ""}
    if len(parts) == 1 and parts[0].get("type") == "text":
        return {"role": "user", "content": parts[0].get("text", "")}
    return {"role": "user", "content": parts}


def db_user_message(
    user_message: dict[str, Any], sender_name: str, timestamp: float | None = None
) -> dict[str, Any]:
    """将 *user_message* 扁平化以供数据库存储。"""
    import time as _time

    message = copy.deepcopy(user_message)
    message["content"] = content_to_text(message.get("content"))
    message["sender_name"] = sender_name
    message["timestamp"] = timestamp if timestamp is not None else _time.time()
    return message


def event_content_parts(event: Any) -> list[dict[str, Any]]:
    """从 AstrMessageEvent 中提取内容片段。"""
    from astrbot.core.message.components import Plain as _Plain

    parts: list[dict[str, Any]] = []
    for comp in event.get_messages():
        comp_type = comp.__class__.__name__
        if isinstance(comp, _Plain):
            parts.append({"type": "text", "text": comp.text})
        elif comp_type == "Image":
            ref = getattr(comp, "url", "") or getattr(comp, "file", "") or "[image]"
            parts.append({"type": "image_url", "image_url": {"url": str(ref)}})
        else:
            parts.append({"type": "text", "text": f"[{comp_type}]"})
    if not parts and event.message_str:
        parts.append({"type": "text", "text": event.message_str})
    return parts


def l2_messages_from_run_context(run_context: Any) -> list[dict[str, Any]]:
    """从 agent 运行上下文中提取并清洗 L2 消息。"""
    raw_messages = getattr(run_context, "messages", None) or []
    messages = [_message_to_context_dict(m) for m in raw_messages]
    body = _strip_runtime_preamble(messages)
    return [m for m in body if _keep_l2_message(m)]


def diff_l2(stored: list[dict[str, Any]], agent_body: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """返回 *agent_body* 中有而 *stored* 中无的消息。

    执行前缀对齐的差分：并行遍历 *stored* 和 *agent_body*，
    按 (role, content, timestamp) 比较，返回 *agent_body* 中
    从第一个不匹配处开始的后缀。这避免了重新追加已在之前
    agent 步骤中保存到 L2 的消息。
    """
    if not stored:
        return agent_body
    match_idx = 0
    for idx, (s_msg, a_msg) in enumerate(zip(stored, agent_body, strict=False)):
        if _msg_key(s_msg) == _msg_key(a_msg):
            match_idx = idx + 1
        else:
            break
    return agent_body[match_idx:]


def _msg_key(message: dict[str, Any]) -> tuple:
    ts = message.get("timestamp", 0)
    return (
        str(message.get("role") or "").lower(),
        content_to_text(message.get("content")).strip(),
        round(float(ts), 1) if ts else 0,
    )


def _message_to_context_dict(message: Any) -> dict[str, Any]:
    if isinstance(message, dict):
        data = copy.deepcopy(message)
    elif hasattr(message, "model_dump"):
        data = message.model_dump()
    else:
        data = {
            "role": getattr(message, "role", ""),
            "content": getattr(message, "content", ""),
        }
        tool_calls = getattr(message, "tool_calls", None)
        if tool_calls is not None:
            data["tool_calls"] = tool_calls
        tool_call_id = getattr(message, "tool_call_id", None)
        if tool_call_id is not None:
            data["tool_call_id"] = tool_call_id
    if getattr(message, "_no_save", False):
        data["_no_save"] = True
    return data


def _strip_runtime_preamble(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """剥离前导 system 消息及连续的 ``_no_save`` preamble。"""
    if not messages:
        return []
    idx = 0
    if messages[0].get("role") == "system":
        idx = 1
    while idx < len(messages) and messages[idx].get("_no_save"):
        idx += 1
    while idx < len(messages) and messages[idx].get("role") not in {"user", "assistant", "tool"}:
        idx += 1
    return messages[idx:]


def _keep_l2_message(message: dict[str, Any]) -> bool:
    role = message.get("role")
    if role not in {"user", "assistant", "tool"}:
        return False
    if role == "assistant" and message.get("tool_calls"):
        return True
    if role == "tool":
        return True
    return bool(content_to_text(message.get("content")).strip())


def history_from_request(req: Any) -> list[dict[str, Any]]:
    """从 ProviderRequest 的 conversation 字段加载对话历史。"""
    conversation = getattr(req, "conversation", None)
    if not conversation:
        return []
    try:
        history = json.loads(conversation.history or "[]")
        if isinstance(history, list):
            return [item for item in history if isinstance(item, dict)]
    except Exception:
        return []
    return []

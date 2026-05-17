from __future__ import annotations

import asyncio
import copy
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .models import DialogueWindowConfig

logger = logging.getLogger("astrbot_plugin_custom_persona")

TTL_SECONDS = 86400  # 24 小时无活动触发清理


@dataclass(slots=True)
class PendingTurn:
    conversation_id: str
    user_message: dict[str, Any]
    persona_name: str
    streaming: bool


@dataclass(slots=True)
class SessionState:
    persona_name: str = ""
    l1_text: str = ""
    l1_loaded: bool = False
    l1_from_preset: bool = False
    l1_needs_reload: bool = False
    l2_messages: list[dict[str, Any]] = field(default_factory=list)
    pending_turn: PendingTurn | None = None
    updated_at: float = field(default_factory=time.time)
    needs_compression: bool = False
    pending_compressed_l2: list[dict[str, Any]] | None = None
    """来自后台任务的压缩结果，待被换入。"""


class SessionStateManager:
    """按会话管理状态，含异步安全锁与 TTL 淘汰。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._states: dict[str, SessionState] = {}

    # ── 内部方法（调用方必须持有锁）──────────────────────

    def _get_locked(self, session_id: str) -> SessionState:
        """返回或创建 *session_id* 的状态。调用方必须持有 ``_lock``。"""
        state = self._states.get(session_id)
        if state is None:
            state = SessionState()
            self._states[session_id] = state
        elif (
            time.time() - state.updated_at > TTL_SECONDS
            and not state.pending_turn
            and not state.l2_messages
            and state.pending_compressed_l2 is None
        ):
            logger.debug(
                "CustomPersona: evicting stale session %s (idle %.0fs)",
                session_id,
                time.time() - state.updated_at,
            )
            state = SessionState()
            self._states[session_id] = state
        return state

    # ── 公开读取方法 ────────────────────────────────────────────────

    async def get(self, session_id: str) -> SessionState:
        async with self._lock:
            return self._get_locked(session_id)

    async def contexts_for_request(self, session_id: str) -> list[dict[str, Any]]:
        async with self._lock:
            state = self._states.get(session_id)
            if state is None:
                return []
            return copy.deepcopy(state.l2_messages)

    async def needs_compression(self, session_id: str) -> bool:
        async with self._lock:
            state = self._states.get(session_id)
            return state.needs_compression if state else False

    # ── 公开写入方法 ───────────────────────────────────────────────

    async def reset_if_persona_changed(self, session_id: str, persona_name: str) -> SessionState:
        async with self._lock:
            state = self._get_locked(session_id)
            if state.persona_name and state.persona_name != persona_name:
                state.l1_text = ""
                state.l1_loaded = False
                state.l1_from_preset = False
                state.l1_needs_reload = False
                state.l2_messages = []
                state.pending_turn = None
                state.needs_compression = False
                state.pending_compressed_l2 = None
            state.persona_name = persona_name
            state.updated_at = time.time()
            return state

    async def set_l1(self, session_id: str, text: str, *, from_preset: bool) -> None:
        async with self._lock:
            state = self._get_locked(session_id)
            state.l1_text = text
            state.l1_loaded = True
            state.l1_from_preset = from_preset
            state.l1_needs_reload = False
            state.updated_at = time.time()

    async def set_l2_needs_reload(self, session_id: str) -> None:
        async with self._lock:
            state = self._get_locked(session_id)
            state.l1_needs_reload = True
            state.updated_at = time.time()

    async def set_pending(self, session_id: str, pending: PendingTurn) -> None:
        async with self._lock:
            state = self._get_locked(session_id)
            state.pending_turn = pending
            state.updated_at = time.time()

    async def consume_pending(self, session_id: str) -> PendingTurn | None:
        async with self._lock:
            state = self._get_locked(session_id)
            pending = state.pending_turn
            state.pending_turn = None
            state.updated_at = time.time()
            return pending

    async def append_l2(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        window: DialogueWindowConfig,
    ) -> bool:
        """向 L2 窗口追加新消息，必要时触发滑动。

        若发生滑动（需重新加载 L1）则返回 True。
        """
        async with self._lock:
            state = self._get_locked(session_id)
            state.l2_messages.extend(copy.deepcopy(messages))
            state.l2_messages = self._ensure_starts_with_user(state.l2_messages)
            slid = False
            if len(state.l2_messages) >= window.max_messages:
                state.l2_messages = self._slide_messages(
                    state.l2_messages,
                    keep_messages=window.keep_messages,
                )
                state.l1_needs_reload = True
                slid = True
            state.updated_at = time.time()
            return slid

    async def replace_l2(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
    ) -> None:
        """替换整个 L2 窗口（压缩后使用）。"""
        async with self._lock:
            state = self._get_locked(session_id)
            state.l2_messages = self._ensure_starts_with_user(copy.deepcopy(messages))
            state.updated_at = time.time()

    async def set_needs_compression(self, session_id: str, value: bool) -> None:
        async with self._lock:
            state = self._get_locked(session_id)
            state.needs_compression = value
            state.updated_at = time.time()

    async def store_compressed_l2(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        """存储预计算的压缩 L2（来自后台任务）。"""
        async with self._lock:
            state = self._get_locked(session_id)
            state.pending_compressed_l2 = copy.deepcopy(messages)
            state.updated_at = time.time()

    async def take_compressed_l2(self, session_id: str) -> list[dict[str, Any]] | None:
        """取走待应用的压缩 L2（若存在）。"""
        async with self._lock:
            state = self._states.get(session_id)
            if state is None or state.pending_compressed_l2 is None:
                return None
            result = state.pending_compressed_l2
            state.pending_compressed_l2 = None
            return result

    async def clear_session(self, session_id: str) -> None:
        """清除 *session_id* 的全部状态（在 /reset 或 /new 时调用）。"""
        async with self._lock:
            self._states.pop(session_id, None)

    async def cleanup_stale(self) -> int:
        """移除超过 TTL 的空闲会话。返回移除数量。"""
        async with self._lock:
            now = time.time()
            stale = [sid for sid, s in self._states.items() if now - s.updated_at > TTL_SECONDS]
            for sid in stale:
                del self._states[sid]
            if stale:
                logger.info("CustomPersona: cleaned up %d stale sessions", len(stale))
            return len(stale)

    async def clear(self) -> None:
        async with self._lock:
            self._states.clear()

    # ── 静态辅助方法 ────────────────────────────────────────────

    @staticmethod
    def _slide_messages(
        messages: list[dict[str, Any]], *, keep_messages: int
    ) -> list[dict[str, Any]]:
        if len(messages) <= keep_messages:
            return SessionStateManager._ensure_starts_with_user(messages)
        cutoff = max(0, len(messages) - keep_messages)
        while cutoff > 0 and messages[cutoff].get("role") != "user":
            cutoff -= 1
        trimmed = messages[cutoff:]
        return SessionStateManager._ensure_starts_with_user(trimmed)

    @staticmethod
    def _ensure_starts_with_user(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """去除前导非 user 消息，确保列表以 user 轮次开头。

        LLM 提供方要求对话以 user 消息起始；此方法丢弃前导的
        assistant/system/tool 条目。
        """
        if not messages or messages[0].get("role") == "user":
            return messages
        for idx, item in enumerate(messages):
            if item.get("role") == "user":
                return messages[idx:]
        return []

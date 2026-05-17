"""astrbot_plugin_custom_persona —— 完全由 Persona 驱动的 LLM 请求定制插件。"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import FunctionTool, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register

from .core.compression import CompressionHandler
from .core.history import (
    content_to_text,
    exclude_l2_overlap,
    render_ledger_history_text,
)
from .core.ledger import ConversationLedger
from .core.message_utils import (
    build_current_user_message,
    event_content_parts,
)
from .core.models import PersonaConfig, PluginConfig
from .core.persona_store import PersonaStore
from .core.renderer import PreambleRenderer
from .core.response import ResponseHandler
from .core.state import PendingTurn, SessionStateManager
from .core.template_vars import TemplateVariableBuilder
from .core.web_api import WebApiController

PLUGIN_NAME = "astrbot_plugin_custom_persona"


@register(
    PLUGIN_NAME,
    "SDDantes",
    "自定义人格 Preamble、模型路由与输出后处理",
    "0.1.0",
)
class CustomPersonaPlugin(Star):
    """插件协调器。

    在此处承担的职责（跨模块协调）：
    * 生命周期管理（初始化 / 终止）
    * 收消息时写入账本记录
    * ``on_llm_request`` —— 核心请求重写钩子
    * L1 历史加载与渲染
    """

    # ── 生命周期 ────────────────────────────────────────────

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.context = context
        self.config = PluginConfig.from_dict(config or {})
        self.plugin_dir = Path(__file__).resolve().parent
        self.data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self.data_dir.mkdir(parents=True, exist_ok=True)

        personas_dir = (
            Path(self.config.personas_dir).expanduser()
            if self.config.personas_dir
            else self.data_dir / "personas"
        )
        self.persona_store = PersonaStore(
            personas_dir=personas_dir,
            bundled_dir=self.plugin_dir / "personas",
        )
        self.renderer = PreambleRenderer()
        self.states = SessionStateManager()
        self.ledger = ConversationLedger(
            self.data_dir / "conversation_ledger.sqlite3",
            per_chat_limit=self.config.ledger.per_chat_limit,
        )
        self.compressor = CompressionHandler()

        # ── 子处理器 ──────────────────────────────────────
        self.var_builder = TemplateVariableBuilder(
            config=self.config,
            context=self.context,
            persona_store=self.persona_store,
            data_dir=self.data_dir,
        )
        self.response_handler = ResponseHandler(
            config=self.config,
            states=self.states,
            context=self.context,
            ledger=self.ledger,
            persona_store=self.persona_store,
            compressor=self.compressor,
            var_builder=self.var_builder,
        )
        self.web_api = WebApiController(
            persona_store=self.persona_store,
            renderer=self.renderer,
            var_builder=self.var_builder,
        )
        self.web_api.register(self.context)

        logger.info("CustomPersona: 插件已初始化")

    async def terminate(self) -> None:
        self.ledger.close()
        logger.info("CustomPersona: 插件已终止")

    # ── 账本记录 ──────────────────────────────────────

    @filter.event_message_type(filter.EventMessageType.ALL, priority=9999)
    async def record_message(self, event: AstrMessageEvent) -> None:
        if not self.config.enabled:
            return
        try:
            self.ledger.record(
                session_id=event.unified_msg_origin,
                role="user",
                user_id=event.get_sender_id(),
                user_name=event.get_sender_name(),
                content=event_content_parts(event),
                ts=getattr(event, "created_at", time.time()),
            )
        except Exception:
            logger.warning("CustomPersona: 写入账本消息失败", exc_info=True)

    # ── 上下文重置检测 ───────────────────────────────

    @filter.on_decorating_result(priority=-9999)
    async def _on_reset_check(self, event: AstrMessageEvent) -> None:
        """检测 AstrBot 内置的 /reset 与 /new 命令。

        这些命令会在事件上设置 ``_clean_ltm_session`` 标记。我们据此清除
        插件自身的会话状态，并软删除会话账本中的记录。
        """
        if not event.get_extra("_clean_ltm_session"):
            return
        sid = event.unified_msg_origin
        logger.info(
            "CustomPersona[%s]: 检测到对话重置 —— 正在清除状态",
            sid,
        )
        await self.states.clear_session(sid)
        deleted = self.ledger.soft_delete_session(sid)
        if deleted:
            logger.info(
                "CustomPersona[%s]: 已软删除 %d 条账本消息",
                sid,
                deleted,
            )

    # ── 核心请求钩子 ─────────────────────────────────────

    @filter.on_llm_request(priority=9999)
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        if not self.config.enabled:
            return

        persona = self.persona_store.resolve(event.unified_msg_origin)
        if persona is None:
            return

        await self.states.reset_if_persona_changed(event.unified_msg_origin, persona.name)

        # L1 历史
        await self._ensure_l1_history(event, req, persona)

        # 预计算的压缩 L2
        compressed = await self.states.take_compressed_l2(event.unified_msg_origin)
        if compressed is not None:
            await self.states.replace_l2(event.unified_msg_origin, compressed)

        # 将 ConversationLedger 中的群聊上下文注入到用户提示词中，
        # 使 LLM 能看到两次触发之间的所有消息（含发送者和时间戳）。
        group_context = self._format_ledger_context(event)
        if group_context:
            req.prompt = f"{group_context}\n\n[触发消息]\n{req.prompt or ''}"

        # 模板变量 + 渲染
        state = await self.states.get(event.unified_msg_origin)
        variables = self.var_builder.build(
            event,
            req,
            persona,
            state.l1_text,
        )
        rendered = self.renderer.render(persona, variables)

        # 路由
        if self.config.routing.mode == "simple" and self.config.routing.simple_model_id:
            req.model = self.config.routing.simple_model_id

        # 为下游钩子设置元数据
        conversation_id = getattr(req.conversation, "cid", "") or ""
        event.set_extra("custom_persona_active", persona.name)
        event.set_extra("custom_persona_streaming", bool(variables["streaming"]))
        event.set_extra(
            "custom_persona_no_response_mark",
            persona.no_response_mark or self.config.no_response.mark,
        )
        event.set_extra("custom_persona_conversation_id", conversation_id)

        # 覆写请求
        req.contexts = [
            *rendered.contexts,
            *(await self.states.contexts_for_request(event.unified_msg_origin)),
        ]
        req.system_prompt = rendered.system_prompt
        req.conversation = None

        await self.states.set_pending(
            event.unified_msg_origin,
            PendingTurn(
                conversation_id=conversation_id,
                user_message=build_current_user_message(req),
                persona_name=persona.name,
                streaming=bool(variables["streaming"]),
            ),
        )
        logger.debug(
            "CustomPersona[%s]: 已渲染 persona=%s segments=%d l2=%d",
            event.unified_msg_origin,
            persona.name,
            len(rendered.rendered_segments),
            len(req.contexts) - len(rendered.contexts),
        )

    # ── 响应 / agent_done / 分段回复 ───────────────

    @filter.on_llm_response(priority=-100)
    async def on_llm_response(self, event: AstrMessageEvent, response: LLMResponse | None) -> None:
        await self.response_handler.on_llm_response(event, response)

    @filter.on_agent_done(priority=-100)
    async def sync_l2_from_agent_done(
        self, event: AstrMessageEvent, run_context: Any, response: LLMResponse | None
    ) -> None:
        await self.response_handler.sync_l2_from_agent_done(event, run_context, response)

    @filter.on_decorating_result(priority=100)
    async def segmented_reply(self, event: AstrMessageEvent) -> None:
        await self.response_handler.segmented_reply(event)

    @filter.on_using_llm_tool(priority=9999)
    async def _on_tool_message_markers(
        self, event: AstrMessageEvent, tool: FunctionTool, tool_args: dict | None
    ) -> None:
        """在工具执行前拦截 ``send_message_to_user``，处理消息中的分段/T2I/TTS 标记。"""
        if not self.config.enabled:
            return
        if not tool_args or not isinstance(tool_args, dict):
            return
        persona = self.persona_store.resolve(event.unified_msg_origin)
        if persona is None or not persona.segmented_reply.enabled:
            return
        if tool.name != "send_message_to_user":
            return
        messages = tool_args.get("messages")
        if not messages or not isinstance(messages, list):
            return
        await self.response_handler.process_tool_messages(messages, event, persona)

    # ── L1 历史管理 ─────────────────────────────────

    async def _ensure_l1_history(
        self, event: AstrMessageEvent, _req: ProviderRequest, persona: PersonaConfig
    ) -> None:
        state = await self.states.get(event.unified_msg_origin)
        if state.l1_loaded and not state.l1_needs_reload and state.persona_name == persona.name:
            return
        # L1 完全基于 ConversationLedger 构建。
        ledger_history = self._l1_history_from_ledger(event, persona)
        state_l2 = await self.states.contexts_for_request(event.unified_msg_origin)
        if state_l2:
            ledger_history = exclude_l2_overlap(ledger_history, state_l2)
        text, from_preset = render_ledger_history_text(
            ledger_history,
            persona.chat_history,
            max_messages=max(1, persona.chat_history.max_turns * 2),
        )
        if not text and persona.chat_history.preset_dialogs.strip():
            text = persona.chat_history.preset_dialogs.strip()
            from_preset = True
        await self.states.set_l1(
            event.unified_msg_origin,
            text,
            from_preset=from_preset,
        )

    # ── 群聊上下文格式化器 ──────────────────────────

    def _format_ledger_context(self, event: AstrMessageEvent) -> str:
        """将 ConversationLedger 中的最近消息格式化为带时间戳的群聊日志。

        返回如下格式的多行文本：
            [2026-05-12 19:10:01] SenderName: 消息内容
            [2026-05-12 19:10:08] OtherUser: 更多内容
        """
        from zoneinfo import ZoneInfo

        try:
            tz = ZoneInfo(self.config.timezone)
        except Exception:
            tz = ZoneInfo("UTC")

        raw = self.ledger.recent(event.unified_msg_origin, limit=40)
        if not raw:
            return ""

        # 只取用户消息，不含机器人自己发出的。
        lines: list[str] = []
        for item in reversed(raw):  # 按时间正序排列
            if item.get("role") != "user":
                continue
            ts = item.get("timestamp")
            if ts:
                try:
                    dt = datetime.fromtimestamp(float(ts), tz=tz)
                    ts_str = dt.strftime("%Y-%m-%d %H:%M:%S")
                except (OSError, ValueError, TypeError):
                    ts_str = str(ts)
            else:
                ts_str = "?"
            name = item.get("user_name") or item.get("user_id") or "unknown"
            content = content_to_text(item.get("content")).strip()
            if content:
                lines.append(f"[{ts_str}] {name}: {content}")

        if not lines:
            return ""
        return "\n".join(lines)

    # ── L1 账本加载 ─────────────────────────────────────

    def _l1_history_from_ledger(
        self,
        event: AstrMessageEvent,
        persona: PersonaConfig,
    ) -> list[dict[str, Any]]:
        limit = max(1, self.config.ledger.per_chat_limit)
        window = (
            persona.dialogue_window
            if persona.dialogue_window.explicit
            else self.config.dialogue_window
        )
        raw_messages = self.ledger.recent(
            event.unified_msg_origin,
            limit=min(limit, max(20, window.max_messages, persona.chat_history.max_turns * 2)),
        )
        if raw_messages and raw_messages[-1].get("role") == "user":
            raw_messages = raw_messages[:-1]

        messages: list[dict[str, Any]] = []
        for item in raw_messages:
            messages.append(
                {
                    "role": item.get("role"),
                    "content": item.get("content"),
                    "sender_name": item.get("user_name") or item.get("sender_name"),
                    "user_id": item.get("user_id", ""),
                    "timestamp": item.get("timestamp"),
                }
            )
        return messages

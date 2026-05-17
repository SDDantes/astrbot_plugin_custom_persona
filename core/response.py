"""响应后处理：NO_RESPONSE 拦截、持久化、分段回复、L2 维护。"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

from astrbot.api import html_renderer
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.provider import LLMResponse
from astrbot.core import file_token_service
from astrbot.core.message.components import Image, Plain, Record

from .compression import CompressionHandler
from .message_utils import (
    db_user_message,
    diff_l2,
    l2_messages_from_run_context,
)
from .models import DialogueWindowConfig, PersonaConfig

logger = logging.getLogger("astrbot_plugin_custom_persona")

DEFAULT_MAX_CONTEXT_TOKENS = 64000


class ResponseHandler:
    """处理 LLM 响应与 agent-done 后处理。"""

    def __init__(
        self,
        *,
        config: Any,
        states: Any,
        context: Any,
        ledger: Any,
        persona_store: Any,
        compressor: CompressionHandler,
        var_builder: Any,  # TemplateVariableBuilder
    ) -> None:
        self._config = config
        self._states = states
        self._context = context
        self._ledger = ledger
        self._persona_store = persona_store
        self._compressor = compressor
        self._var_builder = var_builder

    # ── NO_RESPONSE 拦截 + 持久化 ─────────────────────────────

    async def on_llm_response(self, event: AstrMessageEvent, response: LLMResponse | None) -> None:
        """处理 LLM 响应：NO_RESPONSE 拦截与数据库持久化。"""
        persona_name = event.get_extra("custom_persona_active")
        if not persona_name or response is None:
            return

        mark = str(event.get_extra("custom_persona_no_response_mark") or "")
        streaming = bool(event.get_extra("custom_persona_streaming"))
        completion_text = response.completion_text or ""
        if (
            self._config.no_response.enabled
            and not streaming
            and mark
            and completion_text.strip() == mark
        ):
            response.completion_text = ""
            event.clear_result()
            event.stop_event()
            event.set_extra("custom_persona_skip_l2_sync", True)
            await self._states.consume_pending(event.unified_msg_origin)
            logger.info(
                "CustomPersona[%s]: NO_RESPONSE intercepted",
                event.unified_msg_origin,
            )
            return

        pending = await self._states.consume_pending(event.unified_msg_origin)
        if pending is None:
            return
        if response.role != "assistant":
            event.set_extra("custom_persona_skip_l2_sync", True)
            return

        assistant_message = {"role": "assistant", "content": completion_text}

        if pending.conversation_id and completion_text.strip():
            try:
                await self._context.conversation_manager.add_message_pair(
                    pending.conversation_id,
                    db_user_message(
                        pending.user_message,
                        event.get_sender_name(),
                    ),
                    assistant_message,
                )
            except Exception:
                logger.warning(
                    "CustomPersona[%s]: failed to persist message pair",
                    event.unified_msg_origin,
                    exc_info=True,
                )

        try:
            self._ledger.record(
                session_id=event.unified_msg_origin,
                role="assistant",
                content=completion_text,
            )
        except Exception:
            logger.debug("CustomPersona: assistant ledger record failed", exc_info=True)

    # ── 分段回复 ───────────────────────────────────────

    async def segmented_reply(self, event: AstrMessageEvent) -> None:
        """Persona 级别的分段回复。"""
        persona_name = event.get_extra("custom_persona_active")
        if not persona_name:
            return
        persona = self._persona_store.resolve(event.unified_msg_origin)
        if not persona or not persona.segmented_reply.enabled:
            return
        result = event.get_result()
        if result is None or not result.chain:
            return
        mark = persona.segmented_reply.segment_mark
        if not mark:
            return

        # 注意：T2I/TTS 有意保持启用 —— Persona 作者可按需
        # 通过响应文本中的特殊标记来控制。
        text_parts: list[str] = []
        has_non_plain = False
        for comp in result.chain:
            if isinstance(comp, Plain):
                text_parts.append(comp.text)
            else:
                has_non_plain = True
        if has_non_plain:
            logger.debug(
                "CustomPersona[%s]: segmented reply skipped — "
                "result chain contains non-Plain components",
                event.unified_msg_origin,
            )
            return
        text = "".join(text_parts)
        t2i_trigger = persona.segmented_reply.t2i_trigger
        tts_trigger = persona.segmented_reply.tts_trigger
        if mark not in text:
            await self._handle_standalone_trigger(
                text,
                t2i_trigger,
                tts_trigger,
                event,
                persona,
            )
            return
        segments = [part.strip() for part in text.split(mark) if part.strip()]
        if not segments:
            event.clear_result()
            event.stop_event()
            return
        for idx, segment in enumerate(segments):
            if idx > 0:
                await asyncio.sleep(
                    random.uniform(
                        persona.segmented_reply.interval_min,
                        persona.segmented_reply.interval_max,
                    )
                )
            if t2i_trigger and segment.startswith(t2i_trigger):
                text = segment[len(t2i_trigger) :].strip()
                if not text:
                    continue
                chain = await self._render_segment_t2i(text, event, persona)
            elif tts_trigger and segment.startswith(tts_trigger):
                text = segment[len(tts_trigger) :].strip()
                if not text:
                    continue
                chain = await self._render_segment_tts(text, event, persona)
            else:
                chain = [Plain(segment)]
            await event.send(MessageChain(chain))
        event.clear_result()
        event.stop_event()

    # ── 逐段 T2I / TTS ─────────────────────────────────

    async def _render_segment_t2i(
        self, text: str, event: AstrMessageEvent, persona: PersonaConfig
    ) -> list:
        try:
            cfg = self._context.get_config(umo=event.unified_msg_origin)
        except Exception:
            return [Plain(text)]
        if not cfg.get("t2i"):
            return [Plain(text)]
        try:
            template_name = persona.segmented_reply.t2i_template or cfg.get(
                "t2i_active_template", "base"
            )
            use_network = cfg.get("t2i_strategy", "remote") == "remote"
            url = await html_renderer.render_t2i(
                text,
                return_url=True,
                use_network=use_network,
                template_name=template_name,
            )
            if url:
                if url.startswith("http"):
                    image = Image.fromURL(url)
                elif cfg.get("t2i_use_file_service") and cfg.get("callback_api_base"):
                    token = await file_token_service.register_file(url)
                    url = f"{cfg['callback_api_base']}/api/file/{token}"
                    image = Image.fromURL(url)
                else:
                    image = Image.fromFileSystem(url)
                chain: list = [image]
                if persona.segmented_reply.t2i_dual_output:
                    chain.append(Plain(text))
                return chain
        except Exception:
            logger.warning(
                "CustomPersona[%s]: T2I failed for segment, falling back to text",
                event.unified_msg_origin,
                exc_info=True,
            )
        return [Plain(text)]

    async def _render_segment_tts(
        self, text: str, event: AstrMessageEvent, persona: PersonaConfig
    ) -> list:
        try:
            cfg = self._context.get_config(umo=event.unified_msg_origin)
        except Exception:
            return [Plain(text)]
        tts_cfg = cfg.get("provider_tts_settings", {})
        if not tts_cfg.get("enable"):
            return [Plain(text)]
        try:
            tts_provider = self._context.get_using_tts_provider(umo=event.unified_msg_origin)
            if not tts_provider:
                return [Plain(text)]
            audio_path = await tts_provider.get_audio(text)
            if not audio_path:
                return [Plain(text)]
            url = audio_path
            if tts_cfg.get("use_file_service") and cfg.get("callback_api_base"):
                token = await file_token_service.register_file(audio_path)
                url = f"{cfg['callback_api_base']}/api/file/{token}"
            record = Record(file=url, url=url, text=text)
            chain: list = [record]
            if persona.segmented_reply.tts_dual_output:
                chain.append(Plain(text))
            return chain
        except Exception:
            logger.warning(
                "CustomPersona[%s]: TTS failed for segment, falling back to text",
                event.unified_msg_origin,
                exc_info=True,
            )
        return [Plain(text)]

    # ── 独立触发器（无 segment_mark 时）──────────────────

    async def _handle_standalone_trigger(
        self,
        text: str,
        t2i_trigger: str,
        tts_trigger: str,
        event: AstrMessageEvent,
        persona: PersonaConfig,
    ) -> None:
        """处理没有 ``segment_mark`` 时的 T2I/TTS 触发。"""
        if t2i_trigger and text.startswith(t2i_trigger):
            content = text[len(t2i_trigger) :].strip()
            if content:
                chain = await self._render_segment_t2i(content, event, persona)
                await event.send(MessageChain(chain))
            event.clear_result()
            event.stop_event()
        elif tts_trigger and text.startswith(tts_trigger):
            content = text[len(tts_trigger) :].strip()
            if content:
                chain = await self._render_segment_tts(content, event, persona)
                await event.send(MessageChain(chain))
            event.clear_result()
            event.stop_event()

    # ── L2 维护 ────────────────────────────────────────

    async def sync_l2_from_agent_done(
        self,
        event: AstrMessageEvent,
        run_context: Any,
        response: LLMResponse | None,
    ) -> None:
        """向 L2 追加新消息，并按需触发后台压缩。"""
        if not event.get_extra("custom_persona_active"):
            return
        if event.get_extra("custom_persona_skip_l2_sync"):
            return
        if response is not None and response.role not in {"assistant", ""}:
            return
        persona = self._persona_store.resolve(event.unified_msg_origin)
        if persona is None:
            return

        agent_body = l2_messages_from_run_context(run_context)
        if not agent_body:
            return
        stored_l2 = await self._states.contexts_for_request(event.unified_msg_origin)
        new_messages = diff_l2(stored_l2, agent_body)
        if not new_messages:
            return

        window = self._window_for_persona(persona)
        slid = await self._states.append_l2(event.unified_msg_origin, new_messages, window)
        if slid:
            await self._states.set_l2_needs_reload(event.unified_msg_origin)

        # 守卫：若此会话已有压缩任务在执行则跳过。
        state = await self._states.get(event.unified_msg_origin)
        if state.pending_compressed_l2 is not None:
            return

        all_l2 = await self._states.contexts_for_request(event.unified_msg_origin)
        token_est = CompressionHandler.estimate_tokens(all_l2)
        max_tokens = self._max_context_tokens()
        if CompressionHandler.should_compress(token_est, max_tokens):
            sid = event.unified_msg_origin
            logger.debug(
                "CustomPersona[%s]: starting background compression (l2_tokens~%d, max=%d)",
                sid,
                token_est,
                max_tokens,
            )
            asyncio.create_task(self._background_compress(sid, personacfg=persona))

    async def _background_compress(self, session_id: str, *, personacfg: PersonaConfig) -> None:
        try:
            provider = self._get_provider_for_session(session_id)
            if provider is None:
                logger.debug(
                    "CustomPersona[%s]: background compress skipped — no provider",
                    session_id,
                )
                return

            l2 = await self._states.contexts_for_request(session_id)
            preamble, body = CompressionHandler.split_preamble(l2)
            if not body:
                return

            max_ctx = self._max_context_tokens()
            result = await self._compressor.compress(
                preamble=preamble,
                body=body,
                provider=provider,
                system_prompt=personacfg.name or "AstrBot",
                assistant_template=personacfg.compression.assistant_template,
                user_template=personacfg.compression.user_template,
                variables={
                    "persona_name": personacfg.name,
                    "session_id": session_id,
                },
                max_context_tokens=max_ctx,
                custom_instructions=(personacfg.compression.custom_instructions or None),
            )
            await self._states.store_compressed_l2(session_id, result.messages)
            level_tag = (
                f" (fallback level {result.fallback_level})" if result.fallback_level > 0 else ""
            )
            logger.info(
                "CustomPersona[%s]: background compression complete — %d→%d messages%s",
                session_id,
                len(body),
                len(result.messages),
                level_tag,
            )
        except Exception:
            logger.warning(
                "CustomPersona[%s]: background compression failed",
                session_id,
                exc_info=True,
            )

    # ── 辅助方法 ───────────────────────────────────────────────

    def _window_for_persona(self, persona: PersonaConfig | None) -> DialogueWindowConfig:
        if persona is None:
            return self._config.dialogue_window
        if persona.dialogue_window.explicit:
            return persona.dialogue_window
        return self._config.dialogue_window

    def _max_context_tokens(self) -> int:
        model_id = ""
        if self._config.routing.mode == "simple" and self._config.routing.simple_model_id:
            model_id = self._config.routing.simple_model_id
        if model_id:
            try:
                from astrbot.core.utils.llm_metadata import LLM_METADATAS

                if model_id in LLM_METADATAS:
                    return int(LLM_METADATAS[model_id]["limit"]["context"])
            except Exception:
                pass
        return DEFAULT_MAX_CONTEXT_TOKENS

    def _get_provider_for_session(self, session_id: str) -> Any | None:
        try:
            return self._context.get_using_provider(session_id)
        except Exception:
            logger.debug("CustomPersona: get_using_provider failed", exc_info=True)
        try:
            cfg = self._context.get_config(session_id)
            prov_id = cfg.get("provider_settings", {}).get("provider_id")
            if prov_id:
                return self._context.provider_manager.get_provider_by_id(prov_id)
        except Exception:
            logger.debug("CustomPersona: provider_manager lookup failed", exc_info=True)
        return None

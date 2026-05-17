"""模板变量构建器 —— 为 Preamble 渲染组装所有 Jinja2 变量。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.platform import MessageType
from astrbot.api.provider import ProviderRequest
from astrbot.core.astr_main_agent_resources import (
    TOOL_CALL_PROMPT,
    TOOL_CALL_PROMPT_SKILLS_LIKE_MODE,
)

from .models import PersonaConfig

DEFAULT_LIVE_MODE_PROMPT = (
    "You are in a real-time conversation. "
    "Speak like a real person, casual and natural. "
    "Keep replies short, one thought at a time. "
    "No templates, no lists, no formatting. "
    "No parentheses, quotes, or markdown. "
    "It is okay to pause, hesitate, or speak in fragments. "
    "Respond to tone and emotion. "
    "Simple questions get simple answers. "
    "Sound like a real conversation, not a Q&A system."
)

# ── skill 管理器缓存 ───────────────────────────────────────

_skill_mgr: Any = None
_skill_mgr_unavailable: bool = False


def _cached_skill_manager() -> Any:
    global _skill_mgr, _skill_mgr_unavailable
    if _skill_mgr_unavailable:
        raise RuntimeError("SkillManager unavailable")
    if _skill_mgr is None:
        try:
            from astrbot.core.skills.skill_manager import SkillManager

            _skill_mgr = SkillManager()
        except Exception:
            _skill_mgr_unavailable = True
            raise
    return _skill_mgr


# ── 构建器 ───────────────────────────────────────────────────


class TemplateVariableBuilder:
    """为 Preamble 渲染阶段组装所有模板变量。"""

    def __init__(
        self,
        *,
        config: Any,  # PluginConfig
        context: Any,  # Context
        persona_store: Any,  # PersonaStore
        data_dir: Path,
    ) -> None:
        self._config = config
        self._context = context
        self._persona_store = persona_store
        self._data_dir = data_dir

    # ── 主入口 ──────────────────────────────────────

    def build(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
        persona: PersonaConfig,
        chat_history: str,
    ) -> dict[str, Any]:
        extra_prompt_path = self._extra_prompt_path(event.unified_msg_origin)
        extra_prompt = self._read_extra_prompt(extra_prompt_path)
        streaming = self._streaming_enabled(event)
        is_group = (
            bool(event.get_group_id()) or event.get_message_type() == MessageType.GROUP_MESSAGE
        )
        tool_schema_mode = self._tool_schema_mode(event)
        return {
            "chat_history": chat_history,
            "system_time": self._system_time(),
            "segment_mark": persona.segmented_reply.segment_mark,
            "t2i_trigger": persona.segmented_reply.t2i_trigger,
            "tts_trigger": persona.segmented_reply.tts_trigger,
            "skill_list": self._skill_list(event),
            "tool_list": self._tool_list(req),
            "tool_call_prompt": self._tool_call_prompt(persona, tool_schema_mode),
            "live_mode_prompt": self._live_mode_prompt(persona, event),
            "extra_prompt": extra_prompt,
            "extra_prompt_path": str(extra_prompt_path),
            "user_id": event.get_sender_id(),
            "user_nickname": event.get_sender_name(),
            "group_name": self._group_name(event),
            "platform_name": event.get_platform_name(),
            "message_type": event.get_message_type().name,
            "session_id": event.unified_msg_origin,
            "no_response_mark": self._no_response_mark(persona),
            "streaming": streaming,
            "is_group": is_group,
            "is_private": not is_group,
            "has_images": self._detect_images(req),
            "tool_schema_mode": tool_schema_mode,
            "t2i_enabled": self._t2i_enabled(event),
            "tts_enabled": self._tts_enabled(event),
            "is_admin": event.is_admin(),
            "admin_ids": self._context.get_config().get("admins_id", []),
        }

    def preview_defaults(self, persona: PersonaConfig) -> dict[str, Any]:
        """管理后台预览 API 使用的默认值。"""
        return {
            "chat_history": "[preview chat history]",
            "system_time": self._system_time(),
            "segment_mark": persona.segmented_reply.segment_mark,
            "t2i_trigger": persona.segmented_reply.t2i_trigger,
            "tts_trigger": persona.segmented_reply.tts_trigger,
            "skill_list": "[preview skills]",
            "tool_list": "[preview tools]",
            "tool_call_prompt": self._tool_call_prompt(persona, "full"),
            "live_mode_prompt": persona.live_mode_prompt or "",
            "extra_prompt": "",
            "extra_prompt_path": "",
            "user_id": "preview-user",
            "user_nickname": "Preview User",
            "group_name": "",
            "platform_name": "preview",
            "message_type": "PRIVATE_MESSAGE",
            "session_id": "preview:FriendMessage:1",
            "no_response_mark": self._no_response_mark(persona),
            "streaming": False,
            "is_group": False,
            "is_private": True,
            "has_images": False,
            "tool_schema_mode": "full",
            "t2i_enabled": False,
            "tts_enabled": False,
            "is_admin": False,
            "admin_ids": [],
        }

    # ── 变量辅助方法 ──────────────────────────────────────

    @staticmethod
    def _tool_call_prompt(persona: PersonaConfig, tool_schema_mode: str) -> str:
        if persona.tool_call_prompt:
            return persona.tool_call_prompt
        if tool_schema_mode == "skills_like":
            return TOOL_CALL_PROMPT_SKILLS_LIKE_MODE
        return TOOL_CALL_PROMPT

    @staticmethod
    def _live_mode_prompt(persona: PersonaConfig, event: AstrMessageEvent) -> str:
        if event.get_extra("action_type") != "live":
            return ""
        return persona.live_mode_prompt or DEFAULT_LIVE_MODE_PROMPT

    def _streaming_enabled(self, event: AstrMessageEvent) -> bool:
        override = event.get_extra("enable_streaming")
        if override is not None:
            return bool(override)
        try:
            cfg = self._context.get_config(event.unified_msg_origin)
            return bool(cfg.get("provider_settings", {}).get("streaming_response"))
        except Exception:
            return False

    def _system_time(self) -> str:
        try:
            tz = ZoneInfo(self._config.timezone)
        except Exception:
            tz = ZoneInfo("UTC")
        return datetime.now(tz).isoformat(timespec="seconds")

    def _skill_list(self, event: AstrMessageEvent) -> str:
        try:
            from astrbot.core.skills.skill_manager import build_skills_prompt

            skill_mgr = _cached_skill_manager()
            skills = skill_mgr.list_skills(active_only=True)
            try:
                cfg = self._context.get_config(event.unified_msg_origin)
                skills = self._filter_skills_for_config(skills, cfg)
            except Exception:
                logger.debug("CustomPersona: skill session-filter skipped", exc_info=True)
            persona = self._persona_store.resolve(event.unified_msg_origin)
            if persona and persona.skill_whitelist is not None:
                if not persona.skill_whitelist:
                    skills = []
                else:
                    allowed = set(persona.skill_whitelist)
                    skills = [s for s in skills if s.name in allowed]
            if not skills:
                return ""
            return build_skills_prompt(skills)
        except Exception:
            logger.warning("CustomPersona: _skill_list failed", exc_info=True)
            return ""

    @staticmethod
    def _filter_skills_for_config(skills: list[Any], cfg: dict) -> list[Any]:
        plugin_set = cfg.get("plugin_set", ["*"])
        if not isinstance(plugin_set, list) or "*" in plugin_set:
            return skills
        allowed_plugins = {str(name) for name in plugin_set}
        filtered: list[Any] = []
        for skill in skills:
            if getattr(skill, "source_type", None) != "plugin":
                filtered.append(skill)
                continue
            if getattr(skill, "plugin_name", "") in allowed_plugins:
                filtered.append(skill)
        return filtered

    @staticmethod
    def _detect_images(req: ProviderRequest) -> bool:
        if req.image_urls:
            return True
        for part in req.extra_user_content_parts or []:
            try:
                dumped = (
                    part.model_dump_for_context()
                    if hasattr(part, "model_dump_for_context")
                    else part
                )
            except Exception:
                continue
            if isinstance(dumped, dict) and dumped.get("type") in (
                "image_url",
                "image",
            ):
                return True
        return False

    def _tool_schema_mode(self, event: AstrMessageEvent) -> str:
        try:
            cfg = self._context.get_config(umo=event.unified_msg_origin)
            return cfg.get("provider_settings", {}).get("tool_schema_mode", "full")
        except Exception:
            return "full"

    def _t2i_enabled(self, event: AstrMessageEvent) -> bool:
        try:
            cfg = self._context.get_config(umo=event.unified_msg_origin)
            return bool(cfg.get("t2i"))
        except Exception:
            return False

    def _tts_enabled(self, event: AstrMessageEvent) -> bool:
        try:
            cfg = self._context.get_config(umo=event.unified_msg_origin)
            tts_cfg = cfg.get("provider_tts_settings", {})
            return bool(tts_cfg.get("enable"))
        except Exception:
            return False

    @staticmethod
    def _tool_list(req: ProviderRequest) -> str:
        if not req.func_tool:
            return ""
        lines: list[str] = ["## Available Tools"]
        for tool in req.func_tool.tools:
            name = getattr(tool, "name", "")
            description = getattr(tool, "description", "") or ""
            if not name:
                continue
            lines.append(f"\n- **{name}**: {description}")
            parameters = getattr(tool, "parameters", None) or {}
            properties = parameters.get("properties", {}) if isinstance(parameters, dict) else {}
            required_set = set(
                parameters.get("required", []) if isinstance(parameters, dict) else []
            )
            if properties:
                lines.append("  Parameters:")
                for param_name, param_schema in properties.items():
                    param_type = (
                        param_schema.get("type", "any") if isinstance(param_schema, dict) else "any"
                    )
                    param_desc = (
                        param_schema.get("description", "")
                        if isinstance(param_schema, dict)
                        else ""
                    )
                    required_label = "required" if param_name in required_set else "optional"
                    lines.append(
                        f"  - `{param_name}` ({param_type}, {required_label})"
                        f"{': ' + param_desc if param_desc else ''}"
                    )
        return "\n".join(lines)

    @staticmethod
    def _group_name(event: AstrMessageEvent) -> str:
        group = getattr(event.message_obj, "group", None)
        if group is not None:
            return str(getattr(group, "group_name", "") or getattr(group, "name", "") or "")
        return str(getattr(event.message_obj, "group_name", "") or "")

    def _extra_prompt_path(self, session_id: str) -> Path:
        """复现 AstrBot 的工作区路径逻辑，无需调用私有辅助方法。

        ``<data>/workspaces/<normalized_umo>/<extra_prompt_filename>``
        """
        import re

        normalized = re.sub(r"[^A-Za-z0-9._-]+", "_", session_id.strip()) or "unknown"
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            workspace = Path(get_astrbot_data_path()) / "workspaces" / normalized
        except Exception:
            workspace = self._data_dir / "workspaces" / normalized
        return workspace / self._config.extra_prompt_filename

    @staticmethod
    def _read_extra_prompt(path: Path) -> str:
        """读取 *path*，返回去除首尾空白的内容。

        当文件为空时返回占位字符串，使其在模板中可见。
        首次访问时创建父目录及空文件，方便用户知道在哪里编写提示词。
        """
        if not path.is_file():
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch(exist_ok=True)
            except OSError:
                logger.debug("CustomPersona: could not create %s", path, exc_info=True)
                content = ""
        try:
            content = path.read_text(encoding="utf-8").strip()
        except Exception:
            logger.warning(
                "CustomPersona: failed to read %s",
                path,
                exc_info=True,
            )
            content = ""
        if not content:
            return "(EXTRA_PROMPT.md is empty — add instructions here)"
        return content

    def _no_response_mark(self, persona: PersonaConfig) -> str:
        return persona.no_response_mark or self._config.no_response.mark

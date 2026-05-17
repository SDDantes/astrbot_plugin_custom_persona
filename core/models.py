from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("astrbot_plugin_custom_persona")

VALID_ROLES = {"SYSTEM", "USER", "ASSISTANT"}


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass(slots=True)
class SessionBinding:
    session_id: str

    @classmethod
    def from_raw(cls, raw: Any) -> SessionBinding | None:
        if isinstance(raw, str):
            session_id = raw.strip()
        elif isinstance(raw, dict):
            session_id = str(raw.get("session_id", "")).strip()
        else:
            session_id = ""
        if not session_id:
            return None
        return cls(session_id=session_id)


@dataclass(slots=True)
class ActivationConfig:
    global_default: bool = False
    session_bindings: list[SessionBinding] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ActivationConfig:
        data = data or {}
        bindings = []
        for item in data.get("session_bindings") or []:
            binding = SessionBinding.from_raw(item)
            if binding:
                bindings.append(binding)
        return cls(
            global_default=_as_bool(data.get("global_default"), False),
            session_bindings=bindings,
        )


@dataclass(slots=True)
class SegmentConfig:
    id: str
    role: str
    depth: int
    condition: str = ""
    template: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SegmentConfig:
        role = str(data.get("role", "USER")).strip().upper()
        if role not in VALID_ROLES:
            logger.warning(
                "CustomPersona: segment '%s' has invalid role '%s', falling back to USER",
                data.get("id", "?"),
                role,
            )
            role = "USER"
        return cls(
            id=str(data.get("id", "")).strip() or f"segment_{data.get('depth', 0)}",
            role=role,
            depth=_as_int(data.get("depth"), 0),
            condition=str(data.get("condition", "") or ""),
            template=str(data.get("template", "") or ""),
        )


@dataclass(slots=True)
class ChatHistoryConfig:
    max_turns: int = 30
    format_template: str = "[{sender_name}/{timestamp}]: {content}"
    preset_dialogs: str = ""
    max_tokens: int = 8000

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ChatHistoryConfig:
        data = data or {}
        return cls(
            max_turns=max(0, _as_int(data.get("max_turns"), 30)),
            format_template=str(
                data.get("format_template") or "[{sender_name}/{timestamp}]: {content}"
            ),
            preset_dialogs=str(data.get("preset_dialogs", "") or ""),
            max_tokens=max(0, _as_int(data.get("max_tokens"), 8000)),
        )


@dataclass(slots=True)
class DialogueWindowConfig:
    max_messages: int = 100
    keep_messages: int = 60
    explicit: bool = False

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any] | None,
        *,
        explicit: bool | None = None,
    ) -> DialogueWindowConfig:
        was_explicit = data is not None if explicit is None else explicit
        data = data or {}
        max_messages = max(1, _as_int(data.get("max_messages"), 100))
        keep_messages = max(1, _as_int(data.get("keep_messages"), 60))
        keep_messages = min(keep_messages, max_messages)
        return cls(
            max_messages=max_messages,
            keep_messages=keep_messages,
            explicit=was_explicit,
        )


@dataclass(slots=True)
class SegmentedReplyConfig:
    enabled: bool = False
    segment_mark: str = "✺SEG✺"
    interval_min: float = 1.5
    interval_max: float = 3.5
    t2i_trigger: str = "✺T2I✺"
    tts_trigger: str = "✺TTS✺"
    t2i_template: str = ""
    tts_dual_output: bool = False
    t2i_dual_output: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> SegmentedReplyConfig:
        data = data or {}
        interval_min = max(0.0, _as_float(data.get("interval_min"), 1.5))
        interval_max = max(interval_min, _as_float(data.get("interval_max"), 3.5))
        return cls(
            enabled=_as_bool(data.get("enabled"), False),
            segment_mark=str(data.get("segment_mark") or "✺SEG✺"),
            interval_min=interval_min,
            interval_max=interval_max,
            t2i_trigger=str(data.get("t2i_trigger") or "✺T2I✺"),
            tts_trigger=str(data.get("tts_trigger") or "✺TTS✺"),
            t2i_template=str(data.get("t2i_template") or ""),
            tts_dual_output=_as_bool(data.get("tts_dual_output"), False),
            t2i_dual_output=_as_bool(data.get("t2i_dual_output"), False),
        )


@dataclass(slots=True)
class CompressionConfig:
    assistant_template: str = (
        "The previous working context has been compacted. I will continue from "
        "the summary in the next user message."
    )
    user_template: str = "Compressed conversation summary:\n{{ summary }}"
    custom_instructions: str = ""
    """按 Persona 覆写的摘要指令，发送给 LLM 使用。
    为空时使用 compression.py 中的默认 ``COMPRESSION_PROMPT``。"""

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> CompressionConfig:
        data = data or {}
        return cls(
            assistant_template=str(data.get("assistant_template") or cls.assistant_template),
            user_template=str(data.get("user_template") or cls.user_template),
            custom_instructions=str(data.get("custom_instructions") or ""),
        )


@dataclass(slots=True)
class PersonaConfig:
    name: str
    display_name: str = ""
    description: str = ""
    activation: ActivationConfig = field(default_factory=ActivationConfig)
    segments: list[SegmentConfig] = field(default_factory=list)
    chat_history: ChatHistoryConfig = field(default_factory=ChatHistoryConfig)
    dialogue_window: DialogueWindowConfig = field(default_factory=DialogueWindowConfig)
    segmented_reply: SegmentedReplyConfig = field(default_factory=SegmentedReplyConfig)
    compression: CompressionConfig = field(default_factory=CompressionConfig)
    no_response_mark: str = "✺✺✺NO_RESPONSE✺✺✺"
    tool_call_prompt: str = ""
    live_mode_prompt: str = ""
    skill_whitelist: list[str] | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PersonaConfig:
        name = str(data.get("name", "")).strip()
        if not name:
            raise ValueError("persona name is required")
        segments = [
            SegmentConfig.from_dict(item)
            for item in data.get("segments") or []
            if isinstance(item, dict)
        ]
        segments.sort(key=lambda item: (item.depth, item.id))
        return cls(
            name=name,
            display_name=str(data.get("display_name") or name),
            description=str(data.get("description", "") or ""),
            activation=ActivationConfig.from_dict(data.get("activation")),
            segments=segments,
            chat_history=ChatHistoryConfig.from_dict(data.get("chat_history")),
            dialogue_window=DialogueWindowConfig.from_dict(
                data.get("dialogue_window"),
                explicit="dialogue_window" in data,
            ),
            segmented_reply=SegmentedReplyConfig.from_dict(data.get("segmented_reply")),
            compression=CompressionConfig.from_dict(data.get("compression")),
            no_response_mark=str(
                data.get("no_response_mark")
                or data.get("no_response", {}).get("mark")
                or "✺✺✺NO_RESPONSE✺✺✺"
            ),
            tool_call_prompt=str(data.get("tool_call_prompt") or ""),
            live_mode_prompt=str(data.get("live_mode_prompt") or ""),
            skill_whitelist=(
                [str(s) for s in data["skill_whitelist"]]
                if isinstance(data.get("skill_whitelist"), list)
                else None
            ),
            raw=data,
        )


@dataclass(slots=True)
class PluginRoutingConfig:
    mode: str = "default"
    simple_model_id: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> PluginRoutingConfig:
        data = data or {}
        mode = str(data.get("mode") or "default").strip().lower()
        if mode not in {"default", "simple"}:
            logger.warning(
                "CustomPersona: unsupported routing mode '%s', falling back to 'default'",
                mode,
            )
            mode = "default"
        return cls(mode=mode, simple_model_id=str(data.get("simple_model_id") or ""))


@dataclass(slots=True)
class NoResponseConfig:
    enabled: bool = True
    mark: str = "✺✺✺NO_RESPONSE✺✺✺"

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> NoResponseConfig:
        data = data or {}
        return cls(
            enabled=_as_bool(data.get("enabled"), True),
            mark=str(data.get("mark") or "✺✺✺NO_RESPONSE✺✺✺"),
        )


@dataclass(slots=True)
class LedgerConfig:
    per_chat_limit: int = 1000

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> LedgerConfig:
        data = data or {}
        return cls(per_chat_limit=max(1, _as_int(data.get("per_chat_limit"), 1000)))


@dataclass(slots=True)
class PluginConfig:
    enabled: bool = True
    timezone: str = "Asia/Tokyo"
    personas_dir: str = ""
    extra_prompt_filename: str = "EXTRA_PROMPT.md"
    routing: PluginRoutingConfig = field(default_factory=PluginRoutingConfig)
    no_response: NoResponseConfig = field(default_factory=NoResponseConfig)
    dialogue_window: DialogueWindowConfig = field(default_factory=DialogueWindowConfig)
    ledger: LedgerConfig = field(default_factory=LedgerConfig)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> PluginConfig:
        data = data or {}
        return cls(
            enabled=_as_bool(data.get("enabled"), True),
            timezone=str(data.get("timezone") or "Asia/Tokyo"),
            personas_dir=str(data.get("personas_dir") or ""),
            extra_prompt_filename=str(data.get("extra_prompt_filename") or "EXTRA_PROMPT.md"),
            routing=PluginRoutingConfig.from_dict(data.get("routing")),
            no_response=NoResponseConfig.from_dict(data.get("no_response")),
            dialogue_window=DialogueWindowConfig.from_dict(data.get("dialogue_window")),
            ledger=LedgerConfig.from_dict(data.get("ledger")),
        )

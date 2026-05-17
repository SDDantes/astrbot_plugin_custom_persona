"""custom-persona 插件的上下文压缩模块。

采用 LLM 摘要方案（Codex 风格）：将对话正文发送给模型，要求生成简洁的
结构化摘要，然后将正文替换为"摘要对 + 最近消息"。

摘要流水线采用三级降级回退链，参考自 OpenClaw 的 ``summarizeWithFallback``：

1. **完整摘要** — 通过分块 LLM 调用（带重试）摘要全部消息。
2. **部分摘要** — 排除超大消息，摘要其余部分。
3. **纯文本占位** — 当以上均失败时返回静态占位文本。

每次 LLM 调用均包裹在 ``retry_async`` 中，含指数退避与抖动量，
确保瞬时提供方错误能自愈。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from jinja2 import Environment

from .history import strip_tool_records as _strip_tool_records
from .retry import retry_async

logger = logging.getLogger("astrbot_plugin_custom_persona")

COMPRESSION_PROMPT = (
    "You are performing a CONTEXT CHECKPOINT COMPACTION. "
    "Create a concise handoff summary for another LLM to continue this "
    "conversation.  Include:\n"
    "- the user's current goal / task and progress toward it\n"
    "- key decisions, tool-call names and their important results\n"
    "- important file paths, URLs, data values, or conclusions\n"
    "- the last few exchanges verbatim if they are still relevant\n"
    "- any running-state information needed to resume the task\n"
    "Return only the summary, no meta-commentary."
)

MERGE_SUMMARIES_PROMPT = (
    "Below are partial summaries of different parts of a conversation. "
    "Merge them into a single cohesive summary following the same "
    "summarisation instructions given above.  Remove duplicate "
    "information and preserve all important details."
)

# ---------------------------------------------------------------------------
# Token 估算辅助
# ---------------------------------------------------------------------------

CHARS_PER_TOKEN = 4
"""粗略边界：对于大多数自然语言文本，一个 token 约等于四个字符。"""

SUMMARIZATION_OVERHEAD_CHARS = 2_000
"""为摘要提示词、系统提示等预留的开销。"""

SAFETY_MARGIN = 1.1
"""应用于 token 估算的乘数，避免低估。"""

MIN_CHUNK_RATIO = 0.05
"""单个分块可占上下文窗口的最小比例。"""

BASE_CHUNK_RATIO = 0.35
"""分配给单个分块的上下文窗口默认比例。"""

DEFAULT_MAX_CONTEXT_TOKENS = 64_000


# ---------------------------------------------------------------------------
# 结果类型
# ---------------------------------------------------------------------------


@dataclass
class CompressionResult:
    messages: list[dict[str, Any]]
    """压缩后的消息列表（preamble + recent + 摘要对）。"""

    fallback_level: int = 0
    """生成摘要所使用的降级级别（0 = 完整，1 = 部分，2 = 纯文本占位）。"""


# ---------------------------------------------------------------------------
# Token 估算
# ---------------------------------------------------------------------------


def estimate_content_chars(content: Any) -> int:
    """返回 *content* 的字符长度，用于 token 估算。"""
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(len(str(part.get("text", ""))) for part in content if isinstance(part, dict))
    return len(str(content))


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """粗略 token 计数：4 个字符 ≈ 1 token。"""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        total += estimate_content_chars(content)
    return max(1, total // CHARS_PER_TOKEN)


def estimate_message_tokens(msg: dict[str, Any]) -> int:
    """单条消息的 token 估算。"""
    return max(1, estimate_content_chars(msg.get("content", "")) // CHARS_PER_TOKEN)


# ---------------------------------------------------------------------------
# 超大消息检测
# ---------------------------------------------------------------------------


def is_oversized_for_summary(msg: dict[str, Any], context_window_tokens: int) -> bool:
    """若单条消息过大无法安全摘要则返回 True。

    占用超过上下文窗口 50% 的消息无法被摘要，因为 LLM 需要为提示词、
    上一次摘要及响应留出空间。
    """
    tokens = estimate_message_tokens(msg) * SAFETY_MARGIN
    return tokens > context_window_tokens * 0.5


# ---------------------------------------------------------------------------
# 分块
# ---------------------------------------------------------------------------


def _compute_adaptive_chunk_ratio(
    messages: list[dict[str, Any]], context_window_tokens: int
) -> float:
    """选择一个分块比例，为最大消息留出空间。"""
    if not messages:
        return BASE_CHUNK_RATIO
    max_msg_tokens = max(estimate_message_tokens(m) for m in messages)
    if max_msg_tokens <= 0:
        return BASE_CHUNK_RATIO
    ratio = 1.0 - (max_msg_tokens / context_window_tokens)
    return max(MIN_CHUNK_RATIO, min(BASE_CHUNK_RATIO, ratio))


def _chunk_messages_by_max_tokens(
    messages: list[dict[str, Any]], max_chunk_tokens: int
) -> list[list[dict[str, Any]]]:
    """将 *messages* 拆分为每个分块 ≤ *max_chunk_tokens* 估算 token。"""
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_tokens = 0
    for msg in messages:
        msg_tokens = estimate_message_tokens(msg)
        if current_tokens + msg_tokens > max_chunk_tokens and current:
            chunks.append(current)
            current = []
            current_tokens = 0
        current.append(msg)
        current_tokens += msg_tokens
    if current:
        chunks.append(current)
    return chunks


def _split_by_token_share(messages: list[dict[str, Any]], parts: int) -> list[list[dict[str, Any]]]:
    """将 *messages* 拆分为 *parts* 组，每组 token 占比大致相等。"""
    if parts <= 1 or not messages:
        return [list(messages)]
    total = sum(estimate_message_tokens(m) for m in messages)
    target = total / parts
    splits: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_sum = 0
    for msg in messages:
        current.append(msg)
        current_sum += estimate_message_tokens(msg)
        if current_sum >= target and len(splits) < parts - 1:
            splits.append(current)
            current = []
            current_sum = 0
    if current:
        splits.append(current)
    return [s for s in splits if s]


# ---------------------------------------------------------------------------
# CompressionHandler
# ---------------------------------------------------------------------------


class CompressionHandler:
    """独立压缩器，含三级降级回退链。

    Codex 风格：摘要*整个*正文，剥离工具消息，保留一小段最近窗口，
    将摘要对放在**末尾**，使模型先读取最近消息再看历史上下文。
    """

    def __init__(self) -> None:
        self._env = Environment(autoescape=False)

    # -- 公开 API --------------------------------------------------

    @staticmethod
    def should_compress(token_estimate: int, max_tokens: int) -> bool:
        if max_tokens <= 0:
            return False
        return token_estimate > int(max_tokens * 0.78)

    async def compress(
        self,
        *,
        preamble: list[dict[str, Any]],
        body: list[dict[str, Any]],
        provider: Any,
        system_prompt: str,
        assistant_template: str,
        user_template: str,
        variables: dict[str, Any],
        max_context_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS,
        custom_instructions: str | None = None,
    ) -> CompressionResult:
        """压缩 *body* 中的消息，含渐进式降级。

        返回 ``preamble + recent_clean + summary_pair`` —— 摘要放在末尾，
        以便 LLM 先看到最新上下文。
        """
        if not body:
            return CompressionResult(messages=[*preamble])

        # 检测并提取已有摘要以避免重复压缩。
        _idx, previous_summary, body_without_summary = self.detect_existing_summaries(body)
        if previous_summary:
            logger.debug(
                "CustomPersona: detected existing summary (%d chars), "
                "will merge rather than re-summarise from scratch",
                len(previous_summary),
            )

        summary_text, fallback_level = await self._summarize_with_fallback(
            provider=provider,
            system_prompt=system_prompt,
            messages=list(body_without_summary),
            context_window_tokens=max_context_tokens,
            custom_instructions=custom_instructions,
            previous_summary=previous_summary,
        )

        # 从正文中剥离工具消息。
        clean_body = _strip_tool_records(body_without_summary)

        # 保留一小段最近窗口。
        recent_count = max(2, min(6, len(clean_body) // 3))
        recent = clean_body[-recent_count:] if len(clean_body) > recent_count else clean_body

        summary_pair = self._render_summary_pair(
            summary_text=summary_text,
            assistant_template=assistant_template,
            user_template=user_template,
            variables=variables,
        )

        if fallback_level >= 2:
            logger.warning(
                "CustomPersona: compression fell back to level %d (text placeholder). "
                "Context may be truncated.",
                fallback_level,
            )

        return CompressionResult(
            messages=[*preamble, *recent, *summary_pair],
            fallback_level=fallback_level,
        )

    # -- 降级回退链 ----------------------------------------------

    async def _summarize_with_fallback(
        self,
        *,
        provider: Any,
        system_prompt: str,
        messages: list[dict[str, Any]],
        context_window_tokens: int,
        custom_instructions: str | None = None,
        previous_summary: str | None = None,
    ) -> tuple[str, int]:
        """三级渐进式降级摘要。

        返回 ``(summary_text, fallback_level)``，其中 *fallback_level*
        为 0（完整）、1（部分 / 排除超大消息）或 2（文本占位）。
        """
        if not messages:
            return (previous_summary or "", 0)

        # 级别 1 —— 完整摘要
        try:
            summary = await self._summarize_chunks(
                provider=provider,
                system_prompt=system_prompt,
                messages=messages,
                context_window_tokens=context_window_tokens,
                custom_instructions=custom_instructions,
                previous_summary=previous_summary,
            )
            return (summary, 0)
        except Exception as exc:
            logger.warning("CustomPersona: full summarisation failed: %s", exc)

        # 级别 2 —— 部分摘要（排除超大消息）
        small_messages: list[dict[str, Any]] = []
        oversized_notes: list[str] = []
        for msg in messages:
            if is_oversized_for_summary(msg, context_window_tokens):
                role = str(msg.get("role", "message"))
                tokens = estimate_message_tokens(msg)
                oversized_notes.append(
                    f"[Large {role} (~{round(tokens / 1000)}K tokens) omitted from summary]"
                )
            else:
                small_messages.append(msg)

        if small_messages and len(small_messages) != len(messages):
            try:
                partial_summary = await self._summarize_chunks(
                    provider=provider,
                    system_prompt=system_prompt,
                    messages=small_messages,
                    context_window_tokens=context_window_tokens,
                    custom_instructions=custom_instructions,
                    previous_summary=previous_summary,
                )
                if oversized_notes:
                    partial_summary += "\n\n" + "\n".join(oversized_notes)
                return (partial_summary, 1)
            except Exception as exc:
                logger.warning("CustomPersona: partial summarisation also failed: %s", exc)

        # 级别 3 —— 纯文本占位
        placeholder = (
            f"Context contained {len(messages)} messages "
            f"({len(oversized_notes)} oversized). "
            "Summary unavailable due to size limits."
        )
        return (placeholder, 2)

    async def _summarize_in_stages(
        self,
        *,
        provider: Any,
        system_prompt: str,
        messages: list[dict[str, Any]],
        context_window_tokens: int,
        custom_instructions: str | None = None,
        previous_summary: str | None = None,
        parts: int = 2,
        min_messages_for_split: int = 4,
    ) -> tuple[str, int]:
        """拆分较大消息集，逐部分摘要，然后合并。

        当拆分无益时（消息太少或总 token 可放入单个分块），
        会降级到 ``_summarize_with_fallback``。
        """
        if not messages:
            return (previous_summary or "", 0)

        parts = max(1, parts)
        min_messages_for_split = max(2, min_messages_for_split)

        adaptive_ratio = _compute_adaptive_chunk_ratio(messages, context_window_tokens)
        max_chunk_tokens = max(
            1,
            int(context_window_tokens * adaptive_ratio)
            - SUMMARIZATION_OVERHEAD_CHARS // CHARS_PER_TOKEN,
        )

        total_tokens = estimate_tokens(messages)
        if parts <= 1 or len(messages) < min_messages_for_split or total_tokens <= max_chunk_tokens:
            return await self._summarize_with_fallback(
                provider=provider,
                system_prompt=system_prompt,
                messages=messages,
                context_window_tokens=context_window_tokens,
                custom_instructions=custom_instructions,
                previous_summary=previous_summary,
            )

        splits = _split_by_token_share(messages, parts)
        splits = [s for s in splits if s]
        if len(splits) <= 1:
            return await self._summarize_with_fallback(
                provider=provider,
                system_prompt=system_prompt,
                messages=messages,
                context_window_tokens=context_window_tokens,
                custom_instructions=custom_instructions,
                previous_summary=previous_summary,
            )

        partial_summaries: list[str] = []
        for chunk in splits:
            text, _level = await self._summarize_with_fallback(
                provider=provider,
                system_prompt=system_prompt,
                messages=chunk,
                context_window_tokens=context_window_tokens,
                custom_instructions=custom_instructions,
                previous_summary=None,
            )
            partial_summaries.append(text)

        if len(partial_summaries) == 1:
            return (partial_summaries[0], 0)

        merge_messages: list[dict[str, Any]] = [
            {"role": "user", "content": s} for s in partial_summaries
        ]
        merge_instructions = (
            f"{MERGE_SUMMARIES_PROMPT}\n\n{custom_instructions}"
            if custom_instructions
            else MERGE_SUMMARIES_PROMPT
        )

        return await self._summarize_with_fallback(
            provider=provider,
            system_prompt=system_prompt,
            messages=merge_messages,
            context_window_tokens=context_window_tokens,
            custom_instructions=merge_instructions,
            previous_summary=None,
        )

    # -- 分块摘要 ---------------------------------------

    async def _summarize_chunks(
        self,
        *,
        provider: Any,
        system_prompt: str,
        messages: list[dict[str, Any]],
        context_window_tokens: int,
        custom_instructions: str | None = None,
        previous_summary: str | None = None,
    ) -> str:
        """将 *messages* 分块以适配上下文窗口，逐块摘要。

        每个分块的 LLM 调用均包裹在 ``retry_async`` 中（3 次尝试，
        指数退避 + 抖动）。
        """
        if not messages:
            return previous_summary or ""

        adaptive_ratio = _compute_adaptive_chunk_ratio(messages, context_window_tokens)
        max_chunk_tokens = max(
            1,
            int(context_window_tokens * adaptive_ratio)
            - SUMMARIZATION_OVERHEAD_CHARS // CHARS_PER_TOKEN,
        )

        chunks = _chunk_messages_by_max_tokens(messages, max_chunk_tokens)
        summary = previous_summary

        for idx, chunk in enumerate(chunks):
            summary = await retry_async(
                lambda c=chunk, s=summary: self._call_llm_summary(
                    provider=provider,
                    system_prompt=system_prompt,
                    messages=c,
                    previous_summary=s,
                    custom_instructions=custom_instructions,
                ),
                attempts=3,
                min_delay_ms=500,
                max_delay_ms=5_000,
                jitter=0.2,
                label=f"compression/chunk-{idx + 1}/{len(chunks)}",
            )

        return summary or ""

    # -- 原始 LLM 调用 ------------------------------------------------

    async def _call_llm_summary(
        self,
        provider: Any,
        system_prompt: str,
        messages: list[dict[str, Any]],
        previous_summary: str | None = None,
        custom_instructions: str | None = None,
    ) -> str:
        """将 *messages* 发送给提供方并返回摘要文本。

        不含重试 —— 调用方应将其包裹在 ``retry_async`` 中。
        """
        payload = list(messages)

        if previous_summary:
            payload.insert(
                0,
                {
                    "role": "user",
                    "content": (
                        "Previous conversation summary (use this as context "
                        f"for summarising the messages that follow):\n\n"
                        f"{previous_summary}"
                    ),
                },
            )

        instruction = custom_instructions or COMPRESSION_PROMPT
        payload.append({"role": "user", "content": instruction})

        response = await provider.text_chat(
            system_prompt=system_prompt,
            contexts=payload,
        )
        return (response.completion_text or "").strip()

    # -- 摘要对渲染 -------------------------------------

    def _render_summary_pair(
        self,
        summary_text: str,
        assistant_template: str,
        user_template: str,
        variables: dict[str, Any],
    ) -> list[dict[str, Any]]:
        vars_with_summary = {**variables, "summary": summary_text}
        assistant_text = self._env.from_string(assistant_template).render(**vars_with_summary)
        user_text = self._env.from_string(user_template).render(**vars_with_summary)
        return [
            {
                "role": "assistant",
                "content": assistant_text.strip(),
                "_is_compressed_summary": True,
            },
            {"role": "user", "content": user_text.strip()},
        ]

    # -- 静态辅助方法 ----------------------------------------------

    @staticmethod
    def split_preamble(
        messages: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """按 ``_no_save`` 标记拆分，返回 ``(preamble, body)``。"""
        if not messages:
            return [], []
        preamble: list[dict[str, Any]] = []
        idx = 0
        if messages[0].get("role") == "system":
            preamble.append(messages[0])
            idx = 1
        while idx < len(messages) and messages[idx].get("_no_save"):
            preamble.append(messages[idx])
            idx += 1
        return preamble, messages[idx:]

    @staticmethod
    def strip_tool_messages(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return _strip_tool_records(messages)

    @staticmethod
    def estimate_tokens(messages: list[dict[str, Any]]) -> int:
        """消息列表的粗略 token 计数。"""
        return estimate_tokens(messages)

    @staticmethod
    def detect_existing_summaries(
        messages: list[dict[str, Any]],
    ) -> tuple[int, str | None, list[dict[str, Any]]]:
        """检测 *messages* 中是否存在已有的压缩摘要对。

        返回 ``(index, summary_text, remaining_messages)``，其中 *index*
        为携带 ``_is_compressed_summary`` 标记的 assistant 消息位置
        （若未找到则为 ``-1``），*summary_text* 为提取的摘要内容
        （无则为 ``None``），*remaining_messages* 为移除摘要对后的
        *messages*。
        """
        for i in range(len(messages) - 2, -1, -1):
            if (
                messages[i].get("role") == "assistant"
                and messages[i].get("_is_compressed_summary")
                and i + 1 < len(messages)
                and messages[i + 1].get("role") == "user"
            ):
                summary_text = messages[i + 1].get("content", "")
                if isinstance(summary_text, str) and summary_text.strip():
                    remaining = messages[:i] + messages[i + 2 :]
                    return (i, summary_text.strip(), remaining)
        return (-1, None, messages)

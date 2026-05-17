from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from jinja2 import Environment

from .models import PersonaConfig


@dataclass(slots=True)
class RenderedPreamble:
    system_prompt: str
    contexts: list[dict[str, Any]]
    rendered_segments: list[dict[str, Any]]


class PreambleRenderer:
    def __init__(self) -> None:
        self.env = Environment(autoescape=False)

    def render(self, persona: PersonaConfig, variables: dict[str, Any]) -> RenderedPreamble:
        system_parts: list[str] = []
        contexts: list[dict[str, Any]] = []
        rendered_segments: list[dict[str, Any]] = []

        for segment in persona.segments:
            if not self._condition_matches(segment.condition, variables):
                continue
            content = self.env.from_string(segment.template).render(**variables).strip()
            if not content:
                continue
            role = segment.role.lower()
            rendered_segments.append(
                {
                    "id": segment.id,
                    "role": role,
                    "depth": segment.depth,
                    "content": content,
                }
            )
            if segment.role == "SYSTEM":
                system_parts.append(content)
                continue
            contexts.append(
                {
                    "role": role,
                    "content": content,
                    "_no_save": True,
                }
            )

        return RenderedPreamble(
            system_prompt="\n\n".join(system_parts),
            contexts=contexts,
            rendered_segments=rendered_segments,
        )

    def render_string(self, template: str, variables: dict[str, Any]) -> str:
        return self.env.from_string(template).render(**variables)

    def _condition_matches(self, condition: str, variables: dict[str, Any]) -> bool:
        condition = (condition or "").strip()
        if not condition:
            return True
        normalized = self._normalize_condition(condition)
        try:
            expression = self.env.compile_expression(normalized)
            return bool(expression(**variables))
        except Exception:
            return False

    @staticmethod
    def _normalize_condition(condition: str) -> str:
        """将 Persona 条件语法转换为合法的 Jinja2 表达式。

        ``!`` 在 Jinja2 中不是布尔运算符，需要转换为 ``not``，
        同时处理 ``!streaming`` 和 ``is_group and !streaming`` 两种形式。
        """
        if condition.startswith("{{") and condition.endswith("}}"):
            condition = condition[2:-2].strip()
        # 将 ``!`` 布尔前缀替换为 ``not``（Jinja2 要求使用 ``not``）。
        # 使用否定后顾断言避免误伤 ``!=``。
        condition = re.sub(r"(?<![=!])\!\s*", "not ", condition)
        return condition

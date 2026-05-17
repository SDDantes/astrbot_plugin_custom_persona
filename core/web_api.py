"""custom-persona 插件管理页面的 Web API 控制器。"""

from __future__ import annotations

from typing import Any

from .models import PersonaConfig

PLUGIN_NAME = "astrbot_plugin_custom_persona"


class WebApiController:
    """注册并处理所有管理页面 HTTP 端点。"""

    def __init__(
        self,
        *,
        persona_store: Any,
        renderer: Any,
        var_builder: Any,  # TemplateVariableBuilder
    ) -> None:
        self._persona_store = persona_store
        self._renderer = renderer
        self._var_builder = var_builder

    def register(self, context: Any) -> None:
        prefix = f"/{PLUGIN_NAME}"
        context.register_web_api(
            f"{prefix}/personas", self.list_personas, ["GET"], "List custom personas"
        )
        context.register_web_api(
            f"{prefix}/persona/<name>", self.get_persona, ["GET"], "Get persona YAML"
        )
        context.register_web_api(
            f"{prefix}/persona/<name>", self.save_persona, ["POST"], "Save persona YAML"
        )
        context.register_web_api(
            f"{prefix}/persona/<name>/rename",
            self.rename_persona,
            ["POST"],
            "Rename persona",
        )
        context.register_web_api(
            f"{prefix}/persona/<name>/delete",
            self.delete_persona,
            ["POST"],
            "Delete persona",
        )
        context.register_web_api(
            f"{prefix}/preview",
            self.preview,
            ["POST"],
            "Preview rendered persona",
        )

    # ── 请求处理器 ──────────────────────────────────────────────

    async def list_personas(self) -> dict:
        return {"status": "ok", "data": {"items": self._persona_store.summary()}}

    async def get_persona(self, name: str) -> dict:
        try:
            path, content = self._persona_store.get_raw(name)
            return {
                "status": "ok",
                "data": {"name": name, "file": path.name, "content": content},
            }
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    async def save_persona(self, name: str) -> dict:
        try:
            from quart import request

            payload = await request.get_json(force=True)
            content = str((payload or {}).get("content") or "")
            persona = self._persona_store.save_raw(name, content)
            return {
                "status": "ok",
                "data": {"name": persona.name, "display_name": persona.display_name},
            }
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    async def rename_persona(self, name: str) -> dict:
        try:
            from quart import request

            payload = await request.get_json(force=True)
            new_name = str((payload or {}).get("new_name") or "").strip()
            persona = self._persona_store.rename(name, new_name)
            return {
                "status": "ok",
                "data": {"name": persona.name, "display_name": persona.display_name},
            }
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    async def delete_persona(self, name: str) -> dict:
        try:
            self._persona_store.delete(name)
            return {"status": "ok", "data": {"name": name}}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    async def preview(self) -> dict:
        try:
            import yaml
            from quart import request

            payload = await request.get_json(force=True)
            content = str((payload or {}).get("content") or "")
            variables = (payload or {}).get("variables") or {}
            data = yaml.safe_load(content) or {}
            persona = PersonaConfig.from_dict(data)
            defaults = self._var_builder.preview_defaults(persona)
            rendered = self._renderer.render(persona, {**defaults, **variables})
            return {
                "status": "ok",
                "data": {
                    "system_prompt": rendered.system_prompt,
                    "contexts": rendered.contexts,
                    "segments": rendered.rendered_segments,
                },
            }
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

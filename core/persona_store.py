from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path
from typing import Any

import yaml

from .models import PersonaConfig

logger = logging.getLogger("astrbot_plugin_custom_persona")

SAFE_PERSONA_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class PersonaStore:
    def __init__(self, personas_dir: Path, bundled_dir: Path | None = None) -> None:
        self.personas_dir = personas_dir
        self.bundled_dir = bundled_dir
        self.personas_dir.mkdir(parents=True, exist_ok=True)
        self._copy_bundled_defaults_if_empty()

        # ── mtime-based cache ──
        self._cache_mtimes: dict[str, float] = {}
        self._cache_personas: list[PersonaConfig] | None = None

    # ── cache management ──────────────────────────────────────────

    def _invalidate_cache(self) -> None:
        self._cache_mtimes.clear()
        self._cache_personas = None

    def _cached_load_all(self) -> list[PersonaConfig]:
        """Return all personas, re-reading from disk only when files changed."""
        current: dict[str, float] = {}
        for path in self.list_files():
            try:
                current[str(path)] = path.stat().st_mtime
            except OSError:
                continue

        if self._cache_personas is not None and current == self._cache_mtimes:
            return self._cache_personas

        self._cache_mtimes = current
        self._cache_personas = self._load_all_uncached()
        return self._cache_personas

    def _load_all_uncached(self) -> list[PersonaConfig]:
        personas: list[PersonaConfig] = []
        for path in self.list_files():
            try:
                personas.append(self.load_by_path(path))
            except Exception:
                logger.warning(
                    "CustomPersona: failed to load persona file %s",
                    path,
                    exc_info=True,
                )
        return personas

    # ── file operations ───────────────────────────────────────────

    def _copy_bundled_defaults_if_empty(self) -> None:
        if not self.bundled_dir or not self.bundled_dir.is_dir():
            return
        if any(self.personas_dir.glob("*.yml")) or any(self.personas_dir.glob("*.yaml")):
            return
        for src in sorted(self.bundled_dir.glob("*.y*ml")):
            shutil.copy2(src, self.personas_dir / src.name)

    def list_files(self) -> list[Path]:
        files = [*self.personas_dir.glob("*.yaml"), *self.personas_dir.glob("*.yml")]
        return sorted(set(files), key=lambda path: path.name)

    def load_all(self) -> list[PersonaConfig]:
        """Public uncached load (for admin API use)."""
        return self._load_all_uncached()

    def load_by_path(self, path: Path) -> PersonaConfig:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError(f"persona file {path} must contain a YAML mapping")
        return PersonaConfig.from_dict(data)

    def get_raw(self, name: str) -> tuple[Path, str]:
        path = self.path_for_name(name)
        return path, path.read_text(encoding="utf-8")

    def path_for_name(self, name: str) -> Path:
        safe_name = self._safe_name(name)
        for suffix in (".yaml", ".yml"):
            path = self.personas_dir / f"{safe_name}{suffix}"
            if path.exists():
                return path
        return self.personas_dir / f"{safe_name}.yaml"

    def save_raw(self, name: str, content: str) -> PersonaConfig:
        data = yaml.safe_load(content) or {}
        if not isinstance(data, dict):
            raise ValueError("persona YAML must be a mapping")
        persona = PersonaConfig.from_dict(data)
        safe_name = self._safe_name(name or persona.name)
        if persona.name != safe_name and SAFE_PERSONA_RE.match(persona.name):
            safe_name = persona.name
        path = self.personas_dir / f"{safe_name}.yaml"
        path.write_text(content, encoding="utf-8")
        self._invalidate_cache()
        return persona

    def rename(self, old_name: str, new_name: str) -> PersonaConfig:
        old_path = self.path_for_name(old_name)
        if not old_path.exists():
            raise FileNotFoundError(f"persona {old_name} does not exist")
        safe_new_name = self._safe_name(new_name)
        existing_new_path = self.path_for_name(safe_new_name)
        if existing_new_path.exists() and existing_new_path.resolve() != old_path.resolve():
            raise FileExistsError(f"persona {safe_new_name} already exists")
        new_path = (
            existing_new_path
            if existing_new_path.exists()
            else self.personas_dir / f"{safe_new_name}.yaml"
        )

        data = yaml.safe_load(old_path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError("persona YAML must be a mapping")
        data["name"] = safe_new_name
        persona = PersonaConfig.from_dict(data)
        new_path.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        if new_path.resolve() != old_path.resolve():
            old_path.unlink()
        self._invalidate_cache()
        return persona

    def delete(self, name: str) -> None:
        path = self.path_for_name(name)
        if path.exists():
            path.unlink()
        self._invalidate_cache()

    def resolve(self, session_id: str) -> PersonaConfig | None:
        personas = self._cached_load_all()
        for persona in personas:
            for binding in persona.activation.session_bindings:
                if binding.session_id == session_id:
                    return persona
        for persona in personas:
            if persona.activation.global_default:
                return persona
        return None

    def summary(self) -> list[dict[str, Any]]:
        items = []
        for path in self.list_files():
            try:
                persona = self.load_by_path(path)
                items.append(
                    {
                        "name": persona.name,
                        "display_name": persona.display_name,
                        "description": persona.description,
                        "file": path.name,
                        "global_default": persona.activation.global_default,
                        "bindings": [
                            item.session_id for item in persona.activation.session_bindings
                        ],
                    }
                )
            except Exception as exc:
                items.append({"name": path.stem, "file": path.name, "error": str(exc)})
        return items

    @staticmethod
    def _safe_name(name: str) -> str:
        name = str(name or "").strip()
        if not name:
            raise ValueError("persona name is required")
        if not SAFE_PERSONA_RE.match(name):
            raise ValueError(
                "persona file names may only contain letters, numbers, dot, underscore, and dash"
            )
        return name

"""Plug-in registry for research sources.

Usage:
    from src.research_sources import registry
    from src.research_sources.base import Source

    @registry.register
    class MySource(Source):
        type_id = "my_source"
        ...

    src = registry.get("my_source", {"key": "value"})
"""
from typing import Any, Dict, List, Optional, Type

from .base import Source


class SourceRegistry:
    def __init__(self) -> None:
        self._types: Dict[str, Type[Source]] = {}

    def register(self, cls: Type[Source]) -> Type[Source]:
        """Register a Source subclass. Use as a class decorator."""
        if not getattr(cls, "type_id", None) or cls.type_id == "base":
            raise ValueError(
                f"{cls.__name__} must define a non-empty class attribute 'type_id'"
            )
        if cls.type_id in self._types:
            existing = self._types[cls.type_id]
            if existing is not cls:
                raise ValueError(
                    f"source type_id '{cls.type_id}' already registered by "
                    f"{existing.__name__}; cannot re-register with {cls.__name__}"
                )
        self._types[cls.type_id] = cls
        return cls

    def get(self, type_id: str, config: Optional[Dict[str, Any]] = None) -> Source:
        """Instantiate a registered source by type_id."""
        if type_id not in self._types:
            raise KeyError(
                f"Unknown research source type: {type_id!r}. "
                f"Registered types: {sorted(self._types)}"
            )
        return self._types[type_id](config or {})

    def list(self) -> List[Dict[str, Any]]:
        """Return a list of {type, name, config_schema} for the UI picker."""
        return [
            {"type": c.type_id, "name": c.display_name, "config_schema": c.config_schema}
            for c in self._types.values()
        ]

    def types(self) -> List[str]:
        return sorted(self._types)

    def unregister(self, type_id: str) -> None:
        """Remove a registered type. Intended for tests."""
        self._types.pop(type_id, None)


# Module-level singleton. Import this everywhere.
registry = SourceRegistry()

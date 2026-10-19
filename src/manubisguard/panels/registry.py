"""Adapter registry; adding a provider must not change application services."""
from __future__ import annotations
from .contracts import PanelAdapter, PanelError


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, PanelAdapter] = {}

    def register(self, adapter: PanelAdapter) -> None:
        key = adapter.kind.strip().lower()
        if not key:
            raise ValueError("Adapter kind cannot be empty")
        if key in self._adapters:
            raise ValueError(f"Adapter already registered: {key}")
        self._adapters[key] = adapter

    def get(self, kind: str) -> PanelAdapter:
        try:
            return self._adapters[kind.strip().lower()]
        except KeyError as exc:
            raise PanelError(f"No adapter registered for panel type: {kind}") from exc

    def kinds(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))


def default_registry(secret_resolver=None) -> AdapterRegistry:
    from .adapters.pasarguard import PasarGuardAdapter
    from .adapters.rebecca import RebeccaAdapter
    from .adapters.sanaei import SanaeiAdapter
    registry = AdapterRegistry()
    registry.register(PasarGuardAdapter(secret_resolver=secret_resolver))
    registry.register(RebeccaAdapter(secret_resolver=secret_resolver))
    registry.register(SanaeiAdapter(secret_resolver=secret_resolver))
    return registry

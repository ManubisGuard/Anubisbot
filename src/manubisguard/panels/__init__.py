"""Panel abstraction and adapter registry."""
from .contracts import PanelAdapter, PanelCapabilities, PanelConnection, PanelError, PanelUser, UserCreate, UserUpdate
from .registry import AdapterRegistry, default_registry

__all__ = ["PanelAdapter", "PanelCapabilities", "PanelConnection", "PanelError", "PanelUser", "UserCreate", "UserUpdate", "AdapterRegistry", "default_registry"]

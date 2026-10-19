from manubisguard.panels.registry import AdapterRegistry, default_registry
from manubisguard.panels.contracts import PanelError


class DummyAdapter:
    kind = "dummy"


def test_default_registry_registers_only_implemented_adapter():
    registry = default_registry()
    assert registry.kinds() == ("pasarguard", "rebecca", "sanaei")
    assert registry.get("PASARGUARD").kind == "pasarguard"


def test_registry_rejects_duplicates():
    registry = AdapterRegistry()
    registry.register(DummyAdapter())
    try:
        registry.register(DummyAdapter())
    except ValueError:
        pass
    else:
        raise AssertionError("duplicate registration should fail")


def test_missing_adapter_is_explicit():
    registry = default_registry()
    try:
        registry.get("unimplemented-provider")
    except PanelError as exc:
        assert "No adapter registered" in str(exc)
    else:
        raise AssertionError("missing adapter should fail explicitly")

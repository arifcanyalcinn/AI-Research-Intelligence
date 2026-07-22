"""
Unit tests for SourceRegistry.

Tests verify:
  - Enabled sources are discovered via BaseSource.__subclasses__().
  - Disabled sources (enabled=False in config) are excluded.
  - Sources whose __init__ raises are skipped; others are unaffected.
  - get_active_sources() returns only the registered instances.
  - get_source() returns the right instance or None.

No real source implementations are imported here — all tests use minimal
stub subclasses defined in this module.  This keeps the tests independent
of the source batch being integrated.

IMPORTANT: BaseSource.__subclasses__() is global interpreter state.  Stub
classes defined here persist for the lifetime of the test session.  Tests
assert on the *presence* of specific sources by source_id, not on the total
count, so they are robust against stubs from other test modules appearing in
__subclasses__().
"""

from __future__ import annotations

from pathlib import Path

from arip.config import SourceConfig, load_settings
from arip.entities import NormalizedItem, RawSourcePayload
from arip.enums import SourceType
from arip.interfaces import BaseSource
from arip.sources.registry import SourceRegistry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _settings(tmp_path: Path):
    """Return a minimal AppSettings with only the required llm.model_name set."""
    p = tmp_path / "settings.yaml"
    p.write_text('llm:\n  model_name: "test-model"\n', encoding="utf-8")
    return load_settings(yaml_path=p)


def _inject_source_config(config, source_id: str, source_config: SourceConfig) -> None:
    """Inject a SourceConfig entry for a test-only source_id.

    ``SourcesSettings`` only declares fields for the six production sources.
    For test stubs we set the attribute directly on the instance so that
    ``getattr(config.sources, source_id, None)`` returns the injected value.
    ``object.__setattr__`` bypasses Pydantic's validation (which would reject
    undeclared field names on a strict model).
    """
    object.__setattr__(config.sources, source_id, source_config)


# ---------------------------------------------------------------------------
# Stub source definitions
# (defined at module level so they appear in BaseSource.__subclasses__())
# ---------------------------------------------------------------------------


class _StubAlpha(BaseSource):
    """Minimal valid source — always enabled (no config block)."""

    source_id = "_stub_alpha"
    source_type = SourceType.PAPER

    def fetch(self) -> list[RawSourcePayload]:
        return []

    def normalize(self, payload: RawSourcePayload) -> NormalizedItem:  # pragma: no cover
        raise NotImplementedError

    @classmethod
    def get_config_schema(cls) -> type:
        return SourceConfig


class _StubBeta(BaseSource):
    """Source whose config block will be toggled in tests."""

    source_id = "_stub_beta"
    source_type = SourceType.MODEL

    def fetch(self) -> list[RawSourcePayload]:
        return []

    def normalize(self, payload: RawSourcePayload) -> NormalizedItem:  # pragma: no cover
        raise NotImplementedError

    @classmethod
    def get_config_schema(cls) -> type:
        return SourceConfig


class _StubCrashing(BaseSource):
    """Source whose __init__ always raises — simulates a broken plugin."""

    source_id = "_stub_crashing"
    source_type = SourceType.REPO

    def __init__(self, config: object | None) -> None:
        raise RuntimeError("Simulated __init__ failure")

    def fetch(self) -> list[RawSourcePayload]:  # pragma: no cover
        return []

    def normalize(self, payload: RawSourcePayload) -> NormalizedItem:  # pragma: no cover
        raise NotImplementedError

    @classmethod
    def get_config_schema(cls) -> type:
        return SourceConfig


# ---------------------------------------------------------------------------
# Tests: basic discovery
# ---------------------------------------------------------------------------


def test_enabled_source_is_registered(tmp_path: Path) -> None:
    """A source with no config block (defaults to enabled=True) is registered."""
    config = _settings(tmp_path)
    registry = SourceRegistry(config)

    assert registry.get_source("_stub_alpha") is not None


def test_get_active_sources_contains_registered_source(tmp_path: Path) -> None:
    """get_active_sources() includes the registered stub source."""
    config = _settings(tmp_path)
    registry = SourceRegistry(config)

    ids = {s.source_id for s in registry.get_active_sources()}
    assert "_stub_alpha" in ids


def test_get_source_returns_correct_instance(tmp_path: Path) -> None:
    """get_source() returns a BaseSource instance with the requested source_id."""
    config = _settings(tmp_path)
    registry = SourceRegistry(config)

    source = registry.get_source("_stub_alpha")
    assert isinstance(source, BaseSource)
    assert source.source_id == "_stub_alpha"


def test_get_source_returns_none_for_unknown_id(tmp_path: Path) -> None:
    """get_source() returns None when the source_id is not registered."""
    config = _settings(tmp_path)
    registry = SourceRegistry(config)

    assert registry.get_source("does_not_exist") is None


# ---------------------------------------------------------------------------
# Tests: disabled sources
# ---------------------------------------------------------------------------


def test_disabled_source_is_excluded(tmp_path: Path) -> None:
    """A source with enabled=False in its config is not registered."""
    config = _settings(tmp_path)
    _inject_source_config(config, "_stub_beta", SourceConfig(enabled=False))

    registry = SourceRegistry(config)

    assert registry.get_source("_stub_beta") is None
    ids = {s.source_id for s in registry.get_active_sources()}
    assert "_stub_beta" not in ids


def test_explicit_enabled_true_registers_source(tmp_path: Path) -> None:
    """An explicit enabled=True in config still registers the source."""
    config = _settings(tmp_path)
    _inject_source_config(config, "_stub_beta", SourceConfig(enabled=True))

    registry = SourceRegistry(config)

    assert registry.get_source("_stub_beta") is not None


# ---------------------------------------------------------------------------
# Tests: graceful handling of broken sources
# ---------------------------------------------------------------------------


def test_crashing_init_does_not_stop_other_sources(tmp_path: Path) -> None:
    """A source whose __init__ raises does not prevent other sources from loading."""
    config = _settings(tmp_path)
    registry = SourceRegistry(config)

    # _stub_crashing raises in __init__ — it must be absent
    assert registry.get_source("_stub_crashing") is None
    # _stub_alpha is unaffected — it must still be present
    assert registry.get_source("_stub_alpha") is not None


def test_crashing_source_excluded_from_active_list(tmp_path: Path) -> None:
    """get_active_sources() does not include sources that failed to init."""
    config = _settings(tmp_path)
    registry = SourceRegistry(config)

    ids = {s.source_id for s in registry.get_active_sources()}
    assert "_stub_crashing" not in ids


# ---------------------------------------------------------------------------
# Tests: combined scenarios
# ---------------------------------------------------------------------------


def test_disabled_does_not_affect_other_sources(tmp_path: Path) -> None:
    """Disabling one source leaves other sources unaffected."""
    config = _settings(tmp_path)
    _inject_source_config(config, "_stub_alpha", SourceConfig(enabled=False))
    _inject_source_config(config, "_stub_beta", SourceConfig(enabled=True))

    registry = SourceRegistry(config)

    assert registry.get_source("_stub_alpha") is None
    assert registry.get_source("_stub_beta") is not None


def test_both_stubs_disabled(tmp_path: Path) -> None:
    """Disabling both stubs removes them from the active list."""
    config = _settings(tmp_path)
    _inject_source_config(config, "_stub_alpha", SourceConfig(enabled=False))
    _inject_source_config(config, "_stub_beta", SourceConfig(enabled=False))

    registry = SourceRegistry(config)

    ids = {s.source_id for s in registry.get_active_sources()}
    assert "_stub_alpha" not in ids
    assert "_stub_beta" not in ids

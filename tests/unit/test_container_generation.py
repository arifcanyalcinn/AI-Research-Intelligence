"""
Unit tests for the generation wiring in container.py (SDS §5.15.3, §5.8; rulings Q10, Q16, Q14).

``_build_generation()`` is tested on its own: the rest of ``build_app_components()``
needs a database and the source registry. Settings are loaded from a temporary
YAML in a temporary directory, so no .env file is read. No model is loaded: the
registry hands out an unloaded backend.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from sqlalchemy import create_engine

from arip.config import load_settings
from arip.container import _build_generation
from arip.db.database import build_session_factory, init_db
from arip.enums import Language
from arip.exceptions import ConfigError
from arip.pipeline.stages.generate import GenerateStage

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ARIP_GENERATION__ENABLED", raising=False)
    return tmp_path


def prompts_dir(root: Path, *languages: str) -> Path:
    """A template directory holding the shipped thread template for each language."""
    text = (REPO / "config" / "prompts" / "thread_en.j2").read_text(encoding="utf-8")
    directory = root / "prompts"
    directory.mkdir()
    for code in languages:
        body = text.replace("language: EN", f"language: {code}", 1)
        (directory / f"thread_{code.lower()}.j2").write_bytes(body.encode("utf-8"))
    return directory


def settings(root: Path, enabled: bool, backend: str = "stub", languages: list[str] | None = None):  # noqa: ANN201
    data = {
        "pipeline": {"languages": languages or ["EN"], "max_items_per_run": 7},
        "llm": {"backend": backend, "model_name": "Qwen/Qwen2.5-3B-Instruct"},
        "generation": {"enabled": enabled, "max_retries": 1},
    }
    path = root / "settings.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return load_settings(path)


def test_enabled_wiring_returns_a_stage_factory(in_tmp: Path) -> None:
    wiring = _build_generation(settings(in_tmp, True), prompts_dir(in_tmp, "EN"))

    assert wiring is not None
    registry, factory = wiring
    assert registry.backend_id == "stub"
    assert not registry.get_backend().is_loaded
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    stage = factory(session_factory=build_session_factory(engine))
    assert isinstance(stage, GenerateStage)
    assert stage._languages == [Language.EN]
    assert stage._max_items_per_run == 7
    assert stage._max_retries == 1
    engine.dispose()


def test_disabled_builds_nothing(in_tmp: Path) -> None:
    """Disabled: no registry, no templates read, so neither a bad backend nor a missing
    template directory is an error (ruling Q2)."""
    loaded = settings(in_tmp, False, backend="no-such-backend", languages=["XX"])

    assert _build_generation(loaded, in_tmp / "missing") is None


def test_missing_template_is_config_error_at_wiring(in_tmp: Path) -> None:
    loaded = settings(in_tmp, True, languages=["EN", "TR"])

    with pytest.raises(ConfigError, match="No prompt template 'thread' for language TR"):
        _build_generation(loaded, prompts_dir(in_tmp, "EN"))


def test_unknown_language_is_config_error(in_tmp: Path) -> None:
    loaded = settings(in_tmp, True, languages=["EN", "XX"])

    with pytest.raises(ConfigError, match="Unknown language 'XX' in pipeline.languages"):
        _build_generation(loaded, prompts_dir(in_tmp, "EN"))


def test_repeated_language_is_config_error(in_tmp: Path) -> None:
    loaded = settings(in_tmp, True, languages=["EN", "EN"])

    with pytest.raises(ConfigError, match="listed twice"):
        _build_generation(loaded, prompts_dir(in_tmp, "EN"))


def test_unknown_backend_is_config_error_at_wiring(in_tmp: Path) -> None:
    loaded = settings(in_tmp, True, backend="no-such-backend")

    with pytest.raises(ConfigError, match="Unknown LLM backend 'no-such-backend'"):
        _build_generation(loaded, prompts_dir(in_tmp, "EN"))


def test_missing_template_directory_is_config_error(in_tmp: Path) -> None:
    with pytest.raises(ConfigError, match="Prompt template directory not found"):
        _build_generation(settings(in_tmp, True), in_tmp / "missing")

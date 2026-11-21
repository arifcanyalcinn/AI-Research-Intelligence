"""
Unit tests for the Batch 11 Stage 6 configuration (SDS 5.15.2).

The generation section (GenerationSettings, ValidationConfig), the value
ranges of pipeline.max_items_per_run and llm.generation, and the three
check-config lines. Tests that go through load_settings() run in a temporary
directory, so no .env file is read.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from arip.config import (
    SEED_MAX,
    GenerationParams,
    GenerationSettings,
    PipelineSettings,
    ValidationConfig,
    load_settings,
    print_settings_summary,
)
from arip.exceptions import ConfigError

REPO = Path(__file__).resolve().parents[2]


def write_yaml(path: Path, data: dict) -> Path:
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


@pytest.fixture
def in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run in an empty directory: load_settings() then finds no .env."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# pipeline.max_items_per_run (S3)
# ---------------------------------------------------------------------------


def test_max_items_per_run_below_one_is_rejected() -> None:
    with pytest.raises(ValidationError):
        PipelineSettings(max_items_per_run=0)
    assert PipelineSettings(max_items_per_run=1).max_items_per_run == 1


def test_out_of_range_value_is_config_error(in_tmp: Path) -> None:
    path = write_yaml(
        in_tmp / "settings.yaml",
        {"llm": {"model_name": "m"}, "pipeline": {"max_items_per_run": 0}},
    )
    with pytest.raises(ConfigError, match="max_items_per_run"):
        load_settings(path)


# ---------------------------------------------------------------------------
# llm.generation ranges (D-011 scope)
# ---------------------------------------------------------------------------


def test_negative_temperature_is_rejected() -> None:
    with pytest.raises(ValidationError):
        GenerationParams(temperature=-0.1)


def test_zero_temperature_is_accepted() -> None:
    """0 means greedy decoding (D-011)."""
    assert GenerationParams(temperature=0).temperature == 0


def test_top_p_bounds() -> None:
    for bad in (0, -0.1, 1.01):
        with pytest.raises(ValidationError):
            GenerationParams(top_p=bad)
    assert GenerationParams(top_p=1.0).top_p == 1.0
    assert GenerationParams(top_p=0.01).top_p == 0.01


def test_max_new_tokens_below_one_is_rejected() -> None:
    with pytest.raises(ValidationError):
        GenerationParams(max_new_tokens=0)
    assert GenerationParams(max_new_tokens=1).max_new_tokens == 1


def test_repetition_penalty_must_be_positive() -> None:
    for bad in (0, -1.0):
        with pytest.raises(ValidationError):
            GenerationParams(repetition_penalty=bad)
    assert GenerationParams(repetition_penalty=0.5).repetition_penalty == 0.5


def test_seed_range() -> None:
    assert SEED_MAX == 2**32 - 1
    for good in (None, 0, 42, 2**32 - 1):
        assert GenerationParams(seed=good).seed == good
    for bad in (-1, 2**32):
        with pytest.raises(ValidationError):
            GenerationParams(seed=bad)


def test_stop_sequences_reject_only_empty() -> None:
    with pytest.raises(ValidationError, match="empty string"):
        GenerationParams(stop_sequences=["END", ""])
    assert GenerationParams(stop_sequences=["\n\n", " ", "END"]).stop_sequences == [
        "\n\n",
        " ",
        "END",
    ]


# ---------------------------------------------------------------------------
# generation section (A2, A3)
# ---------------------------------------------------------------------------


def test_generation_defaults() -> None:
    settings = GenerationSettings()
    assert settings.max_retries == 2
    assert settings.validation.max_full_text_chars == 3000
    assert settings.validation.prohibited_phrases == []


def test_max_retries_below_zero_is_rejected() -> None:
    with pytest.raises(ValidationError):
        GenerationSettings(max_retries=-1)
    assert GenerationSettings(max_retries=0).max_retries == 0


def test_max_full_text_chars_below_one_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ValidationConfig(max_full_text_chars=0)
    assert ValidationConfig(max_full_text_chars=1).max_full_text_chars == 1


def test_blank_prohibited_phrase_is_rejected() -> None:
    for bad in ([""], ["ok", "   "], ["\n"]):
        with pytest.raises(ValidationError, match="whitespace-only"):
            ValidationConfig(prohibited_phrases=bad)
    assert ValidationConfig(prohibited_phrases=[" game changer "]).prohibited_phrases == [
        " game changer "
    ]


def test_yaml_without_generation_section_uses_defaults(in_tmp: Path) -> None:
    path = write_yaml(in_tmp / "settings.yaml", {"llm": {"model_name": "m"}})

    settings = load_settings(path)

    assert settings.generation.max_retries == 2
    assert settings.generation.validation.max_full_text_chars == 3000
    assert settings.generation.validation.prohibited_phrases == []


def test_yaml_generation_section_is_read(in_tmp: Path) -> None:
    path = write_yaml(
        in_tmp / "settings.yaml",
        {
            "llm": {"model_name": "m"},
            "generation": {
                "max_retries": 0,
                "validation": {"max_full_text_chars": 1200, "prohibited_phrases": ["x"]},
            },
        },
    )

    settings = load_settings(path)

    assert settings.generation.max_retries == 0
    assert settings.generation.validation.max_full_text_chars == 1200
    assert settings.generation.validation.prohibited_phrases == ["x"]


def test_shipped_settings_generation_section() -> None:
    """Parsed directly: load_settings() would also read .env and environment variables."""
    data = yaml.safe_load((REPO / "config" / "settings.yaml").read_text(encoding="utf-8"))

    assert data["generation"] == {
        "max_retries": 2,
        "validation": {"max_full_text_chars": 3000, "prohibited_phrases": []},
    }
    GenerationSettings(**data["generation"])
    GenerationParams(**data["llm"]["generation"])
    PipelineSettings(**data["pipeline"])


# ---------------------------------------------------------------------------
# check-config (ruling Q18)
# ---------------------------------------------------------------------------


def test_check_config_prints_generation_lines(in_tmp: Path) -> None:
    path = write_yaml(
        in_tmp / "settings.yaml",
        {
            "llm": {"model_name": "m"},
            "generation": {
                "max_retries": 4,
                "validation": {"max_full_text_chars": 2500, "prohibited_phrases": ["a", "b"]},
            },
        },
    )
    out = io.StringIO()

    print_settings_summary(load_settings(path), file=out)

    lines = out.getvalue().splitlines()
    assert "  Max retries       : 4" in lines
    assert "  Max text chars    : 2500" in lines
    assert "  Blocked phrases   : 2" in lines

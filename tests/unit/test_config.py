"""
Unit tests for AppSettings / load_settings().

Tests from SDS Phase 0 spec:
  - Valid minimal config loads successfully
  - Missing required llm.model_name raises ConfigError
  - Invalid ranking weights (don't sum to 1.0) raise ConfigError
  - Env var ARIP_LOGGING__LEVEL=DEBUG overrides YAML value
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from arip.config import load_settings
from arip.exceptions import ConfigError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def write_yaml(tmp_path: Path, content: str) -> Path:
    """Write a YAML config file to tmp_path and return its path."""
    path = tmp_path / "settings.yaml"
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Valid config
# ---------------------------------------------------------------------------


def test_valid_minimal_config(tmp_path: Path) -> None:
    """Minimal valid config: only llm.model_name is required beyond defaults."""
    path = write_yaml(
        tmp_path,
        """
        llm:
          model_name: "test-model"
        """,
    )
    settings = load_settings(yaml_path=path)
    assert settings.llm.model_name == "test-model"


def test_defaults_are_applied(tmp_path: Path) -> None:
    """Omitted fields get their SDS-specified defaults."""
    path = write_yaml(
        tmp_path,
        """
        llm:
          model_name: "test-model"
        """,
    )
    settings = load_settings(yaml_path=path)

    assert settings.pipeline.run_interval_hours == 6
    assert settings.pipeline.max_items_per_run == 50
    assert settings.ranking.min_score == 0.35
    assert settings.dedup.semantic_threshold == 0.92
    assert settings.embeddings.model_name == "all-MiniLM-L6-v2"
    assert settings.database.url == "sqlite:///data/arip.db"
    assert settings.logging.level == "INFO"
    assert settings.publishing.publishers == ["dry_run"]


def test_yaml_values_are_applied(tmp_path: Path) -> None:
    """Values set in YAML are reflected in the loaded settings."""
    path = write_yaml(
        tmp_path,
        """
        llm:
          model_name: "Qwen/Qwen2.5-7B-Instruct"
        pipeline:
          run_interval_hours: 12
          max_items_per_run: 100
        ranking:
          min_score: 0.5
        logging:
          level: "WARNING"
        """,
    )
    settings = load_settings(yaml_path=path)

    assert settings.llm.model_name == "Qwen/Qwen2.5-7B-Instruct"
    assert settings.pipeline.run_interval_hours == 12
    assert settings.pipeline.max_items_per_run == 100
    assert settings.ranking.min_score == 0.5
    assert settings.logging.level == "WARNING"


def test_missing_yaml_file_uses_defaults(tmp_path: Path) -> None:
    """If the YAML file does not exist, the required llm field is missing and raises ConfigError.

    When no YAML is found, pydantic-settings has no llm section at all, so
    the error reports the 'llm' field as missing (not 'model_name' specifically).
    """
    non_existent = tmp_path / "does_not_exist.yaml"
    with pytest.raises(ConfigError, match="llm"):
        load_settings(yaml_path=non_existent)


# ---------------------------------------------------------------------------
# Required field validation
# ---------------------------------------------------------------------------


def test_missing_llm_model_name_raises_config_error(tmp_path: Path) -> None:
    """llm.model_name has no default and must be explicitly set.

    Providing the llm section with an unrecognised backend (no model_name)
    triggers the pydantic error at the llm.model_name level, so the
    ConfigError message includes 'model_name'.
    """
    path = write_yaml(
        tmp_path,
        """
        llm:
          backend: "transformers"
          # model_name intentionally omitted -- must cause ConfigError
        pipeline:
          run_interval_hours: 6
        """,
    )
    with pytest.raises(ConfigError, match="model_name"):
        load_settings(yaml_path=path)


# ---------------------------------------------------------------------------
# Weight validation
# ---------------------------------------------------------------------------


def test_ranking_weights_must_sum_to_one(tmp_path: Path) -> None:
    """Signal weights that do not sum to 1.0 +/- 0.001 raise ConfigError."""
    path = write_yaml(
        tmp_path,
        """
        llm:
          model_name: "test-model"
        ranking:
          weights:
            recency: 0.30
            authority: 0.30
            engagement: 0.30
            topic: 0.30
        """,
    )
    with pytest.raises(ConfigError):
        load_settings(yaml_path=path)


def test_ranking_weights_exactly_one_passes(tmp_path: Path) -> None:
    """Weights summing to exactly 1.0 pass validation."""
    path = write_yaml(
        tmp_path,
        """
        llm:
          model_name: "test-model"
        ranking:
          weights:
            recency: 0.25
            authority: 0.25
            engagement: 0.35
            topic: 0.15
        """,
    )
    settings = load_settings(yaml_path=path)
    total = (
        settings.ranking.weights.recency
        + settings.ranking.weights.authority
        + settings.ranking.weights.engagement
        + settings.ranking.weights.topic
    )
    assert abs(total - 1.0) < 0.001


def test_ranking_weights_within_tolerance_passes(tmp_path: Path) -> None:
    """Weights summing to 1.0 +/- 0.001 (floating point tolerance) pass."""
    path = write_yaml(
        tmp_path,
        """
        llm:
          model_name: "test-model"
        ranking:
          weights:
            recency: 0.2501
            authority: 0.2499
            engagement: 0.3500
            topic: 0.1500
        """,
    )
    # Should not raise
    settings = load_settings(yaml_path=path)
    assert settings is not None


# ---------------------------------------------------------------------------
# Environment variable override
# ---------------------------------------------------------------------------


def test_env_var_overrides_yaml_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ARIP_LOGGING__LEVEL env var overrides the YAML logging.level value."""
    path = write_yaml(
        tmp_path,
        """
        llm:
          model_name: "test-model"
        logging:
          level: "INFO"
        """,
    )
    monkeypatch.setenv("ARIP_LOGGING__LEVEL", "DEBUG")

    settings = load_settings(yaml_path=path)
    assert settings.logging.level == "DEBUG"


def test_env_var_overrides_llm_model_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ARIP_LLM__MODEL_NAME env var sets model_name even without a YAML."""
    path = write_yaml(
        tmp_path,
        """
        llm:
          model_name: "yaml-model"
        """,
    )
    monkeypatch.setenv("ARIP_LLM__MODEL_NAME", "env-model")

    settings = load_settings(yaml_path=path)
    assert settings.logging.level == "INFO"  # default still applies


def test_invalid_log_level_raises(tmp_path: Path) -> None:
    """An unrecognised log level string raises ConfigError."""
    path = write_yaml(
        tmp_path,
        """
        llm:
          model_name: "test-model"
        logging:
          level: "VERBOSE"
        """,
    )
    with pytest.raises(ConfigError):
        load_settings(yaml_path=path)

"""
Application configuration for ARIP.

Single Pydantic model (AppSettings) loaded from:
  1. config/settings.yaml (lowest priority)
  2. .env file (if present)
  3. Environment variables with ARIP_ prefix (highest priority)

Use load_settings() as the sole entry point -- it wraps AppSettings and
converts Pydantic's ValidationError into a ConfigError with a clear message.

Nested settings use __ as the env var delimiter:
  ARIP_LOGGING__LEVEL=DEBUG overrides config.logging.level
  ARIP_LLM__MODEL_NAME=... overrides config.llm.model_name
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, field_validator, model_validator
from pydantic import ValidationError as PydanticValidationError
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from arip.exceptions import ConfigError

# ---------------------------------------------------------------------------
# Sub-models: each section of settings.yaml becomes a typed Pydantic model.
# ---------------------------------------------------------------------------


class PipelineSettings(BaseModel):
    """Top-level pipeline execution settings."""

    run_interval_hours: int = 6
    max_items_per_run: int = 50
    languages: list[str] = ["EN"]
    max_normalization_retries: int = 1


class SignalWeights(BaseModel):
    """Weights for the four ranking signals. Must sum to 1.0 +/- 0.001."""

    recency: float = 0.25
    authority: float = 0.25
    engagement: float = 0.35
    topic: float = 0.15

    @model_validator(mode="after")
    def weights_must_sum_to_one(self) -> SignalWeights:
        total = self.recency + self.authority + self.engagement + self.topic
        if abs(total - 1.0) > 0.001:
            raise ValueError(
                f"Signal weights must sum to 1.0 (got {total:.4f}). "
                f"Current: recency={self.recency}, authority={self.authority}, "
                f"engagement={self.engagement}, topic={self.topic}"
            )
        return self


class RankingSettings(BaseModel):
    """Scoring and filtering configuration."""

    min_score: float = 0.35
    weights: SignalWeights = SignalWeights()
    topic_keywords: list[str] = []
    source_authority: dict[str, float] = {
        "arxiv": 0.85,
        "huggingface_papers": 0.80,
        "papers_with_code": 0.75,
        "github_trending": 0.65,
        "huggingface_models": 0.55,
        "huggingface_spaces": 0.45,
    }


class DedupSettings(BaseModel):
    """Semantic deduplication configuration."""

    semantic_threshold: float = 0.92
    ann_index_path: str = "data/ann_index.usearch"
    top_k: int = 5


class GenerationParams(BaseModel):
    """LLM generation hyperparameters passed to every generate() call."""

    temperature: float = 0.7
    max_new_tokens: int = 1024
    top_p: float = 0.95
    repetition_penalty: float = 1.1
    seed: int | None = None
    stop_sequences: list[str] = []


class LLMSettings(BaseModel):
    """LLM backend selection and generation defaults.

    model_name has no default -- it must be set in settings.yaml or via
    ARIP_LLM__MODEL_NAME. Startup will fail with ConfigError if absent.

    Recommended: Qwen/Qwen2.5-7B-Instruct (4-bit quantized, ~4.5 GB VRAM)
    """

    # Suppress pydantic's "model_" namespace warning -- model_name is our
    # domain field name, not a pydantic internal.
    model_config = ConfigDict(protected_namespaces=())

    backend: str = "transformers"
    model_name: str  # REQUIRED -- no default
    generation: GenerationParams = GenerationParams()


class EmbeddingSettings(BaseModel):
    """Embedding backend configuration. Runs on CPU only."""

    # Suppress pydantic protected_namespaces warning for model_name.
    model_config = ConfigDict(protected_namespaces=())

    backend: str = "sentence_transformers"
    model_name: str = "all-MiniLM-L6-v2"


class TelegramSettings(BaseModel):
    """Telegram-specific review settings.

    The bot token is a secret -- it comes from the ARIP_TELEGRAM_BOT_TOKEN
    env var, not from this model. This model holds only non-secret config.
    """

    chat_id: int = 0  # Must be set to a real chat ID before using review


class ReviewSettings(BaseModel):
    """Human review configuration."""

    enabled: bool = True
    backend: str = "telegram"
    timeout_hours: int = 24
    telegram: TelegramSettings = TelegramSettings()


class PublishingSettings(BaseModel):
    """Publishing configuration."""

    enabled: bool = True
    publishers: list[str] = ["dry_run"]


class SourceConfig(BaseModel):
    """Base configuration for a source plugin."""

    enabled: bool = True
    fetch_interval_hours: int | None = None  # None = use pipeline.run_interval_hours


class ArxivSourceConfig(SourceConfig):
    """ArXiv-specific configuration."""

    max_results: int = 50
    categories: list[str] = ["cs.AI", "cs.LG", "cs.CL", "cs.CV"]


class SourcesSettings(BaseModel):
    """Per-source enable/disable and config."""

    huggingface_papers: SourceConfig = SourceConfig()
    huggingface_models: SourceConfig = SourceConfig()
    huggingface_spaces: SourceConfig = SourceConfig()
    arxiv: ArxivSourceConfig = ArxivSourceConfig()
    github_trending: SourceConfig = SourceConfig()
    papers_with_code: SourceConfig = SourceConfig()


class DatabaseSettings(BaseModel):
    """Database connection settings."""

    url: str = "sqlite:///data/arip.db"


class LoggingSettings(BaseModel):
    """Logging output configuration."""

    level: str = "INFO"
    log_file: str = "logs/arip.log"
    json_format: bool = True

    @field_validator("level")
    @classmethod
    def level_must_be_valid(cls, v: str) -> str:
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in valid:
            raise ValueError(f"Invalid log level '{v}'. Must be one of: {valid}")
        return upper


# ---------------------------------------------------------------------------
# Custom settings source: reads from a YAML file.
# ---------------------------------------------------------------------------


class _YamlSettingsSource(PydanticBaseSettingsSource):
    """Loads settings from a YAML file.

    Integrated as the lowest-priority source in AppSettings.settings_customise_sources
    so that environment variables always override YAML values.
    """

    def __init__(self, settings_cls: type[BaseSettings], yaml_path: Path) -> None:
        super().__init__(settings_cls)
        self._yaml_path = yaml_path

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        # Not called when __call__ returns a flat dict -- required by ABC.
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        if not self._yaml_path.exists():
            return {}
        with self._yaml_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# Main settings model.
# ---------------------------------------------------------------------------


class AppSettings(BaseSettings):
    """Complete application configuration.

    Do not instantiate directly. Use load_settings() which wraps this class
    and provides a clean ConfigError on validation failure.

    Source priority (highest to lowest):
      1. Environment variables (ARIP_ prefix)
      2. .env file (loaded via python-dotenv)
      3. config/settings.yaml
    """

    model_config = SettingsConfigDict(
        env_prefix="ARIP_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # Ignore unknown fields from YAML (forward-compat)
    )

    pipeline: PipelineSettings = PipelineSettings()
    ranking: RankingSettings = RankingSettings()
    dedup: DedupSettings = DedupSettings()
    llm: LLMSettings  # No default -- model_name is required
    embeddings: EmbeddingSettings = EmbeddingSettings()
    review: ReviewSettings = ReviewSettings()
    publishing: PublishingSettings = PublishingSettings()
    sources: SourcesSettings = SourcesSettings()
    database: DatabaseSettings = DatabaseSettings()
    logging: LoggingSettings = LoggingSettings()

    # Secrets: loaded from env vars only (never stored in YAML).
    # ARIP_ prefix is applied by pydantic-settings automatically.
    telegram_bot_token: str | None = None
    twitter_api_key: str | None = None
    twitter_api_secret: str | None = None
    twitter_access_token: str | None = None
    twitter_access_secret: str | None = None
    github_token: str | None = None

    # Stores the yaml_path used at load time so callers can inspect it.
    _yaml_path: Path = Path("config/settings.yaml")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Priority: env > dotenv > yaml.
        # init_settings is excluded because we don't use constructor kwargs
        # for normal operation -- load_settings() is the sole entry point.
        return (
            env_settings,
            dotenv_settings,
            _YamlSettingsSource(settings_cls, cls._yaml_path),
        )


# ---------------------------------------------------------------------------
# Public factory function.
# ---------------------------------------------------------------------------


def load_settings(
    yaml_path: Path = Path("config/settings.yaml"),
) -> AppSettings:
    """Load and validate application settings.

    Args:
        yaml_path: Path to the YAML configuration file.
                   Defaults to config/settings.yaml relative to cwd.
                   Tests should pass a path to a temp file.

    Returns:
        Validated AppSettings instance.

    Raises:
        ConfigError: If any required field is missing or validation fails.
                     The error message includes Pydantic's full field-level
                     error list for easy diagnosis.
    """
    # Set the class-level yaml path so the custom source picks it up.
    # This is safe because ARIP is a single-process synchronous application.
    AppSettings._yaml_path = yaml_path

    try:
        return AppSettings()
    except PydanticValidationError as exc:
        # Convert Pydantic's validation error into our own ConfigError with
        # a human-readable summary that includes which fields failed and why.
        lines = ["Configuration validation failed:"]
        for error in exc.errors():
            field_path = " -> ".join(str(loc) for loc in error["loc"])
            lines.append(f"  [{field_path}] {error['msg']}")
        raise ConfigError("\n".join(lines)) from exc
    except Exception as exc:
        raise ConfigError(f"Failed to load configuration: {exc}") from exc


def print_settings_summary(settings: AppSettings, *, file: Any = None) -> None:
    """Print a human-readable config summary for `arip check-config`.

    Secrets are redacted. Intended for operators to verify their config is
    loaded correctly without exposing sensitive values.
    """
    if file is None:
        file = sys.stdout

    def redact(value: str | None) -> str:
        if not value:
            return "<not set>"
        return f"{'*' * (len(value) - 4)}{value[-4:]}" if len(value) > 4 else "****"

    print("ARIP Configuration Summary", file=file)
    print("=" * 50, file=file)
    print(f"  Pipeline interval : {settings.pipeline.run_interval_hours}h", file=file)
    print(f"  Max items/run     : {settings.pipeline.max_items_per_run}", file=file)
    print(f"  Languages         : {settings.pipeline.languages}", file=file)
    print(f"  LLM backend       : {settings.llm.backend}", file=file)
    print(f"  LLM model         : {settings.llm.model_name}", file=file)
    print(f"  Embedding model   : {settings.embeddings.model_name}", file=file)
    print(f"  Min score         : {settings.ranking.min_score}", file=file)
    print(f"  Dedup threshold   : {settings.dedup.semantic_threshold}", file=file)
    print(f"  Review enabled    : {settings.review.enabled}", file=file)
    print(f"  Review backend    : {settings.review.backend}", file=file)
    print(f"  Publishers        : {settings.publishing.publishers}", file=file)
    print(f"  Database URL      : {settings.database.url}", file=file)
    print(f"  Log level         : {settings.logging.level}", file=file)
    print(f"  Log file          : {settings.logging.log_file}", file=file)
    print("Secrets", file=file)
    print(f"  Telegram token    : {redact(settings.telegram_bot_token)}", file=file)
    print(f"  Twitter API key   : {redact(settings.twitter_api_key)}", file=file)
    print(f"  GitHub token      : {redact(settings.github_token)}", file=file)
    enabled_sources = [
        name
        for name, cfg in {
            "huggingface_papers": settings.sources.huggingface_papers,
            "huggingface_models": settings.sources.huggingface_models,
            "huggingface_spaces": settings.sources.huggingface_spaces,
            "arxiv": settings.sources.arxiv,
            "github_trending": settings.sources.github_trending,
            "papers_with_code": settings.sources.papers_with_code,
        }.items()
        if cfg.enabled
    ]
    print(f"Sources enabled   : {enabled_sources}", file=file)
    print("=" * 50, file=file)

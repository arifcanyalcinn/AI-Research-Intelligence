"""
PromptManager — loads, checks and renders the Jinja2 prompt templates (SDS 5.8, AD-07, D-013).

**Files.** One file per use case and language in ``config/prompts/``, named
``{template_id}_{language code}.j2`` (``thread_en.j2``). A file is a YAML
header, a line holding only ``---``, then the Jinja2 body. Files are read with
universal newlines, so CRLF and LF files load alike.

**Header (D-013).** Exactly these keys, all required: ``template_id``,
``language``, ``description``, ``required_variables``, ``optional_variables``
and ``system`` (the system message, SDS 5.9.1; must not be blank). At load,
each of these raises ``ConfigError``: an unknown or missing key, a filename that
does not match ``template_id`` and ``language``, a variable that is both
required and optional, a Jinja2 syntax error, and any difference between the
variables the body uses and the variables the header declares (so a typo in a
variable name fails at load instead of silently falling back to a default).

**Rendering.** A sandboxed environment with ``StrictUndefined``. Item values
are data and are never parsed as template code. Before rendering, every
missing value is dropped from the context: ``None``, a string that is empty
after ``str.strip()``, and an empty list, tuple, set or dict. An optional
variable then takes its ``default(...)`` or ``is defined`` branch, so ``None``
never renders as the text "None"; a required variable that is missing raises
``jinja2.UndefinedError`` naming it, before rendering (SDS 5.8: caught by the
generator, item FAILED).

**Stored prompt (D-013).** ``stored_prompt()`` builds the
``generated_content.full_prompt`` value from the system text and the rendered
body, in one fixed layout.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import jinja2
import yaml
from jinja2 import StrictUndefined, meta
from jinja2.sandbox import SandboxedEnvironment

from arip.entities import PromptTemplate
from arip.enums import Language
from arip.exceptions import ConfigError

TEMPLATE_SUFFIX = ".j2"
HEADER_SEPARATOR = "---"
HEADER_KEYS = frozenset(
    {
        "template_id",
        "language",
        "description",
        "required_variables",
        "optional_variables",
        "system",
    }
)
STORED_SYSTEM_HEADING = "### SYSTEM"
STORED_USER_HEADING = "### USER"


def is_missing(value: Any) -> bool:
    """True for ``None``, a blank string, and an empty list, tuple, set or dict (ruling Q6)."""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, list | tuple | set | dict):
        return not value
    return False


def stored_prompt(system_text: str, user_prompt: str) -> str:
    """The ``generated_content.full_prompt`` value (D-013, ruling Q2)."""
    return f"{STORED_SYSTEM_HEADING}\n{system_text}\n\n{STORED_USER_HEADING}\n{user_prompt}"


class PromptManager:
    """Loads every template in a directory at construction (SDS 5.8)."""

    def __init__(self, templates_dir: Path) -> None:
        """
        Raises:
            ConfigError: If the directory does not exist, or any template file
                         in it is malformed (see the module docstring).
        """
        self._env = SandboxedEnvironment(
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
            autoescape=False,
        )
        if not templates_dir.is_dir():
            raise ConfigError(f"Prompt template directory not found: {templates_dir}")
        self._templates: dict[tuple[str, Language], tuple[PromptTemplate, jinja2.Template]] = {}
        for path in sorted(templates_dir.glob(f"*{TEMPLATE_SUFFIX}")):
            template, compiled = self._load(path)
            self._templates[(template.template_id, template.language)] = (template, compiled)

    # ------------------------------------------------------------------
    # Public interface (SDS 5.8)
    # ------------------------------------------------------------------

    def get_template(self, template_id: str, language: Language) -> PromptTemplate:
        """Return the template for ``(template_id, language)``.

        Raises:
            ConfigError: If no such template was loaded. Stage 8 calls this for
                         every configured language at wiring, so a missing file
                         fails at startup (ruling Q10).
        """
        return self._entry(template_id, language)[0]

    def render(self, template_id: str, language: Language, context: dict) -> str:
        """Render the template body with ``context``.

        Raises:
            ConfigError: If no such template was loaded.
            jinja2.UndefinedError: If a required variable is missing, or the
                                   body uses a missing optional variable
                                   without a default.
        """
        template, compiled = self._entry(template_id, language)
        values = {key: value for key, value in context.items() if not is_missing(value)}
        missing = [name for name in template.required_variables if name not in values]
        if missing:
            raise jinja2.UndefinedError(
                f"Missing required template variable(s) for "
                f"{template.prompt_template_id}: {', '.join(missing)}"
            )
        return compiled.render(**values)

    @property
    def templates(self) -> list[PromptTemplate]:
        """Every loaded template, sorted by id and language. Diagnostic use."""
        return [self._templates[key][0] for key in sorted(self._templates)]

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def _entry(
        self, template_id: str, language: Language
    ) -> tuple[PromptTemplate, jinja2.Template]:
        try:
            return self._templates[(template_id, language)]
        except KeyError:
            available = ", ".join(t.prompt_template_id for t in self.templates) or "none"
            raise ConfigError(
                f"No prompt template '{template_id}' for language {language.value}. "
                f"Loaded templates: {available}."
            ) from None

    def _load(self, path: Path) -> tuple[PromptTemplate, jinja2.Template]:
        text = path.read_text(encoding="utf-8")
        lines = text.split("\n")
        if HEADER_SEPARATOR not in lines:
            raise ConfigError(f"{path.name}: no '{HEADER_SEPARATOR}' line after the header")
        split = lines.index(HEADER_SEPARATOR)
        header_text, body = "\n".join(lines[:split]), "\n".join(lines[split + 1 :])
        try:
            header = yaml.safe_load(header_text)
        except yaml.YAMLError as exc:
            raise ConfigError(f"{path.name}: the header is not valid YAML: {exc}") from exc
        if not isinstance(header, dict):
            raise ConfigError(f"{path.name}: the header must be a YAML mapping")

        unknown, absent = set(header) - HEADER_KEYS, HEADER_KEYS - set(header)
        if unknown or absent:
            raise ConfigError(
                f"{path.name}: header keys must be exactly {sorted(HEADER_KEYS)}; "
                f"unknown {sorted(unknown)}, missing {sorted(absent)}"
            )
        template_id = header["template_id"]
        if not isinstance(template_id, str) or not template_id.strip():
            raise ConfigError(f"{path.name}: template_id must be a non-empty string")
        try:
            language = Language(header["language"])
        except ValueError:
            raise ConfigError(
                f"{path.name}: unknown language {header['language']!r}; "
                f"one of {[lang.value for lang in Language]}"
            ) from None
        expected_name = f"{template_id}_{language.value.lower()}{TEMPLATE_SUFFIX}"
        if path.name != expected_name:
            raise ConfigError(
                f"{path.name}: the header says template_id={template_id!r}, "
                f"language={language.value!r}, so the file must be named {expected_name}"
            )
        description = header["description"]
        if not isinstance(description, str):
            raise ConfigError(f"{path.name}: description must be a string")
        system = header["system"]
        if not isinstance(system, str) or not system.strip():
            raise ConfigError(f"{path.name}: system must be a non-empty string (SDS 5.9.1)")
        required = self._names(path, header, "required_variables")
        optional = self._names(path, header, "optional_variables")
        both = set(required) & set(optional)
        if both:
            raise ConfigError(f"{path.name}: variables both required and optional: {sorted(both)}")

        try:
            used = meta.find_undeclared_variables(self._env.parse(body))
            compiled = self._env.from_string(body)
        except jinja2.TemplateSyntaxError as exc:
            raise ConfigError(f"{path.name}: Jinja2 syntax error: {exc}") from exc
        declared = set(required) | set(optional)
        if used != declared:
            raise ConfigError(
                f"{path.name}: the body uses variables the header does not declare "
                f"{sorted(used - declared)}, and the header declares variables the body "
                f"does not use {sorted(declared - used)}"
            )

        template = PromptTemplate(
            template_id=template_id,
            language=language,
            description=description,
            required_variables=required,
            optional_variables=optional,
            system_text=system,
            body=body,
            source_path=str(path),
        )
        return template, compiled

    @staticmethod
    def _names(path: Path, header: dict, key: str) -> tuple[str, ...]:
        value = header[key]
        if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
            raise ConfigError(f"{path.name}: {key} must be a list of variable names")
        if len(set(value)) != len(value):
            raise ConfigError(f"{path.name}: {key} lists a variable twice")
        return tuple(value)

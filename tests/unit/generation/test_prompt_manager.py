"""
Unit tests for PromptManager and the shipped thread_en.j2 (SDS 5.8, AD-07, D-013).

Fixture templates are written into a temporary directory per test, as exact
bytes: ``Path.write_text()`` would translate "\n" to "\r\n" on Windows. The
shipped template is checked against the approved wording through a golden
render (thread_en_golden.txt), read with universal newlines on purpose, so the
comparison holds whatever line endings the checkout gives that file.
"""

from __future__ import annotations

from pathlib import Path

import jinja2
import pytest
import yaml
from jinja2.exceptions import SecurityError

from arip.enums import Language, SourceType
from arip.exceptions import ConfigError
from arip.generation.prompt_manager import PromptManager, is_missing, stored_prompt

REPO = Path(__file__).resolve().parents[3]
PROMPTS = REPO / "config" / "prompts"
GOLDEN = Path(__file__).with_name("thread_en_golden.txt")
DEFAULT_BODY = 'Title: {{ title }}\nAbout: {{ abstract | default("Not provided.") }}\n'
APPROVED_SYSTEM = (
    "You are a technical writer for ARIP, a digest of new AI research. You explain "
    "papers, models, demos and open-source projects clearly and accurately for "
    "software engineers and students. You use only the information you are given. "
    "You never invent results, numbers, names or links. You follow the requested "
    "output format exactly."
)
GOLDEN_CONTEXT = {
    "title": "Attention Is All You Need",
    "primary_url": "https://arxiv.org/abs/1706.03762",
    "source_type": "PAPER",
    "source_id": "arxiv",
    "abstract": "We propose the Transformer. It reaches 28.4 BLEU and trains 3x faster.",
    "authors": [
        "Ashish Vaswani",
        "Noam Shazeer",
        "Niki Parmar",
        "Jakob Uszkoreit",
        "Llion Jones",
        "Aidan N. Gomez",
        "Lukasz Kaiser",
    ],
    "published_date": "2017-06-12",
    "source_signals": {"upvotes": 42},
}


def write_template(
    directory: Path,
    body: str = DEFAULT_BODY,
    name: str = "demo_en.j2",
    remove: tuple[str, ...] = (),
    **overrides: object,
) -> Path:
    header: dict[str, object] = {
        "template_id": "demo",
        "language": "EN",
        "description": "A fixture template.",
        "required_variables": ["title"],
        "optional_variables": ["abstract"],
        "system": "Be brief.",
    }
    header.update(overrides)
    for key in remove:
        header.pop(key)
    path = directory / name
    text = yaml.safe_dump(header, sort_keys=False) + "---\n" + body
    path.write_bytes(text.encode("utf-8"))  # LF exactly, on every platform
    return directory


# ---------------------------------------------------------------------------
# Loading and rendering
# ---------------------------------------------------------------------------


def test_loads_and_renders_a_template(tmp_path: Path) -> None:
    manager = PromptManager(write_template(tmp_path))

    template = manager.get_template("demo", Language.EN)
    text = manager.render("demo", Language.EN, {"title": "T", "abstract": "A"})

    assert template.required_variables == ("title",)
    assert template.optional_variables == ("abstract",)
    assert template.system_text == "Be brief."
    assert text == "Title: T\nAbout: A"


def test_crlf_template_renders_like_lf(tmp_path: Path) -> None:
    lf = tmp_path / "lf"
    crlf = tmp_path / "crlf"
    lf.mkdir()
    crlf.mkdir()
    lf_bytes = (write_template(lf) / "demo_en.j2").read_bytes()
    path = write_template(crlf) / "demo_en.j2"
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    assert b"\r" not in lf_bytes
    assert path.read_bytes() == lf_bytes.replace(b"\n", b"\r\n")
    context = {"title": "T", "abstract": "A"}

    crlf_text = PromptManager(crlf).render("demo", Language.EN, context)

    assert crlf_text == PromptManager(lf).render("demo", Language.EN, context)
    assert crlf_text == "Title: T\nAbout: A"


def test_sandbox_blocks_attribute_escape(tmp_path: Path) -> None:
    manager = PromptManager(
        write_template(tmp_path, body="{{ title.__class__.__mro__ }}", optional_variables=[])
    )

    with pytest.raises(SecurityError):
        manager.render("demo", Language.EN, {"title": "T"})


def test_undeclared_optional_without_default_raises(tmp_path: Path) -> None:
    manager = PromptManager(write_template(tmp_path, body="{{ title }} {{ abstract }}"))

    with pytest.raises(jinja2.UndefinedError, match="abstract"):
        manager.render("demo", Language.EN, {"title": "T"})


def test_none_renders_fallback_never_none(tmp_path: Path) -> None:
    manager = PromptManager(write_template(tmp_path))

    text = manager.render("demo", Language.EN, {"title": "T", "abstract": None})

    assert "Not provided." in text
    assert "None" not in text


def test_blank_values_are_dropped(tmp_path: Path) -> None:
    body = (
        'Title: {{ title }}\nAbout: {{ abstract | default("Not provided.") }}\n'
        "{% if authors is defined %}Authors present{% endif %}\n"
    )
    manager = PromptManager(
        write_template(tmp_path, body=body, optional_variables=["abstract", "authors"])
    )

    text = manager.render("demo", Language.EN, {"title": "T", "abstract": "  \n", "authors": []})

    assert "Not provided." in text
    assert "Authors present" not in text


def test_is_missing() -> None:
    for value in (None, "", "   ", "\n", [], (), set(), {}):
        assert is_missing(value), value
    for value in ("x", 0, 0.0, False, ["a"], {"a": 1}):
        assert not is_missing(value), value


def test_missing_required_variable_raises_before_render(tmp_path: Path) -> None:
    manager = PromptManager(write_template(tmp_path))

    with pytest.raises(
        jinja2.UndefinedError, match=r"Missing required template variable"
    ) as raised:
        manager.render("demo", Language.EN, {"abstract": "A"})
    assert "title" in str(raised.value)


def test_braces_in_values_render_literally(tmp_path: Path) -> None:
    manager = PromptManager(write_template(tmp_path))

    text = manager.render(
        "demo", Language.EN, {"title": "{{ 7*7 }} {% if x %}", "abstract": "{# c #}"}
    )

    assert text == "Title: {{ 7*7 }} {% if x %}\nAbout: {# c #}"


# ---------------------------------------------------------------------------
# Load-time checks (ConfigError)
# ---------------------------------------------------------------------------


def test_header_filename_mismatch_is_config_error(tmp_path: Path) -> None:
    write_template(tmp_path, name="other_en.j2")

    with pytest.raises(ConfigError, match="must be named demo_en.j2"):
        PromptManager(tmp_path)


def test_undeclared_template_variable_is_config_error(tmp_path: Path) -> None:
    used_not_declared = tmp_path / "a"
    declared_not_used = tmp_path / "b"
    used_not_declared.mkdir()
    declared_not_used.mkdir()
    write_template(used_not_declared, body="{{ title }} {{ titel }}", optional_variables=[])
    write_template(declared_not_used, body="{{ title }}")

    with pytest.raises(ConfigError, match="titel"):
        PromptManager(used_not_declared)
    with pytest.raises(ConfigError, match="abstract"):
        PromptManager(declared_not_used)


def test_missing_system_text_is_config_error(tmp_path: Path) -> None:
    write_template(tmp_path, system="   ")

    with pytest.raises(ConfigError, match="system"):
        PromptManager(tmp_path)


def test_header_keys_must_be_exact(tmp_path: Path) -> None:
    unknown = tmp_path / "unknown"
    absent = tmp_path / "absent"
    unknown.mkdir()
    absent.mkdir()
    write_template(unknown, version=2)
    write_template(absent, remove=("system",))

    with pytest.raises(ConfigError, match="version"):
        PromptManager(unknown)
    with pytest.raises(ConfigError, match="system"):
        PromptManager(absent)


def test_bad_header_values_are_config_errors(tmp_path: Path) -> None:
    cases = {
        "lang": {"language": "DE"},
        "both": {"optional_variables": ["title"]},
        "list": {"required_variables": "title"},
    }
    for folder, overrides in cases.items():
        directory = tmp_path / folder
        directory.mkdir()
        write_template(directory, **overrides)
        with pytest.raises(ConfigError):
            PromptManager(directory)


def test_no_separator_or_bad_syntax_is_config_error(tmp_path: Path) -> None:
    no_separator = tmp_path / "sep"
    bad_syntax = tmp_path / "syntax"
    no_separator.mkdir()
    bad_syntax.mkdir()
    (no_separator / "demo_en.j2").write_bytes(b"template_id: demo\n")
    write_template(bad_syntax, body="{% if title %}")

    with pytest.raises(ConfigError, match="---"):
        PromptManager(no_separator)
    with pytest.raises(ConfigError, match="syntax"):
        PromptManager(bad_syntax)


def test_missing_directory_is_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        PromptManager(tmp_path / "absent")


def test_unknown_template_is_config_error(tmp_path: Path) -> None:
    manager = PromptManager(write_template(tmp_path))

    with pytest.raises(ConfigError, match="No prompt template 'thread'"):
        manager.get_template("thread", Language.EN)
    with pytest.raises(ConfigError, match="language TR"):
        manager.render("demo", Language.TR, {"title": "T"})


# ---------------------------------------------------------------------------
# Identity and stored prompt (D-013)
# ---------------------------------------------------------------------------


def test_prompt_template_id_is_thread_en() -> None:
    assert PromptManager(PROMPTS).get_template("thread", Language.EN).prompt_template_id == (
        "thread_en"
    )


def test_stored_prompt_layout() -> None:
    assert stored_prompt("Be brief.", "Write.\nNow.") == (
        "### SYSTEM\nBe brief.\n\n### USER\nWrite.\nNow."
    )


# ---------------------------------------------------------------------------
# The shipped template (approved verbatim, 2026-11-21)
# ---------------------------------------------------------------------------


def test_shipped_template_declares_the_agreed_variables() -> None:
    template = PromptManager(PROMPTS).get_template("thread", Language.EN)

    assert set(template.required_variables) == {"title", "primary_url", "source_type", "source_id"}
    assert set(template.optional_variables) == {
        "abstract",
        "authors",
        "published_date",
        "source_signals",
    }
    assert template.system_text == APPROVED_SYSTEM


def test_shipped_template_renders_golden_file() -> None:
    text = PromptManager(PROMPTS).render("thread", Language.EN, dict(GOLDEN_CONTEXT))

    assert text == GOLDEN.read_text(encoding="utf-8")


def test_shipped_template_names_every_source_type() -> None:
    manager = PromptManager(PROMPTS)
    for source_type in SourceType:
        context = {**GOLDEN_CONTEXT, "source_type": source_type.value}
        first_line = manager.render("thread", Language.EN, context).splitlines()[0]
        assert "the item described" not in first_line, source_type


def test_shipped_template_renders_with_required_fields_only() -> None:
    required = {k: GOLDEN_CONTEXT[k] for k in ("title", "primary_url", "source_type", "source_id")}

    text = PromptManager(PROMPTS).render("thread", Language.EN, required)

    assert "Description:\nNot provided." in text
    assert "Authors:" not in text
    assert "Published:" not in text
    assert "Signals:" not in text
    assert text.count("https://arxiv.org/abs/1706.03762") == 2

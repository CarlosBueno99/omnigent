"""Content projection contracts shared by authorization and native delivery."""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from pathlib import Path

import pytest

from omnigent.inner.message_content import (
    extract_text_content,
    native_shell_input,
    native_text_content,
    normalize_message_content,
)


def test_idle_and_live_delivery_normalize_multimodal_content_differently() -> None:
    text_blocks = [
        {"type": "output_text", "text": "hello"},
        {"type": "input_text", "text": "!id"},
    ]
    assert normalize_message_content(text_blocks) == "hello\n!id"
    assert extract_text_content(text_blocks) == "hello\n!id"
    assert native_shell_input(text_blocks, "claude-native") is False

    multimodal = [{"type": "input_audio"}, *text_blocks]
    assert normalize_message_content(multimodal) is multimodal
    assert extract_text_content(multimodal) == "hello\n!id"
    assert native_text_content(multimodal, attachment_text=lambda _: "[Attached]") == "!id"
    assert native_shell_input(multimodal, "claude-native") is True


@pytest.mark.parametrize(
    ("harness", "allow_text_alias", "expected"),
    [
        ("claude", False, "!id"),
        ("pi", False, "!id"),
        ("cursor", True, "hello\n\n!id"),
    ],
)
def test_native_projection_matches_tui_text_alias_behavior(
    harness: str, allow_text_alias: bool, expected: str, tmp_path: Path
) -> None:
    content = [
        {"type": "input_audio"},
        {"type": "output_text", "text": "ignored"},
        {"type": "text", "text": "hello"},
        {"type": "input_text", "text": "!id"},
    ]
    module = importlib.import_module(f"omnigent.inner.{harness}_native_executor")
    assert module._content_to_text(content, tmp_path) == expected
    assert native_shell_input(content, f"{harness}-native") is expected.startswith("!")
    assert (
        native_text_content(
            content,
            attachment_text=lambda _: "[Attached]",
            allow_text_alias=allow_text_alias,
        )
        == expected
    )


def test_attachment_references_precede_text_without_reading_files() -> None:
    attachments: list[Mapping[str, object]] = []

    def attachment_reference(block: Mapping[str, object]) -> str:
        attachments.append(block)
        return f"[Attached: {block['filename']}]"

    image = {"type": "input_image", "filename": "missing.png"}
    file = {"type": "input_file", "filename": "missing.txt"}
    content = [{"type": "input_text", "text": "!id"}, image, file]
    assert native_text_content(content, attachment_text=attachment_reference) == (
        "[Attached: missing.png]\n\n[Attached: missing.txt]\n\n!id"
    )
    assert attachments == [image, file]


def test_antigravity_skips_empty_text_and_strips_edges(tmp_path: Path) -> None:
    from omnigent.inner.antigravity_native_executor import _content_to_text

    content = [
        {"type": "input_text", "text": "  before"},
        {"type": "text", "text": ""},
        {"type": "input_text", "text": "after  "},
    ]
    assert _content_to_text(content, tmp_path) == "before\nafter"
    assert native_text_content(content, attachment_text=lambda _: "[Attached]") == (
        "  before\n\n\n\nafter  "
    )


@pytest.mark.parametrize("block_type", [[], {}, None])
def test_native_projection_ignores_malformed_block_types(block_type: object) -> None:
    content = [
        {"type": block_type, "text": "ignored"},
        {"type": "input_text", "text": 17},
        {"type": "input_text", "text": "!id"},
        "ignored",
    ]
    assert native_text_content(content, attachment_text=lambda _: "[Attached]") == "!id"
    assert normalize_message_content(content) == "ignored\n!id"
    assert native_shell_input([{"type": "input_audio"}, *content], "claude-native") is True


def test_shell_guard_checks_live_input_without_materializing_attachments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.inner import native_attachments

    def unexpected_materialization(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Authorization must not materialize attachments")

    monkeypatch.setattr(native_attachments, "materialize_attachment", unexpected_materialization)
    image = {"type": "input_image", "file_id": "missing"}
    assert native_shell_input([image, {"type": "input_text", "text": "!id"}], "claude-native")
    assert not native_shell_input(
        [image, {"type": "input_text", "text": "describe this"}], "claude-native"
    )


@pytest.mark.parametrize("harness", ["codex-native", "opencode-native"])
def test_api_harnesses_check_flat_input_without_tui_block_filtering(harness: str) -> None:
    content = [
        {"type": "input_audio"},
        {"type": "output_text", "text": "hello"},
        {"type": "input_text", "text": "!id"},
    ]
    assert native_shell_input(content, harness) is False
    assert native_shell_input([{"type": "input_text", "text": "!id"}], harness) is True


@pytest.mark.parametrize("harness", [None, "auto"])
@pytest.mark.parametrize("command_type", ["input_text", "text"])
def test_unresolved_harness_checks_both_tui_text_projections(
    harness: str | None, command_type: str
) -> None:
    content = [
        {"type": "input_audio"},
        {"type": "output_text", "text": "hello"},
        {"type": command_type, "text": "!id"},
    ]
    assert native_shell_input(content, harness) is True
    assert native_shell_input(
        [{"type": "input_audio"}, {"type": "text", "text": "hello"}, content[-1]], harness
    ) is (command_type == "input_text")


@pytest.mark.parametrize("content", [None, 17, {}, [None], [{"type": "input_text", "text": 17}]])
def test_shell_guard_ignores_content_without_deliverable_text(content: object) -> None:
    assert extract_text_content(content) == ""
    assert native_shell_input(content, "claude-native") is False


def test_shell_guard_checks_the_message_prefix() -> None:
    assert native_shell_input(" \t\n!id", "claude-native") is True
    assert (
        native_shell_input(
            [
                {"type": "input_text", "text": "Explain this command:"},
                {"type": "input_text", "text": "!id"},
            ],
            "claude-native",
        )
        is False
    )

"""Shared content projections used by executor delivery and authorization."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

NATIVE_TEXT_BLOCK_TYPES = ("input_text", "text")
_MULTIMODAL_BLOCK_TYPES = ("input_image", "input_file", "input_audio")


def extract_text_content(content: object) -> str:
    """Flatten the text fields delivered by live executor injections."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        block["text"]
        for block in content
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    )


def normalize_message_content(content: object) -> str | list[dict[str, Any]]:
    """Keep multimodal content intact and flatten text-only executor messages."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    if any(
        isinstance(block, dict) and block.get("type") in _MULTIMODAL_BLOCK_TYPES
        for block in content
    ):
        return content
    return "\n".join(
        block["text"]
        for block in content
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    )


def native_text_content(
    content: object,
    *,
    attachment_text: Callable[[Mapping[str, object]], str],
    allow_text_alias: bool = True,
    separator: str = "\n\n",
    skip_empty_text: bool = False,
) -> str:
    """Render native TUI content with attachment references before user text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    attachments: list[str] = []
    texts: list[str] = []
    accepted_types = NATIVE_TEXT_BLOCK_TYPES if allow_text_alias else ("input_text",)
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type", "")
        if block_type in accepted_types:
            text = block.get("text")
            if isinstance(text, str) and (text or not skip_empty_text):
                texts.append(text)
        elif block_type in ("input_image", "input_file"):
            attachments.append(attachment_text(block))
    return separator.join(attachments + texts)


def native_shell_input(content: object, harness: str | None) -> bool:
    """Check both live injection and idle TUI text for a leading shell command."""
    if extract_text_content(content).lstrip().startswith("!"):
        return True
    # These transports send model API inputs without a TUI command parser.
    if harness in ("codex-native", "opencode-native"):
        return False
    normalized = normalize_message_content(content)
    text_alias_options = (
        (False, True)
        if harness in (None, "auto")
        else (harness not in ("claude-native", "pi-native"),)
    )
    for allow_text_alias in text_alias_options:
        text = native_text_content(
            normalized,
            attachment_text=lambda _block: "[Attachment]",
            allow_text_alias=allow_text_alias,
            separator="\n" if harness == "antigravity-native" else "\n\n",
            skip_empty_text=harness == "antigravity-native",
        )
        if text.lstrip().startswith("!"):
            return True
    return False

"""Claude tool-call locators, separate from requester authority.

The retained tool-use ID can join an MCP call to a provider hook. It does not
authenticate an input or identify its execution's requester.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class ClaudeExecutionIdentity:
    """A provider tool-use locator; no session or prompt is inferred."""

    tool_use_id: str

    def to_wire(self) -> dict[str, str]:
        return {"tool_use_id": self.tool_use_id}

    @classmethod
    def from_wire(cls, value: object) -> ClaudeExecutionIdentity | None:
        """Decode only a bounded locator; missing or malformed IDs stay unknown."""
        if not isinstance(value, dict):
            return None
        identifier = value.get("tool_use_id")
        if not isinstance(identifier, str) or not 0 < len(identifier) <= 256:
            return None
        if not identifier.strip():
            return None
        return cls(identifier)

    @classmethod
    def from_mcp_meta(cls, meta: object) -> ClaudeExecutionIdentity | None:
        """Read the provider's retained call ID, never model arguments or bridge state."""
        if not isinstance(meta, dict):
            return None
        return cls.from_wire({"tool_use_id": meta.get("claudecode/toolUseId")})


current_claude_execution: ContextVar[ClaudeExecutionIdentity | None] = ContextVar(
    "current_claude_execution", default=None
)

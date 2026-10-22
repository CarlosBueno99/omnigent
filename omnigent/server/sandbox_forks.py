"""Eligibility and recovery boundaries for provider-owned sandbox copies."""

from __future__ import annotations

from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.managed_hosts import ManagedSandboxDeployment
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.host_store import Host

SANDBOX_FORK_LABEL_PREFIX = "omnigent.sandbox.fork."
SANDBOX_FORK_STATE_LABEL = f"{SANDBOX_FORK_LABEL_PREFIX}state"


def host_has_forked_workspace(store: ConversationStore, host_id: str) -> bool:
    """A sibling session must not recreate a host holding a copied workspace."""
    after = None
    while session_ids := store.list_host_conversation_ids(host_id, after=after, limit=100):
        for session_id in session_ids:
            conversation = store.get_conversation(session_id)
            if conversation is not None and SANDBOX_FORK_STATE_LABEL in conversation.labels:
                return True
        after = session_ids[-1]
    return False


def sandbox_fork_provider(
    conversation: Conversation,
    workspace_readable: bool,
    host: Host | None,
    deployment: ManagedSandboxDeployment | None,
) -> str | None:
    """Return the source provider when this viewer can request a sandbox copy."""
    if (
        not workspace_readable
        or not conversation.workspace
        or host is None
        or host.host_id != conversation.host_id
        or host.deleted_at is not None
        or not host.sandbox_id
        or not host.sandbox_provider
        or deployment is None
    ):
        return None
    capabilities = deployment.provider_ui_capabilities().get(host.sandbox_provider, {})
    return host.sandbox_provider if capabilities.get("fork_sandbox", False) else None


def require_bound_sandbox_fork(conversation: Conversation) -> None:
    """An incomplete copy must never recover by starting with an empty workspace."""
    if SANDBOX_FORK_STATE_LABEL in conversation.labels and conversation.host_id is None:
        raise OmnigentError(
            "The sandbox fork did not finish. Fork the source session again to retry the copy.",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        )

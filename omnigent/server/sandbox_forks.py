"""Eligibility and recovery boundaries for provider-owned sandbox copies."""

from __future__ import annotations

from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.managed_hosts import ManagedSandboxDeployment
from omnigent.stores.host_store import Host

SANDBOX_FORK_LABEL_PREFIX = "omnigent.sandbox.fork."
SANDBOX_FORK_STATE_LABEL = f"{SANDBOX_FORK_LABEL_PREFIX}state"


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
    provider_config = deployment.for_provider(host.sandbox_provider)
    if provider_config is None or not provider_config.managed_launch_supported:
        return None
    capabilities = provider_config.launcher_factory().capabilities
    return host.sandbox_provider if capabilities.fork_sandbox else None


def require_bound_sandbox_fork(conversation: Conversation) -> None:
    """An incomplete copy must never recover by starting with an empty workspace."""
    if SANDBOX_FORK_STATE_LABEL in conversation.labels and conversation.host_id is None:
        if conversation.labels[SANDBOX_FORK_STATE_LABEL] == "pending":
            raise OmnigentError(
                "The sandbox copy is not ready yet; try again shortly. "
                "If provisioning was interrupted, fork the source session again.",
                code=ErrorCode.RUNNER_UNAVAILABLE,
            )
        raise OmnigentError(
            "The sandbox fork did not finish. Fork the source session again to retry the copy.",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        )

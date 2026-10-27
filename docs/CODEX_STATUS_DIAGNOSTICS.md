# Codex lifecycle status evidence

The existing lifecycle log uses `event_name=codex_turn_status` and explicitly
identifies the session receiving the status, including a forwarded child session.
Use its session and turn IDs to correlate a server-side failure with the native
producer rather than relying on the runner's primary-session context.

`protocol_source` identifies the lifecycle boundary. `status`, `has_turn_id`,
`has_error` and `native_error_kind` distinguish ordinary completion, turn-bound
errors and standalone errors with no turn ID.

`native_error_reason=thread_persistence_shutdown` identifies the known persistence
shutdown message in the first 4,096 characters of an error. Other errors use
`unclassified`; clean edges omit the reason. The audit adds no raw error text.
This identifies where a failure surfaced without establishing its initiating
cause. Existing status payloads, error output and session decisions are unchanged.

Bridge snapshots carry the inode and modification time of the opened state file.
Accepted delivery replies compare that revision under the state lock before
recording their turn ID. A start and completion can both arrive before the reply,
returning the payload to idle; the changed file revision preserves that completion
so the next input starts a fresh turn. The revision is observation metadata and
does not change the on-disk state format.

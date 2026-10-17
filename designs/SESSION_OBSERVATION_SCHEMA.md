# Session observation schema rollout

The additive `d42f66e0a5b1` and `e53a77f1b6c2` migrations prepare metadata for
binding epochs, ordered live observations, offline decisions and independent
terminal-startup claims. They do not introduce application writers or change
existing session status. Legacy rows retain their runner, status and history;
epoch/revision default to zero, startup pending defaults to false and optional
payloads remain null.

Deploy these schema migrations before the separate ownership application
release. The old application ignores the added columns and continues to read
and write its existing fields. New application writers must enforce the
16,383-byte maximum on the final serialized/compressed binary values, including
framing overhead, and bounds on the observation/error identifiers. That writer
validation is part of the application release, before any new payload is written.
No backfill or data conversion is performed by these migrations.

Roll back the application first while retaining the additive schema. The old
application remains compatible with the additional columns. Keep the columns
through the mixed-version window; dropping them before all new writers stop
would break the new application. Downgrades remove only the new ownership
metadata and must be used only after disabling those writers. Existing session
rows, runner bindings, normal status and conversation history remain intact.

The migration tests cover upgrade, downgrade and upgrade again for default and
non-default workspaces, preserving existing rows and already-owned state. The
existing historical migration tests remain scoped to their original revision
before they exercise the current ORM schema.

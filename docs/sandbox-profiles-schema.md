# Sandbox profile schema deployment

Migration `nn1a2b3c4d5e`, following `b2e4f6a8c013`, adds one table to the
Omnigent metadata database, plus a runner/host lookup index on conversation
metadata. Existing rows and columns are unchanged.

| Table | Primary key | Purpose |
| --- | --- | --- |
| `sandbox_profile_bindings` | `(workspace_id, session_id)` | Immutable provider/profile revision, public metadata, trusted execution identity, and optional host association. |

The host-binding index includes workspace, host, and session IDs for a bounded,
deterministic lookup. Session and host IDs use the existing binary UUID type.
The JSON reference uses binary storage with a database check constraint limiting
it to 16,383 serialized bytes, including multibyte encodings.
Provider configuration, credentials, and membership lists are never stored here.

The `(workspace_id, runner_id, host_id, id)` index supports two bounded seeks to
identify a runner's host and detect ambiguous host ownership. It skips hostless
children and repeated bindings without hydrating conversations. The existing
runner/session index remains necessary for pagination ordered by session ID.
On PostgreSQL the new index is built concurrently before creating the bindings
table. Retries remove an invalid partial index before rebuilding it, so an
interrupted deployment can resume without blocking session writes.

Deploy this additive migration before enabling `sandbox_profiles`. With the flag
off, servers do not read or write the table. No backfill or production data
cleanup is required.

MySQL commits each DDL statement separately. An interrupted upgrade can leave
the new runner/host index, and possibly the bindings table or its host index,
without recording revision `nn1a2b3c4d5e`. Automatic repair applies only to
PostgreSQL. For an interrupted MySQL upgrade before feature use:

1. Stop other migration attempts and keep profiles disabled. Check
   `alembic_version`, `SHOW INDEX FROM omnigent_conversation_metadata`, and
   `SHOW TABLES LIKE 'sandbox_profile_bindings'` in the metadata database.
2. If the revision is still the predecessor, inspect any bindings table with
   `SHOW CREATE TABLE sandbox_profile_bindings` and
   `SELECT COUNT(*) FROM sandbox_profile_bindings`. Confirm the objects match
   this migration and the table is empty.
3. Drop only the partially created objects that exist: the empty
   `sandbox_profile_bindings` table (including its index), then
   `ix_conversation_metadata_runner_host` on `omnigent_conversation_metadata`.
   Rerun the normal migration command to recreate them and record the revision.

If the table contains accepted references or the revision is already recorded,
preserve the data and investigate the state before making schema changes; this
empty-state recovery procedure does not apply. Keep the existing runner/session
index throughout recovery.

Application rollback must target a build that includes migration `nn1a2b3c4d5e`:
servers reject database revisions they do not recognize. Retain the table and
its data when rolling back to a compatible build. A migration downgrade
removes accepted references; use it only before feature use or after preserving
the data separately. Drain profiled workloads before disabling authorization or
rolling back to a server that does not support it.

The schema PR contains this migration, its ORM declaration, this deployment note,
and dialect/additivity tests. Provider authorization ships in the next PR.

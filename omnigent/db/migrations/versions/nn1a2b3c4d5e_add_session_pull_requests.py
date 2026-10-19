"""Persist session PR associations independently of execution hosts.

Revision ID: nn1a2b3c4d5e
Revises: mm1a2b3c4d5e

Deploy this nullable column before the application starts reading it. Older
servers ignore it; disabling pull_request_management preserves the stored links.
"""

import sqlalchemy as sa
from alembic import op

revision = "nn1a2b3c4d5e"
down_revision = "mm1a2b3c4d5e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "pull_requests" not in {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("omnigent_conversation_metadata")
    }:
        op.add_column(
            "omnigent_conversation_metadata",
            sa.Column("pull_requests", sa.LargeBinary(), nullable=True),
        )


def downgrade() -> None:
    if "pull_requests" in {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("omnigent_conversation_metadata")
    }:
        with op.batch_alter_table("omnigent_conversation_metadata") as batch_op:
            batch_op.drop_column("pull_requests")

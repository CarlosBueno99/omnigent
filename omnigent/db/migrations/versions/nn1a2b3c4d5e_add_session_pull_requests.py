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

_TABLE = "omnigent_conversation_metadata"
_SIZE_CHECK = "ck_session_pull_requests_size"


def upgrade() -> None:
    if "pull_requests" not in {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns(_TABLE)
    }:
        op.add_column(
            _TABLE,
            sa.Column("pull_requests", sa.LargeBinary(), nullable=True),
        )
    if _SIZE_CHECK not in {
        check["name"] for check in sa.inspect(op.get_bind()).get_check_constraints(_TABLE)
    }:
        with op.batch_alter_table(_TABLE) as batch_op:
            batch_op.create_check_constraint(_SIZE_CHECK, "length(pull_requests) < 16384")


def downgrade() -> None:
    if "pull_requests" in {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns(_TABLE)
    }:
        checks = {
            check["name"] for check in sa.inspect(op.get_bind()).get_check_constraints(_TABLE)
        }
        with op.batch_alter_table(_TABLE) as batch_op:
            if _SIZE_CHECK in checks:
                batch_op.drop_constraint(_SIZE_CHECK, type_="check")
            batch_op.drop_column("pull_requests")

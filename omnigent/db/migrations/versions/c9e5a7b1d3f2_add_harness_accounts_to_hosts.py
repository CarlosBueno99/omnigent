"""Store non-secret harness account metadata on hosts.

Revision ID: c9e5a7b1d3f2
Revises: c8e4f2a6b901
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "c9e5a7b1d3f2"
down_revision: str | None = "c8e4f2a6b901"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Add the compressed opaque JSON account snapshot."""
    with op.batch_alter_table("hosts") as batch:
        batch.add_column(sa.Column("harness_accounts", sa.LargeBinary(), nullable=True))


def downgrade() -> None:
    """Drop the account snapshot."""
    with op.batch_alter_table("hosts") as batch:
        batch.drop_column("harness_accounts")

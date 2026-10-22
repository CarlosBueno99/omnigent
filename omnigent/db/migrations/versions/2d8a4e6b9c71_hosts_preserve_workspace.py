"""Preserve copied workspaces across managed-host recovery.

Revision ID: 2d8a4e6b9c71
Revises: c8e4f2a6b901
Create Date: 2026-10-22 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2d8a4e6b9c71"
down_revision: str | None = "c8e4f2a6b901"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add a durable workspace guard, defaulting existing hosts and old writers to false."""
    with op.batch_alter_table("hosts") as batch_op:
        batch_op.add_column(
            sa.Column(
                "preserve_workspace", sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )


def downgrade() -> None:
    """Remove the workspace guard."""
    with op.batch_alter_table("hosts") as batch_op:
        batch_op.drop_column("preserve_workspace")

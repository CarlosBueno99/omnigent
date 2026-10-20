"""add project_id column to scheduled_tasks

Revision ID: 0638f6e46137
Revises: nf12ab34cd56
Create Date: 2026-10-20 00:00:00.000000

Adds an optional ``project_id`` (Uuid16, nullable) column to
``scheduled_tasks``. When set, the fire path files each spawned session into
that first-class project. Rerunnable: an upgrade replayed over a schema that
already has the column leaves it in place.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "0638f6e46137"
down_revision: str | None = "nf12ab34cd56"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add project_id to scheduled_tasks."""
    columns = sa.inspect(op.get_bind()).get_columns("scheduled_tasks")
    if any(column["name"] == "project_id" for column in columns):
        return
    op.add_column(
        "scheduled_tasks",
        sa.Column("project_id", Uuid16(), nullable=True),
    )


def downgrade() -> None:
    """Remove project_id from scheduled_tasks."""
    with op.batch_alter_table("scheduled_tasks") as batch_op:
        batch_op.drop_column("project_id")

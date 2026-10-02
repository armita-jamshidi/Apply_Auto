"""Track when application attempts begin for reliable rate limits."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_application_started_at"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "applications",
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE applications SET started_at = COALESCE(submitted_at, CURRENT_TIMESTAMP) "
            "WHERE started_at IS NULL"
        )
    )
    with op.batch_alter_table("applications") as batch_op:
        batch_op.alter_column(
            "started_at",
            existing_type=sa.DateTime(timezone=True),
            nullable=False,
        )


def downgrade() -> None:
    op.drop_column("applications", "started_at")

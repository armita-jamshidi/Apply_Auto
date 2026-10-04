"""Store when each job was posted, to show its age and skip stale postings."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_job_posted_at"
down_revision: str | None = "0006_job_apply_url"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.drop_column("posted_at")

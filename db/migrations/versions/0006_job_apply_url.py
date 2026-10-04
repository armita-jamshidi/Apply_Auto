"""Store the company's own application link for jobs found on remote boards."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_job_apply_url"
down_revision: str | None = "0005_answers_and_fit_recommendation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("apply_url", sa.String(2048), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.drop_column("apply_url")

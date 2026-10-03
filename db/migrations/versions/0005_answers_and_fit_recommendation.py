"""Keep answer drafts, field notes, and the fit recommendation for the dashboard."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_answers_and_fit_recommendation"
down_revision: str | None = "0004_job_experience_level"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("fit_recommendation", sa.String(20), nullable=True))
    op.add_column("applications", sa.Column("suggested_answers", sa.JSON(), nullable=True))
    op.add_column("applications", sa.Column("field_notes", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("applications") as batch_op:
        batch_op.drop_column("field_notes")
        batch_op.drop_column("suggested_answers")
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.drop_column("fit_recommendation")

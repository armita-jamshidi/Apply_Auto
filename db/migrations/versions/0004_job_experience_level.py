"""Store each job's experience level to find early-career roles."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_job_experience_level"
down_revision: str | None = "0003_application_review_path"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("experience_level", sa.String(20), nullable=True))
    op.add_column("jobs", sa.Column("min_years_experience", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.drop_column("min_years_experience")
        batch_op.drop_column("experience_level")

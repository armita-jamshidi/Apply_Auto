"""Store the company's own title for jobs found on third-party sites."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_job_company_page"
down_revision: str | None = "0007_job_posted_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("listed_title", sa.String(300), nullable=True))
    op.add_column(
        "jobs", sa.Column("company_page_checked_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.drop_column("company_page_checked_at")
        batch_op.drop_column("listed_title")

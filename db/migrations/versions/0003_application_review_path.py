"""Remember the review page written for each application attempt."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_application_review_path"
down_revision: str | None = "0002_application_started_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("applications", sa.Column("review_path", sa.String(2048), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("applications") as batch_op:
        batch_op.drop_column("review_path")

"""add_quantile_paths_and_extras_to_predictions

Revision ID: 85d7e1b07a56
Revises: 4de5247c4a30
Create Date: 2026-05-13 07:11:36.042662+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '85d7e1b07a56'
down_revision: Union[str, Sequence[str], None] = '4de5247c4a30'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 분위수 19개 (Q0.05 ~ Q0.95) 전부 저장하는 컬럼.
    # 포맷: {"0.05": [30 floats], "0.10": [...], ..., "0.95": [...]}
    op.add_column(
        "predictions",
        sa.Column(
            "quantile_paths",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("predictions", "quantile_paths")

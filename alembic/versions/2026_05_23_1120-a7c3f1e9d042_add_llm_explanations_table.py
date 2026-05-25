"""add llm_explanations table

Revision ID: a7c3f1e9d042
Revises: 37cbd34980b6
Create Date: 2026-05-23 11:20:00.000000+00:00

ticker PK 한 줄, 50종목 = 50 rows 영구.
LLM 으로 풀어쓴 verdict 설명 저장 — cron 이 매일 UPSERT, hot path 에서는 SELECT only.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a7c3f1e9d042"
down_revision: Union[str, Sequence[str], None] = "37cbd34980b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "llm_explanations",
        sa.Column("ticker", sa.String(length=10), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("base_date", sa.Date(), nullable=False),
        sa.Column(
            "template_version",
            sa.String(length=30),
            nullable=False,
            server_default=sa.text("'v1'"),
        ),
        sa.Column("llm_model", sa.String(length=60), nullable=False),
        sa.Column(
            "fallback_used",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "generated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["ticker"], ["tickers.ticker"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("ticker"),
    )


def downgrade() -> None:
    op.drop_table("llm_explanations")

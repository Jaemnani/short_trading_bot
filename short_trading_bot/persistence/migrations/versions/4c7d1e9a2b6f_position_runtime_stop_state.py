"""positions에 런타임 스톱 상태 컬럼 추가 (재시작 복원용)

Revision ID: 4c7d1e9a2b6f
Revises: 8244c8da0f8f
Create Date: 2026-07-18 12:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '4c7d1e9a2b6f'
down_revision: str | Sequence[str] | None = '8244c8da0f8f'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MONEY = sa.Numeric(precision=24, scale=8)


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('positions', sa.Column('initial_stop', _MONEY, nullable=False, server_default='0'))
    op.add_column('positions', sa.Column('peak_price', _MONEY, nullable=False, server_default='0'))
    op.add_column('positions', sa.Column('tp_rungs_taken', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('positions', sa.Column('original_qty', _MONEY, nullable=False, server_default='0'))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('positions', 'original_qty')
    op.drop_column('positions', 'tp_rungs_taken')
    op.drop_column('positions', 'peak_price')
    op.drop_column('positions', 'initial_stop')

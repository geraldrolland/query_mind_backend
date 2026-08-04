"""message role, drop sender/recipient

Revision ID: a1b2c3d4e5f6
Revises: 36f195dffc29
Create Date: 2026-08-03 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = '36f195dffc29'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('message', sa.Column('role', sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default='user'))
    op.create_index(op.f('ix_message_role'), 'message', ['role'], unique=False)
    op.drop_index(op.f('ix_message_recipient'), table_name='message')
    op.drop_index(op.f('ix_message_sender'), table_name='message')
    op.drop_column('message', 'recipient')
    op.drop_column('message', 'sender')


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column('message', sa.Column('sender', sqlmodel.sql.sqltypes.AutoString(), nullable=False))
    op.add_column('message', sa.Column('recipient', sqlmodel.sql.sqltypes.AutoString(), nullable=False))
    op.create_index(op.f('ix_message_sender'), 'message', ['sender'], unique=False)
    op.create_index(op.f('ix_message_recipient'), 'message', ['recipient'], unique=False)
    op.drop_index(op.f('ix_message_role'), table_name='message')
    op.drop_column('message', 'role')
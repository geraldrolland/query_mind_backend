"""message cascade delete

Revision ID: 6d4a2b9c1e7f
Revises: 5c1e9f0a2b8d
Create Date: 2026-08-03 22:45:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '6d4a2b9c1e7f'
down_revision: Union[str, Sequence[str], None] = '5c1e9f0a2b8d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint('message_dataset_id_fkey', 'message', type_='foreignkey')
    op.create_foreign_key(
        'message_dataset_id_fkey',
        'message',
        'dataset',
        ['dataset_id'],
        ['id'],
        ondelete='CASCADE',
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('message_dataset_id_fkey', 'message', type_='foreignkey')
    op.create_foreign_key(
        'message_dataset_id_fkey',
        'message',
        'dataset',
        ['dataset_id'],
        ['id'],
    )

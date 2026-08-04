"""cascade deletes: user -> dataset -> datasetrow

Revision ID: 9d3b7e5a1c48
Revises: 6d4a2b9c1e7f
Create Date: 2026-08-04 01:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '9d3b7e5a1c48'
down_revision: Union[str, Sequence[str], None] = '6d4a2b9c1e7f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_foreign_key(
        'dataset_user_id_fkey',
        'dataset',
        'qm_user',
        ['user_id'],
        ['id'],
        ondelete='CASCADE',
    )
    op.create_foreign_key(
        'datasetrow_dataset_id_fkey',
        'datasetrow',
        'dataset',
        ['dataset_id'],
        ['id'],
        ondelete='CASCADE',
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('datasetrow_dataset_id_fkey', 'datasetrow', type_='foreignkey')
    op.drop_constraint('dataset_user_id_fkey', 'dataset', type_='foreignkey')

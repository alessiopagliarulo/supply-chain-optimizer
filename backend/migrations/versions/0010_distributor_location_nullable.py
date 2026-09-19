"""Let a distributor have no known location instead of an invented one

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-19

`seeds/seed_db.py` used to give any distributor missing from
DISTRIBUTOR_LOCATIONS a made-up location: San Francisco's coordinates with
city "Unknown", state "CA", country "USA". One real row carried it, "VNN
Services" (in the 2024 snapshot as a supplier name, zero priced offers, no
verifiable address), so every map would have plotted it in San Francisco.

`latitude` / `longitude` were NOT NULL, which is what forced a value. This
relaxes both so the row can hold NULL, meaning "location unknown". SQLite cannot
drop NOT NULL in place, so batch mode rebuilds the table. The data fix for the
row itself is in the seeder (UNLOCATED_DISTRIBUTORS), applied to the committed
database in the same change.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0010'
down_revision: Union[str, None] = '0009'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if 'distributors' not in sa.inspect(op.get_bind()).get_table_names():
        return
    with op.batch_alter_table('distributors') as batch:
        batch.alter_column('latitude', existing_type=sa.Float(), nullable=True)
        batch.alter_column('longitude', existing_type=sa.Float(), nullable=True)


def downgrade() -> None:
    # Refuses on a database holding an unlocated distributor, by design: the only
    # way back to NOT NULL is to invent coordinates, which this project does not do.
    with op.batch_alter_table('distributors') as batch:
        batch.alter_column('latitude', existing_type=sa.Float(), nullable=False)
        batch.alter_column('longitude', existing_type=sa.Float(), nullable=False)

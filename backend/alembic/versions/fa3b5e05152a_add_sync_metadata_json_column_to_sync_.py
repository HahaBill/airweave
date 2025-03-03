"""Add sync_metadata json column to sync and UC to entity

Revision ID: fa3b5e05152a
Revises: bafb02fba223
Create Date: 2025-01-04 00:24:10.738228

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "fa3b5e05152a"
down_revision = "bafb02fba223"
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_unique_constraint("uq_sync_id_entity_id", "entity", ["sync_id", "entity_id"])
    op.add_column("sync", sa.Column("sync_metadata", sa.JSON(), nullable=True))
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_column("sync", "sync_metadata")
    op.drop_constraint("uq_sync_id_entity_id", "entity", type_="unique")
    # ### end Alembic commands ###

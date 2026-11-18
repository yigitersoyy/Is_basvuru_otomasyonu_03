from alembic import op

from isakisi.db import SCHEMA

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    for statement in SCHEMA.split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade():
    raise RuntimeError("Önceki şema sürümü bulunmuyor. Geri dönmek için yedek kullanın.")

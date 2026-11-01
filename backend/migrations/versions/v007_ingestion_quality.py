"""Keep quality decisions and derived context separate from source evidence."""
import sqlalchemy as sa
from alembic import op

revision = "007_ingestion_quality"
down_revision = "006_operations"


def upgrade():
    op.add_column("document_revisions", sa.Column("quality_report", sa.JSON(), nullable=False, server_default="{}"))
    op.add_column("chunks", sa.Column("context_summary", sa.Text(), nullable=False, server_default=""))
    op.add_column("chunks", sa.Column("source_metadata", sa.JSON(), nullable=False, server_default="{}"))


def downgrade():
    raise RuntimeError("Restore a verified backup for downgrade")

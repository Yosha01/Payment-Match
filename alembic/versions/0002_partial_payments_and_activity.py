"""Allow several payments per invoice and record workspace activity."""
from alembic import op
import sqlalchemy as sa

revision = "0002_partial_payments_and_activity"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("matches", naming_convention={"uq": "uq_%(table_name)s_%(column_0_name)s"}) as batch:
            batch.drop_constraint("uq_matches_invoice_id", type_="unique")
    else:
        op.drop_constraint("matches_invoice_id_key", "matches", type_="unique")
    op.create_index("ix_matches_invoice_id", "matches", ["invoice_id"])
    op.create_table(
        "activity",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("detail", sa.String(1000), nullable=False),
    )


def downgrade():
    bind = op.get_bind()
    duplicate = bind.execute(sa.text("SELECT invoice_id FROM matches GROUP BY invoice_id HAVING count(*) > 1 LIMIT 1")).first()
    if duplicate:
        raise RuntimeError("Cannot downgrade: an invoice has more than one payment. Resolve these matches first.")
    op.drop_table("activity")
    op.drop_index("ix_matches_invoice_id", table_name="matches")
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("matches") as batch:
            batch.create_unique_constraint("uq_matches_invoice_id", ["invoice_id"])
    else:
        op.create_unique_constraint("matches_invoice_id_key", "matches", ["invoice_id"])

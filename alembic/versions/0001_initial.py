"""Initial schema."""
from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("invoices", sa.Column("id", sa.Integer, primary_key=True), sa.Column("invoice_number", sa.String(100), nullable=False, unique=True), sa.Column("customer", sa.String(200), nullable=False), sa.Column("amount", sa.Numeric(14, 2), nullable=False), sa.Column("currency", sa.String(3), nullable=False), sa.Column("issued_date", sa.Date, nullable=False))
    op.create_table("payments", sa.Column("id", sa.Integer, primary_key=True), sa.Column("transaction_id", sa.String(100), nullable=False, unique=True), sa.Column("payer", sa.String(200), nullable=False), sa.Column("amount", sa.Numeric(14, 2), nullable=False), sa.Column("currency", sa.String(3), nullable=False), sa.Column("paid_date", sa.Date, nullable=False), sa.Column("reference", sa.String(500), nullable=False))
    op.create_table("matches", sa.Column("id", sa.Integer, primary_key=True), sa.Column("invoice_id", sa.Integer, sa.ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False, unique=True), sa.Column("payment_id", sa.Integer, sa.ForeignKey("payments.id", ondelete="CASCADE"), nullable=False, unique=True))


def downgrade():
    op.drop_table("matches")
    op.drop_table("payments")
    op.drop_table("invoices")

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

import app.database as database

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
def test_migration_preserves_existing_match_and_allows_multiple_payments(tmp_path, monkeypatch, backend):
    if backend == "sqlite":
        url = f"sqlite:///{tmp_path / 'migration.db'}"
    else:
        url = os.getenv("TEST_DATABASE_URL")
        if not url:
            pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL migration coverage")
        if not (make_url(url).database or "").endswith("_test"):
            raise RuntimeError("TEST_DATABASE_URL must point to a dedicated database ending in _test")
    monkeypatch.setattr(database, "DATABASE_URL", url)
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    engine = create_engine(url)
    command.upgrade(cfg, "0001_initial")
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO invoices (id,invoice_number,customer,amount,currency,issued_date) VALUES (1,'A','Acme',100,'PLN','2026-01-01')"))
        connection.execute(text("INSERT INTO payments (id,transaction_id,payer,amount,currency,paid_date,reference) VALUES (1,'P1','Acme',40,'PLN','2026-01-02','A'),(2,'P2','Acme',60,'PLN','2026-01-03','A')"))
        connection.execute(text("INSERT INTO matches (id,invoice_id,payment_id) VALUES (1,1,1)"))
    command.upgrade(cfg, "head")
    with engine.begin() as connection:
        assert connection.execute(text("SELECT count(*) FROM matches WHERE invoice_id=1 AND payment_id=1")).scalar_one() == 1
        connection.execute(text("INSERT INTO matches (id,invoice_id,payment_id) VALUES (2,1,2)"))
        assert connection.execute(text("SELECT count(*) FROM matches WHERE invoice_id=1")).scalar_one() == 2
    with pytest.raises(RuntimeError, match="more than one payment"):
        command.downgrade(cfg, "0001_initial")
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM matches WHERE payment_id=2"))
    command.downgrade(cfg, "base")
    engine.dispose()

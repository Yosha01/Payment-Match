from pathlib import Path
import csv
import io
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.models import Invoice, Payment
from app.services import ImportProblem, import_records

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


@pytest.fixture
def db():
    url = os.getenv("TEST_DATABASE_URL")
    if url:
        if not (make_url(url).database or "").endswith("_test"):
            raise RuntimeError("TEST_DATABASE_URL must point to a dedicated database ending in _test")
        engine = create_engine(url)
        Base.metadata.drop_all(engine)
    else:
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as session:
        yield session
    if url:
        Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def upload(client, kind):
    content = (EXAMPLES / f"{kind}.csv").read_bytes()
    return client.post(f"/api/imports/{kind}", files={"file": (f"{kind}.csv", content, "text/csv")})


def test_complete_workflow(client):
    assert upload(client, "invoices").json()["imported"] == 5
    assert upload(client, "payments").json()["imported"] == 6
    assert upload(client, "payments").json()["duplicates"] == 6
    data = client.get("/api/reconciliation").json()
    rows = {row["invoice_number"]: row for row in data["invoices"]}
    exact = rows["INV-2026-001"]
    assert exact["candidates"][0]["reason"] == "Invoice number and remaining amount match"
    assert rows["INV-2026-002"]["candidates"][0]["reason"] == "Invoice number matches; amount differs from balance"
    assert len(rows["INV-2026-003"]["candidates"]) == 2
    assert rows["INV-2026-005"]["status"] == "unpaid"
    payment_id = exact["candidates"][0]["payment_id"]
    assert client.put(f"/api/invoices/{exact['id']}/match", json={"payment_id": payment_id}).status_code == 200
    assert client.put(f"/api/invoices/{rows['INV-2026-003']['id']}/match", json={"payment_id": payment_id}).status_code == 409
    report = list(csv.DictReader(io.StringIO(client.get("/api/report.csv").text)))
    assert next(row for row in report if row["invoice_number"] == "INV-2026-001")["status"] == "paid"
    assert len([row for row in report if row["record_type"] == "payment"]) == 5
    html = client.get("/").text
    assert "INV-2026-001" in html and "Paid" in html
    assert client.delete(f"/api/invoices/{exact['id']}/match").status_code == 204
    assert client.get("/api/reconciliation").json()["invoices"][-1]["status"] == "unpaid"


@pytest.mark.parametrize("content, expected", [
    (b"invoice_number,customer,amount,currency\nA,A,10,PLN\n", "Missing columns"),
    (b"invoice_number,customer,amount,currency,issued_date\nA,A,abc,PLN,2026-01-01\n", "Line 2"),
    (b"invoice_number,customer,amount,currency,issued_date\nA,A,10,PLN,2026-02-30\n", "Line 2"),
    (b"invoice_number,customer,amount,currency,issued_date\nA,A,10.999,PLN,2026-01-01\n", "Line 2"),
    (b"invoice_number,customer,amount,currency,issued_date\nA,A,0,PLN,2026-01-01\n", "greater than zero"),
    (b"\xff", "UTF-8"),
])
def test_validation_errors(client, content, expected):
    response = client.post("/api/imports/invoices", files={"file": ("bad.csv", content, "text/csv")})
    assert response.status_code == 400
    assert expected in response.json()["detail"]


def test_import_is_atomic_and_conflicts_are_detected(client, db):
    content = b"invoice_number,customer,amount,currency,issued_date\nA,A,10,PLN,2026-01-01\nB,B,10,EUR,2026-01-01\n"
    response = client.post("/api/imports/invoices", files={"file": ("data.csv", content, "text/csv")})
    assert response.status_code == 400 and "Line 3" in response.json()["detail"]
    assert db.scalars(select(Invoice)).all() == []
    assert upload(client, "invoices").status_code == 200
    conflict = b"invoice_number,customer,amount,currency,issued_date\nINV-2026-001,Northstar Studio,999,PLN,2026-01-04\n"
    response = client.post("/api/imports/invoices", files={"file": ("data.csv", conflict, "text/csv")})
    assert response.status_code == 400 and "different data" in response.json()["detail"]


def test_reference_boundary_and_manual_matching(client):
    invoices = b"invoice_number,customer,amount,currency,issued_date\nINV-1,Example,100,PLN,2026-01-01\n"
    payments = b"transaction_id,payer,amount,currency,paid_date,reference\nP1,Example,90,PLN,2026-01-02,INV-10\n"
    for kind, data in [("invoices", invoices), ("payments", payments)]:
        assert client.post(f"/api/imports/{kind}", files={"file": ("data.csv", data, "text/csv")}).status_code == 200
    row = client.get("/api/reconciliation").json()["invoices"][0]
    assert row["status"] == "unpaid"
    payment_id = client.get("/api/reconciliation").json()["unmatched_payment_ids"][0]
    assert client.put(f"/api/invoices/{row['id']}/match", json={"payment_id": payment_id}).status_code == 200
    assert "Partially paid" in client.get("/").text


def test_csv_export_escapes_spreadsheet_formulas(client):
    content = b"invoice_number,customer,amount,currency,issued_date\nI1,=1+1,10,PLN,2026-01-01\n"
    client.post("/api/imports/invoices", files={"file": ("data.csv", content, "text/csv")})
    report = list(csv.DictReader(io.StringIO(client.get("/api/report.csv").text)))
    assert report[0]["customer"] == "'=1+1"


def test_html_form_import_and_validation(client):
    response = client.post("/import/invoices", files={"file": ("invoices.csv", (EXAMPLES / "invoices.csv").read_bytes(), "text/csv")}, follow_redirects=True)
    assert response.status_code == 200 and "5 invoices imported" in response.text
    response = client.post("/import/payments", files={"file": ("payments.csv", b"not,a,csv\n", "text/csv")})
    assert response.status_code == 400 and "Missing columns" in response.text


def test_partial_payments_overpayment_removal_and_activity(client):
    invoices = b"invoice_number,customer,amount,currency,issued_date\nINV-A,Acme,850,PLN,2026-01-01\nINV-B,Beta,20,PLN,2026-01-02\n"
    payments = b"transaction_id,payer,amount,currency,paid_date,reference\nP1,Acme,400,PLN,2026-01-02,INV-A\nP2,Acme,450,PLN,2026-01-03,INV-A\nP3,Acme,50,PLN,2026-01-04,INV-A\n"
    for kind, content in [("invoices", invoices), ("payments", payments)]:
        assert client.post(f"/api/imports/{kind}", files={"file": (f"{kind}.csv", content, "text/csv")}).status_code == 200
    row = next(r for r in client.get("/api/reconciliation").json()["invoices"] if r["invoice_number"] == "INV-A")
    invoice_id = row["id"]
    ids = {c["transaction_id"]: c["payment_id"] for c in row["candidates"]}
    def match(payment):
        return client.put(f"/api/invoices/{invoice_id}/match", json={"payment_id": ids[payment]})
    assert match("P1").status_code == 200
    row = next(r for r in client.get("/api/reconciliation").json()["invoices"] if r["id"] == invoice_id)
    assert (row["status"], row["paid_amount"], row["balance"]) == ("partial", "400.00", "450.00")
    assert row["candidates"][0]["transaction_id"] == "P2"
    assert match("P2").status_code == 200
    assert match("P2").status_code == 200  # Repeated confirmation is idempotent.
    row = next(r for r in client.get("/api/reconciliation").json()["invoices"] if r["id"] == invoice_id)
    assert (row["status"], row["balance"], len(row["confirmed_payments"])) == ("paid", "0.00", 2)
    assert match("P3").status_code == 200
    row = next(r for r in client.get("/api/reconciliation").json()["invoices"] if r["id"] == invoice_id)
    assert (row["status"], row["overpayment"], row["balance"]) == ("overpaid", "50.00", "0.00")
    report = list(csv.DictReader(io.StringIO(client.get("/api/report.csv").text)))
    exported = next(row for row in report if row["invoice_number"] == "INV-A")
    assert exported["status"] == "overpaid" and exported["paid_amount"] == "900.00"
    assert client.delete(f"/api/invoices/{invoice_id}/matches/{ids['P3']}").status_code == 204
    assert client.delete(f"/api/invoices/{invoice_id}/matches/{ids['P3']}").status_code == 404
    events = client.get("/api/activity").json()
    assert [e["action"] for e in events].count("match_added") == 3
    assert events[0]["action"] == "match_removed"
    assert "Recent activity" in client.get("/").text


def test_search_status_filter_and_pagination(client):
    header = "invoice_number,customer,amount,currency,issued_date\n"
    content = (header + "".join(f"INV-{i:02},Customer {i},10,PLN,2026-01-01\n" for i in range(12))).encode()
    assert client.post("/api/imports/invoices", files={"file": ("data.csv", content, "text/csv")}).status_code == 200
    data = client.get("/api/reconciliation?page=2&status=unpaid").json()
    assert (data["filtered_count"], data["page"], data["total_pages"], len(data["invoices"])) == (12, 2, 2, 2)
    assert client.get("/api/reconciliation?q=customer%2011").json()["filtered_count"] == 1
    assert client.get("/api/reconciliation?q=%25").json()["filtered_count"] == 0
    assert client.get("/api/reconciliation?status=paid").json()["filtered_count"] == 0
    assert client.get("/api/reconciliation?status=unknown").status_code == 400
    assert "Page 2 of 2" in client.get("/?page=2&status=unpaid").text


def test_form_preserves_search_after_match(client):
    upload(client, "invoices")
    upload(client, "payments")
    row = next(r for r in client.get("/api/reconciliation?q=INV-2026-002").json()["invoices"])
    payment_id = next(c["payment_id"] for c in row["candidates"] if c["transaction_id"] == "TXN-002")
    response = client.post(f"/matches/{row['id']}", data={"payment_id": payment_id, "q": "INV-2026-002", "status": "all", "page_number": 1}, follow_redirects=True)
    assert response.status_code == 200
    assert 'value="INV-2026-002"' in response.text
    assert "Partially paid" in response.text and "Remaining <strong>50.00 PLN" in response.text

# Payment Match

Payment Match helps you make sense of a list of invoices and a bank statement. Give it a CSV file for each, and it will bring the records together so you can see which invoices are paid and which still need attention.

It looks for possible matches by invoice number and amount, but leaves the final decision to you. That matters when two payments have the same amount or a customer pays an invoice in several instalments.

![Payment Match workspace](docs/workspace.jpg)

[Watch the 63-second walkthrough](docs/payment-match-demo.mp4) · [Portfolio notes](docs/portfolio.md)

The walkthrough uses screenshots of the application with on-screen captions and fictional sample data; it has no audio.

## An example

Say you have an invoice for **850 PLN** and receive a payment for **800 PLN**. Once you confirm the match, the invoice is marked as partially paid and the remaining balance is **50 PLN**. If you later confirm another payment for **50 PLN**, the invoice is marked as paid. Both payments stay visible, and either match can be removed if you made a mistake.

If a payment is larger than the balance, the invoice is marked as overpaid and the extra amount is shown separately. Suggestions are just that: suggestions. The app never confirms them on your behalf.

## Features

- Import invoices and payments from CSV with validation and line-specific error messages. A failed import rolls back the entire file.
- Suggest payments using the invoice number in the payment reference and the invoice's remaining balance. Amount-only matches are suggestions, not proof of payment. Nothing is confirmed automatically.
- Confirm multiple payments against one invoice. Each payment can only belong to one invoice. Remove individual matches when a correction is needed.
- Track **Unpaid**, **Partially paid**, **Paid**, and **Overpaid** invoices. The interface shows the amount received, remaining balance, and any overpayment.
- Search by invoice number or customer, filter by payment status, and browse invoice results in pages of 10.
- Review the last 20 successful imports and match changes in Recent activity. Each successful action and its activity record are committed together.
- Export all invoices and unassigned payments to CSV, independently of the active search and filter. Amounts use decimal arithmetic.
- Use the browser interface or documented REST API.

## Quick start with Docker

Requirements: Docker with the Compose plugin. From the project directory:

```bash
docker compose up --build
```

Open <http://localhost:8000>. Upload `examples/invoices.csv` and `examples/payments.csv`. Find `INV-2026-002`, confirm `TXN-002` for 800 PLN, and then `TXN-006` for 50 PLN to settle the 850 PLN invoice. Use **Export report** to download the CSV report. API documentation: <http://localhost:8000/docs>.

The app runs database migrations on startup. PostgreSQL data persists in the `postgres_data` Docker volume. Stop the services with `docker compose down`.

### Local Python alternative

For a local demonstration without PostgreSQL, use Python 3.12 and SQLite:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
export DATABASE_URL=sqlite:///./payment-match.db
alembic upgrade head
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

The SQLite database is stored in the project directory. Without `DATABASE_URL`, the application connects to local PostgreSQL using the development credentials in `compose.yaml`.

## CSV formats

Use UTF-8, comma-separated files, up to 2 MB each, with a header and at least one data row. Required values cannot be empty. Dates use `YYYY-MM-DD`; amounts are positive decimals with up to two fractional digits. All records in a workspace must use the same three-letter currency code. Extra columns are ignored.

**Invoices**

```csv
invoice_number,customer,amount,currency,issued_date
INV-2026-001,Northstar Studio,1200.00,PLN,2026-01-04
```

**Payments**

```csv
transaction_id,payer,amount,currency,paid_date,reference
TXN-001,Northstar Studio,1200.00,PLN,2026-01-07,Payment for INV-2026-001
```

Invoice numbers and transaction IDs are unique within their respective datasets. A repeated identifier with identical values is skipped; an identifier with changed values rejects the entire file. Different transaction IDs are treated as separate transactions even if their other fields match.

## API

The interactive documentation at `/docs` includes request and response details.

| Method | Endpoint | Purpose |
| --- | --- | --- |
| POST | `/api/imports/invoices` | Upload an invoice CSV as multipart field `file` |
| POST | `/api/imports/payments` | Upload a payment CSV as multipart field `file` |
| GET | `/api/reconciliation` | List invoice balances, confirmed payments and suggestions; accepts `q`, `status` and `page` |
| PUT | `/api/invoices/{invoice_id}/match` | Confirm one payment using `{"payment_id": 1}`; repeat to add another |
| DELETE | `/api/invoices/{invoice_id}/matches/{payment_id}` | Remove one confirmed payment |
| DELETE | `/api/invoices/{invoice_id}/match` | Remove all confirmed payments for an invoice |
| GET | `/api/activity` | List the latest 20 activity records |
| GET | `/api/report.csv` | Download the complete reconciliation report |

Status filters are `all`, `unpaid`, `partial`, `paid` and `overpaid`. The response includes page and total-page counts. Repeating confirmation of a payment already matched to the same invoice is idempotent; assigning it to another invoice returns `409`. Missing records return `404` and invalid CSV imports return `400`.

```bash
curl -F file=@examples/invoices.csv http://localhost:8000/api/imports/invoices
curl -F file=@examples/payments.csv http://localhost:8000/api/imports/payments
curl 'http://localhost:8000/api/reconciliation?status=unpaid&page=1'
```

The report includes invoice amount, confirmed transaction IDs, received amount, remaining balance, overpayment and payment status, followed by unassigned payments. Spreadsheet formula-like text is escaped in the export.

## Tests and CI

With the Python environment activated:

```bash
python -m pytest -q
```

By default tests use isolated SQLite databases. They cover import validation and rollback, existing data migration, suggestions, partial and overpayments, match changes, activity history, search, filters, pagination and export. The PostgreSQL migration test is skipped without a test database.

To run the same suite against PostgreSQL, create a **dedicated disposable database** whose name ends in `_test`, then run:

```bash
export TEST_DATABASE_URL=postgresql+psycopg://payment_match:payment_match@localhost:5432/payment_match_test
python -m pytest -q
```

**Tests create and drop tables in this database.** Never point `TEST_DATABASE_URL` at data you want to keep. The PostgreSQL migration test upgrades an existing one-payment schema, checks that its match survives, and verifies that a second payment can be added. A downgrade is deliberately refused while an invoice has multiple matches.

The GitHub Actions workflow runs the SQLite and PostgreSQL suites and builds and smoke-tests Docker Compose. It runs after the project is placed in a GitHub repository; a workflow file alone does not establish that those checks have passed.

## Design decisions and limitations

- The remaining balance is zero for an overpaid invoice; the excess is shown separately. A user can confirm an overpayment, which stays visible for review.
- Payment suggestions are calculated from unassigned payments and the current balance. Ambiguous candidates require a human decision.
- A payment cannot be split across multiple invoices. Refunds, bank integrations, automatic conversion and multi-currency workspaces are outside the current scope.
- The activity log records successful actions, not failed attempts; it shows the most recent 20 entries and is not a tamper-proof audit system.
- This is a single-user local demo without authentication. Do not expose it to the public internet or use real financial data in a public demo.
- Invoice searching, status filtering and pagination run in SQL. Candidate calculation still considers available payments in memory and is intended for modest datasets.

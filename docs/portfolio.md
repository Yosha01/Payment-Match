# Portfolio notes

## Problem

A small business needs to see which invoices are unpaid, partially paid or settled after importing a bank statement. Payment references can be ambiguous, and an invoice may be settled across several transactions.

## What the application does

Payment Match imports two CSV files, suggests possible matches, lets the user confirm several payments per invoice, and calculates the balance. It records changes in a local activity log and produces a CSV report.

## Decisions worth discussing

- **Human review:** a reference or amount may match more than one record. Suggestions are displayed with their reason and are never confirmed automatically.
- **Payment ownership:** a database uniqueness constraint prevents a payment from being confirmed against multiple invoices, while the invoice can accept multiple payments.
- **Amounts and states:** monetary values use decimal arithmetic. The balance is derived from confirmed payments, and an excess is shown as overpayment rather than a negative balance.
- **Imports:** an identifier repeated with identical content is skipped; conflicting content rejects the file. Successful import and activity history are saved in the same transaction.
- **Migration safety:** the schema upgrade preserves existing confirmed matches. The reverse migration refuses to proceed when multiple payments would violate the old constraint.

## Example CV description

> Payment Match — Python / FastAPI / SQLAlchemy / PostgreSQL / pytest. A local reconciliation service that imports CSV records, supports partial payments, calculates invoice balances, and exports reports. Includes database migrations, REST endpoints and automated tests.

Adapt the description to the work you personally contributed and can explain. Link to the repository and the short walkthrough after the repository is published.

import csv
import io
import re
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import Integer, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Activity, Invoice, Match, Payment

MAX_FILE_SIZE = 2 * 1024 * 1024
SPECS = {
    "invoices": (Invoice, ("invoice_number", "customer", "amount", "currency", "issued_date")),
    "payments": (Payment, ("transaction_id", "payer", "amount", "currency", "paid_date", "reference")),
}
LENGTHS = {"invoice_number": 100, "transaction_id": 100, "customer": 200, "payer": 200, "reference": 500}


class ImportProblem(ValueError):
    pass


def parse_csv(data: bytes, kind: str):
    if len(data) > MAX_FILE_SIZE:
        raise ImportProblem("File exceeds the 2 MB limit.")
    try:
        content = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ImportProblem("File must be UTF-8 encoded.") from exc
    model, fields = SPECS[kind]
    reader = csv.DictReader(io.StringIO(content, newline=""), strict=True)
    try:
        if not reader.fieldnames or len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise ImportProblem("CSV must have a header with unique column names.")
        missing = set(fields) - set(reader.fieldnames)
        if missing:
            raise ImportProblem("Missing columns: " + ", ".join(sorted(missing)))
        records = []
        for row in reader:
            line = reader.line_num
            if None in row or any(row.get(field) is None for field in fields):
                raise ImportProblem(f"Line {line}: the number of values does not match the header.")
            cleaned = {field: row[field].strip() for field in fields}
            for field in fields:
                if not cleaned[field]:
                    raise ImportProblem(f"Line {line}: {field} is required.")
                if field in LENGTHS and len(cleaned[field]) > LENGTHS[field]:
                    raise ImportProblem(f"Line {line}: {field} exceeds {LENGTHS[field]} characters.")
            value = cleaned["amount"]
            if not re.fullmatch(r"\d{1,12}(?:\.\d{1,2})?", value):
                raise ImportProblem(f"Line {line}: amount must be a positive decimal with at most 2 decimal places.")
            try:
                cleaned["amount"] = Decimal(value).quantize(Decimal("0.01"))
            except InvalidOperation as exc:
                raise ImportProblem(f"Line {line}: invalid amount.") from exc
            if cleaned["amount"] <= 0:
                raise ImportProblem(f"Line {line}: amount must be greater than zero.")
            cleaned["currency"] = cleaned["currency"].upper()
            if not re.fullmatch(r"[A-Z]{3}", cleaned["currency"]):
                raise ImportProblem(f"Line {line}: currency must be a three-letter code.")
            date_field = "issued_date" if kind == "invoices" else "paid_date"
            try:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", cleaned[date_field]):
                    raise ValueError
                cleaned[date_field] = date.fromisoformat(cleaned[date_field])
            except ValueError as exc:
                raise ImportProblem(f"Line {line}: {date_field} must be a valid date in YYYY-MM-DD format.") from exc
            records.append((line, cleaned))
        if not records:
            raise ImportProblem("CSV contains no data rows.")
        return model, fields, records
    except csv.Error as exc:
        raise ImportProblem(f"Line {reader.line_num}: invalid CSV: {exc}") from exc


def import_records(db: Session, kind: str, data: bytes, filename: str = "CSV file"):
    model, fields, records = parse_csv(data, kind)
    existing_currency = db.scalar(select(Invoice.currency).limit(1)) or db.scalar(select(Payment.currency).limit(1))
    currency = existing_currency or records[0][1]["currency"]
    key = "invoice_number" if kind == "invoices" else "transaction_id"
    seen = {}
    added = duplicates = 0
    for line, values in records:
        if values["currency"] != currency:
            raise ImportProblem(f"Line {line}: all records must use {currency}.")
        identity = values[key]
        existing = seen.get(identity) or db.scalar(select(model).where(getattr(model, key) == identity))
        if existing:
            if any(getattr(existing, field) != values[field] for field in fields):
                raise ImportProblem(f"Line {line}: {key} '{identity}' already exists with different data.")
            duplicates += 1
            continue
        item = model(**values)
        seen[identity] = item
        db.add(item)
        added += 1
    name = filename.replace("\n", " ").replace("\r", " ")[:200]
    db.add(Activity(action="import", detail=f"{kind.title()}: {name} — {added} imported, {duplicates} duplicates skipped"))
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ImportProblem("Import conflicted with existing records. Please retry.") from exc
    return {"imported": added, "duplicates": duplicates, "currency": currency}


def candidates(invoice, payments, remaining):
    result = []
    pattern = re.compile(r"(?<![A-Za-z0-9])" + re.escape(invoice.invoice_number) + r"(?![A-Za-z0-9])", re.IGNORECASE)
    for payment in payments:
        ref = bool(pattern.search(payment.reference))
        amount = remaining == payment.amount
        if ref and amount:
            reason, rank = "Invoice number and remaining amount match", 0
        elif ref:
            reason, rank = "Invoice number matches; amount differs from balance", 1
        elif amount:
            reason, rank = "Remaining amount matches; invoice number not found", 2
        else:
            continue
        result.append({"payment": payment, "reason": reason, "rank": rank})
    return sorted(result, key=lambda item: (item["rank"], item["payment"].paid_date, item["payment"].id))


def overview(db: Session, search="", status="all", page=1, page_size=10):
    # Aggregate in SQL, then paginate invoices before building their suggestions.
    totals = (select(Match.invoice_id.label("invoice_id"), func.sum(Payment.amount).label("paid"))
              .join(Payment, Payment.id == Match.payment_id).group_by(Match.invoice_id).subquery())
    paid = func.coalesce(totals.c.paid, 0)
    base = select(Invoice, paid.label("paid")).outerjoin(totals, totals.c.invoice_id == Invoice.id)
    if search:
        term = search.strip()
        base = base.where(Invoice.invoice_number.icontains(term, autoescape=True) | Invoice.customer.icontains(term, autoescape=True))
    if status == "unpaid":
        base = base.where(paid == 0)
    elif status == "partial":
        base = base.where(paid > 0, paid < Invoice.amount)
    elif status == "paid":
        base = base.where(paid == Invoice.amount)
    elif status == "overpaid":
        base = base.where(paid > Invoice.amount)
    elif status != "all":
        raise ValueError("Unknown status filter.")
    count = db.scalar(select(func.count()).select_from(base.subquery()))
    total_pages = max(1, (count + page_size - 1) // page_size) if page_size else 1
    page = max(1, min(page, total_pages))
    query = base.order_by(Invoice.issued_date.desc(), Invoice.id.desc())
    if page_size:
        query = query.limit(page_size).offset((page - 1) * page_size)
    invoices = db.execute(query).all()
    payments = db.scalars(select(Payment).order_by(Payment.paid_date.desc(), Payment.id.desc())).all()
    matches = db.scalars(select(Match)).all()
    payment_map = {p.id: p for p in payments}
    used = {m.payment_id for m in matches}
    available = [p for p in payments if p.id not in used]
    by_invoice = {}
    for match in matches:
        if match.payment_id in payment_map:
            by_invoice.setdefault(match.invoice_id, []).append(payment_map[match.payment_id])
    rows = []
    for invoice, total in invoices:
        total = Decimal(total).quantize(Decimal("0.01"))
        balance = max(invoice.amount - total, Decimal("0.00"))
        overpayment = max(total - invoice.amount, Decimal("0.00"))
        invoice_status = "unpaid" if total == 0 else "partial" if total < invoice.amount else "paid" if total == invoice.amount else "overpaid"
        suggestions = candidates(invoice, available, balance) if balance > 0 else []
        rows.append({"invoice": invoice, "payments": by_invoice.get(invoice.id, []), "paid_amount": total,
                     "balance": balance, "overpayment": overpayment, "candidates": suggestions, "status": invoice_status})
    all_totals = select(Invoice.amount, paid.label("paid")).outerjoin(totals, totals.c.invoice_id == Invoice.id).subquery()
    invoice_count, paid_count, open_count = db.execute(select(
        func.count(),
        func.coalesce(func.sum((all_totals.c.paid >= all_totals.c.amount).cast(Integer)), 0),
        func.coalesce(func.sum((all_totals.c.paid < all_totals.c.amount).cast(Integer)), 0),
    )).one()
    currency = db.scalar(select(Invoice.currency).limit(1)) or db.scalar(select(Payment.currency).limit(1))
    return {"rows": rows, "payments": payments, "available_payments": available,
            "invoice_count": invoice_count, "payment_count": len(payments), "paid_count": paid_count,
            "open_count": open_count, "currency": currency, "search": search, "status_filter": status,
            "page": page, "total_pages": total_pages, "filtered_count": count,
            "activity": db.scalars(select(Activity).order_by(Activity.id.desc()).limit(20)).all()}


def confirm_match(db: Session, invoice_id: int, payment_id: int):
    invoice, payment = db.get(Invoice, invoice_id), db.get(Payment, payment_id)
    if not invoice or not payment:
        raise LookupError("Invoice or payment not found.")
    other = db.scalar(select(Match).where(Match.payment_id == payment_id))
    if other:
        if other.invoice_id == invoice_id:
            return
        raise ImportProblem("This payment is already matched to another invoice.")
    db.add(Match(invoice_id=invoice_id, payment_id=payment_id))
    db.add(Activity(action="match_added", detail=f"{payment.transaction_id} matched to {invoice.invoice_number}"))
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ImportProblem("The match conflicts with another confirmation. Please retry.") from exc


def clear_match(db: Session, invoice_id: int, payment_id: int | None = None):
    invoice = db.get(Invoice, invoice_id)
    if not invoice:
        raise LookupError("Invoice not found.")
    statement = select(Match).where(Match.invoice_id == invoice_id)
    if payment_id is not None:
        statement = statement.where(Match.payment_id == payment_id)
    matches = db.scalars(statement).all()
    if payment_id is not None and not matches:
        raise LookupError("Match not found.")
    for match in matches:
        payment = db.get(Payment, match.payment_id)
        db.add(Activity(action="match_removed", detail=f"{payment.transaction_id} removed from {invoice.invoice_number}"))
        db.delete(match)
    if matches:
        db.commit()


def report_csv(db: Session):
    data = overview(db, page_size=None)
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["record_type", "invoice_number", "customer", "invoice_amount", "transaction_ids", "paid_amount", "balance", "overpayment", "currency", "status"])
    def safe(value):
        value = str(value)
        return "'" + value if value.startswith(("=", "+", "-", "@", "\t", "\r")) else value
    for row in data["rows"]:
        i = row["invoice"]
        ids = "; ".join(p.transaction_id for p in row["payments"])
        writer.writerow(["invoice", safe(i.invoice_number), safe(i.customer), i.amount, safe(ids), row["paid_amount"], row["balance"], row["overpayment"], i.currency, row["status"]])
    for p in data["available_payments"]:
        writer.writerow(["payment", "", safe(p.payer), "", safe(p.transaction_id), p.amount, "", "", p.currency, "unmatched"])
    return output.getvalue()

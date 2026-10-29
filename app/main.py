from pathlib import Path
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Activity
from app.services import MAX_FILE_SIZE, ImportProblem, clear_match, confirm_match, import_records, overview, report_csv

ROOT = Path(__file__).parent
app = FastAPI(title="Payment Match", version="1.0.0", description="Import invoices and payments and review possible matches.")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
templates = Jinja2Templates(directory=ROOT / "templates")


class MatchRequest(BaseModel):
    payment_id: int = Field(gt=0)


def page(request, db, message=None, error=None, status_code=200):
    params = request.query_params
    status = params.get("status", "all")
    status = status if status in {"all", "unpaid", "partial", "paid", "overpaid"} else "all"
    try:
        page_number = max(1, int(params.get("page", "1")))
    except ValueError:
        page_number = 1
    data = overview(db, search=params.get("q", "")[:200], status=status, page=page_number)
    return templates.TemplateResponse(request=request, name="index.html", context={"data": data, "message": message, "error": error}, status_code=status_code)


def perform_import(db, kind, content, filename):
    try:
        return import_records(db, kind, content, filename or "CSV file")
    except ImportProblem:
        db.rollback()
        raise


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index(request: Request, db: Session = Depends(get_db)):
    return page(request, db, message=request.query_params.get("message"))


@app.post("/import/{kind}", response_class=HTMLResponse, include_in_schema=False)
async def import_form(request: Request, kind: str, file: UploadFile = File(...), db: Session = Depends(get_db)):
    if kind not in ("invoices", "payments"):
        raise HTTPException(404, "Unknown import type.")
    try:
        result = perform_import(db, kind, await file.read(MAX_FILE_SIZE + 1), file.filename)
    except ImportProblem as exc:
        return page(request, db, error=str(exc), status_code=400)
    message = f"{result['imported']} {kind} imported · {result['duplicates']} duplicates skipped."
    return RedirectResponse("/?" + urlencode({"message": message}), status_code=303)


def workspace_redirect(message, q, status, page_number):
    return RedirectResponse("/?" + urlencode({"message": message, "q": q[:200], "status": status, "page": page_number}) + "#review", status_code=303)


@app.post("/matches/{invoice_id}", include_in_schema=False)
def match_form(request: Request, invoice_id: int, payment_id: int = Form(...), q: str = Form(""), status: str = Form("all"), page_number: int = Form(1), db: Session = Depends(get_db)):
    try:
        confirm_match(db, invoice_id, payment_id)
    except (ImportProblem, LookupError) as exc:
        db.rollback()
        return page(request, db, error=str(exc), status_code=400)
    return workspace_redirect("Match confirmed", q, status, page_number)


@app.post("/matches/{invoice_id}/{payment_id}/remove", include_in_schema=False)
def remove_form(invoice_id: int, payment_id: int, q: str = Form(""), status: str = Form("all"), page_number: int = Form(1), db: Session = Depends(get_db)):
    try:
        clear_match(db, invoice_id, payment_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return workspace_redirect("Match removed", q, status, page_number)


@app.post("/api/imports/{kind}", tags=["Imports"])
async def api_import(kind: str, file: UploadFile = File(...), db: Session = Depends(get_db)):
    if kind not in ("invoices", "payments"):
        raise HTTPException(404, "Unknown import type.")
    try:
        return perform_import(db, kind, await file.read(MAX_FILE_SIZE + 1), file.filename)
    except ImportProblem as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/reconciliation", tags=["Reconciliation"])
def api_reconciliation(q: str = Query(default="", max_length=200), status: str = "all", page_number: int = Query(default=1, ge=1, alias="page"), db: Session = Depends(get_db)):
    try:
        data = overview(db, search=q, status=status, page=page_number)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"currency": data["currency"], "page": data["page"], "total_pages": data["total_pages"], "filtered_count": data["filtered_count"], "invoices": [{
        "id": r["invoice"].id, "invoice_number": r["invoice"].invoice_number,
        "customer": r["invoice"].customer, "amount": str(r["invoice"].amount), "status": r["status"],
        "paid_amount": str(r["paid_amount"]), "balance": str(r["balance"]), "overpayment": str(r["overpayment"]),
        "confirmed_payments": [{"payment_id": p.id, "transaction_id": p.transaction_id, "amount": str(p.amount)} for p in r["payments"]],
        "candidates": [{"payment_id": c["payment"].id, "transaction_id": c["payment"].transaction_id,
                        "amount": str(c["payment"].amount), "reason": c["reason"]} for c in r["candidates"]]
    } for r in data["rows"]], "unmatched_payment_ids": [p.id for p in data["available_payments"]]}


@app.put("/api/invoices/{invoice_id}/match", tags=["Reconciliation"])
def api_match(invoice_id: int, payload: MatchRequest, db: Session = Depends(get_db)):
    try:
        confirm_match(db, invoice_id, payload.payment_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ImportProblem as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"invoice_id": invoice_id, "payment_id": payload.payment_id, "status": "confirmed"}


@app.delete("/api/invoices/{invoice_id}/matches/{payment_id}", status_code=204, tags=["Reconciliation"])
def api_remove_payment(invoice_id: int, payment_id: int, db: Session = Depends(get_db)):
    try:
        clear_match(db, invoice_id, payment_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return Response(status_code=204)


@app.delete("/api/invoices/{invoice_id}/match", status_code=204, tags=["Reconciliation"])
def api_clear(invoice_id: int, db: Session = Depends(get_db)):
    try:
        clear_match(db, invoice_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return Response(status_code=204)


@app.get("/api/activity", tags=["Activity"])
def api_activity(db: Session = Depends(get_db)):
    events = db.scalars(select(Activity).order_by(Activity.id.desc()).limit(20)).all()
    return [{"id": event.id, "created_at": event.created_at, "action": event.action, "detail": event.detail} for event in events]


@app.get("/api/report.csv", tags=["Reports"])
def download_report(db: Session = Depends(get_db)):
    return Response(report_csv(db), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="payment-match-report.csv"'})

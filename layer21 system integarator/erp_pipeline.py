"""
Shared FIRS pipeline for ERP receivers (Odoo, Sage 200 Evolution, ...).

Each ERP blueprint maps its own payload to the Layer21 canonical dict, then
hands it to run_erp_invoice_pipeline(), which validates it, resumes or creates
the invoice records and runs the IRN pipeline. Must be called inside a request
authenticated by require_api_auth (it reads g.business).
"""

import logging
from datetime import datetime

from flask import jsonify, g

from helpers import (
    validate_basic_invoice_payload,
    build_irn_candidate,
    generate_irn,
    log_submission,
    _process_invoice_irn_request,
    _process_invoice_transmit_only,
    POST_SIGN_STATUSES,
)
from models import InvoiceSubmission, SubmissionLog

logger = logging.getLogger(__name__)


def run_erp_invoice_pipeline(data, source, received_message):
    """
    Run a mapped ERP invoice through the FIRS pipeline.

    Args:
        data:             Layer21 canonical dict produced by the ERP mapper
        source:           ERP name used in logs, e.g. "Odoo"
        received_message: first SubmissionLog message for a new invoice

    Returns:
        A Flask (response, status) tuple.
    """
    try:
        from models import Invoices, Business, db
    except Exception:
        logger.exception("models import failed in %s invoice pipeline", source)
        return jsonify({"ok": False, "error": "internal server error"}), 500

    business_id = data["business_id"]

    # ── Validate required fields ───────────────────────────────────────────────
    ok, err = validate_basic_invoice_payload(data)
    if not ok:
        return jsonify({"ok": False, "error": err}), 400

    biz = g.business
    service_id = getattr(biz, "service_id", None)
    if not service_id:
        return jsonify({
            "ok": False,
            "error": "Business account has no service_id configured",
        }), 400

    # ── Build IRN candidate ────────────────────────────────────────────────────
    try:
        irn_candidate = build_irn_candidate(
            data.get("invoice_number") or f"INV{int(datetime.utcnow().timestamp())}",
            service_id,
            data.get("issue_date") or datetime.utcnow().date().isoformat(),
        )
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    # ── Duplicate / resume check ───────────────────────────────────────────────
    try:
        issue_date_obj = datetime.fromisoformat(data["issue_date"]).date() if data.get("issue_date") else None
        irn = generate_irn(data.get("invoice_number"), service_id, issue_date_obj)
        existing_invoice = Invoices.query.filter(Invoices.irn == irn).first()
    except Exception:
        existing_invoice = None

    if existing_invoice:
        existing_submission = (
            InvoiceSubmission.query
            .filter_by(invoice_id=existing_invoice.id)
            .order_by(InvoiceSubmission.id.desc())
            .first()
        )
        existing_status = getattr(existing_submission, "status", None) or existing_invoice.irn_status
        if existing_invoice.irn_status == "transmitted" and existing_status == "transmitted":
            return jsonify({
                "ok": False,
                "error": "invoice already fully transmitted",
                "invoice_id": existing_invoice.id,
                "irn": existing_invoice.irn,
                "status": existing_status,
            }), 409

        logger.info(
            "%s: invoice IRN %s already exists (id=%s) status=%r — resuming",
            source, irn, existing_invoice.id, existing_status,
        )
        log_submission(
            existing_submission,
            f"{source} duplicate push — resuming pipeline from status={existing_status!r}",
        )
        try:
            incoming_customer = data.get("accounting_customer_party")
            if incoming_customer and incoming_customer.get("tin"):
                existing_invoice.accounting_customer_party = dict(incoming_customer)
                db.session.commit()
            if existing_invoice.irn_status in ("issued", "signed") or existing_status in POST_SIGN_STATUSES:
                pipeline_result = _process_invoice_transmit_only(existing_invoice.id)
            else:
                pipeline_result = _process_invoice_irn_request(existing_invoice.id)
        except Exception as exc:
            logger.exception("Pipeline resume failed for invoice %s: %s", existing_invoice.id, exc)
            return jsonify({"ok": False, "error": "pipeline resume failed", "details": str(exc)}), 500

        log_history = []
        try:
            logs = (
                SubmissionLog.query
                .filter_by(submission_id=getattr(existing_submission, "id", None))
                .order_by(SubmissionLog.created_at.asc())
                .all()
            )
            log_history = [
                {
                    "timestamp": log.created_at.isoformat() if log.created_at else None,
                    "message": getattr(log, "message", None),
                    "level": getattr(log, "level", "INFO"),
                    "status": getattr(log, "status", None),
                }
                for log in logs
            ]
        except Exception:
            logger.exception("Could not fetch log history on %s resume (non-fatal)", source)

        pipeline_result["log_history"] = log_history
        pipeline_result["resumed"] = True
        pipeline_result["previous_status"] = existing_status
        pipeline_result["submission_id"] = getattr(existing_submission, "id", None)
        pipeline_result["irn_candidate"] = irn
        return jsonify(pipeline_result), 200 if pipeline_result.get("ok") else 500

    # ── Create InvoiceSubmission + Invoices records ───────────────────────────
    try:
        due_date_obj = None
        if data.get("due_date"):
            try:
                due_date_obj = datetime.fromisoformat(data["due_date"]).date()
            except Exception:
                pass
        if not due_date_obj:
            due_date_obj = issue_date_obj or datetime.utcnow().date()

        supplier_obj = dict(data.get("accounting_supplier_party") or {})
        supplier_obj["service_id"] = service_id
        customer_obj = dict(data.get("accounting_customer_party") or {})

        legal_monetary_total = data.get("legal_monetary_total") or {}
        invoice_line = data.get("invoice_line") or []
        tax_total = data.get("tax_total") or []
        payment_means = data.get("payment_means") or []

        # InvoiceSubmission.business_id is Integer FK to businesses.id
        submission = InvoiceSubmission(
            business_id=biz.id,
            payload=data,
            status="validated",
            irn_candidate=irn_candidate,
        )
        db.session.add(submission)
        db.session.flush()

        # Invoices.business_id is String (stores the buisness_id string)
        inv = Invoices(
            business_id=business_id,
            issue_date=issue_date_obj or datetime.utcnow().date(),
            due_date=due_date_obj,
            issue_time=datetime.utcnow().time(),
            invoice_type_code=data.get("invoice_type_code"),
            document_currency_code=data.get("document_currency_code", "NGN"),
            tax_point_date=issue_date_obj or datetime.utcnow().date(),
            accounting_supplier_party=supplier_obj,
            accounting_customer_party=customer_obj,
            payment_means=payment_means,
            allowance_charge=[],
            tax_total=tax_total,
            legal_monetary_total=legal_monetary_total,
            invoice_line=invoice_line,
            irn_status="draft",
            order_reference=data.get("invoice_number"),
        )

        db.session.add(inv)
        db.session.flush()
        submission.invoice_id = inv.id
        db.session.commit()

        log_submission(submission, received_message)

        pipeline_result = _process_invoice_irn_request(inv.id)

    except Exception as exc:
        db.session.rollback()
        logger.exception("Failed creating invoice from %s payload: %s", source, exc)
        return jsonify({"ok": False, "error": "failed to create invoice", "details": str(exc)}), 500

    # ── Collect log history and return ────────────────────────────────────────
    log_history = []
    try:
        logs = (
            SubmissionLog.query
            .filter_by(submission_id=submission.id)
            .order_by(SubmissionLog.created_at.asc())
            .all()
        )
        log_history = [
            {
                "timestamp": getattr(log, "created_at", None) and log.created_at.isoformat(),
                "message": getattr(log, "message", None),
                "level": getattr(log, "level", "INFO"),
                "status": getattr(log, "status", None),
            }
            for log in logs
        ]
    except Exception:
        logger.exception("Could not fetch submission log history (non-fatal)")

    pipeline_result["log_history"] = log_history
    pipeline_result["submission_id"] = submission.id
    pipeline_result["invoice_id"] = inv.id
    pipeline_result["irn_candidate"] = irn_candidate

    return jsonify(pipeline_result), 201 if pipeline_result.get("ok") else 500

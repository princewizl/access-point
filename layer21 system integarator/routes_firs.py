# routes_firs.py
"""
Blueprint exposing endpoints:
- POST /api/v1/business/register
- POST /api/v1/invoice/receive
- POST /invoice/<id>/request-irn  (web UI route - uses login_required)
Register with: app.register_blueprint(firs_bp)
"""

from flask import Blueprint, request, jsonify, current_app, g, url_for, redirect, flash, Response, stream_with_context
from flask_login import login_required
from datetime import datetime
import json, logging, os, time
import requests

from helpers import (
    require_api_auth, validate_basic_invoice_payload, build_irn_candidate,
    build_firs_validate_payload_from_invoice, firs_validate_invoice,
    sign_payload_with_app_key, make_invoice_xml, sign_payload_rsa_b64,
    firs_sign_invoice, transmit_to_firs, generate_irn, log_submission,
    _process_invoice_irn_request, _process_invoice_transmit_only
)
from models import InvoiceSubmission,SubmissionLog

logger = logging.getLogger(__name__)
firs_bp = Blueprint("firs", __name__, url_prefix="/api/v1")

# local model imports inside functions to avoid circular import at module load time


@firs_bp.route("/business/register", methods=["POST"])
def api_register_business():
    """
    Register a tenant/business (returns API key + raw secret once).
    POST JSON: { "name": "...", "tin": "...", "service_id": "..." }
    """
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    tin = (data.get("tin") or "").strip()
    service_id = (data.get("service_id") or "").strip() or None
    
    # Use environment variable defaults for testing
    if not name:
        name = os.environ.get("TEST_BUSINESS_NAME")
    if not tin:
        tin = os.environ.get("TEST_BUSINESS_TIN")
    if not service_id:
        service_id = os.environ.get("FIRS_SERVICE_ID")
    
    if not name:
        return jsonify({"ok": False, "error": "name is required"}), 400

    try:
        # import Business locally
        from models import Business, db
        b, raw_secret = Business.create_with_credentials(name=name, tin=tin, service_id=service_id)
        # return credentials (secret shown once)
        return jsonify({"ok": True, "business_id": b.id, "api_key": b.api_key, "api_secret": raw_secret}), 201
    except Exception:
        logger.exception("Business registration failed")
        return jsonify({"ok": False, "error": "failed to create business"}), 500


@firs_bp.route("/invoice/receive", methods=["POST"])
@require_api_auth
def api_receive_invoice():
    """
    Receive invoice from external ERP (middleware endpoint).
    - Auth: X-API-KEY / X-API-SECRET
    - Accepts JSON invoice payload (snake_case) per your schema.
    - Creates InvoiceSubmission + local Invoices record and optionally auto-submits to FIRS.
    """
    try:
        from models import InvoiceSubmission, Invoices, db, Business
    except Exception:
        logger.exception("models import failed in api_receive_invoice")
        return jsonify({"ok": False, "error": "internal server error"}), 500

    data = request.get_json(force=True, silent=True)
    business_id= data.get("business_id")
    if not business_id:
        return jsonify({"ok": False, "error": "missing business_id. Provide 'business_id' in JSON or ensure API key maps to a Business with a service_id."}), 400
    if not data:
        return jsonify({"ok": False, "error": "Expected JSON body"}), 400
    biz = Business.query.filter(Business.buisness_id == business_id).first()
    ok, err = validate_basic_invoice_payload(data)
    if not ok:
        return jsonify({"ok": False, "error": err}), 400

    # Build IRN candidate (best-effort) - use environment variable fallback
    # prefer explicit payload value first, then g.business, then env — but do NOT use a hard default
    service_id = getattr(biz, "service_id", None)
    if not service_id:
        return jsonify({"ok": False, "error": "missing service_id. Provide 'service_id' in JSON or ensure API key maps to a Business with a service_id."}), 400
    try:
        irn_candidate = build_irn_candidate(data.get("invoice_number") or data.get("irn") or f"INV{int(datetime.utcnow().timestamp())}", service_id, data.get("issue_date") or datetime.utcnow().date().isoformat())
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    # create submission record - use environment variable fallback for business_id
    submission = InvoiceSubmission(business_id=business_id, payload=data, status="validated", irn_candidate=irn_candidate)
    db.session.add(submission)
    db.session.flush()

    # create local invoice (map fields; keep minimal required for FIRS)
    try:
        issue_date = None
        if data.get("issue_date"):
            issue_date = datetime.fromisoformat(data["issue_date"]).date()
        due_date = None
        if data.get("due_date"):
            due_date = datetime.fromisoformat(data["due_date"]).date()
        else:
            due_date = issue_date or datetime.utcnow().date()
        email = data.get("email")
        password = data.get("password")

        issue_time = None
        if data.get("issue_time"):
            t = data["issue_time"]
            try:
                issue_time = datetime.fromisoformat(f"1970-01-01T{t}").time()
            except Exception:
                try:
                    issue_time = datetime.strptime(t, "%H:%M:%S").time()
                except Exception:
                    try:
                        issue_time = datetime.strptime(t, "%H:%M").time()
                    except Exception:
                        issue_time = None

        supplier_obj = dict(data.get("accounting_supplier_party") or {})
        if service_id:
            supplier_obj["service_id"] = service_id
        
        # Use environment variable defaults for supplier if missing
        if not supplier_obj.get("tin"):
            supplier_obj["tin"] = os.environ.get("TEST_SUPPLIER_TIN", "12345678-0001")
        if not supplier_obj.get("party_name"):
            supplier_obj["party_name"] = os.environ.get("TEST_SUPPLIER_NAME", "Test Supplier Ltd")
            
        customer_obj = dict(data.get("accounting_customer_party") or {})
        # Use environment variable defaults for customer if missing
        if not customer_obj.get("tin"):
            customer_obj["tin"] = os.environ.get("TEST_CUSTOMER_TIN", "87654321-0001")
        if not customer_obj.get("party_name"):
            customer_obj["party_name"] = os.environ.get("TEST_CUSTOMER_NAME", "Test Customer Corp")

        legal_monetary_total = data.get("legal_monetary_total") or {}
        invoice_line = data.get("invoice_line") or []
        tax_total = data.get("tax_total") or []

        # normalize list fields
        def _norm_list_field(val):
            if val is None:
                return []
            if isinstance(val, list):
                return val
            if isinstance(val, dict):
                return [val]
            return []

        billing_refs = _norm_list_field(data.get("billing_reference"))
        dispatch_refs = _norm_list_field(data.get("dispatch_document_reference"))
        receipt_refs = _norm_list_field(data.get("receipt_document_reference"))
        originator_refs = _norm_list_field(data.get("originator_document_reference"))
        contract_refs = _norm_list_field(data.get("contract_document_reference"))
        additional_refs = _norm_list_field(data.get("_document_reference") or data.get("additional_document_reference"))
        normalized_lines = []
        for ln in invoice_line:
            if not isinstance(ln, dict):
                continue
            ln = dict(ln)  # shallow copy to avoid mutating original unexpectedly

            # ensure top-level description or line_id (model requires description or line_id)
            if not ln.get("description") and not ln.get("line_id"):
                # prefer item.name, then item.description, then sellers_item_identification
                item = ln.get("item") or {}
                ln["description"] = (
                    ln.get("description")
                    or item.get("name")
                    or item.get("description")
                    or item.get("sellers_item_identification")
                    or ""
                )

            # normalize quantity
            if "quantity" not in ln:
                if "invoiced_quantity" in ln:
                    try:
                        ln["quantity"] = float(ln.get("invoiced_quantity") or 0)
                    except Exception:
                        ln["quantity"] = 0
                else:
                    # fallback to 0 or 1 depending on your business rule
                    ln["quantity"] = ln.get("quantity") or ln.get("qty") or 0

            # normalize price: if price is dict, extract price_amount; if already numeric, coerce
            raw_price = ln.get("price")
            if isinstance(raw_price, dict):
                # support both camelCase and snake_case keys
                price_amount = raw_price.get("price_amount") or raw_price.get("priceAmount") or raw_price.get("amount") or 0
                # the model stores price as a bare number, so keep the rest of the
                # price object on the line instead of discarding it
                unit = raw_price.get("price_unit") or raw_price.get("priceUnit")
                if unit and not (ln.get("price_unit") or ln.get("priceUnit")):
                    ln["price_unit"] = unit
                base_qty = raw_price.get("base_quantity") or raw_price.get("baseQuantity")
                if base_qty is not None and ln.get("base_quantity") is None:
                    try:
                        ln["base_quantity"] = float(base_qty)
                    except Exception:
                        pass
            else:
                price_amount = raw_price

            try:
                ln["price"] = float(price_amount) if price_amount not in (None, "") else 0.0
            except Exception:
                ln["price"] = 0.0

            # normalize line_extension_amount numeric
            try:
                ln["line_extension_amount"] = float(ln.get("line_extension_amount") or ln.get("lineExtensionAmount") or 0)
            except Exception:
                ln["line_extension_amount"] = 0.0

            # ensure invoice_line has item normalized (optional)
            if ln.get("item") and isinstance(ln["item"], dict):
                itm = ln["item"]
                # rewriting item drops every key not listed below, so lift any
                # unit of measure onto the line before it is lost
                if not (ln.get("price_unit") or ln.get("priceUnit")):
                    itm_uom = (itm.get("uom") or itm.get("unit")
                               or itm.get("product_uom") or itm.get("productUom")
                               or itm.get("unit_of_measure") or itm.get("unitOfMeasure"))
                    if isinstance(itm_uom, dict):
                        itm_uom = itm_uom.get("name") or itm_uom.get("code")
                    if itm_uom:
                        ln["price_unit"] = itm_uom
                ln["item"] = {
                    "name": itm.get("name") or itm.get("item_name") or "",
                    "description": itm.get("description") or itm.get("item_description") or "",
                    "sellers_item_identification": itm.get("sellers_item_identification") or itm.get("sellersItemIdentification") or ""
                }

            normalized_lines.append(ln)

        # replace invoice_line for DB insertion
        invoice_line = normalized_lines

        # normalize payment_means codes to integers (validator expects int)
        pm = []
        for p in (data.get("payment_means") or []):
            if not isinstance(p, dict):
                continue
            try:
                pcode = int(p.get("payment_means_code")) if p.get("payment_means_code") is not None else None
            except Exception:
                pcode = None
            # normalize payment_due_date (keep as string ISO or date, model validator accepts str or date)
            pd = p.get("payment_due_date")
            pm.append({"payment_means_code": pcode, "payment_due_date": pd} if pcode is not None else {"payment_means_code": p.get("payment_means_code"), "payment_due_date": pd})
        # assign normalized
        # this will be used when building the Invoices() below
        # but your code builds inv with payment_means = data.get("payment_means") or []
        # so set data["payment_means"] to the normalized list so existing code picks it up:
        data["payment_means"] = pm

        # normalize allowance_charge booleans/numbers
        ac = []
        for a in (data.get("allowance_charge") or []):
            if not isinstance(a, dict):
                continue
            try:
                charge_indicator = bool(a.get("charge_indicator")) if "charge_indicator" in a else False
            except Exception:
                charge_indicator = False
            try:
                amount = float(a.get("amount", 0))
            except Exception:
                amount = 0.0
            ac.append({"charge_indicator": charge_indicator, "amount": amount})
        data["allowance_charge"] = ac

        # normalize monetary totals into numbers
        try:
            if isinstance(legal_monetary_total, dict):
                legal_monetary_total = {
                    "line_extension_amount": float(legal_monetary_total.get("line_extension_amount", 0)),
                    "tax_exclusive_amount": float(legal_monetary_total.get("tax_exclusive_amount", 0)),
                    "tax_inclusive_amount": float(legal_monetary_total.get("tax_inclusive_amount", 0)),
                    "payable_amount": float(legal_monetary_total.get("payable_amount", 0)),
                }
        except Exception:
            legal_monetary_total = {"line_extension_amount": 0.0, "tax_exclusive_amount": 0.0, "tax_inclusive_amount": 0.0, "payable_amount": 0.0}
        invoice_number = data.get("invoice_number")
        business = Business.query.filter(Business.buisness_id == business_id).first()
        service_id = business.service_id
        irn = generate_irn(invoice_number, service_id, issue_date)
        existing_invoice = Invoices.query.filter(Invoices.irn == irn).first()
        if existing_invoice:
            # Check whether the pipeline actually finished for this invoice
            existing_submission = (
                InvoiceSubmission.query
                .filter_by(invoice_id=existing_invoice.id)
                .order_by(InvoiceSubmission.id.desc())
                .first()
                )
            existing_status = getattr(existing_submission, "status", None) or existing_invoice.irn_status
            if existing_invoice.irn_status == "transmitted" and existing_status == "transmitted":
                # Genuinely complete — reject as true duplicate
                return jsonify({
                    "ok":        False,
                    "error":     "invoice already fully transmitted",
                    "invoice_id": existing_invoice.id,
                    "irn":        existing_invoice.irn,
                    "status":     existing_status,
                    }), 409
            # Pipeline stalled or failed — resume it automatically
            logger.info(
                "Invoice IRN %s already exists (id=%s) with incomplete status=%r — resuming pipeline",
                irn, existing_invoice.id, existing_status,
                )
            log_submission(
                existing_submission,
                f"Duplicate receive request — resuming pipeline from status={existing_status!r}",
                )
            try:
                if existing_invoice.irn_status in ("issued", "signed") or getattr(existing_submission, "status", None) in ("signed", "issued", "transmit_failed"):
                    incoming_customer = data.get("accounting_customer_party")
                    if incoming_customer and incoming_customer.get("tin"):
                        existing_invoice.accounting_customer_party = dict(incoming_customer)
                        db.session.commit()
                        logger.info("Updated customer party on resume for invoice %s", existing_invoice.id)
                    pipeline_result = _process_invoice_transmit_only(existing_invoice.id)
                else:
                    incoming_customer = data.get("accounting_customer_party")
                    if incoming_customer and incoming_customer.get("tin"):
                        existing_invoice.accounting_customer_party = dict(incoming_customer)
                        db.session.commit()
                        logger.info("Updated customer party on resume for invoice %s", existing_invoice.id)
                    pipeline_result = _process_invoice_irn_request(existing_invoice.id)
            except Exception as exc:
                logger.exception("Pipeline resume failed for invoice %s: %s", existing_invoice.id, exc)
                return jsonify({"ok": False, "error": "pipeline resume failed", "details": str(exc)}), 500
            # Attach log history and return immediately — skip the rest of the route
            log_history = []
            try:
                from models import SubmissionLog
                logs = (
                    SubmissionLog.query
                    .filter_by(submission_id=getattr(existing_submission, "id", None))
                    .order_by(SubmissionLog.created_at.asc())
                    .all()
                    )
                log_history = [
                    {
                        "timestamp": log.created_at.isoformat() if log.created_at else None,
                        "message":   getattr(log, "message", None),
                        "level":     getattr(log, "level", "INFO"),
                        "status":    getattr(log, "status", None),
                        }
                        for log in logs
                ]
            except Exception:
                logger.exception("Could not fetch log history on resume (non-fatal)")
            pipeline_result["log_history"]     = log_history
            pipeline_result["resumed"]         = True
            pipeline_result["previous_status"] = existing_status
            pipeline_result["submission_id"]   = getattr(existing_submission, "id", None)
            pipeline_result["irn_candidate"]   = irn
            
            status_code = 200 if pipeline_result.get("ok") else 500
            return jsonify(pipeline_result), status_code

        inv = Invoices(
            business_id = business_id or data.get("business_id") or getattr(biz, "id", None),
            issue_date = issue_date or datetime.utcnow().date(),
            due_date = due_date,
            issue_time = issue_time or datetime.utcnow().time(),
            invoice_type_code = int(data["invoice_type_code"]) if data.get("invoice_type_code") else None,
            payment_status = data.get("payment_status"),
            note = data.get("note"),
            tax_point_date = (datetime.fromisoformat(data["tax_point_date"]).date() if data.get("tax_point_date") else (issue_date or datetime.utcnow().date())),
            document_currency_code = data.get("document_currency_code", "NGN"),
            tax_currency_code = data.get("tax_currency_code"),
            accounting_cost = int(data["accounting_cost"]) if data.get("accounting_cost") else None,
            buyer_reference = data.get("buyer_reference"),
            delivery_start_date = (datetime.fromisoformat(data["invoice_delivery_period"]["start_date"]).date()
                                   if data.get("invoice_delivery_period") and data["invoice_delivery_period"].get("start_date") else None),
            delivery_end_date = (datetime.fromisoformat(data["invoice_delivery_period"]["end_date"]).date()
                                 if data.get("invoice_delivery_period") and data["invoice_delivery_period"].get("end_date") else None),
            order_reference = data.get("order_reference"),
            billing_reference = billing_refs,
            dispatch_document_reference = dispatch_refs,
            receipt_document_reference = receipt_refs,
            originator_document_reference = originator_refs,
            contract_document_reference = contract_refs,
            additional_document_reference = additional_refs,
            accounting_supplier_party = supplier_obj,
            accounting_customer_party = customer_obj,
            actual_delivery_date = (datetime.fromisoformat(data["actual_delivery_date"]).date() if data.get("actual_delivery_date") else None),
            payment_means = data.get("payment_means") or [],
            payment_terms_note = data.get("payment_terms_note"),
            allowance_charge = data.get("allowance_charge") or [],
            tax_total = tax_total if tax_total else [],
            legal_monetary_total = legal_monetary_total or {},
            invoice_line = invoice_line if invoice_line else [],
            irn = data.get("irn"),
            irn_status = data.get("irn_status") or ("received" if data.get("irn") else "draft"),
            csid = email,
            signature_value = password
        )

        if data.get("invoice_number"):
            inv.order_reference = data.get("invoice_number")

        db.session.add(inv)
        db.session.flush()
        submission.invoice_id = inv.id
        db.session.commit()
        log_submission(submission, f"ERP Invoice recieved initiating firs validation and signing ")
        with current_app.test_request_context():
            pipeline_result = _process_invoice_irn_request(inv.id)
        # ✅ Log into model instead of just jsonify
    except Exception as exc:
        db.session.rollback()
        logger.exception("Failed creating invoice from external payload: %s", exc)
        return jsonify({"ok": False, "error": "failed to create invoice", "details": str(exc)}), 500

    # Return created record + candidate
    result = {"ok": True, "submission_id": submission.id, "invoice_id": inv.id, "irn_candidate": irn_candidate}
    # Optionally auto-submit to FIRS if configured (try/except and update submission/invoice)
    log_history = []
    try:
        from models import SubmissionLog  # adjust import to your actual model name
        logs = (
            SubmissionLog.query
            .filter_by(submission_id=submission.id)
            .order_by(SubmissionLog.created_at.asc())
            .all()
        )
        log_history = [
            {
                "timestamp": getattr(log, "created_at", None) and log.created_at.isoformat(),
                "message":   getattr(log, "message", None),
                "level":     getattr(log, "level", "INFO"),
                "status":    getattr(log, "status", None),
            }
            for log in logs
        ]
    except Exception:
        logger.exception("Could not fetch submission log history (non-fatal)")

    # Merge pipeline result with log history and return
    pipeline_result["log_history"] = log_history
    pipeline_result["submission_id"] = submission.id   # ensure it's always present
    pipeline_result["irn_candidate"] = irn_candidate   # carry forward the candidate
    if current_app.config.get("FIRS_AUTO_SUBMIT", False) or current_app.config.get("FIRS_TEST_MODE"):
        try:
            firs_payload = build_firs_validate_payload_from_invoice(inv, biz=biz)
            # Validate with FIRS
            validate_resp = None
            try:
                validate_resp = firs_validate_invoice(firs_payload)
                logger.info("FIRS validate response: %s", validate_resp)
            except Exception as exc:
                logger.exception("Auto-validate failed: %s", exc)
                # mark submission accordingly but do not fail entire request
                submission.status = "validate_failed"
                submission.error = str(exc)
                db.session.commit()

            # if validate passed and signing requested -> sign
            if validate_resp is not None:
                # try to sign using app private key if configured
                private_key_pem = current_app.config.get("PRIVATE_KEY_PEM")
                if not private_key_pem and current_app.config.get("PRIVATE_KEY_PATH"):
                    try:
                        private_key_pem = open(current_app.config["PRIVATE_KEY_PATH"], "rb").read()
                    except Exception:
                        private_key_pem = None

                if private_key_pem:
                    xml_text = make_invoice_xml(inv)
                    signature_b64 = sign_payload_rsa_b64(private_key_pem, xml_text)
                    sign_payload = {"payload": xml_text, "signature": signature_b64}
                else:
                    sign_payload = firs_payload  # some sandboxes accept JSON signing server-side

                sign_resp = firs_sign_invoice(sign_payload)             
                submission.status = "transmitted" if sign_resp.get("irn") or sign_resp.get("csid") else "issued"
                submission.firs_irn = sign_resp.get("irn") or None
                submission.firs_csid = sign_resp.get("csid") or None
                inv.irn = sign_resp.get("irn") or inv.irn
                inv.csid = sign_resp.get("csid") or inv.csid
                inv.irn_status = submission.status
                log_submission(submission, f"TRANSMITTED → IRN={submission.firs_irn}")
                db.session.commit()
        except Exception:
            logger.exception("Auto-submit pipeline failed (non-fatal)")

    status_code = 201 if pipeline_result.get("ok") else 500
    return jsonify(pipeline_result), status_code


# This route is the same action you had in app.py for requesting IRN from UI
# It is provided here as a blueprint route that depends on your existing login manager
@firs_bp.route("/invoice/<int:invoice_id>/request-irn", methods=["POST"])
@login_required
def invoice_request_irn_web(invoice_id):
    """
    Orchestrates: generate IRN candidate, build FIRS payload, validate, sign and transmit.
    This version uses helpers and logs/returns helpful errors.
    """
    try:
        from models import Invoices, Business, db
    except Exception:
        logger.exception("models import failed in invoice_request_irn_web")
        flash("Internal server error", "danger")
        return redirect(url_for("invoice_detail", invoice_id=invoice_id))

    inv = Invoices.query.get_or_404(invoice_id)

    # ensure issue_date
    if not inv.issue_date:
        inv.issue_date = datetime.utcnow().date()
        try:
            db.session.add(inv)
            db.session.commit()
        except Exception:
            db.session.rollback()

    # basic checks - use environment variable fallback for supplier TIN
    supplier_tin = (inv.accounting_supplier_party or {}).get("tin")
    if not supplier_tin:
        supplier_tin = os.environ.get("TEST_SUPPLIER_TIN", "12345678-0001")
        # Update the invoice with test TIN
        if not inv.accounting_supplier_party:
            inv.accounting_supplier_party = {}
        inv.accounting_supplier_party["tin"] = supplier_tin
        inv.accounting_supplier_party["party_name"] = inv.accounting_supplier_party.get("party_name") or os.environ.get("TEST_SUPPLIER_NAME", "Test Supplier Ltd")
        db.session.add(inv)
        db.session.commit()

    invoice_number = (inv.order_reference or inv.buyer_reference or f"INV{inv.id}").strip()
    if not invoice_number:
        flash("Invoice number cannot be determined.", "danger")
        return redirect(url_for("invoice_detail", invoice_id=invoice_id))

    service_id = os.environ.get("FIRS_SERVICE_ID", "94ND90NR")
    if not service_id:
        flash("FIRS service ID not configured (FIRS_SERVICE_ID).", "danger")
        return redirect(url_for("invoice_detail", invoice_id=invoice_id))

    # generate candidate IRN (enforces format)
    try:
        irn_candidate = generate_irn(invoice_number, service_id, inv.issue_date)
        inv.irn = irn_candidate
        inv.irn_status = "generating"
        db.session.add(inv)
        db.session.commit()
    except Exception as exc:
        logger.exception("Failed to generate IRN: %s", exc)
        flash("Failed to generate IRN", "danger")
        return redirect(url_for("invoice_detail", invoice_id=invoice_id))

    # build payload
    try:
        biz = Business.query.filter_by(id=inv.business_id).first() if inv.business_id else None
        # If no business found, use environment variables for testing
        if not biz:
            logger.info("No business found for invoice %s, using test environment variables", invoice_id)
        firs_payload = build_firs_validate_payload_from_invoice(inv, biz=biz)
        if not firs_payload:
            logger.error("build_firs_validate_payload_from_invoice returned empty payload for invoice %s", invoice_id)
            logger.debug("invoice minimal debug: id=%s irn=%s issue_date=%s business_id=%s", inv.id, getattr(inv,'irn',None), getattr(inv,'issue_date',None), getattr(inv,'business_id',None))
            flash("Failed to prepare payload for FIRS: empty payload", "danger")
            return redirect(url_for("invoice_detail", invoice_id=invoice_id))
        logger.info("FIRS validate payload:\n%s", json.dumps(firs_payload, indent=2))
    except Exception as exc:
        logger.exception("Failed building FIRS payload: %s", exc)
        flash("Failed to prepare payload for FIRS.", "danger")
        return redirect(url_for("invoice_detail", invoice_id=invoice_id))
    if 'submission' not in locals() or submission is None:
        try:
            submission = InvoiceSubmission.query.filter_by(invoice_id=getattr(inv, "id", None)).order_by(InvoiceSubmission.id.desc()).first()
            logger.debug("Lookup submission for invoice %s -> %r", getattr(inv, "id", None), submission)
        except Exception:
            submission = None
            logger.exception("Failed to lookup InvoiceSubmission for invoice %s", getattr(inv, "id", None))
    def _upd(sub: InvoiceSubmission, status: str, error: str | None = None, firs_irn: str | None = None, firs_csid: str | None = None):
        """Helper: set fields, commit, safe rollback on fail."""
        if not sub:
            logger.debug("No submission to update for status=%s", status)
            return
        try:
            sub.status = status
            if error is not None:
                sub.error = str(error)
            else:
                sub.error = None
            if firs_irn:
                sub.firs_irn = firs_irn
            if firs_csid:
                sub.firs_csid = firs_csid
            db.session.add(sub)
            db.session.commit()
            logger.info("Submission %s updated -> status=%s", sub.id, status)
        except Exception:
            db.session.rollback()
            logger.exception("Failed to persist submission status=%s for submission_id=%s", status, getattr(sub, "id", None))
    # Start pipeline: mark validating
    _upd(submission, "validating", error=None)

    # validate
    try:
        validate_resp = None
        # 1) Validate with FIRS
        try:
            validate_resp = firs_validate_invoice(firs_payload)
            logger.info("FIRS validate response: %s", validate_resp)
            log_submission(submission, f"FIRS validate response: {validate_resp}")
            _upd(submission, "validated", error=None)
        except Exception as e:
            # record failure on submission (if present)
            logger.exception("FIRS validate failed")
            log_submission(submission, f"FIRS validate failed: {e}", level="ERROR")
            _upd(submission, "validate_failed", error=str(e))
            if 'submission' in locals():
                try:
                    submission.status = "validate_failed"
                    submission.error = str(e)
                    db.session.add(submission)
                    db.session.commit()
                except Exception:
                    db.session.rollback()
                    logger.exception("Failed to persist submission validate_failed state")
            # In test mode we continue, otherwise return error to caller
            if current_app.config.get("FIRS_TEST_MODE"):
                logger.warning("Validate failed in TEST mode, continuing: %s", e)
                validate_resp = None
                _upd(submission, "validate_failed in test mode", error=str(e))
            else:
                # rollback any partial DB work
                try:
                    db.session.rollback()
                except Exception:
                    logger.exception("DB rollback failed after validate error")
                msg = f"FIRS validation failed: {e}"
                if request.is_json:
                    return jsonify(ok=False, error=msg), 502
                flash(msg, "danger")
                return redirect(url_for("invoice_detail", invoice_id=invoice_id))

        # 2) Prepare signing payload
        # --- Keep behaviour simple: send the same payload we validated (firs_payload).
        # Do NOT send a locally-signed XML payload to the sign endpoint (per request).
        sign_payload = None

        # Ensure the FIRS payload includes the IRN in the shape the sign endpoint expects.
        try:
            # If firs_payload is the flat snake_case payload (build_firs_validate_payload_from_invoice returns that),
            # it should include top-level "irn". Add it explicitly if missing.
            if isinstance(firs_payload, dict) and ("irn" in firs_payload or "business_id" in firs_payload):
                # flat payload - ensure irn present
                if not firs_payload.get("irn"):
                    firs_payload["irn"] = inv.irn or inv.irn_status or ""
                sign_payload = firs_payload
            else:
                # if firs_payload is wrapper/camelCase form, ensure invoiceRequest.invoice.irn exists
                try:
                    if isinstance(firs_payload, dict):
                        invoice_req = firs_payload.setdefault("invoiceRequest", {})
                        invoice = invoice_req.setdefault("invoice", {})
                        if not invoice.get("irn"):
                            invoice["irn"] = inv.irn or ""
                        sign_payload = firs_payload
                    else:
                        # unexpected type: fallback to sending a minimal wrapper that includes irn
                        sign_payload = {"invoiceRequest": {"invoice": {"irn": inv.irn or ""}}}
                except Exception:
                    sign_payload = {"invoiceRequest": {"invoice": {"irn": inv.irn or ""}}}
        except Exception:
            # defensive fallback: always set a minimal wrapper with irn to avoid missing-irn errors
            sign_payload = {"invoiceRequest": {"invoice": {"irn": inv.irn or ""}}}

        # Optional: attach certificate if sign endpoint expects it in the same payload.
        # (Don't overwrite the payload; only attach cert if available.)
        cert_b64 = current_app.config.get("FIRS_CERTIFICATE_B64")
        if not cert_b64:
            try:
                crypto_txt = current_app.config.get("CRYPTO_KEYS_TXT")
                if not crypto_txt:
                    keys_path = current_app.config.get("CRYPTO_KEYS_PATH") or os.path.join(current_app.root_path, "crypto_keys.txt")
                    if os.path.exists(keys_path):
                        with open(keys_path, "r", encoding="utf-8") as fh:
                            crypto_txt = fh.read()
                if crypto_txt:
                    parsed = json.loads(crypto_txt)
                    cert_b64 = parsed.get("certificate") or parsed.get("cert")
            except Exception:
                cert_b64 = None

        if cert_b64:
            # attach at top-level if sign endpoint supports this
            try:
                if isinstance(sign_payload, dict):
                    sign_payload = dict(sign_payload)  # shallow copy to avoid mutating original unexpectedly
                    sign_payload["certificate"] = cert_b64
            except Exception:
                # ignore attach failure and proceed with the payload without certificate
                pass

        # 3) Call sign endpoint (send the same validated payload)
        try:
            sign_resp = firs_sign_invoice(sign_payload)
            logger.info("FIRS sign response: %s", sign_resp)
            log_submission(submission, f"FIRS sign response: {sign_resp}")
        except Exception as e:
            logger.exception("FIRS sign failed")
            log_submission(submission, f"FIRS sign failed: {e}", level="ERROR")
            # update submission/invoice state
            if 'submission' in locals():
                try:
                    submission.status = "sign_failed"
                    submission.error = str(e)
                    db.session.add(submission)
                    db.session.commit()
                except Exception:
                    db.session.rollback()
                    logger.exception("Failed to persist submission sign_failed state")
            try:
                db.session.rollback()
            except Exception:
                logger.exception("DB rollback failed after sign error")
            msg = f"FIRS signing failed: {e}"
            if request.is_json:
                return jsonify(ok=False, error=msg), 502
            flash("Signing/submission failed. See logs.", "danger")
            return redirect(url_for("invoice_detail", invoice_id=invoice_id))

        # 4) Success path: update invoice and submission
        returned_irn = (sign_resp.get("irn") if isinstance(sign_resp, dict) else None) or inv.irn
        inv.irn = returned_irn
        inv.csid = sign_resp.get("csid") if isinstance(sign_resp, dict) else inv.csid
        inv.irn_status = sign_resp.get("status") or "issued"
        inv.submitted_at = datetime.utcnow()
        try:
            db.session.add(inv)
            if 'submission' in locals():
                submission.status = "transmitted" if (sign_resp.get("irn") or sign_resp.get("csid")) else "issued"
                submission.error = None
                db.session.add(submission)
            db.session.commit()
        except Exception:
            db.session.rollback()
            logger.exception("Failed to persist invoice/signing result")
            msg = "Failed to persist signing result"
            if request.is_json:
                return jsonify(ok=False, error=msg), 500
            flash(msg, "danger")
            return redirect(url_for("invoice_detail", invoice_id=invoice_id))

    except Exception as exc:
        # final catch-all: ensure DB rollback and user-visible error
        logger.exception("Signing/submission failed (unexpected): %s", exc)
        try:
            db.session.rollback()
        except Exception:
            logger.exception("DB rollback failed in outer exception")
        if request.is_json:
            return jsonify(ok=False, error=f"Signing/submission failed: {exc}"), 500
        flash("Signing/submission failed. See logs.", "danger")
        return redirect(url_for("invoice_detail", invoice_id=invoice_id))

    # transmit
    try:
        tx_resp = transmit_to_firs(inv.irn)
        logger.info("Transmit response: %s", tx_resp)
        inv.transmitted_at = datetime.utcnow()
        if not inv.irn_status:
            inv.irn_status = "transmitted"
        db.session.add(inv)
        db.session.commit()
    except Exception:
        logger.exception("Transmit failed (non-fatal)")

    flash("IRN process completed.", "success")
    if request.is_json:
        return jsonify(ok=True, invoice_id=inv.id, irn=inv.irn, csid=inv.csid), 200
    return redirect(url_for("invoice_detail", invoice_id=invoice_id))

@firs_bp.route("/api/submission-logs/stream")
def api_submission_logs_stream():
    """
    SSE endpoint that streams SubmissionLog rows as JSON events:
      event: submission_log
      data: {...}
    Clients should pass optional ?last_id=N to receive rows with id>N.
    NOTE: This simple in-process implementation works best with a single worker/process.
    For multi-process (gunicorn with workers) use Redis pub/sub or other broker.
    """
    try:
        # quick auth-like guard: if not logged in return 401 JSON rather than streaming login HTML
        from flask_login import current_user
        if not (current_user and current_user.is_authenticated):
            return jsonify({"ok": False, "error": "not_authenticated"}), 401
    except Exception:
        # if flask-login not present, continue (useful for debugging)
        pass

    try:
        last_id = int(request.args.get("last_id", 0) or 0)
    except Exception:
        last_id = 0

    def gen(last_id):
        # long-running generator that polls DB and yields SSE formatted events
        # runs until client disconnects
        while True:
            try:
                rows = (SubmissionLog.query
                        .filter(SubmissionLog.id > last_id)
                        .order_by(SubmissionLog.id.asc())
                        .limit(50)
                        .all())
                for r in rows:
                    payload = {
                        "id": r.id,
                        "submission_id": r.submission_id,
                        "level": r.level,
                        "message": r.message,
                        "created_at": r.created_at.isoformat() if r.created_at else None
                    }
                    # SSE event named 'submission_log' with JSON payload
                    yield f"event: submission_log\n"
                    yield f"data: {json.dumps(payload)}\n\n"
                    last_id = r.id
                # heartbeat to keep connection alive (optional)
                yield f": heartbeat\n\n"
            except GeneratorExit:
                # client disconnected
                break
            except Exception as exc:
                current_app.logger.exception("SSE stream error")
                # if DB error, wait longer before retrying
                time.sleep(2)
            # poll interval
            time.sleep(1.0)

    return Response(stream_with_context(gen(last_id)), mimetype="text/event-stream")

@firs_bp.route("/api/submission-logs/<int:log_id>")
def api_submission_log_get(log_id):
    """
    Return a single submission log as JSON.
    Endpoint name: 'firs.api_submission_log_get'
    """
    try:
        from flask_login import current_user
        if not (current_user and current_user.is_authenticated):
            return jsonify({"ok": False, "error": "not_authenticated"}), 401
    except Exception:
        pass

    from models import SubmissionLog
    log = SubmissionLog.query.get_or_404(log_id)
    return jsonify({
        "ok": True,
        "id": log.id,
        "submission_id": log.submission_id,
        "level": log.level,
        "message": log.message,
        "created_at": log.created_at.isoformat() if log.created_at else None
    })

@firs_bp.route("/api/submission-logs/recent-simple")
def api_submission_logs_recent_simple():

    try:
        last_id = int(request.args.get("last_id", 0) or 0)
    except Exception:
        last_id = 0
    try:
        limit = min(int(request.args.get("limit", 20) or 20), 200)
    except Exception:
        limit = 20

    q = SubmissionLog.query
    if last_id:
        q = q.filter(SubmissionLog.id > last_id)
    rows = q.order_by(SubmissionLog.id.asc()).limit(limit).all()

    def s(r):
        return {
            "id": r.id,
            "submission_id": r.submission_id,
            "level": r.level,
            "message": r.message,
            "created_at": r.created_at.isoformat() if r.created_at else None
        }

    return jsonify({"ok": True, "logs": [s(r) for r in rows]})

def _firs_headers():
    headers = {"Content-Type": "application/json"}
    if current_app.config.get("FIRS_API_KEY"):
        headers["x-api-key"] = current_app.config["FIRS_API_KEY"]
    if current_app.config.get("FIRS_API_SECRET"):
        headers["x-api-secret"] = current_app.config["FIRS_API_SECRET"]
    return headers

@firs_bp.route("/firs/self-health-check", methods=["GET"])
def firs_self_health_check():
    """Confirm APP readiness before transmitting invoices."""
    base = current_app.config.get("FIRS_API_BASE", "https://einvoice.firs.gov.ng")
    url = f"{base}/api/v1/invoice/transmit/self-health-check"

    try:
        resp = requests.get(url, headers=_firs_headers(), timeout=30)
        resp.raise_for_status()
        return jsonify(resp.json())
    except requests.RequestException as ex:
        return jsonify({"status": "failed", "error": str(ex)}), 500


@firs_bp.route("/firs/lookup/irn/<string:irn>", methods=["GET"])
def firs_lookup_irn(irn):
    """Retrieve invoice details by IRN."""
    base = current_app.config.get("FIRS_API_BASE", "https://einvoice.firs.gov.ng")
    url = f"{base}/api/v1/invoice/transmit/lookup/{irn}"

    try:
        resp = requests.get(url, headers=_firs_headers(), timeout=30)
        resp.raise_for_status()
        return jsonify(resp.json())
    except requests.RequestException as ex:
        return jsonify({"status": "failed", "error": str(ex)}), 500


@firs_bp.route("/firs/lookup/tin/<string:tin>", methods=["GET"])
def firs_lookup_tin(tin):
    """Retrieve invoice details by TIN."""
    base = current_app.config.get("FIRS_API_BASE", "https://einvoice.firs.gov.ng")
    url = f"{base}/api/v1/invoice/transmit/lookup/tin/{tin}"

    try:
        resp = requests.get(url, headers=_firs_headers(), timeout=30)
        resp.raise_for_status()
        return jsonify(resp.json())
    except requests.RequestException as ex:
        return jsonify({"status": "failed", "error": str(ex)}), 500
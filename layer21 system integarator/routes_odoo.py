"""
Blueprint: Odoo ERP Enterprise EDI receiver
Endpoint: POST /api/v1/odoo/invoice/push

Accepts UBL 2.1 XML invoices pushed by Odoo's account_edi module and runs
them through the existing FIRS pipeline without duplicating any pipeline logic.

Authentication: X-API-KEY + X-API-SECRET headers (same as /invoice/receive).
"""

import re
import logging
import xml.etree.ElementTree as ET

from flask import Blueprint, request, jsonify, g

from erp_pipeline import run_erp_invoice_pipeline
from helpers import require_api_auth

logger = logging.getLogger(__name__)

odoo_bp = Blueprint("odoo", __name__, url_prefix="/api/v1/odoo")

NS = {
    "ubl": "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2",
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
}


def _txt(el, xpath):
    """Return stripped text of the first matching element, or None."""
    found = el.find(xpath, NS)
    if found is not None and found.text:
        return found.text.strip()
    return None


def _flt(el, xpath):
    """Return float value of the first matching element, or 0.0."""
    v = _txt(el, xpath)
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _extract_party(party_el):
    """Build a party dict (supplier or customer) from a cac:Party element."""
    if party_el is None:
        return {}
    tin = (
        _txt(party_el, "cac:PartyTaxScheme/cbc:CompanyID")
        or _txt(party_el, "cac:PartyLegalEntity/cbc:CompanyID")
        or _txt(party_el, "cbc:EndpointID")
    )
    name = (
        _txt(party_el, "cac:PartyName/cbc:Name")
        or _txt(party_el, "cac:PartyLegalEntity/cbc:RegistrationName")
    )
    email = _txt(party_el, "cac:Contact/cbc:ElectronicMail")
    phone = _txt(party_el, "cac:Contact/cbc:Telephone")

    addr_el = party_el.find("cac:PostalAddress", NS)
    postal = {}
    if addr_el is not None:
        postal = {
            "street_name": _txt(addr_el, "cbc:StreetName") or "",
            "city_name": _txt(addr_el, "cbc:CityName") or "",
            "postal_zone": _txt(addr_el, "cbc:PostalZone") or "",
            "country": _txt(addr_el, "cac:Country/cbc:IdentificationCode") or "NG",
        }

    party = {}
    if tin:
        party["tin"] = tin
    if name:
        party["party_name"] = name
    if email:
        party["email"] = email
    if phone:
        party["telephone"] = phone
    if postal:
        party["postal_address"] = postal
    return party


def _parse_ubl_to_layer21(xml_bytes, business):
    """
    Parse Odoo UBL 2.1 XML bytes into a Layer21 canonical snake_case dict.

    Args:
        xml_bytes: Raw XML body from the request
        business:  Authenticated Business model instance (from g.business)

    Returns:
        Layer21-format dict ready for validate_basic_invoice_payload()

    Raises:
        ET.ParseError: if the XML is malformed
        ValueError: if a mandatory field is missing
    """
    root = ET.fromstring(xml_bytes)

    # ── Invoice number ─────────────────────────────────────────────────────────
    raw_id = _txt(root, "cbc:ID")
    if not raw_id:
        raise ValueError("UBL XML missing cbc:ID (invoice number)")
    invoice_number = re.sub(r"[^A-Za-z0-9]", "", raw_id)
    if not invoice_number:
        raise ValueError(f"Invoice number '{raw_id}' contains no alphanumeric characters")

    # ── Dates ──────────────────────────────────────────────────────────────────
    issue_date = _txt(root, "cbc:IssueDate")
    if not issue_date:
        raise ValueError("UBL XML missing cbc:IssueDate")

    due_date = (
        _txt(root, "cbc:DueDate")
        or _txt(root, "cac:PaymentMeans/cbc:PaymentDueDate")
    )

    # ── Currency / type ────────────────────────────────────────────────────────
    currency = _txt(root, "cbc:DocumentCurrencyCode") or "NGN"
    invoice_type_raw = _txt(root, "cbc:InvoiceTypeCode")
    try:
        invoice_type_code = int(invoice_type_raw) if invoice_type_raw else 396
    except (TypeError, ValueError):
        invoice_type_code = 396

    # ── Parties ────────────────────────────────────────────────────────────────
    supplier_container = root.find("cac:AccountingSupplierParty", NS)
    supplier_party_el = supplier_container.find("cac:Party", NS) if supplier_container is not None else None
    supplier = _extract_party(supplier_party_el)
    if not supplier.get("tin"):
        raise ValueError("UBL XML missing supplier TIN (cac:AccountingSupplierParty/.../cbc:CompanyID)")

    customer_container = root.find("cac:AccountingCustomerParty", NS)
    customer_party_el = customer_container.find("cac:Party", NS) if customer_container is not None else None
    customer = _extract_party(customer_party_el)
    if not customer.get("tin"):
        raise ValueError("UBL XML missing customer TIN (cac:AccountingCustomerParty/.../cbc:CompanyID)")

    # ── Invoice lines ──────────────────────────────────────────────────────────
    invoice_lines = []
    for line_el in root.findall("cac:InvoiceLine", NS):
        item_name = _txt(line_el, "cac:Item/cbc:Name") or ""
        desc = _txt(line_el, "cac:Item/cbc:Description") or item_name
        qty_el = line_el.find("cbc:InvoicedQuantity", NS)
        unit_code = qty_el.get("unitCode", "") if qty_el is not None else ""
        qty = _flt(line_el, "cbc:InvoicedQuantity")
        price = _flt(line_el, "cac:Price/cbc:PriceAmount")
        line_ext = _flt(line_el, "cbc:LineExtensionAmount")

        hsn_code = ""
        for prop_el in line_el.findall("cac:Item/cac:AdditionalItemProperty", NS):
            if _txt(prop_el, "cbc:Name") in ("HSNCode", "hsn_code", "HSN"):
                hsn_code = _txt(prop_el, "cbc:Value") or ""
                break

        # "quantity"/"description" satisfy the Invoices model validator;
        # "invoiced_quantity"/"item"/"unit" are what the FIRS payload builder reads
        invoice_lines.append({
            "line_id": _txt(line_el, "cbc:ID") or "",
            "description": desc,
            "quantity": qty,
            "invoiced_quantity": qty,
            "price": price,
            "line_extension_amount": line_ext,
            "hsn_code": hsn_code,
            "unit": unit_code,
            "item": {
                "name": item_name or desc,
                "description": desc,
                "sellers_item_identification": _txt(line_el, "cac:Item/cac:SellersItemIdentification/cbc:ID") or "",
            },
        })

    # ── Tax total ──────────────────────────────────────────────────────────────
    tax_total = []
    tax_total_el = root.find("cac:TaxTotal", NS)
    if tax_total_el is not None:
        tax_amount = _flt(tax_total_el, "cbc:TaxAmount")
        subtotals = []
        for sub_el in tax_total_el.findall("cac:TaxSubtotal", NS):
            taxable = _flt(sub_el, "cbc:TaxableAmount")
            sub_tax = _flt(sub_el, "cbc:TaxAmount")
            cat_id = _txt(sub_el, "cac:TaxCategory/cbc:ID") or "VAT"
            percent = _flt(sub_el, "cac:TaxCategory/cbc:Percent")
            subtotals.append({
                "taxable_amount": taxable,
                "tax_amount": sub_tax,
                "tax_category": {"id": cat_id, "percent": percent},
            })
        tax_total = [{"tax_amount": tax_amount, "tax_subtotal": subtotals}]

    # ── Legal monetary total ───────────────────────────────────────────────────
    lmt_el = root.find("cac:LegalMonetaryTotal", NS)
    if lmt_el is None:
        raise ValueError("UBL XML missing cac:LegalMonetaryTotal")
    legal_monetary_total = {
        "line_extension_amount": _flt(lmt_el, "cbc:LineExtensionAmount"),
        "tax_exclusive_amount": _flt(lmt_el, "cbc:TaxExclusiveAmount"),
        "tax_inclusive_amount": _flt(lmt_el, "cbc:TaxInclusiveAmount"),
        "payable_amount": _flt(lmt_el, "cbc:PayableAmount"),
    }

    # ── Payment means ──────────────────────────────────────────────────────────
    # Invoices.payment_means rejects an empty payment_due_date
    default_due = due_date or issue_date
    payment_means = []
    for pm_el in root.findall("cac:PaymentMeans", NS):
        code_raw = _txt(pm_el, "cbc:PaymentMeansCode")
        try:
            code = int(code_raw) if code_raw else 30
        except (TypeError, ValueError):
            code = 30
        pdd = _txt(pm_el, "cbc:PaymentDueDate")
        payment_means.append({"payment_means_code": code, "payment_due_date": pdd or default_due})
    if not payment_means:
        payment_means = [{"payment_means_code": 30, "payment_due_date": default_due}]

    # ── Assemble canonical payload ─────────────────────────────────────────────
    return {
        "business_id": business.buisness_id,
        "invoice_number": invoice_number,
        "issue_date": issue_date,
        "due_date": due_date,
        "document_currency_code": currency,
        "invoice_type_code": invoice_type_code,
        "accounting_supplier_party": supplier,
        "accounting_customer_party": customer,
        "invoice_line": invoice_lines,
        "tax_total": tax_total,
        "legal_monetary_total": legal_monetary_total,
        "payment_means": payment_means,
    }


@odoo_bp.route("/invoice/push", methods=["POST"])
@require_api_auth
def odoo_invoice_push():
    """
    Receive a UBL 2.1 XML invoice from Odoo ERP Enterprise (account_edi direct push).
    Parses the XML, maps it to Layer21 format, and runs the FIRS pipeline.
    """
    # ── Validate content type ──────────────────────────────────────────────────
    ct = request.content_type or ""
    if "xml" not in ct.lower():
        return jsonify({
            "ok": False,
            "error": f"Expected Content-Type application/xml or text/xml, got: {ct!r}",
        }), 415

    xml_body = request.data
    if not xml_body:
        return jsonify({"ok": False, "error": "Empty request body"}), 400

    # ── Parse UBL XML → Layer21 format ────────────────────────────────────────
    try:
        data = _parse_ubl_to_layer21(xml_body, g.business)
    except ET.ParseError as exc:
        logger.warning("Odoo UBL XML parse error: %s", exc)
        return jsonify({"ok": False, "error": f"XML parse error: {exc}"}), 422
    except ValueError as exc:
        logger.warning("Odoo UBL mapping failed: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 422

    return run_erp_invoice_pipeline(
        data,
        source="Odoo",
        received_message="Odoo EDI invoice received — initiating FIRS validation and signing",
    )

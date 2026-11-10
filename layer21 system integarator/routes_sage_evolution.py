"""
Blueprint: Sage 200 Evolution receiver
Endpoint: POST /api/v1/sage-evolution/invoice/push

Accepts one posted Sage 200 Evolution invoice as JSON, sent by the Layer21
connector service running on the customer's network. The connector sends the
Evolution rows close to how they sit in the company database; all mapping to
the Layer21 format happens here, so mapping fixes deploy centrally instead of
at every customer site.

Payload (column names as in the Evolution company database):
    {
      "document_kind": "invoice",            # "credit_note" is rejected for now
      "evolution_version": "10.1.3.005",     # optional, kept for traceability
      "company":  {"name", "tin", "email", "telephone",
                   "street_name", "city_name", "postal_zone", "country"},   # optional
      "invoice":  InvNum row: AutoIndex, InvNumber, InvDate, DueDate, OrderNum,
                  InvTotExcl, InvTotTax, InvTotIncl,
                  ForeignCurrencyID, CurrencyCode (from Currency, when foreign),
                  fExchangeRate, fInvTotForeignExcl, fInvTotForeignTax, fInvTotForeignIncl
      "customer": Client row: Account, Name, Tax_Number, EMail, Telephone,
                  Physical1..Physical5, PhysicalPC, Country (optional)
      "lines":    [_btblInvoiceLines rows: idInvoiceLines, cDescription,
                   StockCode (from StkItem), UnitCode (from unit of measure),
                   fQuantity, fUnitPriceExcl, fLineDiscount, fTaxRate,
                   fQuantityLineTotExcl, fQuantityLineTaxAmount, and the
                   ...Foreign variants for foreign-currency invoices;
                   optional HSCode, ProductCategory, TaxCategory]
    }

Authentication: X-API-KEY + X-API-SECRET headers (same as /invoice/receive).
"""

import re
import logging
from collections import OrderedDict
from datetime import date

from flask import Blueprint, request, jsonify, g

from erp_pipeline import run_erp_invoice_pipeline
from helpers import require_api_auth

logger = logging.getLogger(__name__)

sage_evolution_bp = Blueprint("sage_evolution", __name__, url_prefix="/api/v1/sage-evolution")

HOME_CURRENCY = "NGN"

# FIRS invoice type code for a standard invoice (same default as the other receivers)
INVOICE_TYPE_CODE = 396

# Tax category used when the connector does not send one per line.
# Evolution only gives us the rate, so a 0% line cannot be told apart from an
# exempt one here; the connector can send TaxCategory to override.
STANDARD_TAX_CATEGORY = "STANDARD_VAT"
ZERO_TAX_CATEGORY = "ZERO_VAT"

# Evolution keeps foreign-currency amounts in separate columns
LINE_AMOUNT_FIELDS = {
    "unit_price": ("fUnitPriceExcl", "fUnitPriceExclForeign"),
    "line_total": ("fQuantityLineTotExcl", "fQuantityLineTotExclForeign"),
    "line_tax": ("fQuantityLineTaxAmount", "fQuantityLineTaxAmountForeign"),
}
HEADER_AMOUNT_FIELDS = {
    "total_excl": ("InvTotExcl", "fInvTotForeignExcl"),
    "total_tax": ("InvTotTax", "fInvTotForeignTax"),
    "total_incl": ("InvTotIncl", "fInvTotForeignIncl"),
}


def _str(row, key):
    """Return a stripped string value from an Evolution row, or ''."""
    v = row.get(key)
    return str(v).strip() if v is not None else ""


def _num(row, key):
    """Return a float value from an Evolution row, or 0.0."""
    try:
        return float(row.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def _iso_date(value):
    """
    Normalise an Evolution datetime ("2026-11-03", "2026-11-03T00:00:00",
    "2026-11-03 00:00:00.000") to "YYYY-MM-DD", or None when empty/invalid.
    """
    if not value:
        return None
    text = str(value).strip()[:10]
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def _amount(row, fields, foreign):
    home_key, foreign_key = fields
    return _num(row, foreign_key if foreign else home_key)


def _customer_party(cust):
    account = _str(cust, "Account")
    name = _str(cust, "Name")
    tin = _str(cust, "Tax_Number")
    if not tin:
        raise ValueError(
            f"Customer {account or '?'} ({name or 'no name'}) has no tax number in Sage. "
            "Add the customer's TIN to the Tax Number field of the customer record."
        )

    party = {"tin": tin}
    if name:
        party["party_name"] = name
    if _str(cust, "EMail"):
        party["email"] = _str(cust, "EMail")
    if _str(cust, "Telephone"):
        party["telephone"] = _str(cust, "Telephone")

    street = ", ".join(p for p in (_str(cust, "Physical1"), _str(cust, "Physical2")) if p)
    city = _str(cust, "Physical3") or _str(cust, "Physical4")
    if street or city or _str(cust, "PhysicalPC"):
        party["postal_address"] = {
            "street_name": street,
            "city_name": city,
            "postal_zone": _str(cust, "PhysicalPC"),
            "country": _str(cust, "Country") or "NG",
        }
    return party


def _supplier_party(company, business):
    # The TIN registered with Layer21 is authoritative; Sage's copy is a fallback
    tin = (getattr(business, "tin", None) or "").strip() or _str(company, "tin")
    if not tin:
        raise ValueError("Supplier TIN missing: set the business TIN in Layer21")

    party = {"tin": tin, "party_name": _str(company, "name") or business.name}
    if _str(company, "email"):
        party["email"] = _str(company, "email")
    if _str(company, "telephone"):
        party["telephone"] = _str(company, "telephone")
    if _str(company, "street_name") or _str(company, "city_name"):
        party["postal_address"] = {
            "street_name": _str(company, "street_name"),
            "city_name": _str(company, "city_name"),
            "postal_zone": _str(company, "postal_zone"),
            "country": _str(company, "country") or "NG",
        }
    return party


def _map_evolution_to_layer21(payload, business):
    """
    Map a Sage 200 Evolution connector payload into a Layer21 canonical dict.

    Args:
        payload:  Parsed JSON body from the connector
        business: Authenticated Business model instance (from g.business)

    Returns:
        Layer21-format dict ready for validate_basic_invoice_payload()

    Raises:
        ValueError: if the payload cannot be mapped (message is safe to return)
    """
    kind = (payload.get("document_kind") or "invoice").strip().lower()
    if kind == "credit_note":
        raise ValueError(
            "Credit notes are not supported yet: FIRS needs the original invoice's IRN "
            "as a billing reference, which this pipeline does not send"
        )
    if kind != "invoice":
        raise ValueError(f"Unknown document_kind {kind!r}; expected 'invoice'")

    inv = payload.get("invoice")
    cust = payload.get("customer")
    rows = payload.get("lines")
    if not isinstance(inv, dict) or not isinstance(cust, dict) or not isinstance(rows, list):
        raise ValueError("Payload needs 'invoice' and 'customer' objects and a 'lines' list")
    company = payload.get("company") if isinstance(payload.get("company"), dict) else {}

    # ── Invoice number (FIRS accepts letters and digits only) ─────────────────
    raw_number = _str(inv, "InvNumber")
    if not raw_number:
        raise ValueError("Invoice is missing InvNumber")
    invoice_number = re.sub(r"[^A-Za-z0-9]", "", raw_number)
    if not invoice_number:
        raise ValueError(f"Invoice number '{raw_number}' contains no alphanumeric characters")

    # ── Dates ──────────────────────────────────────────────────────────────────
    issue_date = _iso_date(inv.get("InvDate"))
    if not issue_date:
        raise ValueError(f"Invoice {raw_number} has a missing or invalid InvDate")
    due_date = _iso_date(inv.get("DueDate"))

    # ── Currency ───────────────────────────────────────────────────────────────
    foreign = bool(inv.get("ForeignCurrencyID"))
    currency = _str(inv, "CurrencyCode").upper() if foreign else HOME_CURRENCY
    if foreign and not currency:
        raise ValueError(f"Invoice {raw_number} is in a foreign currency but CurrencyCode is missing")
    foreign = foreign and currency != HOME_CURRENCY

    # ── Invoice lines and tax subtotals ───────────────────────────────────────
    invoice_lines = []
    subtotals = OrderedDict()
    for row in rows:
        if not isinstance(row, dict):
            continue
        qty = _num(row, "fQuantity")
        price = _amount(row, LINE_AMOUNT_FIELDS["unit_price"], foreign)
        line_ext = _amount(row, LINE_AMOUNT_FIELDS["line_total"], foreign)
        line_tax = _amount(row, LINE_AMOUNT_FIELDS["line_tax"], foreign)
        if not qty and not line_ext:
            continue  # comment / note lines carry no value

        rate = _num(row, "fTaxRate")
        category = _str(row, "TaxCategory") or (STANDARD_TAX_CATEGORY if rate > 0 else ZERO_TAX_CATEGORY)
        discount_rate = _num(row, "fLineDiscount")
        discount_amount = round(qty * price - line_ext, 2) if discount_rate else 0.0

        description = _str(row, "cDescription")
        # Invoices.invoice_line requires description/line_id and a numeric price
        invoice_lines.append({
            "line_id": _str(row, "idInvoiceLines"),
            "description": description,
            "hsn_code": _str(row, "HSCode"),
            "product_category": _str(row, "ProductCategory"),
            "discount_rate": discount_rate or None,
            "discount_amount": discount_amount or None,
            "invoiced_quantity": qty,
            "line_extension_amount": round(line_ext, 2),
            "unit": _str(row, "UnitCode"),
            "item": {
                "name": description or _str(row, "StockCode"),
                "description": description,
                "sellers_item_identification": _str(row, "StockCode"),
            },
            "price": price,
            "base_quantity": 1,
        })

        sub = subtotals.setdefault((category, rate), {"taxable_amount": 0.0, "tax_amount": 0.0})
        sub["taxable_amount"] += line_ext
        sub["tax_amount"] += line_tax

    if not invoice_lines:
        raise ValueError(f"Invoice {raw_number} has no lines with a quantity or value")

    tax_subtotal = [
        {
            "taxable_amount": round(v["taxable_amount"], 2),
            "tax_amount": round(v["tax_amount"], 2),
            "tax_category": {"id": category, "percent": rate},
        }
        for (category, rate), v in subtotals.items()
    ]

    # ── Totals ─────────────────────────────────────────────────────────────────
    total_excl = _amount(inv, HEADER_AMOUNT_FIELDS["total_excl"], foreign)
    total_tax = _amount(inv, HEADER_AMOUNT_FIELDS["total_tax"], foreign)
    total_incl = _amount(inv, HEADER_AMOUNT_FIELDS["total_incl"], foreign)
    legal_monetary_total = {
        "line_extension_amount": round(total_excl, 2),
        "tax_exclusive_amount": round(total_excl, 2),
        "tax_inclusive_amount": round(total_incl, 2),
        "payable_amount": round(total_incl, 2),
    }

    return {
        "business_id": business.buisness_id,
        "invoice_number": invoice_number,
        "issue_date": issue_date,
        "due_date": due_date,
        "document_currency_code": currency,
        "invoice_type_code": INVOICE_TYPE_CODE,
        "accounting_supplier_party": _supplier_party(company, business),
        "accounting_customer_party": _customer_party(cust),
        "invoice_line": invoice_lines,
        "tax_total": [{"tax_amount": round(total_tax, 2), "tax_subtotal": tax_subtotal}],
        "legal_monetary_total": legal_monetary_total,
        # Invoices.payment_means rejects an empty payment_due_date
        "payment_means": [{"payment_means_code": 30, "payment_due_date": due_date or issue_date}],
        "erp_source": {
            "system": "sage_200_evolution",
            "evolution_version": _str(payload, "evolution_version"),
            "inv_num_auto_index": inv.get("AutoIndex"),
            "inv_number": raw_number,
            "order_number": _str(inv, "OrderNum"),
            "exchange_rate": _num(inv, "fExchangeRate") if foreign else None,
        },
    }


@sage_evolution_bp.route("/invoice/push", methods=["POST"])
@require_api_auth
def sage_evolution_invoice_push():
    """
    Receive one posted invoice from the Layer21 Sage 200 Evolution connector.
    Maps it to Layer21 format and runs the FIRS pipeline.
    """
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "Expected a JSON object body"}), 400

    try:
        data = _map_evolution_to_layer21(payload, g.business)
    except ValueError as exc:
        logger.warning("Sage Evolution mapping failed: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 422

    return run_erp_invoice_pipeline(
        data,
        source="Sage Evolution",
        received_message="Sage 200 Evolution invoice received — initiating FIRS validation and signing",
    )

"""
FIRS helper functions (stateless / import-safe).
Designed to avoid circular imports: use `flask.current_app` and import models locally where required.

This file has been updated: the original `build_firs_validate_payload_from_invoice` was replaced
with a robust flat snake_case payload builder (the sandbox's canonical shape). The validate helper
now accepts either the wrapper/camelCase form *or* the flat snake_case form and will post the
flat payload directly to the sandbox to match its expectations.
"""

from functools import wraps
from flask import request, jsonify, g, current_app
from datetime import datetime, date
import json, logging, re, base64, os
import requests
from typing import Any, Dict, Tuple, Optional
import qrcode as _qrcode
from io import BytesIO as _BytesIO
import ssl
import smtplib
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Iterable, Optional, Tuple
from flask import current_app as app
from typing import Union

# cryptography imports for signing
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding, rsa,ec
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend
from types import SimpleNamespace
import hmac
import hashlib
from io import BytesIO
import time
import base64
from models import InvoiceSubmission, SubmissionLog, Business
logger = logging.getLogger(__name__)
try:
    import qrcode
except Exception:
    qrcode = None

# ---------------------------
# Authentication decorator (API)
# ---------------------------
import os
from functools import wraps
from flask import request, jsonify, current_app, g

import os
import hmac
import hashlib
from functools import wraps
from flask import request, jsonify, current_app, g
from sqlalchemy.exc import IntegrityError

import os
import hmac
from functools import wraps
from types import SimpleNamespace
from flask import request, jsonify, current_app, g

def _get_incoming_credentials():
    """Extract API credentials from request headers, query params, or Authorization header"""
    api_key = (request.headers.get("x-api-key") or 
               request.args.get("api_key") or 
               request.headers.get("X-API-KEY"))
    
    api_secret = (request.headers.get("x-api-secret") or 
                  request.args.get("api_secret") or 
                  request.headers.get("X-API-SECRET"))
    
    # Support Authorization header format: "ApiKey key:secret"
    if not api_key or not api_secret:
        auth_hdr = request.headers.get("Authorization")
        if auth_hdr and auth_hdr.lower().startswith("apikey"):
            try:
                _, creds = auth_hdr.split(None, 1)
                if ":" in creds:
                    key, secret = creds.split(":", 1)
                    api_key = api_key or key.strip()
                    api_secret = api_secret or secret.strip()
            except (ValueError, IndexError):
                current_app.logger.debug("AUTH_ENV: malformed Authorization header")
    
    return api_key, api_secret

def require_api_auth(f):
    """Auth decorator: fetch Business by service_id (or fallback to api_key) and verify credentials."""
    @wraps(f)
    def wrapped(*args, **kwargs):
        current_app.logger.info("=== COMPREHENSIVE AUTH DEBUG (DB-backed) ===")

        # helper: incoming credentials (you already have this in your codebase)
        api_key, api_secret = _get_incoming_credentials()
        current_app.logger.info("Incoming credentials:")
        current_app.logger.info("  - api_key: %r", api_key)
        current_app.logger.info("  - api_secret: '%s' (len=%d)",
                                (api_secret[:10] + "..." if api_secret and len(api_secret) > 10 else api_secret),
                                len(api_secret) if api_secret else 0)

        if not api_key or not api_secret:
            current_app.logger.error("Missing credentials in request")
            return jsonify({"ok": False, "error": "Missing credentials"}), 401

        # helper: try to extract service_id from headers, query params, or JSON body
        def _extract_service_id():
            # common header names to check
            for h in ("Service-Id", "FIRS-Service-Id", "X-Service-Id", "X-FIRS-SERVICE-ID"):
                v = request.headers.get(h)
                if v:
                    return v.strip()
            # query param
            v = request.args.get("service_id")
            if v:
                return v.strip()
            # json body
            try:
                j = request.get_json(silent=True) or {}
                v = j.get("service_id")
                if v:
                    return str(v).strip()
            except Exception:
                pass
            return None

        service_id = _extract_service_id()
        current_app.logger.info("Extracted service_id: %r", service_id)
        # Lookup business by service_id if given; otherwise fall back to api_key lookup
        business = None
        if service_id:
            current_app.logger.info("Looking up Business by service_id=%r", service_id)
            business = Business.query.filter_by(service_id=service_id).first()
            if business:
                current_app.logger.info("Found Business id=%r (service_id match)", getattr(business, "id", None))

        if business is None:
            # fallback: lookup by api_key (useful if caller provides api_key but not service_id)
            current_app.logger.info("Looking up Business by api_key (fallback)")
            business = Business.query.filter_by(api_key=api_key.strip()).first()
            if business:
                current_app.logger.info("Found Business id=%r (api_key match)", getattr(business, "id", None))

        if business is None:
            current_app.logger.error("No business found for service_id=%r or api_key=%r", service_id, api_key)
            # don't reveal which lookup failed; generic auth error
            return jsonify({"ok": False, "error": "Invalid credentials"}), 401

        # check business active flag
        if not getattr(business, "is_active", True):
            current_app.logger.warning("Business id=%r is not active", getattr(business, "id", None))
            return jsonify({"ok": False, "error": "Business inactive"}), 403

        # Compare api_key in constant time
        try:
            stored_api_key = (business.api_key or "").strip()
            key_match = hmac.compare_digest(stored_api_key, api_key.strip())
        except Exception as e:
            current_app.logger.exception("Error comparing api_key: %s", e)
            key_match = False

        # Verify secret using Business.verify_secret(provided_secret)
        try:
            secret_match = business.verify_secret(api_secret)
        except Exception as e:
            current_app.logger.exception("Error verifying secret: %s", e)
            secret_match = False

        current_app.logger.info("Credential checks: key_match=%s, secret_match=%s", key_match, secret_match)

        if key_match and secret_match:
            # Attach the DB business to g for downstream handlers
            g.business = business
            # (Optional) also attach a safe serializable subset if you need:
            g.business_meta = SimpleNamespace(
                id=business.id,
                name=getattr(business, "name", None),
                tin=getattr(business, "tin", None),
                service_id=getattr(business, "service_id", None),
                is_active=getattr(business, "is_active", True),
            )
            current_app.logger.info("Authentication successful for business id=%r", business.id)
            return f(*args, **kwargs)
        else:
            current_app.logger.error("Authentication failed for business id=%r: key_match=%s secret_match=%s",
                                     getattr(business, "id", None), key_match, secret_match)
            return jsonify({"ok": False, "error": "Invalid credentials"}), 401

    return wrapped
# Add to your Flask app (remove after debugging)
from flask import current_app, request, jsonify
import hmac as _hmac, hashlib as _hashlib


# ---------------------------
# Utilities
# ---------------------------
def _sanitize_invoice_number(invoice_number: str) -> str:
    if invoice_number is None:
        return ""
    s = re.sub(r'[^A-Za-z0-9]', '', str(invoice_number))
    return s.upper()


def _ensure_bytes(maybe_bytes_or_str):
    if maybe_bytes_or_str is None:
        return None
    if isinstance(maybe_bytes_or_str, str):
        return maybe_bytes_or_str.encode("utf-8")
    return maybe_bytes_or_str


# ---------------------------
# IRN generation & helpers
# ---------------------------
def generate_irn(invoice_number: str, service_id: str, issuedate: date) -> str:
    """
    IRN format per doc: SANITIZEDINVOICENUMBER-SERVICEID-YYYYMMDD
    Example: INV001-94ND90NR-20240611
    Enforces service_id length == 8 (common FIRS pattern) but you can relax if needed.
    """
    if not invoice_number:
        raise ValueError("invoice_number required")
    if not service_id:
        service_id = os.environ.get("FIRS_SERVICE_ID")

    sanitized = _sanitize_invoice_number(invoice_number)
    if not sanitized:
        raise ValueError("invoice_number must contain at least one alphanumeric character")

    service_id = str(service_id).strip().upper()
    if not service_id:
        raise ValueError("service_id is empty")
    # enforce 8 chars (common case); relax by removing this check if needed
    if len(service_id) != 8:
        # allow 8; if you want to support other lengths, change this to `if len(service_id) < 1:`
        raise ValueError("service_id must be 8 characters (FIRS format)")

    datestamp = (issuedate or datetime.utcnow().date()).strftime("%Y%m%d")
    irn = f"{sanitized}-{service_id}-{datestamp}"
    return irn


def build_irn_candidate(invoice_number: str, service_id: str, issue_date: str) -> str:
    """
    Accepts invoice_number (string), service_id and issue_date (YYYY-MM-DD or date)
    Returns IRN candidate string (same format as generate_irn).
    """
    # Use environment variable fallback for service_id
    if not service_id:
        service_id = os.environ.get("FIRS_SERVICE_ID")
    
    # convert issue_date to date
    if isinstance(issue_date, date) and not isinstance(issue_date, datetime):
        dt = issue_date
    else:
        if isinstance(issue_date, datetime):
            dt = issue_date.date()
        else:
            s = str(issue_date).strip()
            if not s:
                raise ValueError("issue_date is required")
            try:
                dt = datetime.strptime(s, "%Y-%m-%d").date()
            except ValueError:
                try:
                    dt = datetime.strptime(s, "%Y%m%d").date()
                except ValueError:
                    try:
                        dt = datetime.fromisoformat(s).date()
                    except Exception as e:
                        raise ValueError("issue_date must be YYYY-MM-DD, YYYYMMDD or ISO format") from e
    return generate_irn(invoice_number, service_id, dt)


# ---------------------------
# Signing helpers
# ---------------------------
def sign_payload_rsa_b64(private_key_pem: Union[bytes, str], payload_text: str) -> str:
    """Sign using RSA PKCS1v15 + SHA256; return base64 signature string."""
    private_key_bytes = _ensure_bytes(private_key_pem)
    private_key = serialization.load_pem_private_key(private_key_bytes, password=None)
    signature = private_key.sign(
        payload_text.encode("utf-8"),
        padding.PKCS1v15(),
        hashes.SHA256()
    )
    return base64.b64encode(signature).decode("ascii")


def sign_payload_with_app_key(private_key_pem: Union[bytes, str], payload_text: str, alg: str = "RSA") -> str:
    """Support RSA or ECDSA (P-256). Return base64 signature."""
    private_key_bytes = _ensure_bytes(private_key_pem)
    private_key = serialization.load_pem_private_key(private_key_bytes, password=None)

    if alg.upper() == "RSA":
        sig = private_key.sign(payload_text.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
        return base64.b64encode(sig).decode("ascii")
    elif alg.upper() == "ECDSA":
        sig = private_key.sign(payload_text.encode("utf-8"), ec.ECDSA(hashes.SHA256()))
        return base64.b64encode(sig).decode("ascii")
    else:
        raise ValueError("Unsupported signing algorithm (use 'RSA' or 'ECDSA')")
    
def _parse_crypto_keys_txt(content: str) -> Tuple[bytes, str]:
    js = json.loads(content)
    pub_pem = base64.b64decode(js["public_key"])
    cert_b64 = js["certificate"]
    return pub_pem, cert_b64


def _load_public_key(pub_pem: bytes):
    return serialization.load_pem_public_key(pub_pem, backend=default_backend())


def encrypt_irn_with_public_key(pub_pem: bytes, cert_b64: str, irn: str) -> str:
    pub = _load_public_key(pub_pem)
    unix_ts = int(time.time())
    payload = {"irn": f"{irn}.{unix_ts}", "certificate": cert_b64}
    data = json.dumps(payload, separators=(",", ":")).encode()
    if not isinstance(pub, rsa.RSAPublicKey):
        raise ValueError("Not an RSA key")
    enc = pub.encrypt(data, asym_padding.PKCS1v15())  # ← hardcoded, matches openssl pkeyutl default
    return base64.b64encode(enc).decode()


def qr_png_datauri(b64_text: str) -> str:
    if qrcode is None:
        raise RuntimeError("pip install qrcode[pil] pillow")
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=6, border=4)
    qr.add_data(b64_text); qr.make(fit=True)
    img = qr.make_image()
    buf = BytesIO(); img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def make_irn_qr_from_business(business, irn: str):
    """Generate encrypted IRN QR using crypto keys stored on the Business model."""
    if not business.certificate_pem:
        raise ValueError(f"Business {business.id} has no public key (certificate_pem)")
    if not business.firs_certificate:
        raise ValueError(f"Business {business.id} has no FIRS certificate (firs_certificate)")

    pub_pem = base64.b64decode(business.certificate_pem)
    cert_b64 = business.firs_certificate

    enc_b64 = encrypt_irn_with_public_key(pub_pem, cert_b64, irn)
    return enc_b64, qr_png_datauri(enc_b64)

def qr_datauri_from_text(text: str, box_size: int = 6, border: int = 4) -> str:
    """
    Create a PNG data URI from arbitrary text (small wrapper around qrcode).
    Use this for creating a QR from compact JSON or other payload text.
    """
    if text is None:
        text = ""
    qr = _qrcode.QRCode(version=None, error_correction=_qrcode.constants.ERROR_CORRECT_M, box_size=box_size, border=border)
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = _BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}"

def qr_png_datauri_from_base64_text(b64_text: str, box_size: int = 6, border: int = 4) -> str:
    """
    Convenience: make a QR whose payload is an *already base64-encoded* string.
    This mirrors the FIRS workflow where you encrypt bytes and base64-encode them,
    then produce a QR directly from that base64 blob.
    """
    return qr_datauri_from_text(b64_text, box_size=box_size, border=border)

# ---------------------------
# small camelCase transformer (snake_case -> camelCase)
# recursive for dicts/lists
# ---------------------------
def _snake_to_camel_one(key: str) -> str:
    parts = key.split("_")
    if len(parts) == 1:
        return key
    return parts[0] + "".join(p.title() for p in parts[1:])


def transform_keys_to_camel(obj: Any) -> Any:
    """
    Recursively transform dict keys from snake_case to camelCase.
    Leaves other primitives untouched.
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            newk = _snake_to_camel_one(k) if isinstance(k, str) else k
            out[newk] = transform_keys_to_camel(v)
        return out
    elif isinstance(obj, list):
        return [transform_keys_to_camel(v) for v in obj]
    else:
        return obj


# ---------------------------
# Postal address check
# ---------------------------
def _has_postal_address(addr: dict) -> bool:
    """Return True if a postalAddress-like dict has any meaningful fields (expects camelCase keys)."""
    if not isinstance(addr, dict):
        return False
    # require at least one of the main keys to be non-empty
    for fld in ("streetName", "cityName", "postalZone", "country"):
        if addr.get(fld):
            return True
    # fallback: any non-empty value
    return any(bool(v) for v in addr.values())


# ---------------------------
# Flat payload builder (snake_case) - replaces previous camelCase wrapper builder
# ---------------------------
def _ensure_plus_telephone(tel: str) -> str:
    if not tel:
        return ""
    tel = str(tel).strip()
    if tel and not tel.startswith("+"):
        return "+" + tel
    return tel


def build_firs_validate_payload_from_invoice(inv, biz=None) -> Dict[str, Any]:
    """
    Build the flat snake_case payload expected by the sandbox's /invoice/validate endpoint.
    This returns a dict shaped like the `expected payload` from the sandbox docs (top-level keys
    such as business_id, irn, issue_date, accounting_supplier_party, etc.).

    The function will prefer values from the `biz` object if provided, then invoice attributes,
    then environment fallbacks. It will raise RuntimeError with a clear message if required fields
    (business_id, irn, supplier email, etc.) are missing.
    """
    # resolve biz if not provided
    business_id_val = None
    service_id_val = None
    if biz is None and getattr(inv, "business_id", None):
        try:
            from models import Business  # local import
            biz = Business.query.filter_by(business_id=inv.business_id).first() or Business.query.filter_by(service_id=inv.service_id).first()
        except Exception:
            biz = None
    if biz:
        business_id_val = getattr(biz, "business_id", None) or current_app.config.get("FIRS_BUSINESS_ID")
        service_id_val = getattr(biz, "service_id", None)

    # env fallback
    business_id_val = business_id_val or getattr(inv, "business_id", None) or current_app.config.get("FIRS_BUSINESS_ID")
    service_id_val = service_id_val or current_app.config.get("FIRS_SERVICE_ID")

    # Helper to pull party fields (supplier/customer)
    def _party_from_inv(p_raw, role="supplier") -> Dict[str, Any]:
        p = p_raw or {}
        out = {}
        out["party_name"] = p.get("party_name") or p.get("partyName") or p.get("name") or (f"{role.title()} Name")
        out["tin"] = p.get("tin") or p.get("tax_id") or p.get("taxId") or f"TIN-{role.upper()}"
        # email preferred from party, then env test fallback
        if p.get("email"):
            out["email"] = p.get("email")
        else:
            # use env fallbacks for tests if available
            if role == "supplier":
                out["email"] = current_app.config.get("TEST_SUPPLIER_EMAIL") or os.environ.get("TEST_SUPPLIER_EMAIL") or ""
            else:
                out["email"] = current_app.config.get("TEST_CUSTOMER_EMAIL") or os.environ.get("TEST_CUSTOMER_EMAIL") or ""
        try:
            sup = out.setdefault("accounting_supplier_party", {})
            tel = sup.get("telephone") or ""
            if not tel:
                biz_tel = None
                try:
                    if biz is not None:
                        # biz may be a model instance or a dict
                        biz_tel = getattr(biz, "telephone", None) or getattr(biz, "phone", None) or (biz.get("telephone") if isinstance(biz, dict) else None)
                except Exception:
                    biz_tel = None
                if biz_tel:
                    tel = str(biz_tel).strip()
                else:
                    tel = current_app.config.get("TEST_SUPPLIER_PHONE") or os.environ.get("TEST_SUPPLIER_PHONE") or ""
            # Normalize: ensure starts with '+'
            tel = str(tel).strip()
            if tel and not tel.startswith("+"):
                # If the phone is digits only, prefix +; otherwise try to be conservative
                digits = re.sub(r'[^0-9]', '', tel)
                if digits:
                    # if there is a likely country code present (length >= 10), prefix +
                    tel = "+" + digits
                else:
                    # fallback: prefix + if there's any non-digit text (keeps value)
                    tel = "+" + tel
            # If still missing and running in test mode, synthesize a generic dev number
            if (not tel or tel == "+") and current_app.config.get("FIRS_TEST_MODE"):
                tin = sup.get("tin") or "supplier"
                tin_digits = re.sub(r'[^0-9]', '', str(tin))
                # make something like +234xxxx if you want Nigeria-style. Use generic +000 if we can't derive.
                if tin_digits and len(tin_digits) >= 3:
                    tel = "+" + tin_digits[:3] + "0000000"
                else:
                    tel = current_app.config.get("TEST_SUPPLIER_PHONE") or os.environ.get("TEST_SUPPLIER_PHONE") or "+0000000000"
            sup["telephone"] = tel or ""
        except Exception:
            logger.debug("Supplier telephone population failed; snapshot: %r", out.get("accounting_supplier_party"), exc_info=True)
        out["telephone"] = _ensure_plus_telephone(tel)
        # Handle postal address with proper fallbacks
        postal = p.get("postal_address") or p.get("postalAddress") or {}
        raw_country = postal.get("country") or postal.get("country_code") or postal.get("countryCode") or ""
        # If no country provided and we're dealing with a customer in test mode, use fallbacks
        if not raw_country and current_app.config.get("FIRS_TEST_MODE"):
            if role == "customer":
                raw_country = os.environ.get("TEST_CUSTOMER_COUNTRY", "NG")
            else:
                raw_country = os.environ.get("TEST_SUPPLIER_COUNTRY", "NG")
        # normalize country: prefer ISO alpha-2, fallback mappings
        raw_country = (raw_country or "").strip()
        iso_country = raw_country.upper()
        # simple alias map (expand as needed)
        country_map = {
            "NIGERIA": "NG",
            "NGN": "NG",
            }
        if iso_country in country_map:
            iso_country = country_map[iso_country]
        # if someone passed full name like "Nigeria"
        if len(iso_country) > 2 and iso_country not in country_map.values():
            # fallback: leave blank if invalid
            iso_country = ""
        # For customer addresses, provide fallbacks if empty and in test mode
        street_name = postal.get("street_name") or postal.get("streetName") or postal.get("street") or ""
        city_name = postal.get("city_name") or postal.get("cityName") or postal.get("city") or ""
        postal_zone = postal.get("postal_zone") or postal.get("postalZone") or postal.get("postal_code") or ""
        if current_app.config.get("FIRS_TEST_MODE") and role == "customer":
            # Provide test fallbacks for customer address if missing
            if not street_name:
                street_name = os.environ.get("TEST_CUSTOMER_STREET", "Test Customer Street")
            if not city_name:
                city_name = os.environ.get("TEST_CUSTOMER_CITY", "Lagos")
            if not postal_zone:
                postal_zone = os.environ.get("TEST_CUSTOMER_POSTAL", "100001")
            if not iso_country:
                iso_country = "NG"
        # For supplier, use existing environment fallbacks
        elif role == "supplier":
            # Try to get supplier address from environment variable
            try:
                default_supplier_addr = os.environ.get("FIRS_DEFAULT_SUPPLIER_ADDRESS")
                if default_supplier_addr and not any([street_name, city_name, postal_zone, iso_country]):
                    addr_data = json.loads(default_supplier_addr)
                    street_name = street_name or addr_data.get("street_name", "")
                    city_name = city_name or addr_data.get("city_name", "")
                    postal_zone = postal_zone or addr_data.get("postal_zone", "")
                    iso_country = iso_country or addr_data.get("country", "NG")
            except Exception:
                # If parsing fails, use basic fallbacks
                if not street_name and current_app.config.get("FIRS_TEST_MODE"):
                    street_name = "32, Owonikoko Street"
                if not city_name and current_app.config.get("FIRS_TEST_MODE"):
                    city_name = "Gwarinpa"
                if not postal_zone and current_app.config.get("FIRS_TEST_MODE"):
                    postal_zone = "900101"
                if not iso_country:
                    iso_country = "NG"
        out["postal_address"] = {
            "street_name": street_name,
            "city_name": city_name,
            "postal_zone": postal_zone,
            "country": iso_country
            }
        if p.get("service_id"):
            out["service_id"] = p.get("service_id")
        if p.get("business_description"):
            out["business_description"] = p.get("business_description")
        return out

    # Build top-level flat payload
    out: Dict[str, Any] = {}

    if business_id_val:
        out["business_id"] = str(business_id_val)

    # IRN and date/time
    out["irn"] = getattr(inv, "irn", None)
    out["issue_date"] = (getattr(inv, "issue_date", None).isoformat() if getattr(inv, "issue_date", None) else None)
    if getattr(inv, "due_date", None):
        out["due_date"] = inv.due_date.isoformat()
    if getattr(inv, "issue_time", None):
        try:
            out["issue_time"] = inv.issue_time.strftime("%H:%M:%S")
        except Exception:
            out["issue_time"] = str(inv.issue_time)

    out["invoice_type_code"] = str(getattr(inv, "invoice_type_code", None)) if getattr(inv, "invoice_type_code", None) else "396"
    out["payment_status"] = getattr(inv, "payment_status", None) or "PENDING"
    if getattr(inv, "note", None):
        out["note"] = getattr(inv, "note")

    out["document_currency_code"] = getattr(inv, "document_currency_code", None) or "NGN"
    out["tax_currency_code"] = getattr(inv, "tax_currency_code", None) or out["document_currency_code"]

    # monetary totals
    lmt = getattr(inv, "legal_monetary_total", {}) or {}
    out["legal_monetary_total"] = {
        "line_extension_amount": float(lmt.get("line_extension_amount", 0)),
        "tax_exclusive_amount": float(lmt.get("tax_exclusive_amount", 0)),
        "tax_inclusive_amount": float(lmt.get("tax_inclusive_amount", 0)),
        "payable_amount": float(lmt.get("payable_amount", 0))
    }

    # invoice lines
    lines = []
    for ln in (getattr(inv, "invoice_line", []) or []):
        # ensure ln is a dict (defensive)
        if not isinstance(ln, dict):
            logger.debug("Skipping non-dict invoice_line entry for invoice id=%s: %r", getattr(inv, "id", None), ln)
            continue

        # item & raw price may be dict or primitive
        raw_price = ln.get("price")
        item = ln.get("item") or {}

        # normalize price: support dict {priceAmount, baseQuantity, priceUnit} OR primitive numeric value
        if isinstance(raw_price, dict):
            try:
                price_amount = raw_price.get("priceAmount") or raw_price.get("price_amount") or raw_price.get("amount") or 0
                price_amount = float(price_amount) if price_amount not in (None, "") else 0.0
            except Exception:
                price_amount = 0.0
            base_qty = raw_price.get("baseQuantity") or raw_price.get("base_quantity") or 1
            try:
                base_qty = float(base_qty)
            except Exception:
                base_qty = 1.0
            price_unit = raw_price.get("priceUnit") or raw_price.get("price_unit") or "NGN per 1"
        else:
            # primitive: could be int/float/string; try to coerce to float
            try:
                price_amount = float(raw_price) if raw_price is not None and raw_price != "" else 0.0
            except Exception:
                price_amount = 0.0
            base_qty = 1.0
            price_unit = "NGN per 1"

        l = {
            "hsn_code": ln.get("hsn_code") or ln.get("hsnCode"),
            "product_category": ln.get("product_category") or ln.get("productCategory"),
            "discount_rate": ln.get("discount_rate") or ln.get("discountRate"),
            "discount_amount": ln.get("discount_amount") or ln.get("discountAmount"),
            "fee_rate": ln.get("fee_rate") or ln.get("feeRate"),
            "fee_amount": ln.get("fee_amount") or ln.get("feeAmount"),
            "invoiced_quantity": ln.get("invoiced_quantity") or ln.get("invoicedQuantity") or 0,
            "line_extension_amount": ln.get("line_extension_amount") or ln.get("lineExtensionAmount") or 0,
            "item": {
                "name": item.get("name") or ln.get("item_name") or "",
                "description": item.get("description") or ln.get("item_description") or "",
                "sellers_item_identification": item.get("sellersItemIdentification") or item.get("sellers_item_identification") or ""
            },
            "price": {
                "price_amount": price_amount,
                "base_quantity": base_qty,
                "price_unit": price_unit
            }
        }

        lines.append(l)
    if lines:
        out["invoice_line"] = lines

    # tax_total mapping
    tt = []
    for t in (getattr(inv, "tax_total", None) or getattr(inv, "taxTotal", None) or []):
        subt = t.get("tax_subtotal") or t.get("taxSubtotal") or []
        mapped_sub = []
        for s in subt:
            mapped_sub.append({
                "taxable_amount": s.get("taxable_amount") or s.get("taxableAmount") or 0,
                "tax_amount": s.get("tax_amount") or s.get("taxAmount") or 0,
                "tax_category": {
                    "id": (s.get("tax_category") or s.get("taxCategory") or {}).get("id"),
                    "percent": (s.get("tax_category") or s.get("taxCategory") or {}).get("percent")
                }
            })
        if mapped_sub:
            tt.append({"tax_amount": t.get("tax_amount") or t.get("taxAmount") or 0, "tax_subtotal": mapped_sub})
    if tt:
        out["tax_total"] = tt

    # Parties
    out["accounting_supplier_party"] = _party_from_inv(getattr(inv, "accounting_supplier_party", None) or getattr(inv, "accountingSupplierParty", None), role="supplier")
    out["accounting_customer_party"] = _party_from_inv(getattr(inv, "accounting_customer_party", None) or getattr(inv, "accountingCustomerParty", None), role="customer")
    _simple_email_re = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

    def _ensure_email_for_party(party_key: str, role: str):
        """
        Ensure party at out[party_key] has a valid 'email'.
        Order of sources: existing party email -> biz attribute -> env TEST_*_EMAIL -> synthesize in TEST_MODE.
        In non-test mode, raise RuntimeError if no valid email is found.
        """
        party = out.setdefault(party_key, {})
        existing = (party.get("email") or "").strip()
        if existing and _simple_email_re.match(existing):
            party["email"] = existing
            return

        # try biz record (if present)
        biz_email = None
        try:
            if biz is not None:
                # handle biz being model or dict
                biz_email = getattr(biz, "email", None) or (biz.get("email") if isinstance(biz, dict) else None)
        except Exception:
            biz_email = None

        if biz_email and _simple_email_re.match(str(biz_email).strip()):
            party["email"] = str(biz_email).strip()
            return

        # try configured test fallback
        env_key = "TEST_SUPPLIER_EMAIL" if role == "supplier" else "TEST_CUSTOMER_EMAIL"
        cfg_email = current_app.config.get(env_key) or os.environ.get(env_key) or ""
        if cfg_email and _simple_email_re.match(cfg_email):
            party["email"] = cfg_email
            return

        # synthesize only in test mode (safe dev fallback)
        if current_app.config.get("FIRS_TEST_MODE"):
            tin = party.get("tin") or "unknown"
            tin_slug = re.sub(r'[^A-Za-z0-9]', '', str(tin)).lower()[:20] or role
            party["email"] = f"{tin_slug}@example.com"
            return

        # non-test: fail loudly so caller can fix data
        raise RuntimeError(f"{party_key}.email is required and must be a valid email address")

    # ensure supplier email
    _ensure_email_for_party("accounting_supplier_party", role="supplier")
    # ensure customer email
    _ensure_email_for_party("accounting_customer_party", role="customer")
    # Attach service_id if available
    if service_id_val:
        out.setdefault("accounting_supplier_party", {})
        out["accounting_supplier_party"]["service_id"] = service_id_val

    # Basic validation: business_id, irn, supplier email
    missing = []
    if not out.get("business_id"):
        missing.append("business_id")
    if not out.get("irn"):
        missing.append("irn")
    sup_email = out.get("accounting_supplier_party", {}).get("email")
    if not sup_email:
        missing.append("accounting_supplier_party.email")

    if missing:
        msg = f"Flat payload missing required fields: {', '.join(missing)}"
        logger.error(msg + " -- payload snippet: %s", json.dumps(out)[:1000])
        raise RuntimeError(msg)

    logger.info("Built flat snake_case payload for FIRS validate (first 2000 chars): %s", json.dumps(out, indent=2)[:2000])
    return out


# ---------------------------
# Basic payload validator (kept for compatibility/testing)
# ---------------------------
def validate_basic_invoice_payload(payload):
    required = ["invoice_number", "issue_date", "document_currency_code", "accounting_supplier_party", "accounting_customer_party", "legal_monetary_total"]
    missing = [k for k in required if k not in payload]
    if missing:
        return False, f"Missing fields: {', '.join(missing)}"

    if not str(payload["invoice_number"]).isalnum():
        return False, "invoice_number must be alphanumeric (no spaces or special chars)"

    try:
        datetime.fromisoformat(payload["issue_date"])
    except Exception:
        return False, "issue_date must be ISO date (YYYY-MM-DD)"

    # legal_monetary_total.payable_amount must exist and be numeric
    lmt = payload.get("legal_monetary_total", {})
    if "payable_amount" not in lmt:
        return False, "legal_monetary_total.payable_amount is required"
    try:
        float(lmt["payable_amount"])
    except Exception:
        return False, "legal_monetary_total.payable_amount must be numeric"

    # supplier & customer party checks
    if not isinstance(payload.get("accounting_supplier_party"), dict) or "tin" not in payload["accounting_supplier_party"]:
        return False, "accounting_supplier_party.tin is required"
    if not isinstance(payload.get("accounting_customer_party"), dict) or "tin" not in payload["accounting_customer_party"]:
        return False, "accounting_customer_party.tin is required"

    return True, None

# ---------------------------
# FIRS Sequential Pipeline
# ---------------------------


def _process_invoice_irn_request(invoice_id) -> dict:
    """
    Runs the IRN pipeline for the given invoice_id without any request/login decorators.
    Returns a result dict:
      {"ok": True, "invoice_id": id, "submission_id": sid, "irn": "IRN...", "csid": "CSID..."}
    On failure returns {"ok": False, "error": "...", "step": "...", "invoice_id": id or None}
    This function also updates InvoiceSubmission records and writes log entries via log_submission().
    """
    try:
        from models import Invoices, InvoiceSubmission, Business, db
        # helper functions from your codebase
        # - generate_irn
        # - build_firs_validate_payload_from_invoice
        # - firs_validate_invoice
        # - firs_sign_invoice
        # - transmit_to_firs
        # - log_submission
        # all assumed importable from their modules (adjust imports if needed)
    except Exception as exc:
        logger.exception("models/helpers import failed in _process_invoice_irn_request: %s", exc)
        return {"ok": False, "error": "internal import error", "details": str(exc), "invoice_id": invoice_id}

    # Fetch invoice
    inv = Invoices.query.get(invoice_id)
    if not inv:
        return {"ok": False, "error": "invoice not found", "invoice_id": invoice_id}

    submission = None
    try:
        # Ensure issue_date
        if not inv.issue_date:
            inv.issue_date = datetime.utcnow().date()
            db.session.add(inv)
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()

        # Ensure supplier TIN present (use env fallback)
        supplier_obj = inv.accounting_supplier_party or {}
        if not supplier_obj.get("tin"):
            supplier_obj["tin"] = os.environ.get("TEST_SUPPLIER_TIN", "12345678-0001")
            supplier_obj["party_name"] = supplier_obj.get("party_name") or os.environ.get("TEST_SUPPLIER_NAME", "Test Supplier Ltd")
            inv.accounting_supplier_party = supplier_obj
            db.session.add(inv)
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()

        # Get/create a submission record for this invoice
        try:
            submission = (InvoiceSubmission.query
                          .filter_by(invoice_id=getattr(inv, "id", None))
                          .order_by(InvoiceSubmission.id.desc())
                          .first())
        except Exception:
            submission = None
            logger.exception("Failed to lookup InvoiceSubmission for invoice %s", getattr(inv, "id", None))

        if not submission:
            try:
                submission = InvoiceSubmission(
                    business_id=inv.business_id or os.environ.get("TEST_BUSINESS_ID"),
                    invoice_id=inv.id,
                    payload={},       # minimal placeholder; route that creates submission earlier may provide full payload
                    status="created",
                    irn_candidate=None,
                )
                db.session.add(submission)
                db.session.flush()
                db.session.commit()
            except Exception:
                db.session.rollback()
                logger.exception("Failed to create fallback InvoiceSubmission for invoice %s", inv.id)

        def _upd(sub: InvoiceSubmission, status: str, error: str | None = None, firs_irn: str | None = None, firs_csid: str | None = None):
            if not sub:
                logger.debug("No submission to update for status=%s (invoice=%s)", status, inv.id)
                return
            try:
                sub.status = status
                sub.error = str(error) if error is not None else None
                if firs_irn:
                    sub.firs_irn = firs_irn
                if firs_csid:
                    sub.firs_csid = firs_csid
                db.session.add(sub)
                db.session.commit()
                logger.info("Updated submission %s -> %s", sub.id, status)
            except Exception:
                db.session.rollback()
                logger.exception("Failed to persist submission status update (submission=%s)", getattr(sub, "id", None))

        # Prepare invoice_number and service_id
        invoice_number = (inv.order_reference or inv.buyer_reference or f"INV{inv.id}").strip()
        if not invoice_number:
            _upd(submission, "error", error="invoice number missing")
            return {"ok": False, "error": "invoice number cannot be determined", "invoice_id": inv.id}
        
        business = Business.query.filter(Business.buisness_id == inv.business_id).first()

        service_id = business.service_id
        if not service_id:
            _upd(submission, "error", error="service id missing")
            return {"ok": False, "error": "FIRS service id missing", "invoice_id": inv.id}

        # 1) Generate IRN candidate
        try:
            irn_candidate = generate_irn(invoice_number, service_id, inv.issue_date)
            inv.irn = irn_candidate
            inv.irn_status = "generating"
            db.session.add(inv)
            db.session.commit()
            if submission:
                submission.irn_candidate = irn_candidate
                _upd(submission, "generating", error=None)
        except Exception as exc:
            logger.exception("Failed to generate IRN for invoice %s: %s", inv.id, exc)
            safe_exc = str(exc)
            if len(safe_exc) > 1000:
                safe_exc = safe_exc[:1000] + " ... (truncated)"
            email_subject = f"[ALERT] Failed to generate IRN for invoice {inv.id}"
            email_body = (
                f"Environment: {app.config.get('ENV')}\n"
                f"Invoice ID: {inv.id}\n"
                f"Submission ID: {getattr(submission, 'id', 'n/a')}\n"
                f"Status (pre-except): {getattr(submission, 'status', 'n/a')}\n\n"
                f"Error: {safe_exc}\n\n"
                "This is an automated alert. Do not reply."
                )
            # Send alert (silently logs failures)
            ok, info = send_alert_email(
                subject=email_subject,
                body=email_body,
                recipients=['support@layer21.com'],   # or omit to use ALERT_RECIPIENTS / ADMIN_EMAIL from config
                reply_to=None,
                logger=logger
                )
            if not ok:
                logger.warning("Alert email not sent: %s", info)
            if submission:
                _upd(submission, "irn_generation_failed", error=str(exc))
                log_submission(submission, f"IRN generation failed: {exc}", level="ERROR")
            return {"ok": False, "error": "irn generation failed", "details": str(exc), "invoice_id": inv.id}

        # 2) Build FIRS payload
        try:
            biz = Business.query.filter_by(id=inv.business_id).first() if inv.business_id else None
            firs_payload = build_firs_validate_payload_from_invoice(inv, biz=biz)
            if not firs_payload:
                _upd(submission, "validate_failed", error="empty payload")
                log_submission(submission, "build_firs_validate_payload_from_invoice returned empty payload", level="ERROR")
                return {"ok": False, "error": "empty firs payload", "invoice_id": inv.id}
            log_submission(submission, f"Payload built for invoice {inv.id}")
            _upd(submission, "validating", error=None)
        except Exception as exc:
            logger.exception("Failed building FIRS payload for invoice %s: %s", inv.id, exc)
            safe_exc = str(exc)
            if len(safe_exc) > 1000:
                safe_exc = safe_exc[:1000] + " ... (truncated)"
            email_subject = f"[ALERT] Failed building payload for invoice {inv.id}"
            email_body = (
                f"Environment: {app.config.get('ENV')}\n"
                f"Invoice ID: {inv.id}\n"
                f"Submission ID: {getattr(submission, 'id', 'n/a')}\n"
                f"Status (pre-except): {getattr(submission, 'status', 'n/a')}\n\n"
                f"Error: {safe_exc}\n\n"
                "This is an automated alert. Do not reply."
                )
            # Send alert (silently logs failures)
            ok, info = send_alert_email(
                subject=email_subject,
                body=email_body,
                recipients=['support@layer21.com'],   # or omit to use ALERT_RECIPIENTS / ADMIN_EMAIL from config
                reply_to=None,
                logger=logger
                )
            if not ok:
                logger.warning("Alert email not sent: %s", info)
            _upd(submission, "validate_failed", error=str(exc))
            log_submission(submission, f"Failed building payload: {exc}", level="ERROR")
            return {"ok": False, "error": "failed to build firs payload", "details": str(exc), "invoice_id": inv.id}
        
        # 2.1)FIRS Login Tax Payer
        validate_resp = None
        try:
            validate_resp = firs_taxpayer_login(inv)
            logger.info("FIRS Login Response %s: %r", inv.id, validate_resp)
            log_submission(submission, f"FIRS Login Response: {validate_resp}")
            _upd(submission, "irn validated")
        except Exception as exc:
            logger.exception("FIRS Login failed for invoice %s: %s", inv.id, exc)
            safe_exc = str(exc)
            if len(safe_exc) > 1000:
                safe_exc = safe_exc[:1000] + " ... (truncated)"
            email_subject = f"[ALERT] FIRS Login failed for invoice {inv.id}"
            email_body = (
                f"Environment: {app.config.get('ENV')}\n"
                f"Invoice ID: {inv.id}\n"
                f"Submission ID: {getattr(submission, 'id', 'n/a')}\n"
                f"Status (pre-except): {getattr(submission, 'status', 'n/a')}\n\n"
                f"Error: {safe_exc}\n\n"
                "This is an automated alert. Do not reply."
                )
            # Send alert (silently logs failures)
            ok, info = send_alert_email(
                subject=email_subject,
                body=email_body,
                recipients=['support@layer21.com'],   # or omit to use ALERT_RECIPIENTS / ADMIN_EMAIL from config
                reply_to=None,
                logger=logger
                )
            if not ok:
                logger.warning("Alert email not sent: %s", info)
            log_submission(submission, f"FIRS Login failed: {exc}", level="ERROR")
            _upd(submission, "FIRS_Login_failed", error=str(exc))
            # If in TEST MODE continue; otherwise return error
            if current_app.config.get("FIRS_TEST_MODE"):
                logger.warning("Validate failed but continuing in TEST MODE: %s", exc)
            else:
                return {"ok": False, "error": "firs validate failed", "details": str(exc), "invoice_id": inv.id}
        # 2.5) Validate IRN with FIRS
        validate_resp = None
        try:
            validate_resp = firs_validate_irn(inv, biz=biz)
            logger.info("FIRS validate IRN response for invoice %s: %r", inv.id, validate_resp)
            log_submission(submission, f"FIRS validate irn response: {validate_resp}")
            _upd(submission, "irn validated")
        except Exception as exc:
            logger.exception("FIRS validate irn failed for invoice %s: %s", inv.id, exc)
            safe_exc = str(exc)
            if len(safe_exc) > 1000:
                safe_exc = safe_exc[:1000] + " ... (truncated)"
            email_subject = f"[ALERT] FIRS validate irn failed for invoice {inv.id}"
            email_body = (
                f"Environment: {app.config.get('ENV')}\n"
                f"Invoice ID: {inv.id}\n"
                f"Submission ID: {getattr(submission, 'id', 'n/a')}\n"
                f"Status (pre-except): {getattr(submission, 'status', 'n/a')}\n\n"
                f"Error: {safe_exc}\n\n"
                "This is an automated alert. Do not reply."
                )
            # Send alert (silently logs failures)
            ok, info = send_alert_email(
                subject=email_subject,
                body=email_body,
                recipients=['support@layer21.com'],   # or omit to use ALERT_RECIPIENTS / ADMIN_EMAIL from config
                reply_to=None,
                logger=logger
                )
            if not ok:
                logger.warning("Alert email not sent: %s", info)
            log_submission(submission, f"FIRS validate irn failed: {exc}", level="ERROR")
            _upd(submission, "irn_validate_failed", error=str(exc))
            # If in TEST MODE continue; otherwise return error
            if current_app.config.get("FIRS_TEST_MODE"):
                logger.warning("Validate failed but continuing in TEST MODE: %s", exc)
            else:
                return {"ok": False, "error": "firs validate failed", "details": str(exc), "invoice_id": inv.id}

        # 3) Validate with FIRS
        validate_resp = None
        try:
            validate_resp = firs_validate_invoice(firs_payload)
            logger.info("FIRS validate response for invoice %s: %r", inv.id, validate_resp)
            log_submission(submission, f"FIRS validate response: {validate_resp}")
            _upd(submission, "validated")
        except Exception as exc:
            logger.exception("FIRS validate failed for invoice %s: %s", inv.id, exc)
            safe_exc = str(exc)
            if len(safe_exc) > 1000:
                safe_exc = safe_exc[:1000] + " ... (truncated)"
            email_subject = f"[ALERT] FIRS validate failed for invoice {inv.id}"
            email_body = (
                f"Environment: {app.config.get('ENV')}\n"
                f"Invoice ID: {inv.id}\n"
                f"Submission ID: {getattr(submission, 'id', 'n/a')}\n"
                f"Status (pre-except): {getattr(submission, 'status', 'n/a')}\n\n"
                f"Error: {safe_exc}\n\n"
                "This is an automated alert. Do not reply."
                )
            # Send alert (silently logs failures)
            ok, info = send_alert_email(
                subject=email_subject,
                body=email_body,
                recipients=['support@layer21.com'],   # or omit to use ALERT_RECIPIENTS / ADMIN_EMAIL from config
                reply_to=None,
                logger=logger
                )
            if not ok:
                logger.warning("Alert email not sent: %s", info)
            log_submission(submission, f"FIRS validate failed: {exc}", level="ERROR")
            _upd(submission, "validate_failed", error=str(exc))
            # If in TEST MODE continue; otherwise return error
            if current_app.config.get("FIRS_TEST_MODE"):
                logger.warning("Validate failed but continuing in TEST MODE: %s", exc)
            else:
                return {"ok": False, "error": "firs validate failed", "details": str(exc), "invoice_id": inv.id}

        # 4) Prepare sign payload (ensure irn present)
        try:
            sign_payload = None
            if isinstance(firs_payload, dict) and ("irn" in firs_payload or "business_id" in firs_payload):
                if not firs_payload.get("irn"):
                    firs_payload["irn"] = inv.irn or ""
                sign_payload = firs_payload
            else:
                # attempt nested structure
                try:
                    if isinstance(firs_payload, dict):
                        invoice_req = firs_payload.setdefault("invoiceRequest", {})
                        invoice = invoice_req.setdefault("invoice", {})
                        if not invoice.get("irn"):
                            invoice["irn"] = inv.irn or ""
                        sign_payload = firs_payload
                    else:
                        sign_payload = {"invoiceRequest": {"invoice": {"irn": inv.irn or ""}}}
                except Exception:
                    sign_payload = {"invoiceRequest": {"invoice": {"irn": inv.irn or ""}}}
        except Exception:
            sign_payload = {"invoiceRequest": {"invoice": {"irn": inv.irn or ""}}}

        # attach certificate when available
        try:
            cert_b64 = current_app.config.get("FIRS_CERTIFICATE_B64")
            if not cert_b64:
                crypto_txt = current_app.config.get("CRYPTO_KEYS_TXT")
                if not crypto_txt:
                    keys_path = current_app.config.get("CRYPTO_KEYS_PATH") or os.path.join(current_app.root_path, "crypto_keys.txt")
                    if os.path.exists(keys_path):
                        with open(keys_path, "r", encoding="utf-8") as fh:
                            crypto_txt = fh.read()
                if crypto_txt:
                    parsed = json.loads(crypto_txt)
                    cert_b64 = parsed.get("certificate") or parsed.get("cert")
            if cert_b64 and isinstance(sign_payload, dict):
                sign_payload = dict(sign_payload)
                sign_payload["certificate"] = cert_b64
        except Exception:
            logger.exception("Failed to attach certificate (non-fatal)")

        # 5) Sign
        try:
            sign_resp = firs_sign_invoice(sign_payload)
            logger.info("FIRS sign response for invoice %s: %r", inv.id, sign_resp)
            log_submission(submission, f"FIRS sign response: {sign_resp}")
            _upd(submission, "signed", error=None)
        except Exception as exc:
            logger.exception("FIRS sign failed for invoice %s: %s", inv.id, exc)
            safe_exc = str(exc)
            if len(safe_exc) > 1000:
                safe_exc = safe_exc[:1000] + " ... (truncated)"
            email_subject = f"[ALERT] FIRS sign failed for invoice {inv.id}"
            email_body = (
                f"Environment: {app.config.get('ENV')}\n"
                f"Invoice ID: {inv.id}\n"
                f"Submission ID: {getattr(submission, 'id', 'n/a')}\n"
                f"Status (pre-except): {getattr(submission, 'status', 'n/a')}\n\n"
                f"Error: {safe_exc}\n\n"
                "This is an automated alert. Do not reply."
                )
            # Send alert (silently logs failures)
            ok, info = send_alert_email(
                subject=email_subject,
                body=email_body,
                recipients=['support@layer21.com'],   # or omit to use ALERT_RECIPIENTS / ADMIN_EMAIL from config
                reply_to=None,
                logger=logger
                )
            if not ok:
                logger.warning("Alert email not sent: %s", info)
            log_submission(submission, f"FIRS sign failed: {exc}", level="ERROR")
            _upd(submission, "sign_failed", error=str(exc))
            # rollback partial DB work and return
            try:
                db.session.rollback()
            except Exception:
                logger.exception("DB rollback failed after sign error")
            return {"ok": False, "error": "sign failed", "details": str(exc), "invoice_id": inv.id}

        # 6) Update invoice + submission on success
        try:
            returned_irn = (sign_resp.get("irn") if isinstance(sign_resp, dict) else None) or inv.irn
            returned_csid = sign_resp.get("csid") if isinstance(sign_resp, dict) else None
            inv.irn = returned_irn
            if returned_csid:
                inv.csid = returned_csid
            inv.irn_status = sign_resp.get("status") or "issued"
            inv.submitted_at = datetime.utcnow()
            db.session.add(inv)
            _upd(submission, "signed", error=None)
            if submission:
                submission.status = "transmitted" if (sign_resp.get("irn") or sign_resp.get("csid")) else "issued"
                submission.error = None
                submission.firs_irn = returned_irn
                submission.firs_csid = returned_csid
                db.session.add(submission)

            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            logger.exception("Failed to persist signing result for invoice %s: %s", inv.id, exc)
            _upd(submission, "persist_failed", error=str(exc))
            return {"ok": False, "error": "persist signing result failed", "details": str(exc), "invoice_id": inv.id}
        
        # 7) Self health-check before transmit
        try:
            hc = firs_self_health_check_call()
            log_submission(submission, f"Self-health-check result: {hc}")
            if not hc.get("ok"):
                submission.status = "health_check_failed"
                submission.error = hc.get("error")
                db.session.add(submission)
                db.session.commit()
                return {
                    "ok": False,
                    "error": "self-health-check failed",
                    "details": hc.get("error"),
                    "invoice_id": inv.id,
                }
        except Exception as exc:
            logger.exception("Health check failed unexpectedly for invoice %s: %s", inv.id, exc)
            safe_exc = str(exc)
            if len(safe_exc) > 1000:
                safe_exc = safe_exc[:1000] + " ... (truncated)"
            send_alert_email(
                subject=f"[ALERT] Health check failed for invoice {inv.id}",
                body=(
                    f"Environment: {app.config.get('ENV')}\n"
                    f"Invoice ID: {inv.id}\n"
                    f"Submission ID: {getattr(submission, 'id', 'n/a')}\n"
                    f"Status (pre-except): {getattr(submission, 'status', 'n/a')}\n\n"
                    f"Error: {safe_exc}\n\nThis is an automated alert. Do not reply."
                ),
                recipients=["support@layer21.com"],
                reply_to=None,
                logger=logger,
            )
            log_submission(submission, f"Health check exception: {exc}", level="ERROR")
            submission.status = "health_check_failed"
            submission.error = str(exc)
            db.session.add(submission)
            db.session.commit()
            return {"ok": False, "error": "health_check failed", "details": str(exc), "invoice_id": inv.id}

        # ── 7.5) TIN lookup — must succeed before transmit is attempted ────────
        customer_tin = (inv.accounting_customer_party or {}).get("tin")
        tin_lookup_ok = False

        if not customer_tin:
            # No TIN on the invoice — log and block transmit
            logger.warning("TIN lookup skipped for invoice %s: supplier TIN missing", inv.id)
            log_submission(submission, "TIN lookup skipped — supplier TIN missing", level="ERROR")
            _upd(submission, "tin_lookup_failed", error="supplier TIN missing")
            return {
                "ok": False,
                "error": "tin lookup failed — supplier TIN missing on invoice",
                "invoice_id": inv.id,
            }

        try:
            tin_resp = firs_lookup_tin(customer_tin)
            logger.info("FIRS TIN lookup response for invoice %s (TIN %s): %r", inv.id, customer_tin, tin_resp)
            log_submission(submission, f"FIRS TIN lookup response: {tin_resp}")

            tin_up = tin_resp.get("data", {}).get("up")
            if tin_resp.get("ok") and tin_up:
                tin_lookup_ok = True
                _upd(submission, "tin_lookup_success")
                logger.info("  TIN lookup OK")
            else:
                # FIRS returned 2xx but body signals failure (edge case)
                raise ValueError(f"TIN lookup returned ok=False: {tin_resp}")

        except Exception as exc:
            logger.exception("FIRS TIN lookup failed for invoice %s (TIN %s): %s", inv.id, customer_tin, exc)
            safe_exc = str(exc)
            if len(safe_exc) > 1000:
                safe_exc = safe_exc[:1000] + " ... (truncated)"
            send_alert_email(
                subject=f"[ALERT] FIRS TIN lookup failed for invoice {inv.id}",
                body=(
                    f"Environment: {app.config.get('ENV')}\n"
                    f"Invoice ID: {inv.id}\n"
                    f"Customer TIN: {customer_tin}\n"
                    f"Submission ID: {getattr(submission, 'id', 'n/a')}\n"
                    f"Status (pre-except): {getattr(submission, 'status', 'n/a')}\n\n"
                    f"Error: {safe_exc}\n\nThis is an automated alert. Do not reply."
                ),
                recipients=["support@layer21.com"],
                reply_to=None,
                logger=logger,
            )
            log_submission(submission, f"FIRS TIN lookup failed: {exc}", level="ERROR")
            _upd(submission, "tin_lookup_failed", error=str(exc))

            if current_app.config.get("FIRS_TEST_MODE"):
                logger.warning("TIN lookup failed but continuing in TEST MODE: %s", exc)
                tin_lookup_ok = True   # allow transmit to proceed in test mode
            else:
                return {
                    "ok": False,
                    "error": "tin lookup failed",
                    "details": str(exc),
                    "invoice_id": inv.id,
                }

        # 8) Transmit to FIRS — only if TIN lookup succeeded
        if tin_lookup_ok:
            try:
                tx_resp = transmit_to_firs(inv.irn)
                logger.info("Transmit result for invoice %s: %r", inv.id, tx_resp)
                log_submission(submission, f"Transmit response: {tx_resp}")
                if tx_resp.get("status") == "failed":
                    err = tx_resp.get("body", {}).get("error", {}).get("details")
                    _upd(submission, "transmit_failed", err)
                    raise RuntimeError(f"Transmit failed: {err}")
                _upd(submission, "transmitted", error=None)
                inv.transmitted_at = datetime.utcnow()
                inv.irn_status = inv.irn_status or "transmitted"
                db.session.add(inv)
                db.session.commit()
            except Exception as exc:
                logger.exception("Transmit to FIRS failed for invoice %s (non-fatal)", inv.id)
                safe_exc = str(exc)
                if len(safe_exc) > 1000:
                    safe_exc = safe_exc[:1000] + " ... (truncated)"
                send_alert_email(
                    subject=f"[ALERT] Transmit to FIRS failed for invoice {inv.id}",
                    body=(
                        f"Environment: {app.config.get('ENV')}\n"
                        f"Invoice ID: {inv.id}\n"
                        f"Submission ID: {getattr(submission, 'id', 'n/a')}\n"
                        f"Status (pre-except): {getattr(submission, 'status', 'n/a')}\n\n"
                        f"Error: {safe_exc}\n\nThis is an automated alert. Do not reply."
                    ),
                    recipients=["support@layer21.com"],
                    reply_to=None,
                    logger=logger,
                )
                log_submission(submission, f"Transmit failed: {exc}", level="ERROR")
                _upd(submission, "transmit_failed", error=str(exc))
        else:
            # Should not reach here — return above handles it — but belt-and-braces
            logger.warning("Transmit skipped for invoice %s: TIN lookup did not succeed", inv.id)
            log_submission(submission, "Transmit skipped — TIN lookup did not succeed", level="ERROR")

        return {
            "ok": True,
            "invoice_id": inv.id,
            "submission_id": getattr(submission, "id", None),
            "irn": inv.irn,
            "csid": inv.csid,
        }

    except Exception as exc:
        # final catch-all
        logger.exception("Unexpected error in _process_invoice_irn_request for invoice %s: %s", invoice_id, exc)
        safe_exc = str(exc)
        if len(safe_exc) > 1000:
            safe_exc = safe_exc[:1000] + " ... (truncated)"
        email_subject = f"[ALERT] Unexpected error in FIRS Pipeline for invoice {inv.id}"
        email_body = (
            f"Environment: {app.config.get('ENV')}\n"
            f"Invoice ID: {inv.id}\n"
            f"Submission ID: {getattr(submission, 'id', 'n/a')}\n"
            f"Status (pre-except): {getattr(submission, 'status', 'n/a')}\n\n"
            f"Error: {safe_exc}\n\n"
            "This is an automated alert. Do not reply."
            )
        # Send alert (silently logs failures)
        ok, info = send_alert_email(
            subject=email_subject,
            body=email_body,
            recipients=['support@layer21.com'],   # or omit to use ALERT_RECIPIENTS / ADMIN_EMAIL from config
            reply_to=None,
            logger=logger
            )
        if not ok:
            logger.warning("Alert email not sent: %s", info)
        try:
            db.session.rollback()
        except Exception:
            logger.exception("DB rollback failed in final catch")
        if submission:
            _upd(submission, "error", error=str(exc))
            log_submission(submission, f"Unexpected error: {exc}", level="ERROR")
        return {"ok": False, "error": "unexpected error", "details": str(exc), "invoice_id": invoice_id}

def _process_invoice_transmit_only(invoice_id: int) -> dict:
    """
    Runs only the final two steps of the FIRS pipeline for an invoice
    that has already been signed (submission.status == 'signed' or 'issued').

    Step 7 — Self health-check
    Step 8 — TIN lookup + Transmit

    Use this when the full pipeline succeeded up to signing but transmit
    failed (e.g. access points offline) and you need to retry transmit
    without re-running login / IRN validate / sign.

    Returns:
        {"ok": True,  "invoice_id": ..., "submission_id": ..., "irn": ..., "csid": ...}
        {"ok": False, "error": ..., "invoice_id": ...}
    """
    try:
        from models import Invoices, InvoiceSubmission, db
    except Exception as exc:
        logger.exception("models import failed in _process_invoice_transmit_only: %s", exc)
        return {"ok": False, "error": "internal import error",
                "details": str(exc), "invoice_id": invoice_id}

    inv = Invoices.query.get(invoice_id)
    if not inv:
        return {"ok": False, "error": "invoice not found", "invoice_id": invoice_id}

    # Fetch latest submission
    try:
        submission = (
            InvoiceSubmission.query
            .filter_by(invoice_id=inv.id)
            .order_by(InvoiceSubmission.id.desc())
            .first()
        )
    except Exception:
        submission = None
        logger.exception("Failed to lookup submission for invoice %s", inv.id)

    # Guard — refuse to transmit if already fully transmitted
    if getattr(submission, "status", None) == "transmitted" and \
            inv.irn_status == "transmitted":
        return {
            "ok":         False,
            "error":      "invoice already fully transmitted",
            "invoice_id": inv.id,
            "irn":        inv.irn,
            "status":     "transmitted",
        }

    def _upd(sub, status, error=None):
        if not sub:
            return
        try:
            sub.status = status
            sub.error  = str(error) if error is not None else None
            db.session.add(sub)
            db.session.commit()
            logger.info("  submission %s → %s", sub.id, status)
        except Exception:
            db.session.rollback()
            logger.exception("Failed to update submission status (sub=%s)",
                             getattr(sub, "id", None))

    def _alert(subject, extra=""):
        send_alert_email(
            subject=subject,
            body=(
                f"Environment: {app.config.get('ENV')}\n"
                f"Invoice ID: {inv.id}\n"
                f"Submission ID: {getattr(submission, 'id', 'n/a')}\n"
                f"Status (pre-except): {getattr(submission, 'status', 'n/a')}\n\n"
                f"{extra}\n\nThis is an automated alert. Do not reply."
            ),
            recipients=["support@layer21.com"],
            reply_to=None,
            logger=logger,
        )

    logger.info(
        "transmit-only pipeline for invoice %s (status=%r, irn=%s)",
        inv.id, getattr(submission, "status", None), inv.irn,
    )

    # ═══════════════════════════════════════════════════════════════════
    # STEP 7 — Self health-check
    # ═══════════════════════════════════════════════════════════════════
    logger.info("  → step 7a: self health-check")
    try:
        hc = firs_self_health_check_call()
        log_submission(submission, f"Self-health-check result: {hc}")
        if not hc.get("ok"):
            _upd(submission, "health_check_failed", error=hc.get("error"))
            return {
                "ok":         False,
                "error":      "self-health-check failed",
                "details":    hc.get("error"),
                "invoice_id": inv.id,
            }
        logger.info("  health-check OK")
    except Exception as exc:
        safe = str(exc)[:1000]
        _alert(f"[ALERT] Health check failed for invoice {inv.id}", f"Error: {safe}")
        log_submission(submission, f"Health check exception: {exc}", level="ERROR")
        _upd(submission, "health_check_failed", error=str(exc))
        return {"ok": False, "error": "health_check failed",
                "details": safe, "invoice_id": inv.id}

    # ═══════════════════════════════════════════════════════════════════
    # STEP 7b — TIN lookup
    # ═══════════════════════════════════════════════════════════════════
    logger.info("  → step 7b: TIN lookup")
    customer_tin = (inv.accounting_customer_party or {}).get("tin")
    tin_lookup_ok = False

    if not customer_tin:
        logger.warning("TIN lookup skipped — customer TIN missing for invoice %s", inv.id)
        log_submission(submission, "TIN lookup skipped — customer TIN missing", level="ERROR")
        _upd(submission, "tin_lookup_failed", error="customer TIN missing")
        return {"ok": False, "error": "tin lookup failed — customer TIN missing",
                "invoice_id": inv.id}

    try:
        tin_resp = firs_lookup_tin(customer_tin)
        logger.info("  TIN lookup (TIN %s): %r", customer_tin, tin_resp)
        log_submission(submission, f"FIRS TIN lookup response: {tin_resp}")
        # FIRS always returns ok:True on 200 — must also check data.up
        # up:False  → access point offline → treat as failure
        # up:True   → access point live, TIN confirmed
        tin_up = tin_resp.get("data", {}).get("up")
        if tin_resp.get("ok") and tin_up:
            tin_lookup_ok = True
            _upd(submission, "tin_lookup_success")
            logger.info("  TIN lookup OK")
        elif tin_up is False:
            raise ValueError(f"TIN lookup: access point offline (up=False): {tin_resp}")
        else:
            raise ValueError(f"TIN lookup returned ok=False: {tin_resp}")
    except Exception as exc:
        safe = str(exc)[:1000]
        _alert(
            f"[ALERT] TIN lookup failed for invoice {inv.id}",
            f"Customer TIN: {customer_tin}\nError: {safe}",
        )
        log_submission(submission, f"FIRS TIN lookup failed: {exc}", level="ERROR")
        _upd(submission, "tin_lookup_failed", error=str(exc))
        if current_app.config.get("FIRS_TEST_MODE"):
            logger.warning("TIN lookup failed — continuing in TEST MODE")
            tin_lookup_ok = True
        else:
            return {"ok": False, "error": "tin lookup failed",
                    "details": safe, "invoice_id": inv.id}

    # ═══════════════════════════════════════════════════════════════════
    # STEP 8 — Transmit (only if TIN lookup passed)
    # ═══════════════════════════════════════════════════════════════════
    if not tin_lookup_ok:
        logger.warning("Transmit skipped — TIN lookup did not succeed for invoice %s", inv.id)
        log_submission(submission, "Transmit skipped — TIN lookup did not succeed",
                       level="ERROR")
        return {"ok": False, "error": "transmit skipped — tin lookup failed",
                "invoice_id": inv.id}

    logger.info("  → step 8: transmit")
    try:
        tx_resp = transmit_to_firs(inv.irn)
        logger.info("  transmit raw response: %r", tx_resp)
        log_submission(submission, f"Transmit response: {tx_resp}")

        # transmit_to_firs returns {"status":"failed",...} on 4xx — does NOT raise
        if isinstance(tx_resp, dict) and tx_resp.get("status") == "failed":
            error_detail = str(tx_resp.get("body") or tx_resp)[:1000]

            # ── Idempotent success: FIRS already has this invoice ────────────
            # Happens when transmit succeeded on a previous attempt but the
            # local record was never updated (e.g. crash after transmit).
            already_transmitted_phrases = [
                "already transmitting",
                "confirmed transmitted",
                "already transmitted",
            ]
            if any(p in error_detail.lower() for p in already_transmitted_phrases):
                logger.warning(
                    "  transmit: FIRS reports already transmitted for invoice %s "
                    "— recording local success (idempotent)",
                    inv.id,
                )
                log_submission(
                    submission,
                    f"FIRS already-transmitted — recording local success: {error_detail}",
                )
                inv.irn_status     = "transmitted"
                db.session.add(inv)
                db.session.commit()
                _upd(submission, "transmitted")
                logger.info("  transmit idempotent-OK for invoice %s", inv.id)
            else:
                logger.error("  transmit failed (dict) for invoice %s: %s",
                             inv.id, error_detail)
                log_submission(submission, f"Transmit failed: {error_detail}", level="ERROR")
                _upd(submission, "transmit_failed", error=error_detail)
                return {"ok": False, "error": "transmit failed",
                        "details": error_detail, "invoice_id": inv.id}
        else:
            # Success
            inv.irn_status     = "transmitted"
            db.session.add(inv)
            db.session.commit()
            _upd(submission, "transmitted")
            logger.info("  transmit OK for invoice %s", inv.id)

    except Exception as exc:
        safe = str(exc)[:1000]
        _alert(f"[ALERT] Transmit raised for invoice {inv.id}", f"Error: {safe}")
        log_submission(submission, f"Transmit exception: {safe}", level="ERROR")
        _upd(submission, "transmit_failed", error=str(exc))
        return {"ok": False, "error": "transmit failed",
                "details": safe, "invoice_id": inv.id}

    return {
        "ok":            True,
        "invoice_id":    inv.id,
        "submission_id": getattr(submission, "id", None),
        "irn":           inv.irn,
        "csid":          inv.csid,
    }
# ---------------------------
# Validate invoice function
# ---------------------------

def firs_validate_irn(inv, biz=None, raise_for_status: bool = True) -> Dict[str, Any]:
    """
    Validate an IRN with FIRS using values taken from `inv`.

    Required fields taken from `inv`:
      - inv.business_id
      - inv.irn
      - inv.order_reference  (mapped to invoice_reference)

    Optional:
      - biz: Business instance (only used as fallback if inv.business_id is falsy)

    Returns parsed JSON on success. If raise_for_status is True, raises RuntimeError
    on HTTP errors (including the response body if available).
    """
    base = current_app.config.get("FIRS_API_BASE", "https://einvoice1.firs.gov.ng")
    path = current_app.config.get("FIRS_IRN_VALIDATE_PATH", "/api/v1/invoice/irn/validate")
    url = base.rstrip("/") + "/" + path.lstrip("/")

    headers = {"Content-Type": "application/json"}
    api_key = current_app.config.get("FIRS_API_KEY")
    api_secret = current_app.config.get("FIRS_API_SECRET")
    if api_key:
        headers["x-api-key"] = api_key
    if api_secret:
        headers["x-api-secret"] = api_secret

    # --- Build flat payload directly from inv (preferred) ---
    business_id = getattr(inv, "business_id", None) or (getattr(biz, "id", None) if biz is not None else None)
    irn = getattr(inv, "irn", None)
    invoice_reference = getattr(inv, "order_reference", None)

    # final required checks
    if not irn:
        raise RuntimeError("irn is required by FIRS IRN validate endpoint (missing on inv).")
    if not business_id:
        raise RuntimeError("business_id is required by FIRS IRN validate endpoint (missing on inv and no biz fallback).")
    if not invoice_reference:
        raise RuntimeError("invoice_reference is required by FIRS IRN validate endpoint (missing inv.order_reference).")

    posting_payload = {
        "invoice_reference": str(invoice_reference),
        "business_id": str(business_id),
        "irn": str(irn),
    }

    # log request (truncated)
    try:
        pretty = json.dumps(posting_payload, indent=2, ensure_ascii=False)
        pretty_trunc = pretty if len(pretty) <= 2000 else pretty[:2000] + "\n... (truncated)"
    except Exception:
        pretty_trunc = str(posting_payload)
    logger.info("FIRS IRN validate POST %s\npayload:\n%s", url, pretty_trunc)

    # Send request
    resp = requests.post(
        url,
        json=posting_payload,
        headers=headers,
        timeout=current_app.config.get("FIRS_REQUEST_TIMEOUT", 30),
    )

    if not raise_for_status:
        try:
            return resp.json()
        except Exception:
            return {"status_code": resp.status_code, "text": resp.text}

    # Raise on HTTP error, but capture body for logging and friendly RuntimeError
    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        body = None
        try:
            body = resp.json()
        except Exception:
            body = resp.text
        logger.error("FIRS IRN validate failed: status=%s url=%s body=%s", resp.status_code, url, body)
        raise RuntimeError(f"FIRS IRN validate failed: status={resp.status_code} body={body}") from e

    try:
        return resp.json()
    except Exception:
        return {"status_code": resp.status_code, "text": resp.text}

def firs_taxpayer_login(inv, raise_for_status: bool = True) -> Dict[str, Any]:
    """
    Authenticate a taxpayer with FIRS using values taken from `inv`.

    Required fields taken from `inv`:
      - inv.csid              -> email
      - inv.signature_value   -> password

    Returns parsed JSON on success. If raise_for_status is True, raises RuntimeError
    on HTTP errors (including the response body if available).
    """
    base = current_app.config.get("FIRS_API_BASE", "https://einvoice1.firs.gov.ng")
    path = current_app.config.get(
        "FIRS_LOGIN_PATH",
        "/api/v1/utilities/authenticate"
    )
    url = base.rstrip("/") + "/" + path.lstrip("/")

    # --- Build headers ---
    headers = {
        "Content-Type": "application/json"
    }
    biz = Business.query.filter(Business.buisness_id == inv.business_id).first()
    api_key = current_app.config.get("FIRS_API_KEY")
    api_secret = current_app.config.get("FIRS_API_SECRET")

    if api_key:
        headers["x-api-key"] = api_key
    if api_secret:
        headers["x-api-secret"] = api_secret

    # --- Build payload from inv ---
    email = getattr(inv, "csid", None)
    password = getattr(inv, "signature_value", None)

    # final required checks
    if not email:
        raise RuntimeError("email is required by FIRS login endpoint (missing on inv.csid).")
    if not password:
        raise RuntimeError("password is required by FIRS login endpoint (missing on inv.signature_value).")

    posting_payload = {
        "email": str(email),
        "password": str(password),
    }

    # log request (do NOT log password value)
    safe_log_payload = {
        "email": posting_payload["email"],
        "password": "***"
    }

    try:
        pretty = json.dumps(safe_log_payload, indent=2, ensure_ascii=False)
    except Exception:
        pretty = str(safe_log_payload)

    logger.info("FIRS Login POST %s\nheaders=%s\npayload:\n%s", url, {
        "Content-Type": headers.get("Content-Type"),
        "x-api-key": "***" if api_key else None,
        "x-api-secret": "***" if api_secret else None,
    }, pretty)

    # Send request
    resp = requests.post(
        url,
        json=posting_payload,
        headers=headers,
        timeout=current_app.config.get("FIRS_REQUEST_TIMEOUT", 30),
    )

    if not raise_for_status:
        try:
            return resp.json()
        except Exception:
            return {"status_code": resp.status_code, "text": resp.text}

    # Raise on HTTP error, but capture body for logging and friendly RuntimeError
    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        try:
            body = resp.json()
        except Exception:
            body = resp.text

        logger.error(
            "FIRS Login failed: status=%s url=%s body=%s",
            resp.status_code,
            url,
            body,
        )
        raise RuntimeError(
            f"FIRS Login failed: status={resp.status_code} body={body}"
        ) from e

    try:
        return resp.json()
    except Exception:
        return {"status_code": resp.status_code, "text": resp.text}


def firs_validate_invoice(payload: dict, raise_for_status=True) -> dict:
    """
    POST to FIRS validate endpoint and return parsed JSON.
    Accepts either:
      - the wrapper/camelCase `{"invoiceRequest": {"invoice": {...}}}` (existing behaviour), or
      - the flat snake_case payload the sandbox docs expect (top-level keys like business_id, irn, ...).

    If a flat payload is provided, it will be posted as-is (no camelCase transform or wrapper).
    """
    base = current_app.config.get("FIRS_API_BASE", "https://einvoice1.firs.gov.ng")
    path = current_app.config.get("FIRS_VALIDATE_PATH", "/api/v1/invoice/validate")
    url = base.rstrip("/") + "/" + path.lstrip("/")

    headers = {"Content-Type": "application/json"}
    api_key = current_app.config.get("FIRS_API_KEY")
    api_secret = current_app.config.get("FIRS_API_SECRET")
    if api_key:
        headers["x-api-key"] = api_key
    if api_secret:
        headers["x-api-secret"] = api_secret

    # If payload already appears to be the flat snake_case payload (top-level 'irn' or 'business_id'),
    # assume caller passed the final shape and post as-is (this avoids sandbox wrapper/camelCase mismatches).
    posting_payload = None
    if isinstance(payload, dict) and ("irn" in payload or "business_id" in payload):
        posting_payload = payload
        logger.info("firs_validate_invoice: detected flat payload; will POST flat snake_case payload")
    else:
        # fall back to old behaviour: transform to camelCase and wrap into invoiceRequest if needed
        try:
            firs_payload = transform_keys_to_camel(payload)
        except Exception:
            firs_payload = dict(payload)

        # Normalize to ensure we have an invoiceRequest.invoice structure
        invoice_request = firs_payload.get("invoiceRequest") or firs_payload
        invoice = invoice_request.get("invoice") if isinstance(invoice_request, dict) else None
        if not invoice:
            if firs_payload.get("invoice"):
                invoice = firs_payload.get("invoice")
                invoice_request = {"invoice": invoice}
                firs_payload = {"invoiceRequest": invoice_request}
            else:
                invoice = firs_payload
                firs_payload = {"invoiceRequest": {"invoice": invoice}}

        # ensure supplier postalAddress exists, or try to populate from payload's snake_case or tenant
        supplier = firs_payload["invoiceRequest"]["invoice"].get("accountingSupplierParty") or {}
        postal = supplier.get("postalAddress")
        if not _has_postal_address(postal):
            cand = payload.get("accounting_supplier_party") or payload.get("accountingSupplierParty") or {}
            cand_post = cand.get("postal_address") if isinstance(cand, dict) else None
            if cand_post and isinstance(cand_post, dict):
                firs_payload["invoiceRequest"]["invoice"].setdefault("accountingSupplierParty", {})
                firs_payload["invoiceRequest"]["invoice"]["accountingSupplierParty"]["postalAddress"] = transform_keys_to_camel(cand_post)
                postal = firs_payload["invoiceRequest"]["invoice"]["accountingSupplierParty"]["postalAddress"]

        if not _has_postal_address(postal):
            business_id = payload.get("business_id") or payload.get("businessId") or firs_payload["invoiceRequest"].get("businessId")
            if business_id:
                try:
                    from models import Business
                    biz = Business.query.filter_by(id=business_id).first() or Business.query.filter_by(name=business_id).first()
                    if biz:
                        stored = getattr(biz, "postal_address", None) or getattr(biz, "address", None)
                        if stored and isinstance(stored, dict):
                            firs_payload["invoiceRequest"]["invoice"].setdefault("accountingSupplierParty", {})
                            firs_payload["invoiceRequest"]["invoice"]["accountingSupplierParty"]["postalAddress"] = transform_keys_to_camel(stored)
                            postal = firs_payload["invoiceRequest"]["invoice"]["accountingSupplierParty"]["postalAddress"]
                except Exception:
                    logger.debug("Business postal address lookup failed for business_id=%s", business_id)

        # final check for wrapper path
        if not _has_postal_address(firs_payload["invoiceRequest"]["invoice"].get("accountingSupplierParty", {}).get("postalAddress")):
            msg = ("accountingSupplierParty.postalAddress is required by FIRS but missing or empty. "
                   "Provide supplier postal_address in your payload (street_name, city_name, postal_zone, country) "
                   "or store a default address on the tenant Business record.")
            logger.warning("FIRS payload missing postal address; aborting validate: %s", msg)
            raise RuntimeError(msg)

        # ensure invoice.irn exists (FIRS sandbox requires it for validation)
        if not firs_payload["invoiceRequest"]["invoice"].get("irn"):
            raise RuntimeError("invoice.irn is required by FIRS validate endpoint")

        posting_payload = firs_payload

    # log (truncated)
    try:
        pretty = json.dumps(posting_payload, indent=2, ensure_ascii=False)
        pretty_trunc = pretty if len(pretty) <= 2000 else pretty[:2000] + "\n... (truncated)"
    except Exception:
        pretty_trunc = str(posting_payload)
    logger.info("FIRS validate POST %s\npayload:\n%s", url, pretty_trunc)

    # send request
    resp = requests.post(url, json=posting_payload, headers=headers, timeout=current_app.config.get("FIRS_REQUEST_TIMEOUT", 30))

    if not raise_for_status:
        try:
            return resp.json()
        except Exception:
            return {"status_code": resp.status_code, "text": resp.text}

    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        body = None
        try:
            body = resp.json()
        except Exception:
            body = resp.text
        logger.error("FIRS validate failed: status=%s url=%s body=%s", resp.status_code, url, body)

        # If the wrapper POST failed with an IRN error, attempt the alternate flat snake_case payload once
        details = ""
        if isinstance(body, dict):
            details = (body.get("error") or {}).get("details", "") or body.get("message", "") or str(body)
        if resp.status_code == 400 and "irn" in details.lower():
            # attempt fallback only if we didn't already send flat
            if not (isinstance(payload, dict) and ("irn" in payload or "business_id" in payload)):
                try:
                    alt_payload = build_firs_validate_payload_from_invoice(inv=payload if isinstance(payload, dict) else {}, biz=None)  # best-effort
                except Exception:
                    alt_payload = _invoice_wrapper_to_flat_snake(firs_payload) if 'firs_payload' in locals() else None
                if alt_payload:
                    try:
                        alt_resp = requests.post(url, json=alt_payload, headers=headers, timeout=current_app.config.get("FIRS_REQUEST_TIMEOUT", 30))
                        alt_body = None
                        try:
                            alt_body = alt_resp.json()
                        except Exception:
                            alt_body = alt_resp.text
                        if 200 <= alt_resp.status_code < 300:
                            logger.info("Fallback validate succeeded: %s", alt_body)
                            return alt_body
                        else:
                            logger.error("Fallback validate also failed: status=%s body=%s", alt_resp.status_code, alt_body)
                            raise RuntimeError(f"FIRS validate failed (fallback): status={alt_resp.status_code} body={alt_body}") from e
                    except requests.RequestException as ex:
                        logger.exception("Fallback validate request failed: %s", ex)
                        raise RuntimeError(f"FIRS validate failed: status={resp.status_code} body={body}") from e

        raise RuntimeError(f"FIRS validate failed: status={resp.status_code} body={body}") from e
    # ✅ success branch
    try:
        return resp.json()
    except Exception:
        return {"status_code": resp.status_code, "text": resp.text}


def _camel_to_snake_key(k: str) -> str:
    return re.sub(r'(?<!^)(?=[A-Z])', '_', k).lower()


def _invoice_wrapper_to_flat_snake(firs_payload: dict) -> dict:
    # Minimal helper: convert a wrapper/camelCase payload to flat snake_case.
    inv = firs_payload.get("invoiceRequest", {}).get("invoice", {}) if isinstance(firs_payload, dict) else {}
    out = {}
    biz_id = firs_payload.get("invoiceRequest", {}).get("businessId") or current_app.config.get("FIRS_BUSINESS_ID")
    if biz_id:
        out["business_id"] = biz_id
    fld_map = {
        "irn": "irn",
        "issueDate": "issue_date",
        "dueDate": "due_date",
        "issueTime": "issue_time",
        "invoiceTypeCode": "invoice_type_code",
        "documentCurrencyCode": "document_currency_code",
        "taxCurrencyCode": "tax_currency_code",
    }
    for src, dst in fld_map.items():
        if src in inv:
            out[dst] = inv[src]
    if inv.get("legalMonetaryTotal"):
        l = inv["legalMonetaryTotal"]
        out["legal_monetary_total"] = {
            "line_extension_amount": l.get("lineExtensionAmount", 0),
            "tax_exclusive_amount": l.get("taxExclusiveAmount", 0),
            "tax_inclusive_amount": l.get("taxInclusiveAmount", 0),
            "payable_amount": l.get("payableAmount", 0),
        }
    if inv.get("invoiceLine") and isinstance(inv["invoiceLine"], list):
        lines = []
        for ln in inv["invoiceLine"]:
            line = {
                "description": ln.get("description", ""),
                "invoiced_quantity": ln.get("invoicedQuantity", 0),
                "line_extension_amount": ln.get("lineExtensionAmount", 0),
            }
            price = ln.get("price") or {}
            line["price"] = {
                "price_amount": price.get("priceAmount") or price.get("price_amount") or 0,
                "base_quantity": price.get("baseQuantity") or price.get("base_quantity") or 1,
                "price_unit": price.get("priceUnit") or price.get("price_unit") or "NGN per 1"
            }
            item = ln.get("item") or {}
            line["item"] = {
                "name": item.get("name",""),
                "description": item.get("description",""),
                "sellers_item_identification": item.get("sellersItemIdentification","")
            }
            if ln.get("hsnCode"): line["hsn_code"] = ln.get("hsnCode")
            if ln.get("productCategory"): line["product_category"] = ln.get("productCategory")
            lines.append(line)
        out["invoice_line"] = lines
    def _party_camel_to_snake(p_camel):
        if not p_camel or not isinstance(p_camel, dict):
            return {}
        pa = {}
        pa["party_name"] = p_camel.get("partyName") or p_camel.get("party_name")
        pa["tin"] = p_camel.get("tin")
        if p_camel.get("email"): pa["email"] = p_camel.get("email")
        if p_camel.get("telephone"): pa["telephone"] = p_camel.get("telephone")
        postal = p_camel.get("postalAddress") or p_camel.get("postal_address") or {}
        pa["postal_address"] = {
            "street_name": postal.get("streetName") or postal.get("street_name",""),
            "city_name": postal.get("cityName") or postal.get("city_name",""),
            "postal_zone": postal.get("postalZone") or postal.get("postal_zone",""),
            "country": postal.get("country") or postal.get("country",
                                                           "")
        }
        if p_camel.get("serviceId"):
            pa["service_id"] = p_camel.get("serviceId")
        return pa
    out["accounting_supplier_party"] = _party_camel_to_snake(inv.get("accountingSupplierParty"))
    out["accounting_customer_party"] = _party_camel_to_snake(inv.get("accountingCustomerParty"))
    if inv.get("taxTotal"):
        tt = []
        for t in inv.get("taxTotal", []):
            subt = t.get("taxSubtotal", [])
            mapped_sub = []
            for s in subt:
                mapped_sub.append({
                    "taxable_amount": s.get("taxableAmount", 0),
                    "tax_amount": s.get("taxAmount", 0),
                    "tax_category": {
                        "id": s.get("taxCategory", {}).get("id"),
                        "percent": s.get("taxCategory", {}).get("percent")
                    }
                })
            tt.append({"tax_amount": t.get("taxAmount", 0), "tax_subtotal": mapped_sub})
        out["tax_total"] = tt
    return out


# ---------------------------
# Sign invoice function
# ---------------------------
def firs_sign_invoice(payload: dict, api_key: str = None, api_secret: str = None) -> dict:
    """
    POST to FIRS sign endpoint and return parsed JSON.

    Accepts:
      - signature payload: {'payload': '<xml-or-json-string>', 'signature': '<base64>' [, 'certificate': '<base64>']}
      - flat snake_case invoice payload with top-level 'irn' (or 'business_id')
      - wrapper/camelCase invoice payload (invoiceRequest.invoice)

    Behavior change: if the initial attempt used a flat snake_case payload (i.e. it contained
    top-level 'irn' or 'business_id') and the sign endpoint returns an error (e.g. 400),
    this function will stop and return that error (raise RuntimeError) rather than attempting
    the "wrapped_payload_json" fallback which can trigger the 'invoice.irn is required' error.
    For wrapper payloads or signature payloads, the previous fallback attempts remain.
    """
    base = current_app.config.get("FIRS_API_BASE", "https://eivc-k6z6d.ondigitalocean.app")
    path = current_app.config.get("FIRS_SIGN_PATH", "/api/v1/invoice/sign")
    url = base.rstrip("/") + "/" + path.lstrip("/")

    headers = {"Content-Type": "application/json"}
    api_key = api_key or current_app.config.get("FIRS_API_KEY")
    api_secret = api_secret or current_app.config.get("FIRS_API_SECRET")
    if api_key:
        headers["x-api-key"] = api_key
    if api_secret:
        headers["x-api-secret"] = api_secret

    timeout = current_app.config.get("FIRS_REQUEST_TIMEOUT", 30)

    def _find_irn(obj):
        if isinstance(obj, dict):
            if "irn" in obj and obj["irn"]:
                return obj["irn"]
            for v in obj.values():
                found = _find_irn(v)
                if found:
                    return found
        elif isinstance(obj, list):
            for it in obj:
                found = _find_irn(it)
                if found:
                    return found
        return None

    # Determine posting_payload and record initial type
    posting_payload = None
    initial_type = "unknown"  # one of: 'signature', 'flat', 'wrapper'
    if isinstance(payload, dict) and payload.get("payload") and payload.get("signature"):
        posting_payload = payload
        initial_type = "signature"
        logger.info("firs_sign_invoice: detected signature payload; will POST signature payload")
    elif isinstance(payload, dict) and ("irn" in payload or "business_id" in payload):
        posting_payload = dict(payload)  # shallow copy
        # try to populate top-level irn if nested
        if posting_payload.get("irn") is None:
            nested = _find_irn(payload)
            if nested:
                posting_payload["irn"] = nested
        initial_type = "flat"
        logger.info("firs_sign_invoice: detected flat payload; will POST flat snake_case payload")
    else:
        # treat as wrapper/camelCase path
        try:
            firs_payload = transform_keys_to_camel(payload)
        except Exception:
            firs_payload = dict(payload)

        invoice_request = firs_payload.get("invoiceRequest") or firs_payload
        invoice = invoice_request.get("invoice") if isinstance(invoice_request, dict) else None
        if not invoice:
            if firs_payload.get("invoice"):
                invoice = firs_payload.get("invoice")
                invoice_request = {"invoice": invoice}
                firs_payload = {"invoiceRequest": invoice_request}
            else:
                invoice = firs_payload
                firs_payload = {"invoiceRequest": {"invoice": invoice}}

        # ensure supplier postalAddress or try to populate
        supplier = firs_payload["invoiceRequest"]["invoice"].get("accountingSupplierParty") or {}
        postal = supplier.get("postalAddress")
        if not _has_postal_address(postal):
            cand = payload.get("accounting_supplier_party") or payload.get("accountingSupplierParty") or {}
            cand_post = cand.get("postal_address") if isinstance(cand, dict) else None
            if cand_post and isinstance(cand_post, dict):
                firs_payload["invoiceRequest"]["invoice"].setdefault("accountingSupplierParty", {})
                firs_payload["invoiceRequest"]["invoice"]["accountingSupplierParty"]["postalAddress"] = transform_keys_to_camel(cand_post)
                postal = firs_payload["invoiceRequest"]["invoice"]["accountingSupplierParty"]["postalAddress"]

        if not _has_postal_address(postal):
            business_id = payload.get("business_id") or payload.get("businessId") or firs_payload["invoiceRequest"].get("businessId")
            if business_id:
                try:
                    from models import Business
                    biz = Business.query.filter_by(id=business_id).first() or Business.query.filter_by(name=business_id).first()
                    if biz:
                        stored = getattr(biz, "postal_address", None) or getattr(biz, "address", None)
                        if stored and isinstance(stored, dict):
                            firs_payload["invoiceRequest"]["invoice"].setdefault("accountingSupplierParty", {})
                            firs_payload["invoiceRequest"]["invoice"]["accountingSupplierParty"]["postalAddress"] = transform_keys_to_camel(stored)
                            postal = firs_payload["invoiceRequest"]["invoice"]["accountingSupplierParty"]["postalAddress"]
                except Exception:
                    logger.debug("Business postal address lookup failed for business_id=%s", business_id)

        if not _has_postal_address(firs_payload["invoiceRequest"]["invoice"].get("accountingSupplierParty", {}).get("postalAddress")):
            msg = ("accountingSupplierParty.postalAddress is required by FIRS but missing or empty. "
                   "Provide supplier postal_address in your payload (street_name, city_name, postal_zone, country) "
                   "or store a default address on the tenant Business record.")
            logger.warning("FIRS payload missing postal address; aborting sign: %s", msg)
            raise RuntimeError(msg)

        # set irn from nested if available
        try:
            inv_obj = firs_payload["invoiceRequest"]["invoice"]
            if not inv_obj.get("irn"):
                nested_irn = _find_irn(payload)
                if nested_irn:
                    inv_obj["irn"] = nested_irn
        except Exception:
            pass

        posting_payload = firs_payload
        initial_type = "wrapper"
        logger.info("firs_sign_invoice: using wrapper/camelCase payload; will POST invoiceRequest.invoice wrapper")

    # Log truncated payload
    try:
        pretty = json.dumps(posting_payload, indent=2, ensure_ascii=False)
        logger.debug("firs_sign_invoice: posting payload (truncated): %s", pretty[:1200])
    except Exception:
        logger.debug("firs_sign_invoice: posting payload repr: %r", posting_payload)

    def _post_once(body):
        try:
            resp = requests.post(url, json=body, headers=headers, timeout=timeout)
        except requests.RequestException as ex:
            logger.exception("firs_sign_invoice: request to sign endpoint failed: %s", ex)
            raise RuntimeError(f"FIRS sign request failed: {ex}")

        try:
            body_parsed = resp.json()
        except Exception:
            body_parsed = resp.text

        if 200 <= resp.status_code < 300:
            return True, body_parsed, resp
        else:
            logger.error("firs_sign_invoice: sign endpoint returned status=%s body=%s", resp.status_code, body_parsed)
            return False, body_parsed, resp

    # initial attempt
    success, body_or_text, resp_obj = _post_once(posting_payload)
    if success:
        return body_or_text

    # If the initial attempt used a flat payload, prefer to stop and surface that generic error
    if initial_type == "flat":
        # raise with the endpoint's response body (generic error preferred)
        raise RuntimeError(f"FIRS sign failed (status={getattr(resp_obj,'status_code', None)} body={body_or_text} tried=[])")

    # For signature or wrapper paths, attempt limited fallbacks (certificate add, wrapping, flat fallback)
    tried = []

    # Fallback: if signature payload without certificate, try to add certificate from config/crypto
    if initial_type == "signature" and isinstance(posting_payload, dict) and posting_payload.get("payload") and posting_payload.get("signature") and not posting_payload.get("certificate"):
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
                    try:
                        parsed = json.loads(crypto_txt)
                        cert_b64 = parsed.get("certificate") or parsed.get("cert") or cert_b64
                    except Exception:
                        cert_b64 = cert_b64
            except Exception:
                logger.debug("firs_sign_invoice: no certificate found in config or crypto file")
        if cert_b64:
            payload_with_cert = dict(posting_payload)
            payload_with_cert["certificate"] = cert_b64
            tried.append("added_certificate")
            success, body_or_text, resp_obj = _post_once(payload_with_cert)
            if success:
                return body_or_text

    # Fallback: wrap the wrapper payload as {"payload": "<json>"} only if an IRN exists somewhere
    if isinstance(payload, dict) and initial_type != "flat":
        found_irn = _find_irn(payload)
        if found_irn:
            try:
                inner = dict(payload)
                if not inner.get("irn"):
                    inner["irn"] = found_irn
                wrapped = {"payload": json.dumps(inner, separators=(",", ":"), ensure_ascii=False)}
                tried.append("wrapped_payload_json")
                success, body_or_text, resp_obj = _post_once(wrapped)
                if success:
                    return body_or_text
            except Exception:
                logger.exception("firs_sign_invoice: wrapping payload as JSON failed")

    # Fallback: attempt flat snake_case conversion once (only for wrapper/signature initial types)
    details = ""
    if isinstance(body_or_text, dict):
        details = (body_or_text.get("error") or {}).get("details", "") or body_or_text.get("message", "") or str(body_or_text)

    if getattr(resp_obj, "status_code", None) == 400:
        try:
            alt_payload = None
            try:
                alt_payload = build_firs_validate_payload_from_invoice(inv=payload if isinstance(payload, dict) else {}, biz=None)
            except Exception:
                alt_payload = _invoice_wrapper_to_flat_snake(posting_payload) if posting_payload else None

            if alt_payload:
                tried.append("flat_snake_case_fallback")
                success, alt_body, alt_resp = _post_once(alt_payload)
                if success:
                    logger.info("Fallback sign (flat) succeeded: %s", alt_body)
                    return alt_body
                else:
                    logger.error("Fallback sign (flat) also failed: status=%s body=%s", getattr(alt_resp, "status_code", None), alt_body)
        except Exception as ex:
            logger.exception("Fallback sign request failed: %s", ex)

    # nothing worked: raise with helpful diagnostic
    detail = body_or_text
    raise RuntimeError(f"FIRS sign failed (status={getattr(resp_obj, 'status_code', None)} body={detail} tried={tried})")

# ---------------------------
# Transmit Invoice Function
# ---------------------------
def transmit_to_firs(irn: str) -> dict:
    """
    Calls the FIRS transmit endpoint using configured base/path.
    Returns parsed JSON on 2xx, otherwise returns {"status":"failed", "status_code":..., "body": ...}
    """
    import requests
    from flask import current_app

    # allow test mode to simulate success quickly
    if current_app.config.get("FIRS_TEST_MODE", False):
        logger.info("FIRS_TEST_MODE enabled: simulate transmit for IRN %s", irn)
        return {"status": "transmitted", "irn": irn, "simulated": True}

    base = current_app.config.get("FIRS_API_BASE", "https://einvoice.firs.gov.ng").rstrip("/")
    # allow custom path like "/api/v1/invoice/transmit/{irn}" or "/api/v1/invoice/transmit"
    transmit_path = current_app.config.get("FIRS_TRANSMIT_PATH", "/api/v1/invoice/transmit/{irn}")
    if "{irn}" in transmit_path:
        path = transmit_path.format(irn=irn)
    else:
        # append irn
        path = transmit_path.rstrip("/") + "/" + irn

    url = base + "/" + path.lstrip("/")

    headers = {"Content-Type": "application/json"}
    api_key = current_app.config.get("FIRS_API_KEY")
    api_secret = current_app.config.get("FIRS_API_SECRET")
    if api_key:
        headers["x-api-key"] = api_key
    if api_secret:
        headers["x-api-secret"] = api_secret

    timeout = int(current_app.config.get("FIRS_REQUEST_TIMEOUT", 30))

    try:
        resp = requests.post(url, headers=headers, timeout=timeout)
    except requests.RequestException as ex:
        logger.exception("Transmit request to FIRS failed for IRN %s: %s", irn, ex)
        return {"status": "failed", "error": str(ex)}

    # If non-2xx, return body and status for debugging
    if not (200 <= resp.status_code < 300):
        body = None
        try:
            body = resp.json()
        except Exception:
            body = resp.text
        logger.error("FIRS transmit error: status=%s url=%s body=%s", resp.status_code, url, body)
        return {"status": "failed", "status_code": resp.status_code, "body": body, "url": url}

    # success
    try:
        parsed = resp.json()
    except Exception:
        parsed = {"status_code": resp.status_code, "text": resp.text}
    logger.info("FIRS transmit success for IRN %s: %r", irn, parsed)
    return parsed

def firs_lookup_tin(tin: str) -> dict:
    """
    Calls the FIRS TIN lookup endpoint: GET base_url/api/v1/invoice/transmit/lookup/tin/{TIN}
    Returns parsed JSON on 2xx.
    On failure returns {"ok": False, "status_code": ..., "body": ...}
    On success returns the parsed response with "ok": True injected.
    """
    import requests
    from flask import current_app

    if current_app.config.get("FIRS_TEST_MODE", False):
        logger.info("FIRS_TEST_MODE enabled: simulating TIN lookup for TIN %s", tin)
        return {"ok": True, "tin": tin, "simulated": True, "status": "found"}

    base = current_app.config.get("FIRS_API_BASE", "https://einvoice.firs.gov.ng").rstrip("/")
    lookup_path = current_app.config.get(
        "FIRS_TIN_LOOKUP_PATH",
        "/api/v1/invoice/transmit/lookup/tin/{tin}"
    )
    if "{tin}" in lookup_path:
        path = lookup_path.format(tin=tin)
    else:
        path = lookup_path.rstrip("/") + "/" + tin

    url = base + "/" + path.lstrip("/")

    headers = {"Content-Type": "application/json"}
    api_key    = current_app.config.get("FIRS_API_KEY")
    api_secret = current_app.config.get("FIRS_API_SECRET")
    if api_key:
        headers["x-api-key"] = api_key
    if api_secret:
        headers["x-api-secret"] = api_secret

    timeout = int(current_app.config.get("FIRS_REQUEST_TIMEOUT", 30))

    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
    except requests.RequestException as ex:
        logger.exception("TIN lookup request to FIRS failed for TIN %s: %s", tin, ex)
        return {"ok": False, "error": str(ex)}

    if not (200 <= resp.status_code < 300):
        body = None
        try:
            body = resp.json()
        except Exception:
            body = resp.text
        logger.error(
            "FIRS TIN lookup error: status=%s url=%s body=%s",
            resp.status_code, url, body
        )
        return {"ok": False, "status_code": resp.status_code, "body": body, "url": url}

    try:
        parsed = resp.json()
    except Exception:
        parsed = {"status_code": resp.status_code, "text": resp.text}

    logger.info("FIRS TIN lookup success for TIN %s: %r", tin, parsed)
    # inject ok=True so pipeline can gate on it cleanly
    if isinstance(parsed, dict):
        parsed.setdefault("ok", True)
    return parsed


def make_invoice_xml(inv) -> str:
    """Minimal XML builder (used for signing flows). Returns UTF-8 string."""
    import xml.etree.ElementTree as ET
    root = ET.Element("Invoice")
    header = ET.SubElement(root, "Header")
    ET.SubElement(header, "IRN").text = getattr(inv, "irn", "") or ""
    ET.SubElement(header, "IssueDate").text = (getattr(inv, "issue_date", None).isoformat() if getattr(inv, "issue_date", None) else "")
    ET.SubElement(header, "DueDate").text = (getattr(inv, "due_date", None).isoformat() if getattr(inv, "due_date", None) else "")
    seller = ET.SubElement(root, "Seller")
    seller_obj = getattr(inv, "accounting_supplier_party", {}) or {}
    ET.SubElement(seller, "Name").text = seller_obj.get("party_name") if isinstance(seller_obj, dict) else ""
    ET.SubElement(seller, "TIN").text = seller_obj.get("tin") if isinstance(seller_obj, dict) else ""
    if isinstance(seller_obj, dict) and seller_obj.get("service_id"):
        ET.SubElement(seller, "ServiceID").text = seller_obj.get("service_id")
    buyer = ET.SubElement(root, "Buyer")
    buyer_obj = getattr(inv, "accounting_customer_party", {}) or {}
    ET.SubElement(buyer, "Name").text = buyer_obj.get("party_name") if isinstance(buyer_obj, dict) else ""
    ET.SubElement(buyer, "TIN").text = buyer_obj.get("tin") if isinstance(buyer_obj, dict) else ""
    totals = ET.SubElement(root, "Totals")
    lmt = getattr(inv, "legal_monetary_total", {}) or {}
    ET.SubElement(totals, "LineExtensionAmount").text = str(lmt.get("line_extension_amount", 0))
    ET.SubElement(totals, "TaxExclusiveAmount").text = str(lmt.get("tax_exclusive_amount", 0))
    ET.SubElement(totals, "TaxInclusiveAmount").text = str(lmt.get("tax_inclusive_amount", 0))
    ET.SubElement(totals, "PayableAmount").text = str(lmt.get("payable_amount", 0))
    ET.SubElement(totals, "Currency").text = getattr(inv, "document_currency_code", None) or "NGN"
    lines = ET.SubElement(root, "InvoiceLines")
    for ln in (getattr(inv, "invoice_line", []) or []):
        li = ET.SubElement(lines, "Line")
        ET.SubElement(li, "Description").text = str(ln.get("description") or ln.get("item_name") or "")
        ET.SubElement(li, "Quantity").text = str(ln.get("quantity") or "")
        ET.SubElement(li, "UnitPrice").text = str(ln.get("price") or "")
        ET.SubElement(li, "LineExtensionAmount").text = str(ln.get("line_extension_amount") or "")
    xml_bytes = ET.tostring(root, encoding="utf-8", method="xml")
    return xml_bytes.decode("utf-8")

# ---------------------------
# Fiirs submissions Logger
# ---------------------------
def log_submission(submission: "InvoiceSubmission", message: str, level="INFO"):
    from models import db, SubmissionLog
    try:
        log = SubmissionLog(submission_id=submission.id, message=message, level=level)
        db.session.add(log)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.exception("Failed to persist submission log: %s", e)
    # Broadcast via Socket.IO if available (non-fatal)
    try:
        from app import socketio  # import whatever name you used for socketio
        payload = {
            "id": log.id,
            "submission_id": log.submission_id,
            "level": log.level,
            "message": (log.message[:400] + "…") if len(log.message) > 400 else log.message,
            "created_at": log.created_at.isoformat()
        }
        # send to all connected clients; namespace optional
        socketio.emit("submission_log", payload, broadcast=True, namespace="/logs")
    except Exception:
        logger.debug("SocketIO emit failed (continuing): %s", exc_info=True)


def send_webhook_notifications(irn: str, message: str = "TRANSMITTING", extra: dict = None):
    """
    Post a simple webhook JSON payload to each registered webhook URL.

    - Looks first for current_app.config["FIRS_WEBHOOK_URLS"] (list of urls).
    - If unavailable, tries to query AccessPoint / AccessPointProvider model
      for webhook_url fields (best-effort; won't error if model missing).
    - The payload is: {"irn": <irn>, "message": <message>, ...extra}
    - This function is deliberately non-fatal: exceptions are logged and swallowed.
    """
    payload = {"irn": irn, "message": message}
    if extra and isinstance(extra, dict):
        payload.update(extra)

    # Gather webhook URLs: config or DB fallback
    urls = []
    try:
        conf_urls = current_app.config.get("FIRS_WEBHOOK_URLS")
        if conf_urls:
            if isinstance(conf_urls, (list, tuple, set)):
                urls.extend(list(conf_urls))
            elif isinstance(conf_urls, str):
                urls.append(conf_urls)
    except Exception:
        logger.debug("Error reading FIRS_WEBHOOK_URLS from config", exc_info=True)

    # DB fallback: try to read AccessPoint-like records with webhook_url attribute
    if not urls:
        try:
            from models import AccessPoint  # adjust model name if different
            aps = AccessPoint.query.filter(AccessPoint.webhook_url.isnot(None)).all()
            for ap in aps:
                if getattr(ap, "webhook_url", None):
                    urls.append(ap.webhook_url)
        except Exception:
            # Not fatal - just log at debug
            logger.debug("No AccessPoint model or DB webhook lookup failed", exc_info=True)

    if not urls:
        logger.info("No webhook URLs configured (FIRS_WEBHOOK_URLS or AccessPoint entries). Skipping webhooks.")
        return

    headers = {"Content-Type": "application/json"}
    timeout = current_app.config.get("FIRS_WEBHOOK_TIMEOUT", 5)
    max_retries = int(current_app.config.get("FIRS_WEBHOOK_RETRIES", 2))

    for url in urls:
        if not url:
            continue
        attempt = 0
        while attempt <= max_retries:
            try:
                attempt += 1
                resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
                status_ok = (resp.status_code == 200)
                logger.info("Webhook POST to %s attempt=%s status=%s body=%s", url, attempt, resp.status_code, getattr(resp, "text", "<no-text>"))
                if status_ok:
                    # success for that url; break retry loop
                    break
                else:
                    # don't raise; retry if attempts remain
                    if attempt <= max_retries:
                        sleep_for = 0.5 * attempt
                        time.sleep(sleep_for)
                    else:
                        logger.warning("Webhook URL %s did not return 200 after %s attempts", url, attempt)
                        break
            except requests.RequestException as ex:
                logger.exception("Webhook POST to %s failed on attempt %s: %s", url, attempt, ex)
                if attempt <= max_retries:
                    time.sleep(0.5 * attempt)
                else:
                    logger.warning("Giving up on webhook %s after %s attempts", url, attempt)
                    break

def _firs_headers():
    from flask import current_app
    headers = {"Content-Type": "application/json"}
    token = current_app.config.get("FIRS_API_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
        return headers
    api_key = current_app.config.get("FIRS_API_KEY")
    api_secret = current_app.config.get("FIRS_API_SECRET")
    if api_key:
        headers["x-api-key"] = api_key
    if api_secret:
        headers["x-api-secret"] = api_secret
    client_id = current_app.config.get("FIRS_CLIENT_ID")
    if client_id:
        headers["client-id"] = client_id
    return headers



def firs_self_health_check_call() -> dict:
    """Call FIRS self-health-check before transmitting."""
    import requests
    from flask import current_app

    base = current_app.config.get("FIRS_API_BASE", "https://einvoice.firs.gov.ng")
    url = f"{base}/api/v1/invoice/transmit/self-health-check"

    try:
        resp = requests.get(url, headers=_firs_headers(), timeout=20)
        resp.raise_for_status()
        return {"ok": True, "response": resp.json()}
    except requests.RequestException as ex:
        return {"ok": False, "error": str(ex)}


def _is_safe_header(s: Optional[str]) -> bool:
    """Reject header values containing CR/LF to avoid header injection."""
    if s is None:
        return True
    return '\n' not in s and '\r' not in s

def _is_likely_email(addr: Optional[str]) -> bool:
    if not addr:
        return False
    return '@' in parseaddr(addr)[1]

def send_alert_email(
    subject: str,
    body: str,
    recipients: Optional[Iterable[str]] = None,
    html: Optional[str] = None,
    reply_to: Optional[str] = None,
    smtp_override: Optional[dict] = None,
    logger = None,
    max_body_chars: int = 10000
) -> Tuple[bool, str]:
    """
    Send a synchronous alert email. Returns (ok, info) where info is
    message-id on success or an error message on failure.

    smtp_override: optional dict to override SMTP settings, keys:
        server, port, username, password, use_ssl (bool), use_tls (bool), sender
    """
    logger = logger or app.logger

    # sanity / header safety
    if not _is_safe_header(subject):
        return False, "unsafe subject"
    if reply_to and not _is_safe_header(reply_to):
        reply_to = None

    # recipients: explicit, then app config fallback(s)
    if recipients:
        recipients = [r.strip() for r in recipients if r and r.strip()]
    else:
        env_list = (app.config.get('ALERT_RECIPIENTS') or app.config.get('ADMIN_EMAIL') or
                    app.config.get('SALES_EMAIL') or '')
        recipients = [r.strip() for r in str(env_list).split(',') if r.strip()]

    if not recipients:
        logger.error("send_alert_email: no recipients configured")
        return False, "no recipients"

    # SMTP configuration (override > app.config)
    if smtp_override:
        server = smtp_override.get('server')
        port = int(smtp_override.get('port', 465))
        username = smtp_override.get('username')
        password = smtp_override.get('password')
        sender = smtp_override.get('sender') or username
        use_ssl = bool(smtp_override.get('use_ssl', True))
        use_tls = bool(smtp_override.get('use_tls', False))
    else:
        server = app.config.get('MAIL_SERVER')
        port = int(app.config.get('MAIL_PORT') or 465)
        username = app.config.get('MAIL_USERNAME')
        password = app.config.get('MAIL_PASSWORD')
        sender = app.config.get('MAIL_DEFAULT_SENDER') or username
        use_ssl = bool(app.config.get('MAIL_USE_SSL') or False)
        use_tls = bool(app.config.get('MAIL_USE_TLS') or False)

    # Truncate very long bodies to avoid giant emails / leaking huge logs
    if body and len(body) > max_body_chars:
        body = body[:max_body_chars] + "\n\n... (truncated)"

    # Build message
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = sender
    msg['To'] = ', '.join(recipients)
    if reply_to and _is_likely_email(reply_to):
        msg['Reply-To'] = reply_to

    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype='html')

    # Send (synchronously) - handle both implicit SSL (SMTP_SSL) and STARTTLS flows
    try:
        if use_ssl:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(server, port, timeout=30, context=context) as smtp:
                if app.config.get('MAIL_DEBUG'):
                    smtp.set_debuglevel(1)
                if username and password:
                    smtp.login(username, password)
                response = smtp.send_message(msg)  # dict of refused recipients or None
        else:
            with smtplib.SMTP(server, port, timeout=30) as smtp:
                if app.config.get('MAIL_DEBUG'):
                    smtp.set_debuglevel(1)
                smtp.ehlo()
                if use_tls:
                    smtp.starttls(context=ssl.create_default_context())
                    smtp.ehlo()
                if username and password:
                    smtp.login(username, password)
                response = smtp.send_message(msg)
    except (smtplib.SMTPException, ConnectionError) as exc:
        logger.exception("Alert email failed to send (SMTP error).")
        return False, f"SMTP error: {exc}"
    except Exception as exc:
        logger.exception("Alert email failed to send (unexpected).")
        return False, f"unexpected error: {exc}"

    # send_message returns {} or None; construct a small info string
    try:
        # If the SMTP library returned a dict of refused recipients, include that
        if isinstance(response, dict) and response:
            info = f"refused: {response}"
            logger.warning("send_alert_email: some recipients refused: %s", response)
            return False, info
    except Exception:
        pass

    # Success: return OK and a small confirmation string
    return True, "sent"
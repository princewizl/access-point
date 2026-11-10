import os
from datetime import datetime, timedelta,date,time
from flask import current_app
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import Column, Integer, String, Date, CheckConstraint, DateTime, Boolean, Text
from sqlalchemy.ext.mutable import MutableList, MutableDict
from sqlalchemy.ext.mutable import MutableList
from sqlalchemy.orm import validates    
from flask_login import UserMixin
from flask_bcrypt import Bcrypt
from itsdangerous import URLSafeTimedSerializer
import json, base64
from io import BytesIO
import qrcode
import hmac
import hashlib
from flask_login import current_user



db = SQLAlchemy()
bcrypt = Bcrypt()



def get_token_serializer(secret=None):
    secret = secret or current_app.config["SECRET_KEY"]
    return URLSafeTimedSerializer(secret, salt="layer21-token-salt")

class User(UserMixin, db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    first_name = db.Column(db.String(150), nullable=True)
    last_name = db.Column(db.String(150), nullable=True)
    business_id = db.Column(db.String(150), nullable=True)
    email = db.Column(db.String(255), nullable=False, index=True)
    username = db.Column(db.String(150), nullable=False, unique=True, index=True)
    # store the bcrypt hash (utf-8 string)
    password_hash = db.Column(db.String(128), nullable=False)
    # prefer storing file path/URL instead of blobs in DB for production.
    profile_image_url = db.Column(db.String(1024), nullable=True)
    profile_image_mime = db.Column(db.String(120), nullable=True)
    user_type = db.Column(db.String(64), nullable=True)   # e.g. 'admin', 'staff', 'client'
    company_name = db.Column(db.String(255), nullable=True)
    # security / lifecycle
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    failed_login_count = db.Column(db.Integer, default=0, nullable=False)
    locked_until = db.Column(db.DateTime, nullable=True)
    email_confirmed = db.Column(db.Boolean, default=False, nullable=False)
    email_confirmed_at = db.Column(db.DateTime, nullable=True)
    last_login_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    def __repr__(self):
        return f"<User {self.id} {self.email}>"

    def set_password(self, plaintext_password: str):
     
        if not plaintext_password or len(plaintext_password) < 10:
            raise ValueError("Password must be at least 10 characters long")

        # bcrypt generates its own salt; store the unicode string
        pw_hash = bcrypt.generate_password_hash(plaintext_password).decode("utf-8")
        self.password_hash = pw_hash

    def check_password(self, plaintext_password: str) -> bool:
        """
        Constant-time comparison using Bcrypt's check function.
        """
        if not self.password_hash:
            return False
        return bcrypt.check_password_hash(self.password_hash, plaintext_password)
    def generate_token(self, purpose: str, expires_sec: int = 3600) -> str:
        """
        purpose: e.g. 'password-reset' or 'email-confirm'
        expires_sec is used when verifying (itsdangerous doesn't embed expiry, but we will pass it to loads)
        """
        s = get_token_serializer()
        payload = {"user_id": self.id, "purpose": purpose}
        return s.dumps(payload)

    @staticmethod
    def verify_token(token: str, max_age: int = 3600):
        s = get_token_serializer()
        try:
            data = s.loads(token, max_age=max_age)
        except Exception:
            return None
        user_id = data.get("user_id")
        if not user_id:
            return None
        return User.query.get(user_id)

    #
    # Login/failure helpers
    #
    def record_failed_login(self, lock_threshold: int = 5, lock_minutes: int = 15):
        self.failed_login_count = (self.failed_login_count or 0) + 1
        if self.failed_login_count >= lock_threshold:
            self.locked_until = datetime.utcnow() + timedelta(minutes=lock_minutes)
        db.session.add(self)
        db.session.commit()

    def reset_failed_logins(self):
        self.failed_login_count = 0
        self.locked_until = None
        db.session.add(self)
        db.session.commit()

    def is_locked(self) -> bool:
        if self.locked_until and datetime.utcnow() < self.locked_until:
            return True
        return False
    
    def is_client(self) -> bool:
        return (self.user_type or "").lower() == "client"
    
class Invoices(db.Model):
    __tablename__ = "invoices"

    id = db.Column(db.Integer, primary_key=True)
    business_id = db.Column(db.String(150), nullable=True)

    # IRN lifecycle
    irn = db.Column(db.String(150), nullable=True, index=True)
    irn_status = db.Column(db.String(32), nullable=True)   # e.g. 'draft','submitted','validated','rejected'
    irn_issued_at = db.Column(db.DateTime, nullable=True)  # when FIRS returned IRN
    csid = db.Column(db.String(255), nullable=True)        # cryptographic stamp identifier returned by FIRS
    signature_value = db.Column(db.Text, nullable=True)    # if you store base64 signature returned/used

    # timestamps
    submitted_at = db.Column(db.DateTime, nullable=True)   # when you submit signed payload to FIRS
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # existing fields...
    issue_date = db.Column(db.Date, default=date.today, nullable=False)
    due_date = db.Column(db.Date, default=date.today, nullable=False)
    issue_time = db.Column(db.Time, default=lambda: datetime.utcnow().time(), nullable=False)
    invoice_type_code = db.Column(db.Integer, nullable=True)
    payment_status   = db.Column(db.String(64), nullable=True)
    note = db.Column(db.String(150), nullable=True)
    tax_point_date = db.Column(db.Date, default=date.today, nullable=False)
    document_currency_code = db.Column(db.String(8), nullable=True)
    tax_currency_code = db.Column(db.String(8), nullable=True)
    accounting_cost = db.Column(db.Integer, nullable=True)
    buyer_reference = db.Column(db.String(150), nullable=True)

    # delivery period
    delivery_start_date = db.Column(db.Date, nullable=True)
    delivery_end_date   = db.Column(db.Date, nullable=True)

    order_reference = db.Column(db.String(150), nullable=True)

    # Document refs (JSON arrays) - keep as you had
    billing_reference = db.Column(MutableList.as_mutable(db.JSON), nullable=True, default=list)
    dispatch_document_reference = db.Column(MutableList.as_mutable(db.JSON), nullable=True, default=list)
    receipt_document_reference = db.Column(MutableList.as_mutable(db.JSON), nullable=True, default=list)
    originator_document_reference = db.Column(MutableList.as_mutable(db.JSON), nullable=True, default=list)
    contract_document_reference = db.Column(MutableList.as_mutable(db.JSON), nullable=True, default=list)
    additional_document_reference = db.Column(MutableList.as_mutable(db.JSON), nullable=True, default=list)

    # Parties (JSON objects)
    accounting_customer_party = db.Column(MutableDict.as_mutable(db.JSON), nullable=True, default=dict)
    accounting_supplier_party = db.Column(MutableDict.as_mutable(db.JSON), nullable=False, default=dict)
    payee_party = db.Column(MutableDict.as_mutable(db.JSON), nullable=True, default=dict)
    bill_party = db.Column(MutableDict.as_mutable(db.JSON), nullable=True, default=dict)
    ship_party = db.Column(MutableDict.as_mutable(db.JSON), nullable=True, default=dict)
    tax_representative_party = db.Column(MutableDict.as_mutable(db.JSON), nullable=True, default=dict)

    # actual delivery date
    actual_delivery_date = db.Column(db.Date, nullable=True)

    # payment & other JSON arrays/objects
    payment_means = db.Column(MutableList.as_mutable(db.JSON), nullable=True, default=list)
    payment_terms_note = db.Column(db.String(1024), nullable=True)
    allowance_charge = db.Column(MutableList.as_mutable(db.JSON), nullable=True, default=list)
    tax_total = db.Column(MutableList.as_mutable(db.JSON), nullable=False, default=list)
    legal_monetary_total = db.Column(MutableDict.as_mutable(db.JSON), nullable=False, default=dict)
    invoice_line = db.Column(MutableList.as_mutable(db.JSON), nullable=False, default=list)
    tax_percent = db.Column(db.Float, nullable=True, default=0.0)
    submissions = db.relationship("InvoiceSubmission", backref="invoice", lazy=True)

    # DB-level constraints: ensure delivery_end_date >= delivery_start_date when both present
    __table_args__ = (
        CheckConstraint(
            "(delivery_start_date IS NULL) OR (delivery_end_date IS NULL) OR (delivery_end_date >= delivery_start_date)",
            name="ck_delivery_end_after_start"
        ),
    )
    def supplier_tin(self) -> str:
        """Convenience accessor: supplier TIN (top-level string)."""
        # Fallback: if accounting_supplier_party stores tin under different key, adapt here
        return (self.accounting_supplier_party or {}).get("tin") or (self.accounting_supplier_party or {}).get("tax_id") or None

    def to_firs_canonical(self) -> str:
        """
        Produce a canonical JSON string used for submission and for creating the QR payload.
        This is a compact JSON (no whitespace). Adapt keys/order to match FIRS exact schema if needed.
        """
        payload = {
            # NOTE: FIRS may expect specific key names (IRN generation uses the invoice JSON schema).
            # The QR payload is smaller — see make_firs_qr_payload() below. This canonical payload is
            # a minimal representation — extend to full invoice schema before signing/submission.
            "irn": self.irn or None,
            "issue_date": self.issue_date.isoformat() if self.issue_date else None,
            "issue_time": self.issue_time.isoformat() if self.issue_time else None,
            "supplier_tin": self.supplier_tin(),
            "buyer_tin": (self.accounting_customer_party or {}).get("tin"),
            "legal_monetary_total": self.legal_monetary_total or {},
            "invoice_line": self.invoice_line or [],
            # Add other FIRS-mandatory fields here when you implement full payload
        }
        # deterministic compact JSON
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)

    def make_firs_qr_payload_dict(self):
        return {
            "irn": self.irn or "SAMPLE-IRN-000000",
            "issue_date": self.issue_date.isoformat() if self.issue_date else date.today().isoformat(),
            "seller_tin": (self.accounting_supplier_party or {}).get("tin"),
            "total_amount": (self.legal_monetary_total or {}).get("payable_amount", 0),
            "currency": self.document_currency_code or "NGN",
            "csid": getattr(self, "csid", None)
            }
    
    def make_firs_qr_payload_text(self):
        return json.dumps(self.make_firs_qr_payload_dict(), separators=(",", ":"), ensure_ascii=False)
    
    def make_qr_datauri(self, box_size:int=6, border:int=4) -> str:
        """Return data:image/png;base64,... for embedding in HTML/PDF"""
        txt = self.make_firs_qr_payload_text()
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=box_size, border=border)
        qr.add_data(txt)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return f"data:image/png;base64,{b64}"

    # ----------------- Validators -----------------
    def _validate_irn_date_list(self, value, field_name):
        """Helper to validate lists of {irn, issue_date}"""
        if value is None:
            return value
        if not isinstance(value, list):
            raise ValueError(f"{field_name} must be a list")
        for entry in value:
            if not isinstance(entry, dict):
                raise ValueError(f"Each entry in {field_name} must be an object")
            if "irn" not in entry or not isinstance(entry["irn"], str):
                raise ValueError(f"Each {field_name} entry must include 'irn' as string")
            if "issue_date" not in entry:
                raise ValueError(f"Each {field_name} entry must include 'issue_date'")
            # validate issue_date format or date object
            if isinstance(entry["issue_date"], str):
                try:
                    date.fromisoformat(entry["issue_date"])
                except ValueError:
                    raise ValueError(f"Invalid issue_date format in {field_name}: {entry['issue_date']}")
            elif not isinstance(entry["issue_date"], date):
                raise ValueError(f"issue_date in {field_name} must be YYYY-MM-DD string or date object")
        return value

    @validates(
        "billing_reference", "dispatch_document_reference", "receipt_document_reference",
        "originator_document_reference", "contract_document_reference", "additional_document_reference"
    )
    def validate_document_refs(self, key, value):
        return self._validate_irn_date_list(value, key)

    @validates("payment_means")
    def validate_payment_means(self, key, value):
        if value is None:
            return value
        if not isinstance(value, list):
            raise ValueError("payment_means must be a list")
        for p in value:
            if not isinstance(p, dict):
                raise ValueError("Each payment_means entry must be an object")
            if "payment_means_code" not in p or not isinstance(p["payment_means_code"], int):
                raise ValueError("payment_means entries must include integer 'payment_means_code'")
            if "payment_due_date" in p:
                if isinstance(p["payment_due_date"], str):
                    try:
                        date.fromisoformat(p["payment_due_date"])
                    except ValueError:
                        raise ValueError("payment_due_date must be YYYY-MM-DD")
                elif not isinstance(p["payment_due_date"], date):
                    raise ValueError("payment_due_date must be YYYY-MM-DD string or date")
        return value

    @validates("allowance_charge")
    def validate_allowance_charge(self, key, value):
        if value is None:
            return value
        if not isinstance(value, list):
            raise ValueError("allowance_charge must be a list")
        for item in value:
            if not isinstance(item, dict):
                raise ValueError("Each allowance_charge entry must be an object")
            if "charge_indicator" not in item or not isinstance(item["charge_indicator"], bool):
                raise ValueError("Each allowance_charge entry must include boolean 'charge_indicator'")
            if "amount" not in item or not (isinstance(item["amount"], int) or isinstance(item["amount"], float)):
                raise ValueError("Each allowance_charge entry must include numeric 'amount'")
        return value

    @validates("tax_total")
    def validate_tax_total(self, key, value):
        if value is None or not isinstance(value, list):
            raise ValueError("tax_total must be a non-null list")
        for t in value:
            if not isinstance(t, dict):
                raise ValueError("Each tax_total entry must be an object")
            if "tax_amount" not in t:
                raise ValueError("Each tax_total entry must include 'tax_amount'")
            if "tax_subtotal" not in t or not isinstance(t["tax_subtotal"], list):
                raise ValueError("Each tax_total entry must include 'tax_subtotal' as list")
            for st in t["tax_subtotal"]:
                if not isinstance(st, dict):
                    raise ValueError("tax_subtotal entries must be objects")
                if "taxable_amount" not in st or "tax_amount" not in st or "tax_category" not in st:
                    raise ValueError("tax_subtotal entry must include taxable_amount, tax_amount and tax_category")
                tc = st["tax_category"]
                if not isinstance(tc, dict) or "id" not in tc or "percent" not in tc:
                    raise ValueError("tax_category must be object with 'id' and 'percent'")
        return value

    @validates("legal_monetary_total")
    def validate_legal_monetary_total(self, key, value):
        if not isinstance(value, dict):
            raise ValueError("legal_monetary_total must be an object")
        required = ["line_extension_amount", "tax_exclusive_amount", "tax_inclusive_amount", "payable_amount"]
        for r in required:
            if r not in value:
                raise ValueError(f"legal_monetary_total missing required field {r}")
            if not (isinstance(value[r], int) or isinstance(value[r], float)):
                raise ValueError(f"{r} must be numeric")
        return value

    @validates("invoice_line")
    def validate_invoice_line(self, key, value):
        if value is None or not isinstance(value, list):
            raise ValueError("invoice_line must be a non-null list")
        for ln in value:
            if not isinstance(ln, dict):
                raise ValueError("Each invoice_line entry must be an object")
            if not ("description" in ln or "line_id" in ln):
                raise ValueError("Each invoice_line must have 'description' or 'line_id'")
            if "quantity" in ln and not (isinstance(ln["quantity"], int) or isinstance(ln["quantity"], float)):
                raise ValueError("invoice_line.quantity must be numeric")
            if "price" in ln and not (isinstance(ln["price"], int) or isinstance(ln["price"], float)):
                raise ValueError("invoice_line.price must be numeric")
        return value

    # ----------------- Properties to return canonical JSON -----------------
    def _format_irn_date_list(self, value):
        if not value:
            return []
        formatted = []
        for ref in value:
            formatted.append({
                "irn": ref.get("irn"),
                "issue_date": (
                    ref["issue_date"].isoformat()
                    if isinstance(ref.get("issue_date"), date) else ref.get("issue_date")
                )
            })
        return formatted

    @property
    def invoice_billing_reference(self):
        return self._format_irn_date_list(self.billing_reference)

    @property
    def invoice_dispatch_document_reference(self):
        return self._format_irn_date_list(self.dispatch_document_reference)

    @property
    def invoice_receipt_document_reference(self):
        return self._format_irn_date_list(self.receipt_document_reference)

    @property
    def invoice_originator_document_reference(self):
        return self._format_irn_date_list(self.originator_document_reference)

    @property
    def invoice_contract_document_reference(self):
        return self._format_irn_date_list(self.contract_document_reference)

    @property
    def invoice_additional_document_reference(self):
        return self._format_irn_date_list(self.additional_document_reference)

    @property
    def invoice_payment_means(self):
        if not self.payment_means:
            return []
        out = []
        for p in self.payment_means:
            out.append({
                "payment_means_code": p.get("payment_means_code"),
                "payment_due_date": (
                    p["payment_due_date"].isoformat()
                    if isinstance(p.get("payment_due_date"), date) else p.get("payment_due_date")
                )
            })
        return out

    @property
    def invoice_allowance_charge(self):
        return self.allowance_charge or []

    @property
    def invoice_tax_total(self):
        return self.tax_total or []

    @property
    def invoice_legal_monetary_total(self):
        return self.legal_monetary_total or {}

    @property
    def invoice_invoice_line(self):
        return self.invoice_line or []

    @property
    def invoice_accounting_customer_party(self):
        return self.accounting_customer_party or {}

    @property
    def invoice_accounting_supplier_party(self):
        return self.accounting_supplier_party or {}

    @property
    def invoice_payee_party(self):
        return self.payee_party or {}

    @property
    def invoice_bill_party(self):
        return self.bill_party or {}

    @property
    def invoice_ship_party(self):
        return self.ship_party or {}

    @property
    def invoice_tax_representative_party(self):
        return self.tax_representative_party or {}
    
    @property
    def tax_amount(self):
        if not self.invoice_line:
            return 0.0
        return sum(float(line.get("fee_amount", 0)) for line in self.invoice_line)
    
    @property
    def taxable_amount(self):
        if not self.invoice_line:
            return 0.0
        return sum(float(line.get("fee_amount", 0)) for line in self.invoice_line)
    




def generate_token(nbytes=24):
    return base64.urlsafe_b64encode(os.urandom(nbytes)).decode("ascii").rstrip("=")

class Business(db.Model):
    __tablename__ = "businesses"
    id = Column(Integer, primary_key=True)
    buisness_id = Column(String(255), nullable=False)
    name = Column(String(255), nullable=False)
    tin = Column(String(64), nullable=True, index=True)
    service_id = Column(String(16), nullable=True, unique=True)  # FIRS-assigned
    api_key = Column(String(64), nullable=False, unique=True, index=True)   # public id
    api_secret_hmac = Column(String(128), nullable=False)  # HMAC of secret or store secret hashed
    callback_url = Column(String(1024), nullable=True)
    certificate_pem = Column(Text, nullable=True)  # public cert for this business (optional)
    firs_certificate = Column(Text, nullable=True)
    private_key_enc = Column(Text, nullable=True)  # if storing private key (encrypt strongly)
    firs_api_key = db.Column(db.String(255), nullable=True)
    firs_api_secret = db.Column(db.String(255), nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    # Which partner company referred / onboarded this business (nullable — direct clients have None)
    referred_by_partner_id = Column(Integer, nullable=True, index=True)
    # Billing / rate-limit tier: 'starter' | 'professional' | 'enterprise'
    tier = Column(String(16), nullable=True)  # NULL treated as 'starter'

    @classmethod
    def create_with_credentials(cls, name, tin=None, service_id=None, secret_key=None):
        from flask import current_app
        sk = secret_key or current_app.config.get("API_SECRET_KEY")
        if not sk:
            raise RuntimeError("API_SECRET_KEY not configured (provide secret_key or set config)")

        api_key = generate_token(18)
        secret = generate_token(32)  # return this raw secret to user once
        secret_hmac = hmac.new(sk.encode(), secret.encode(), hashlib.sha256).hexdigest()

        b = cls(name=name, tin=tin, service_id=service_id, api_key=api_key, api_secret_hmac=secret_hmac)
        db.session.add(b)
        db.session.commit()
        return b, secret

    def verify_secret(self, provided_secret):
        from flask import current_app
        import hmac
        import hashlib
        hmac_key = current_app.config.get("API_SECRET_KEY") or current_app.config.get("SECRET_KEY")
        if not hmac_key:
            return False
        candidate_hmac = hmac.new(hmac_key.encode(), provided_secret.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(self.api_secret_hmac or "", candidate_hmac)

class InvoiceSubmission(db.Model):
    __tablename__ = "invoice_submissions"
    id = Column(Integer, primary_key=True)
    business_id = Column(Integer, db.ForeignKey("businesses.id"), nullable=False)
    invoice_id = Column(Integer, db.ForeignKey("invoices.id"), nullable=True)  # local invoice record if linked
    payload = Column(db.JSON, nullable=False)  # raw payload submitted by ERP (JSON)
    status = Column(String(50), default="received")  # received / validating / validated / queued / transmitted / failed
    error = Column(Text, nullable=True)
    irn_candidate = Column(String(128), nullable=True)
    firs_irn = Column(String(128), nullable=True)   # IRN returned by FIRS
    firs_csid = Column(String(128), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    login_done = db.Column(db.Boolean, default=False)
    irn_validated = db.Column(db.Boolean, default=False)
    invoice_validated = db.Column(db.Boolean, default=False)
    health_check_done = db.Column(db.Boolean, default=False)
    tin_lookup_done = db.Column(db.Boolean, default=False)

class AuditLog(db.Model):
    __tablename__ = "audit_logs"
    id = Column(Integer, primary_key=True)
    business_id = Column(Integer, db.ForeignKey("businesses.id"), nullable=True)
    invoice_submission_id = Column(Integer, db.ForeignKey("invoice_submissions.id"), nullable=True)
    action = Column(String(128), nullable=False)
    detail = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class SubmissionLog(db.Model):
    __tablename__ = "submission_logs"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer)
    submission_id = db.Column(db.Integer, db.ForeignKey("invoice_submissions.id"), nullable=False)
    level = db.Column(db.String(20), default="INFO")  # INFO, WARNING, ERROR
    message = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    submission = db.relationship("InvoiceSubmission", backref="logs")

class Complaint(db.Model):
    __tablename__ = 'complaints'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer)
    title = db.Column(db.String(200), nullable=False)
    category = db.Column(db.String(150), nullable=False)
    description = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(32), default='open')  # new / open / in_progress / escalated / resolved / closed
    priority = db.Column(db.String(16), default='normal')  # low/normal/high/critical
    created_by = db.Column(db.String(100), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    # Business that raised the ticket (Integer FK to businesses.id, kept loose to avoid cross-model import)
    business_id = db.Column(db.Integer, nullable=True, index=True)
    # Set when the ticket is auto-routed to a partner (FK to partner_companies.id)
    routed_to_partner_id = db.Column(db.Integer, nullable=True)

class TierConfig(db.Model):
    """Admin-configurable limits for each business and partner tier."""
    __tablename__ = 'tier_configs'

    id           = db.Column(db.Integer, primary_key=True)
    tier_type    = db.Column(db.String(16), nullable=False)  # 'business' or 'partner'
    tier_name    = db.Column(db.String(32), nullable=False)  # e.g. 'starter', 'professional'
    display_name = db.Column(db.String(64), nullable=False)
    # Business-tier limits
    rate_per_min      = db.Column(db.Integer, default=60)    # API requests per minute
    daily_invoice_cap = db.Column(db.Integer, default=500)   # -1 = unlimited
    # Partner-tier limits
    max_businesses    = db.Column(db.Integer, default=10)    # -1 = unlimited
    sla_hours         = db.Column(db.Integer, default=48)    # first-response SLA in hours
    updated_at        = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint('tier_type', 'tier_name', name='uq_tier_type_name'),)


# Default seed data — inserted by _migrate_schema() if the table is empty
TIER_DEFAULTS = [
    # Business tiers
    dict(tier_type='business', tier_name='starter',      display_name='Starter',      rate_per_min=60,    daily_invoice_cap=500,   max_businesses=0,  sla_hours=48),
    dict(tier_type='business', tier_name='professional', display_name='Professional', rate_per_min=300,   daily_invoice_cap=5000,  max_businesses=0,  sla_hours=8),
    dict(tier_type='business', tier_name='enterprise',   display_name='Enterprise',   rate_per_min=1000,  daily_invoice_cap=-1,    max_businesses=0,  sla_hours=4),
    # Partner tiers
    dict(tier_type='partner',  tier_name='registered',   display_name='Registered',   rate_per_min=0,     daily_invoice_cap=0,     max_businesses=10, sla_hours=48),
    dict(tier_type='partner',  tier_name='certified',    display_name='Certified',    rate_per_min=0,     daily_invoice_cap=0,     max_businesses=-1, sla_hours=8),
    dict(tier_type='partner',  tier_name='premier',      display_name='Premier',      rate_per_min=0,     daily_invoice_cap=0,     max_businesses=-1, sla_hours=4),
]


class Document(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    original_filename = db.Column(db.String(260), nullable=False)
    stored_filename = db.Column(db.String(260), nullable=False, unique=True)  # what we save on disk
    mimetype = db.Column(db.String(120))
    is_public = db.Column(db.Boolean, default=False, nullable=False)
    uploaded_by_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    uploaded_by = db.relationship('User', backref='documents')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
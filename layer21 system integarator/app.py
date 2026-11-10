import os
from datetime import timedelta
from flask import Flask, render_template, request, redirect, flash, url_for, current_app, abort,session, json, jsonify, send_file, g, Blueprint, send_from_directory, render_template_string
from flask_mail import Mail, Message
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from dotenv import load_dotenv
from models import  db, bcrypt, User, Invoices, Business, InvoiceSubmission, Complaint, SubmissionLog, get_token_serializer, TierConfig, TIER_DEFAULTS
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy import or_, func, desc
from datetime import datetime, timedelta,date,time, timezone
import json
import qrcode
from io import BytesIO
import base64
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
import requests
from jinja2 import Environment, FileSystemLoader
from weasyprint import HTML
from typing import Union
import re
import xml.etree.ElementTree as ET
import secrets, hmac, hashlib
from models_partner import PartnerCompany, PartnerAgent, PartnerTicket
from routes_firs import firs_bp
from webhook_receiver import webhook_bp 
from documents_routes import bp
import ssl
import smtplib
from email.message import EmailMessage
from email.utils import parseaddr
import unicodedata
from helpers import (
    make_irn_qr_from_business,
    qr_png_datauri_from_base64_text,generate_irn,   # optional, if you want helper for data-uri creation
    encrypt_irn_with_public_key,       # optionally used directly
    sign_payload_rsa_b64,              # use this instead of your local sign_payload_rsa
)
import logging
from collections import defaultdict
from math import ceil
from typing import Optional
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_talisman import Talisman
from functools import wraps
from email_templates import NEW_TICKET_TEMPLATE, NEW_TICKET_CUSTOMER_TEMPLATE
from routes_partner import partner_bp
from routes_admin_partner import admin_partner_bp
from routes_odoo import odoo_bp
from routes_sage_evolution import sage_evolution_bp

logger = logging.getLogger(__name__)
base_dir = os.path.dirname(os.path.abspath(__file__))

# Build full path to the .env file in the same directory
dotenv_path = os.path.join(base_dir, ".env")

# Load it
load_dotenv(dotenv_path=dotenv_path)
app = Flask(__name__, instance_relative_config=False)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'change-this-in-production')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///database.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('SESSION_COOKIE_SECURE', 'True') == 'True'
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['REMEMBER_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = os.environ.get('SESSION_COOKIE_SAMESITE', 'Lax')
db.init_app(app)
bcrypt.init_app(app)

    # === Mail configuration ===
app.config['MAIL_SERVER'] = os.environ.get('MAIL_SERVER', 'smtp.gmail.com')
app.config['MAIL_PORT'] = int(os.environ.get('MAIL_PORT', 587))
app.config['MAIL_USE_TLS'] = os.environ.get('MAIL_USE_TLS', 'True') == 'True'
app.config['MAIL_USE_SSL'] = os.environ.get('MAIL_USE_SSL', 'False') == 'True'
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME')   # e.g. 'you@example.com'
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD')   # app password or SMTP credential
app.config['MAIL_DEFAULT_SENDER'] = os.environ.get('MAIL_DEFAULT_SENDER', app.config['MAIL_USERNAME'])   
app.config['FIRS_SERVICE_ID'] = os.environ.get('FIRS_SERVICE_ID') 
app.config['FIRS_BUSINESS_ID'] = os.environ.get('TEST_BUSINESS_ID')
app.config['FIRS_API_BASE'] = os.environ.get('FIRS_API_BASE', app.config.get('FIRS_API_BASE'))
app.config['FIRS_API_KEY'] = os.environ.get('FIRS_API_KEY')
app.config['FIRS_API_SECRET'] = os.environ.get('FIRS_API_SECRET')
app.config['API_KEY'] = os.environ.get('API_KEY')
app.config['API_SECRET'] = os.environ.get('API_SECRET')
app.config['FIRS_TEST_MODE'] = os.environ.get('FIRS_TEST_MODE', 'True') == 'True'
app.config['FIRS_AUTO_SUBMIT'] = os.environ.get('FIRS_AUTO_SUBMIT', 'True') == 'True'
app.config['FIRS_REQUEST_TIMEOUT'] = int(os.environ.get('FIRS_REQUEST_TIMEOUT', 30))
app.config['FIRS_DEFAULT_SUPPLIER_ADDRESS'] = os.environ.get('FIRS_DEFAULT_SUPPLIER_ADDRESS') 
app.config['FIRS_QR_ENCRYPTED'] = os.environ.get('FIRS_QR_ENCRYPTED')
app.config['FIRS_AUTO_ACK'] = True
mail = Mail(app)
app.register_blueprint(firs_bp, url_prefix='/api/v1')
app.register_blueprint(webhook_bp)
app.register_blueprint(bp)
app.register_blueprint(partner_bp)
app.register_blueprint(admin_partner_bp)
app.register_blueprint(odoo_bp, url_prefix='/api/v1/odoo')
app.register_blueprint(sage_evolution_bp, url_prefix='/api/v1/sage-evolution')
    # Login manager
login_manager = LoginManager()
login_manager.login_view = 'login'
login_manager.init_app(app)
GENERATE_IRN_PATH = "/api/generate-irn"
IRN_INVOICE_NO_RE = re.compile(r"^[A-Z0-9]+$")        # uppercase alnum, no special chars
IRN_SERVICE_ID_RE  = re.compile(r"^[A-Z0-9]{8}$")
FIRS_HOST = os.environ.get("FIRS_HOST", "https://einvoice.firs.gov.ng")
FIRS_GENERATE_IRN_PATH = os.environ.get("FIRS_GENERATE_IRN_PATH", "/api/v1/invoice/generate")  # set per your docs
FIRS_TRANSMIT_PATH = os.environ.get("FIRS_TRANSMIT_PATH", "/api/v1/invoice/transmit")  # will be used as /.../transmit/{irn}
FIRS_TEST_MODE = os.environ.get("FIRS_TEST_MODE", "1") == "1"  # if true, simulate FIRS responses when keys not present

limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://"  # Or use Redis: "redis://localhost:6379"
)

Talisman(app,
    force_https=False,
    strict_transport_security=True,
    strict_transport_security_max_age=31536000,
    content_security_policy={
        'default-src': ["'self'"],

        'script-src': [
            "'self'",
            "'unsafe-inline'",
            "cdn.tailwindcss.com",
            "unpkg.com"
        ],

        'style-src': [
            "'self'",
            "'unsafe-inline'",
            "fonts.googleapis.com",
            "cdn.jsdelivr.net",
            "unpkg.com"
        ],

        'font-src': [
            "'self'",
            "fonts.gstatic.com",
            "cdn.jsdelivr.net"
        ],

        'img-src': [
            "'self'",
            "data:"
        ],
    },
)

@app.errorhandler(403)
def forbidden(e):
    return render_template('403.html'), 403

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):

        if not current_user.is_authenticated:
            abort(401)

        if not current_user.is_admin:
            abort(403)

        return f(*args, **kwargs)

    return decorated_function

def client_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):

        if not current_user.is_authenticated:
            abort(401)

        if not current_user.is_client():
            abort(403)

        return f(*args, **kwargs)

    return wrapper

_HEADER_UNSAFE = re.compile(r'[\r\n\x00]')
 
 
def _is_safe_header(value: str) -> bool:
    """Reject values that could inject extra mail headers."""
    return not _HEADER_UNSAFE.search(value)
 
 
def _is_likely_email(value: str) -> bool:
    return bool(re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', value))
 
 
def _normalise(value) -> str:
    """Strip, normalise unicode, collapse internal whitespace."""
    s = unicodedata.normalize('NFKC', str(value or '')).strip()
    return re.sub(r'\s+', ' ', s)

@login_manager.user_loader
def load_user(user_id):
    try:
        return User.query.get(int(user_id))
    except Exception:
        return None

@app.before_request
def validate_host():
    """Validate Host header to prevent host header injection"""
    allowed_hosts = ['app.layer21.com', 'appstaging.layer21.com', 'layer21.com', 'localhost', '127.0.0.1:5000', '127.0.0.1:8000']
    if request.host not in allowed_hosts:
        abort(400, 'Invalid Host header')

@app.context_processor
def inject_now():
    from datetime import datetime
    return {"current_year": datetime.utcnow().year}  # Direct value

def compute_firs_stats(session, total_invoices):
    """
    Compute FIRS stats (counts by final InvoiceSubmission.status) in a robust way.
    Returns a dict:
      {
        "firs_success_count": int,
        "firs_failed_count": int,
        "firs_validated": int,
        "firs_unknown": int,
        "debug": { ... }   # diagnostic numbers to help debugging
      }
    """
    # normalize lists
    success_statuses = {"transmitted", "signed", "issued"}
    failed_statuses = {"failed", "sign_failed", "validate_failed", "transmit_failed"}
    validated_statuses = {"validated"}

    result = {
        "firs_success_count": 0,
        "firs_failed_count": 0,
        "firs_validated": 0,
        "firs_unknown": 0,
        "debug": {}
    }

    try:
        # --- Efficient SQL method (preferred) ---
        last_subq = (
            session.query(
                InvoiceSubmission.invoice_id.label("invoice_id"),
                func.max(InvoiceSubmission.id).label("last_submission_id"),
            )
            .filter(InvoiceSubmission.invoice_id != None)
            .group_by(InvoiceSubmission.invoice_id)
            .subquery()
        )

        # count by joining back to InvoiceSubmission and checking status (use func.lower for safety)
        success_q = (
            session.query(func.count(InvoiceSubmission.id))
            .join(last_subq, InvoiceSubmission.id == last_subq.c.last_submission_id)
            .filter(func.lower(InvoiceSubmission.status).in_(list(success_statuses)))
        )
        failed_q = (
            session.query(func.count(InvoiceSubmission.id))
            .join(last_subq, InvoiceSubmission.id == last_subq.c.last_submission_id)
            .filter(func.lower(InvoiceSubmission.status).in_(list(failed_statuses)))
        )
        validated_q = (
            session.query(func.count(InvoiceSubmission.id))
            .join(last_subq, InvoiceSubmission.id == last_subq.c.last_submission_id)
            .filter(func.lower(InvoiceSubmission.status).in_(list(validated_statuses)))
        )

        s_count = int(success_q.scalar() or 0)
        f_count = int(failed_q.scalar() or 0)
        v_count = int(validated_q.scalar() or 0)

        checked = s_count + f_count + v_count
        unknown = max(0, int(total_invoices or 0) - checked)

        result.update({
            "firs_success_count": s_count,
            "firs_failed_count": f_count,
            "firs_validated": v_count,
            "firs_unknown": unknown,
            "debug": {
                "method": "sql",
                "checked": checked,
                "total_invoices": int(total_invoices or 0),
                "s_count": s_count,
                "f_count": f_count,
                "v_count": v_count,
            }
        })
        return result

    except Exception as ex_sql:
        # log the SQL error and fall back
        logger.exception("compute_firs_stats: SQL method failed: %s", ex_sql)

    # --- Fallback: Python aggregation (memory-bounded using yield_per) ---
    try:
        latest_by_invoice = {}  # invoice_id -> (submission_id, status)
        # iterate submissions in batches (yield_per to cap memory)
        q = session.query(InvoiceSubmission.id, InvoiceSubmission.invoice_id, InvoiceSubmission.status).yield_per(1000)
        for sid, inv_id, status in q:
            if not inv_id:
                continue
            current = latest_by_invoice.get(inv_id)
            if current is None or sid > current[0]:
                latest_by_invoice[inv_id] = (sid, (status or "").strip().lower())

        s_count = f_count = v_count = 0
        for inv_id, (_sid, st) in latest_by_invoice.items():
            if st in success_statuses:
                s_count += 1
            elif st in failed_statuses:
                f_count += 1
            elif st in validated_statuses:
                v_count += 1
            else:
                # treat as unknown for now
                pass

        checked = s_count + f_count + v_count
        unknown = max(0, int(total_invoices or 0) - checked)

        result.update({
            "firs_success_count": s_count,
            "firs_failed_count": f_count,
            "firs_validated": v_count,
            "firs_unknown": unknown,
            "debug": {
                "method": "python_fallback",
                "distinct_invoices_seen": len(latest_by_invoice),
                "checked": checked,
                "total_invoices": int(total_invoices or 0),
                "s_count": s_count,
                "f_count": f_count,
                "v_count": v_count,
            }
        })
        return result

    except Exception as ex_py:
        logger.exception("compute_firs_stats: Python fallback failed: %s", ex_py)
        # Last resort: return zeros and debug info
        result["debug"] = {"method": "failed", "error_sql": str(ex_sql), "error_py": str(ex_py)}
        return result

def compute_bfirs_stats(session, total_invoices, business_id=None):
    """
    Compute FIRS stats for invoices filtered by business_id (if given).
    Returns dict with:
        firs_success_count, firs_failed_count, firs_validated, firs_unknown, debug
    """
    success_statuses = {"transmitted", "signed", "issued"}
    failed_statuses = {"failed", "sign_failed", "validate_failed", "transmit_failed"}
    validated_statuses = {"validated"}

    result = {
        "firs_success_count": 0,
        "firs_failed_count": 0,
        "firs_validated": 0,
        "firs_unknown": 0,
        "debug": {}
    }

    try:
        # --- Efficient SQL method (preferred) ---
        last_subq = (
            session.query(
                InvoiceSubmission.invoice_id.label("invoice_id"),
                func.max(InvoiceSubmission.id).label("last_submission_id")
            )
            .join(Invoices, Invoices.id == InvoiceSubmission.invoice_id)
            .filter(InvoiceSubmission.invoice_id != None)
        )

        # filter by business_id if provided
        if business_id:
            last_subq = last_subq.filter(Invoices.business_id == business_id)

        last_subq = last_subq.group_by(InvoiceSubmission.invoice_id).subquery()

        # count by joining back to InvoiceSubmission and checking status
        success_q = (
            session.query(func.count(InvoiceSubmission.id))
            .join(last_subq, InvoiceSubmission.id == last_subq.c.last_submission_id)
            .filter(func.lower(InvoiceSubmission.status).in_(list(success_statuses)))
        )

        failed_q = (
            session.query(func.count(InvoiceSubmission.id))
            .join(last_subq, InvoiceSubmission.id == last_subq.c.last_submission_id)
            .filter(func.lower(InvoiceSubmission.status).in_(list(failed_statuses)))
        )

        validated_q = (
            session.query(func.count(InvoiceSubmission.id))
            .join(last_subq, InvoiceSubmission.id == last_subq.c.last_submission_id)
            .filter(func.lower(InvoiceSubmission.status).in_(list(validated_statuses)))
        )

        s_count = int(success_q.scalar() or 0)
        f_count = int(failed_q.scalar() or 0)
        v_count = int(validated_q.scalar() or 0)
        checked = s_count + f_count + v_count
        unknown = max(0, int(total_invoices or 0) - checked)

        result.update({
            "firs_success_count": s_count,
            "firs_failed_count": f_count,
            "firs_validated": v_count,
            "firs_unknown": unknown,
            "debug": {
                "method": "sql",
                "checked": checked,
                "total_invoices": int(total_invoices or 0),
                "s_count": s_count,
                "f_count": f_count,
                "v_count": v_count,
                "business_id": business_id
            }
        })
        return result

    except Exception as ex_sql:
        logger.exception("compute_firs_stats: SQL method failed: %s", ex_sql)

    # --- Python fallback method ---
    try:
        latest_by_invoice = {}
        q = session.query(
            InvoiceSubmission.id,
            InvoiceSubmission.invoice_id,
            InvoiceSubmission.status,
            Invoices.business_id
        ).join(Invoices, Invoices.id == InvoiceSubmission.invoice_id).yield_per(1000)

        for sid, inv_id, status, inv_biz in q:
            if not inv_id or (business_id and inv_biz != business_id):
                continue
            current = latest_by_invoice.get(inv_id)
            if current is None or sid > current[0]:
                latest_by_invoice[inv_id] = (sid, (status or "").strip().lower())

        s_count = f_count = v_count = 0
        for _inv_id, (_sid, st) in latest_by_invoice.items():
            if st in success_statuses:
                s_count += 1
            elif st in failed_statuses:
                f_count += 1
            elif st in validated_statuses:
                v_count += 1

        checked = s_count + f_count + v_count
        unknown = max(0, int(total_invoices or 0) - checked)

        result.update({
            "firs_success_count": s_count,
            "firs_failed_count": f_count,
            "firs_validated": v_count,
            "firs_unknown": unknown,
            "debug": {
                "method": "python_fallback",
                "distinct_invoices_seen": len(latest_by_invoice),
                "checked": checked,
                "total_invoices": int(total_invoices or 0),
                "s_count": s_count,
                "f_count": f_count,
                "v_count": v_count,
                "business_id": business_id
            }
        })
        return result

    except Exception as ex_py:
        logger.exception("compute_firs_stats: Python fallback failed: %s", ex_py)
        result["debug"] = {"method": "failed", "error_sql": str(ex_sql), "error_py": str(ex_py)}
        return result

    # === Routes ===
    
@app.route('/')
def index():
    return render_template('index.html')

@app.route("/register", methods=["GET", "POST"])
@limiter.limit("3 per hour")
@login_required
@admin_required
def register():
    if request.method == "POST":
        first_name = (request.form.get("first_name") or "").strip()
        last_name = (request.form.get("last_name") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm_password") or ""
        business_id = request.form.get("business_id") or ""
        company_name = (request.form.get("company_name") or "").strip()
        user_type = request.form.get("user_type") or ""
        if not email or not username or not password or not confirm:
            flash("Please fill all required fields.", "danger")
            return render_template("register.html", form=request.form)
        if "@" not in email:
            flash("Please provide a valid email address.", "danger")
            return render_template("register.html", form=request.form)
        if password != confirm:
            flash("Passwords do not match.", "danger")
            return render_template("register.html", form=request.form)
        if len(password) < 10:
            flash("Password must be at least 10 characters long.", "danger")
            return render_template("register.html", form=request.form)
        existing = User.query.filter((User.email == email) | (User.username == username)).first()
        if existing:
            flash("A user with that email or username already exists.", "danger")
            return render_template("register.html", form=request.form)
        if user_type == 'admin':
            is_admin = True
        if user_type != 'admin':
            is_admin = False
        try:
            user = User(
                first_name=first_name,
                last_name=last_name,
                email=email,
                username=username,
                company_name=company_name,
                user_type=user_type,
                business_id=business_id,
                is_admin=is_admin
                )
            # Use model helper which does hashing and policy check
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash("Unable to create account. Please try again.", "danger")
            return render_template("register.html", form=request.form)
        except ValueError as ve:
            # set_password can raise for short passwords
            db.session.rollback()
            flash(str(ve), "danger")
            return render_template("register.html", form=request.form)
        except Exception:
            db.session.rollback()
            current_app.logger.exception("Failed creating user")
            flash("An unexpected error occurred. Try again later.", "danger")
            return render_template("register.html", form=request.form)
            # Generate email confirmation token and send email (best-effort)
        try:
            token = user.generate_token("email-confirm")
            confirm_url = url_for("verify_email", token=token, _external=True)
            msg = Message(
                subject="Confirm your Layer21 account",
                recipients=[user.email],
                body=f"Hi {user.first_name or user.username},\n\n"
                f"Thanks for registering. Click the link below to confirm your email address:\n\n{confirm_url}\n\n"
                "If you did not sign up, ignore this message."
                )
            mail.send(msg)
            flash("Account created. We sent a confirmation email — check your inbox.", "success")
        except Exception:
            current_app.logger.exception("Failed to send confirmation email")
            flash("Account created — but we couldn't send a confirmation email. Contact support.", "warning")
        return redirect(url_for("list_users"))
    # GET
    return render_template("register.html", form={})
    
@app.route("/verify-email/<token>")
def verify_email(token):
    user = User.verify_token(token, max_age=60 * 60 * 24)  # token valid 24 hours
    if not user:
        flash("Invalid or expired verification link.", "danger")
        return redirect(url_for("login"))
    # mark confirmed if not already
    if not user.email_confirmed:
        user.email_confirmed = True
        user.email_confirmed_at = db.func.now()
        db.session.add(user)
        db.session.commit()
        flash("Email confirmed — thank you! Please sign in.", "success")
    else:
        flash("Email already confirmed. Please sign in.", "info")
    return redirect(url_for("login"))


@app.route('/login', methods=['GET', 'POST'])
@limiter.limit("3 per minute")
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        remember = bool(request.form.get('remember'))

        if not username or not password:
            flash("Username and password are required.", "danger")
            return redirect(url_for("login"))

        # Case-insensitive lookup for username
        user = User.query.filter(db.func.lower(User.username) == username.lower()).first()

        if not user:
            flash('Incorrect login Information', 'danger')
            return redirect(url_for('login'))

        if user.is_locked():
            flash('Account locked temporarily due to failed sign-in attempts. Try again later.', 'warning')
            return redirect(url_for('login'))

        if user.check_password(password):
            user.reset_failed_logins()
            user.last_login_at = db.func.now()
            db.session.commit()
            login_user(user, remember=remember, duration=timedelta(days=30))
            session["user_id"] = user.id
            flash('Signed in successfully', 'success')
            if user.is_admin == True:
                return redirect(url_for('dashboard'))
            elif user.user_type == 'client':
                return redirect(url_for('client_dashboard'))
            elif user.user_type == 'partner':
                return redirect(url_for('partner_portal.dashboard'))
        else:
            user.record_failed_login()
            flash('Incorrect login Information', 'danger')
            return redirect(url_for('login'))

    return render_template('login.html')


def _send_reset_email(user, reset_url):
    cfg = current_app.config
    MAIL_SERVER   = cfg.get('MAIL_SERVER', 'smtp.gmail.com')
    MAIL_PORT     = int(cfg.get('MAIL_PORT', 587))
    MAIL_USE_SSL  = str(cfg.get('MAIL_USE_SSL', 'false')).lower() == 'true'
    MAIL_USE_TLS  = str(cfg.get('MAIL_USE_TLS', 'true')).lower() == 'true'
    MAIL_USERNAME = cfg.get('MAIL_USERNAME', '')
    MAIL_PASSWORD = cfg.get('MAIL_PASSWORD', '')
    MAIL_SENDER   = cfg.get('MAIL_DEFAULT_SENDER', MAIL_USERNAME)

    msg = EmailMessage()
    msg['Subject'] = 'Layer21 — Reset your password'
    msg['From']    = MAIL_SENDER
    msg['To']      = user.email
    msg.set_content(
        f"Hi {user.first_name or user.username},\n\n"
        f"We received a request to reset your Layer21 account password.\n\n"
        f"Click the link below to set a new password (expires in 30 minutes):\n\n"
        f"  {reset_url}\n\n"
        f"If you did not request a password reset, you can ignore this email.\n\n"
        f"— Layer21 Support\n"
        f"  info@layer21.com\n"
    )
    html = f"""<!doctype html>
<html><body style="font-family:Arial,sans-serif;background:#f0f4f8;padding:2rem">
<div style="max-width:520px;margin:auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 4px 20px rgba(0,0,0,.08)">
  <div style="background:#0b1220;padding:1.5rem 2rem;display:flex;align-items:center;gap:.75rem">
    <span style="font-family:Arial,sans-serif;font-weight:900;font-size:1rem;color:#2ec5db;letter-spacing:.05em">LAYER21</span>
  </div>
  <div style="padding:2rem">
    <h2 style="margin:0 0 .75rem;font-size:1.25rem;color:#0b1220">Password Reset Request</h2>
    <p style="color:#4b5563;line-height:1.6">Hi {user.first_name or user.username},</p>
    <p style="color:#4b5563;line-height:1.6">We received a request to reset your Layer21 account password. Click the button below to choose a new one. This link expires in <strong>30 minutes</strong>.</p>
    <div style="text-align:center;margin:2rem 0">
      <a href="{reset_url}" style="display:inline-block;padding:.85rem 2.5rem;background:#2ec5db;color:#041018;font-weight:700;font-size:.95rem;border-radius:10px;text-decoration:none">Reset My Password</a>
    </div>
    <p style="color:#9ca3af;font-size:.82rem;line-height:1.5">If you did not request a password reset, you can safely ignore this email — your password will remain unchanged.</p>
    <hr style="border:none;border-top:1px solid #e5e7eb;margin:1.5rem 0"/>
    <p style="color:#9ca3af;font-size:.75rem">Layer21 IT Services &nbsp;·&nbsp; <a href="mailto:info@layer21.com" style="color:#2ec5db">info@layer21.com</a></p>
  </div>
</div>
</body></html>"""
    msg.add_alternative(html, subtype='html')

    ssl_ctx = ssl.create_default_context()
    try:
        if MAIL_USE_SSL:
            with smtplib.SMTP_SSL(MAIL_SERVER, MAIL_PORT, timeout=30, context=ssl_ctx) as smtp:
                if MAIL_USERNAME and MAIL_PASSWORD:
                    smtp.login(MAIL_USERNAME, MAIL_PASSWORD)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(MAIL_SERVER, MAIL_PORT, timeout=30) as smtp:
                smtp.ehlo()
                if MAIL_USE_TLS:
                    smtp.starttls(context=ssl_ctx)
                    smtp.ehlo()
                if MAIL_USERNAME and MAIL_PASSWORD:
                    smtp.login(MAIL_USERNAME, MAIL_PASSWORD)
                smtp.send_message(msg)
        current_app.logger.info('Password reset email sent to %s', user.email)
    except Exception:
        current_app.logger.exception('Failed to send password reset email to %s', user.email)


@app.route('/forgot-password', methods=['POST'])
@limiter.limit("5 per hour")
def forgot_password():
    data       = request.get_json(silent=True) or request.form
    identifier = data.get('identifier', '').strip().lower()
    if not identifier:
        return jsonify(ok=False, error='Email or username is required.'), 400

    user = User.query.filter(
        or_(
            db.func.lower(User.email) == identifier,
            db.func.lower(User.username) == identifier
        )
    ).first()

    if user and user.is_active:
        token     = user.generate_token('password-reset', expires_sec=1800)
        reset_url = url_for('reset_password', token=token, _external=True)
        _send_reset_email(user, reset_url)

    # Always return success — prevents user enumeration
    return jsonify(ok=True), 202


@app.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    try:
        payload = get_token_serializer().loads(token, max_age=1800)
    except Exception:
        payload = None

    if not payload or payload.get('purpose') != 'password-reset':
        flash('This password reset link is invalid or has expired.', 'danger')
        return redirect(url_for('login'))

    user = User.query.get(payload['user_id'])
    if not user or not user.is_active:
        flash('Account not found or inactive.', 'danger')
        return redirect(url_for('login'))

    if request.method == 'POST':
        pw  = request.form.get('password', '')
        pw2 = request.form.get('confirm_password', '')
        if pw != pw2:
            flash('Passwords do not match.', 'danger')
            return render_template('reset_password.html', token=token)
        try:
            user.set_password(pw)
            user.failed_login_count = 0
            user.locked_until       = None
            db.session.commit()
            current_app.logger.info('Password reset completed for user %s', user.id)
            flash('Password updated — you can now sign in.', 'success')
            return redirect(url_for('login'))
        except ValueError as exc:
            flash(str(exc), 'danger')
            return render_template('reset_password.html', token=token)

    return render_template('reset_password.html', token=token)


@app.route("/portal/dashboard")
@login_required
@admin_required
def dashboard():
    # Pagination + filtering params from querystring (keep your existing behavior)
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 20, type=int)
    q = (request.args.get("q") or "").strip()
    status = (request.args.get("status") or "").strip()

    # Base query for list view (unchanged)
    query = Invoices.query
    if q:
        like = f"%{q}%"
        query = query.filter(
            or_(
                Invoices.irn.ilike(like),
                Invoices.buyer_reference.ilike(like),
                Invoices.note.ilike(like)
            )
        )
    if status:
        query = query.filter(Invoices.payment_status == status)
    query = query.order_by(Invoices.issue_date.desc(), Invoices.id.desc())
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    invoices = pagination.items
    total_count = pagination.total
    total_pages = pagination.pages

    # --- Dashboard stats (aggregate across all invoices) ---
    db = Invoices.query.session  # SQLAlchemy session

    # 1) Total invoices (fast)
    total_invoices = db.query(func.count(Invoices.id)).scalar() or 0

    # 2) Payment status counts (fast)
    paid_count = db.query(func.count(Invoices.id)).filter(Invoices.payment_status == "PAID").scalar() or 0
    pending_count = db.query(func.count(Invoices.id)).filter(
        (Invoices.payment_status == "PENDING") | (Invoices.payment_status == None)
    ).scalar() or 0
    overdue_count = db.query(func.count(Invoices.id)).filter(Invoices.payment_status == "OVERDUE").scalar() or 0

    # 3) Total revenue (try efficient DB sum, fallback to python safe sum)
    try:
        # resolve a usable SQLAlchemy session
        try:
            session = Invoices.query.session
        except Exception:
            session = db.session
        total_revenue = 0.0
        # 1) Prefer a real numeric column on the Invoices model named `payable_amount
        if hasattr(Invoices, "payable_amount"):
            total_revenue = float(
                session.query(func.coalesce(func.sum(getattr(Invoices, "payable_amount")), 0)).scalar() or 0.0)
        else:
            # 2) Fallback: iterate invoices in batches and extract payable_amount
            # # Use yield_per to keep memory use bounded.
            q = session.query(Invoices.id, Invoices.legal_monetary_total).yield_per(500)
            for inv_id, lm in q:
                if lm is None:
                    continue
                # lm may be a dict (JSON) or an object with attribute(s)
                pa = None
                # If JSON/dict-like
                if isinstance(lm, dict):
                    # support several possible keys/casing
                    pa = lm.get("payable_amount") or lm.get("payableAmount") or lm.get("payable") or 0
                else:
                    # object-like: try several attribute names
                    pa = getattr(lm, "payable_amount", None)
                    if pa is None:
                        pa = getattr(lm, "payableAmount", None)
                    if pa is None:
                        pa = getattr(lm, "payable", None)
                    if pa is None:
                        pa = 0
                try:
                    total_revenue += float(pa or 0)
                except Exception:
                    # skip bad values but continue scanning
                    continue
    except Exception:
        total_revenue = 0.0
    recent_revenue = 0.0
    try:
        if hasattr(Invoices, "payable_amount"):
            recent_revenue = float(
                 session.query(func.coalesce(func.sum(getattr(Invoices, "payable_amount")), 0))
                 .filter(Invoices.issue_date >= month_start)
                 .scalar() or 0.0
                 )
        else:
            q = session.query(Invoices.id, Invoices.legal_monetary_total).filter(
                Invoices.issue_date >= month_start
                ).yield_per(500)
            for inv_id, lm in q:
                if lm is None:
                    continue
                # lm may be a dict (JSON) or an object with attribute(s)
                pa = None
                # If JSON/dict-like
                if isinstance(lm, dict):
                    # support several possible keys/casing
                    pa = lm.get("payable_amount") or lm.get("payableAmount") or lm.get("payable") or 0
                else:
                    # object-like: try several attribute names
                    pa = getattr(lm, "payable_amount", None)
                    if pa is None:
                        pa = getattr(lm, "payableAmount", None)
                    if pa is None:
                        pa = getattr(lm, "payable", None)
                    if pa is None:
                        pa = 0
                try:
                    total_revenue += float(pa or 0)
                except Exception:
                    # skip bad values but continue scanning
                    continue
    except Exception:
        recent_revenue = 0.0

    # 4) Payment percentages (guard divide-by-zero)
    pct_paid = (paid_count / total_invoices * 100) if total_invoices else 0
    pct_pending = (pending_count / total_invoices * 100) if total_invoices else 0
    pct_overdue = (overdue_count / total_invoices * 100) if total_invoices else 0

    # --- FIRS submission-based stats (count by last submission status) ---
    # We want to take *the last submission per invoice* and classify it.
    # ensure total_invoices computed earlier
    stats_from_firs = compute_firs_stats(session, total_invoices)
    firs_success_count = stats_from_firs["firs_success_count"]
    firs_failed_count = stats_from_firs["firs_failed_count"]
    firs_validated = stats_from_firs["firs_validated"]
    firs_unknown = stats_from_firs["firs_unknown"]

    # 5) Recent revenue (this month)
    now = datetime.utcnow()
    month_start = datetime(now.year, now.month, 1)
    try:
        # try DB-side filter by invoice.issue_date if available
        recent_revenue = db.query(func.coalesce(func.sum(Invoices.payable_amount), 0)).filter(
            Invoices.issue_date >= month_start
        ).scalar() or 0.0
    except Exception:
        # fallback: compute from invoices list or zero
        recent_revenue = 0.0
        try:
            for inv in db.query(Invoices).filter(Invoices.issue_date >= month_start):
                lm = getattr(inv, "legal_monetary_total", None)
                if lm and getattr(lm, "payable_amount", None) is not None:
                    recent_revenue += float(lm.payable_amount or 0)
                else:
                    recent_revenue += float(getattr(inv, "payable_amount", 0) or 0)
        except Exception:
            recent_revenue = 0.0

    # Format/prepare values for template
    stats = {
        "total_invoices": int(total_invoices),
        "paid_count": int(paid_count),
        "pending_count": int(pending_count),
        "overdue_count": int(overdue_count),
        "total_revenue": float(total_revenue),
        "pct_paid": round(pct_paid, 1),
        "pct_pending": round(pct_pending, 1),
        "pct_overdue": round(pct_overdue, 1),
        "firs_success_count": int(firs_success_count),
        "firs_failed_count": int(firs_failed_count),
        "firs_validated_count": int(firs_validated),
        "firs_unknown": int(firs_unknown),
        "recent_revenue": float(recent_revenue),
    }

    return render_template(
        "dashboard.html",
        user=current_user,
        invoices=invoices,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
        total_count=total_count,
        q=q,
        status=status,
        stats=stats,
        pagination=pagination,
    )

@app.route("/portal/client/dashboard")
@login_required
@client_required
def client_dashboard():
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 20, type=int)
    q = (request.args.get("q") or "").strip()
    status = (request.args.get("status") or "").strip()

    business_id = current_user.business_id
    bus = Business.query.filter_by(buisness_id=business_id).first()

    # ---------------------------
    # Invoice list query
    # ---------------------------
    query = Invoices.query.filter(Invoices.business_id == business_id)

    if q:
        like = f"%{q}%"
        query = query.filter(
            or_(
                Invoices.irn.ilike(like),
                Invoices.buyer_reference.ilike(like),
                Invoices.note.ilike(like)
            )
        )

    if status:
        query = query.filter(Invoices.payment_status == status)

    query = query.order_by(Invoices.issue_date.desc(), Invoices.id.desc())

    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    invoices = pagination.items

    total_count = pagination.total
    total_pages = pagination.pages

    # ---------------------------
    # Stats (BUSINESS SCOPED)
    # ---------------------------
    session = Invoices.query.session

    base = session.query(Invoices).filter(Invoices.business_id == business_id)

    total_invoices = base.count()

    paid_count = base.filter(
        Invoices.payment_status == "PAID"
    ).count()

    pending_count = base.filter(
        or_(
            Invoices.payment_status == "PENDING",
            Invoices.payment_status == None
        )
    ).count()

    overdue_count = base.filter(
        Invoices.payment_status == "OVERDUE"
    ).count()

    # ---------------------------
    # Total revenue
    # ---------------------------
    total_revenue = 0.0

    if hasattr(Invoices, "payable_amount"):

        total_revenue = float(
            session.query(
                func.coalesce(func.sum(Invoices.payable_amount), 0)
            ).filter(
                Invoices.business_id == business_id
            ).scalar() or 0.0
        )

    else:

        q_revenue = session.query(
            Invoices.legal_monetary_total
        ).filter(
            Invoices.business_id == business_id
        ).yield_per(500)

        for lm in q_revenue:
            if lm and isinstance(lm, dict):
                total_revenue += float(lm.get("payable_amount", 0))

    # ---------------------------
    # Recent revenue (this month)
    # ---------------------------
    now = datetime.utcnow()
    month_start = datetime(now.year, now.month, 1)

    recent_revenue = 0.0

    if hasattr(Invoices, "payable_amount"):

        recent_revenue = float(
            session.query(
                func.coalesce(func.sum(Invoices.payable_amount), 0)
            ).filter(
                Invoices.business_id == business_id,
                Invoices.issue_date >= month_start
            ).scalar() or 0.0
        )

    else:

        q_recent = session.query(
            Invoices.legal_monetary_total
        ).filter(
            Invoices.business_id == business_id,
            Invoices.issue_date >= month_start
        ).yield_per(500)

        for lm in q_recent:
            if lm and isinstance(lm, dict):
                recent_revenue += float(lm.get("payable_amount", 0))

    # ---------------------------
    # Percentages
    # ---------------------------
    pct_paid = (paid_count / total_invoices * 100) if total_invoices else 0
    pct_pending = (pending_count / total_invoices * 100) if total_invoices else 0
    pct_overdue = (overdue_count / total_invoices * 100) if total_invoices else 0

    # ---------------------------
    # FIRS stats (BUSINESS SCOPED)
    # ---------------------------
    stats_from_firs = compute_bfirs_stats(session, total_invoices, business_id)

    # ---------------------------
    # Final stats object
    # ---------------------------
    stats = {
        "total_invoices": int(total_invoices),
        "paid_count": int(paid_count),
        "pending_count": int(pending_count),
        "overdue_count": int(overdue_count),
        "total_revenue": float(total_revenue),
        "pct_paid": round(pct_paid, 1),
        "pct_pending": round(pct_pending, 1),
        "pct_overdue": round(pct_overdue, 1),
        "firs_success_count": int(stats_from_firs["firs_success_count"]),
        "firs_failed_count": int(stats_from_firs["firs_failed_count"]),
        "firs_validated_count": int(stats_from_firs["firs_validated"]),
        "firs_unknown": int(stats_from_firs["firs_unknown"]),
        "recent_revenue": float(recent_revenue),
    }

    return render_template(
        "cdashboard.html",
        user=current_user,
        invoices=invoices,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
        total_count=total_count,
        q=q,
        status=status,
        stats=stats,
        pagination=pagination,
        bus=bus
    )


@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have been signed out.', 'info')
    return redirect(url_for('login'))

    
def _is_safe_header(s: str) -> bool:
    if s is None:
        return True
    return '\n' not in s and '\r' not in s

def _is_likely_email(addr: str) -> bool:
    if not addr:
        return False
    return '@' in parseaddr(addr)[1]

@app.route('/contact_sales', methods=['POST'])
@limiter.limit("10 per hour")
def contact_sales():
    cfg = current_app.config
 
    # ── read config (never hardcoded) ─────────────────────────────────────
    MAIL_SERVER          = cfg.get('MAIL_SERVER', 'localhost')
    MAIL_PORT            = int(cfg.get('MAIL_PORT', 465))
    MAIL_USE_SSL         = str(cfg.get('MAIL_USE_SSL', 'true')).lower() == 'true'
    MAIL_USE_TLS         = str(cfg.get('MAIL_USE_TLS', 'false')).lower() == 'true'
    MAIL_USERNAME        = cfg.get('MAIL_USERNAME', '')
    MAIL_PASSWORD        = cfg.get('MAIL_PASSWORD', '')
    MAIL_DEFAULT_SENDER  = cfg.get('MAIL_DEFAULT_SENDER', MAIL_USERNAME)
    SALES_EMAIL          = 'info@layer21.com'
 
    # ── parse + sanitise input ─────────────────────────────────────────────
    data            = request.get_json(silent=True) or request.form
    business        = _normalise(data.get('business_name'))
    contact_person  = _normalise(data.get('contact_person'))
    email           = _normalise(data.get('email')).lower()
    phone           = _normalise(data.get('phone'))
    service         = _normalise(data.get('service'))
    message         = _normalise(data.get('message', ''))[:2000]   # cap at 2 000 chars
 
    # honeypot — bots fill this hidden field
    if data.get('website', '').strip():
        # Silently succeed — don't reveal detection to bots
        return {'ok': True}, 202
 
    # required fields
    if not all([business, contact_person, email, phone, service]):
        return {'error': 'Please fill all required fields.'}, 400
 
    if not _is_likely_email(email):
        return {'error': 'Please enter a valid email address.'}, 400
 
    # ── resolve recipients ────────────────────────────────────────────────
    recipients = [s.strip() for s in SALES_EMAIL.split(',') if s.strip()]
    if not recipients:
        current_app.logger.error('SALES_EMAIL is not configured.')
        return {'error': 'Mail recipient not configured — contact support.'}, 500
 
    # ── header-injection guard ────────────────────────────────────────────
    subject = f'[Sales Enquiry] {business} — {service}'
    if not all(_is_safe_header(x) for x in (subject, contact_person, phone, email)):
        current_app.logger.warning('Rejected contact form: unsafe header characters detected.')
        return {'error': 'Invalid input.'}, 400
 
    # ── build the message ─────────────────────────────────────────────────
    submitted_at = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
 
    msg = EmailMessage()
    msg['Subject']  = subject
    msg['From']     = MAIL_DEFAULT_SENDER
    msg['To']       = ', '.join(recipients)
    msg['Reply-To'] = email     # already validated above
 
    # Plain-text fallback (always included)
    plain_parts = [
        f'Business:  {business}',
        f'Contact:   {contact_person}',
        f'Email:     {email}',
        f'Phone:     {phone}',
        f'Service:   {service}',
        f'Submitted: {submitted_at}',
    ]
    if message:
        plain_parts += ['', 'Message:', message]
    msg.set_content('\n'.join(plain_parts))
 
    # HTML alternative — non-fatal if template is missing
    try:
        html_body = render_template(
            'emails/sales_enquiry.html',
            business=business,
            contact_person=contact_person,
            email=email,
            phone=phone,
            service=service,
            message=message,
            submitted_at=submitted_at,
        )
        if html_body:
            msg.add_alternative(html_body, subtype='html')
    except Exception:
        current_app.logger.debug('sales_enquiry.html template not found or errored — plain text only.')
 
    # ── send via SMTP ─────────────────────────────────────────────────────
    try:
        ssl_ctx = ssl.create_default_context()
 
        if MAIL_USE_SSL:
            # Implicit SSL — port 465
            with smtplib.SMTP_SSL(MAIL_SERVER, MAIL_PORT, timeout=30, context=ssl_ctx) as smtp:
                if MAIL_USERNAME and MAIL_PASSWORD:
                    smtp.login(MAIL_USERNAME, MAIL_PASSWORD)
                smtp.send_message(msg)
        else:
            # STARTTLS — port 587 (or plain 25 if neither flag set)
            with smtplib.SMTP(MAIL_SERVER, MAIL_PORT, timeout=30) as smtp:
                smtp.ehlo()
                if MAIL_USE_TLS:
                    smtp.starttls(context=ssl_ctx)
                    smtp.ehlo()
                if MAIL_USERNAME and MAIL_PASSWORD:
                    smtp.login(MAIL_USERNAME, MAIL_PASSWORD)
                smtp.send_message(msg)
 
    except smtplib.SMTPAuthenticationError:
        current_app.logger.error('SMTP authentication failed — check MAIL_USERNAME / MAIL_PASSWORD.')
        return {'error': 'Failed to send — mail server authentication error.'}, 502
    except smtplib.SMTPException as exc:
        current_app.logger.exception('SMTP failure sending sales enquiry.')
        return {'error': 'Failed to send — mail server error.'}, 502
    except OSError as exc:
        current_app.logger.exception('Network error reaching SMTP server.')
        return {'error': 'Network error — please try again.'}, 502
    except Exception:
        current_app.logger.exception('Unexpected error sending sales enquiry.')
        return {'error': 'Failed to send email.'}, 500
 
    current_app.logger.info(
        'Sales enquiry sent: business=%r service=%r recipients=%r',
        business, service, recipients
    )
    return {'ok': True}, 202
 



@app.route('/invoices/create', methods=['POST'])
def create_invoice():
    is_json = request.is_json
    data = request.get_json() if is_json else request.form

    due_date_s = (data.get('due_date') or '').strip()
    start_s = (data.get('delivery_start_date') or '').strip()
    end_s = (data.get('delivery_end_date') or '').strip()
    note = (data.get('note') or '').strip()

    if not due_date_s:
        msg = "Due date is required."
        if is_json:
            return jsonify(ok=False, error=msg), 400
        flash(msg, "danger")
        return redirect(request.referrer or url_for('index'))

    try:
        due_date = datetime.strptime(due_date_s, '%Y-%m-%d').date()
        start_date = datetime.strptime(start_s, '%Y-%m-%d').date() if start_s else None
        end_date = datetime.strptime(end_s, '%Y-%m-%d').date() if end_s else None
    except ValueError:
        msg = "Dates must be in YYYY-MM-DD format."
        if is_json:
            return jsonify(ok=False, error=msg), 400
        flash(msg, "danger")
        return redirect(request.referrer or url_for('index'))

    if start_date and end_date and end_date < start_date:
        msg = "Delivery end date must be the same or after the start date."
        if is_json:
            return jsonify(ok=False, error=msg), 400
        flash(msg, "danger")
        return redirect(request.referrer or url_for('index'))

    # if caller provided supplier details (e.g. from UI), accept them
    supplier_tin = (data.get('supplier_tin') or '').strip()
    supplier_name = (data.get('supplier_name') or '').strip()

    # placeholders for mandatory fields in your DB model
    minimal_tax_total = [{"tax_amount": 0.0, "tax_subtotal": []}]
    minimal_legal_monetary_total = {
        "line_extension_amount": 0,
        "tax_exclusive_amount": 0,
        "tax_inclusive_amount": 0,
        "payable_amount": 0
    }
    minimal_invoice_line = []

    inv = Invoices(
        issue_date = datetime.utcnow().date(),
        due_date = due_date,
        delivery_start_date = start_date,
        delivery_end_date = end_date,
        note = note,
        tax_total = minimal_tax_total,
        legal_monetary_total = minimal_legal_monetary_total,
        invoice_line = minimal_invoice_line,
        irn_status = 'draft'
    )

    # set supplier object if provided
    if supplier_tin or supplier_name:
        inv.accounting_supplier_party = {
            "party_name": supplier_name or None,
            "tin": supplier_tin or None,
        }

    try:
        db.session.add(inv)
        db.session.commit()
        invoice_number = (data.get('invoice_number') or inv.order_reference or f"INV{inv.id:06d}").strip()
        service_id = os.environ.get("FIRS_SERVICE_ID", "94ND90NR")  # set this from your onboarding config
        issue_date = inv.issue_date or datetime.utcnow().date()
        try:
            irn_candidate = generate_irn(invoice_number, service_id, issue_date)
        except ValueError as ve:
        # handle validation error (flash, return JSON error, etc.)
            flash(str(ve), "danger")
            return redirect(url_for('invoice_detail', invoice_id=inv.id))
        # ensure uniqueness in DB
        collision = Invoices.query.filter_by(irn=irn_candidate).first()
        if collision:
            # collision unlikely if invoice_number is unique per taxpayer, but handle gracefully:
            current_app.logger.error("IRN collision for %s", irn_candidate)
            flash("IRN collision: this invoice number already used. Choose a different invoice number.", "danger")
            return redirect(url_for('invoice_detail', invoice_id=inv.id))
        # persist the IRN
        inv.irn = irn_candidate
        inv.irn_status = 'generated'   # or whatever lifecycle states you use
        db.session.add(inv)
        db.session.commit()
    except Exception as exc:
        current_app.logger.exception("Failed creating invoice: %s", exc)
        if is_json:
            return jsonify(ok=False, error="Failed to create invoice"), 500
        flash("Failed to create invoice. Try again.", "danger")
        return redirect(request.referrer or url_for('index'))

    # test convenience: return QR payload and data URI without contacting FIRS
    if request.args.get("return_qr") == "1":
        qr_payload = inv.make_firs_qr_payload_dict()
        qr_data_uri = inv.make_qr_datauri()
        return jsonify(ok=True, invoice_id=inv.id, qr_payload=qr_payload, qr_data_uri=qr_data_uri), 201

    if is_json:
        return jsonify(ok=True, invoice_id=inv.id, redirect=url_for('invoice_detail', invoice_id=inv.id)), 201

    flash("Invoice created.", "success")
    return redirect(url_for('invoice_detail', invoice_id=inv.id))

def invoice_to_canonical(invoice):
    """
    Build a canonical invoice JSON string from your Invoices model.
    NOTE: field names here are the *canonical example* for QR payload.
    If FIRS requires slightly different names, swap them here.
    """
    payload = {
        # FIRS needs an IRN produced by FIRS. If not present you can
        # still create a sample payload with a fake/sample IRN.
        "irn": invoice.irn or "SAMPLE-IRN-000000",
        "issue_date": invoice.issue_date.isoformat() if invoice.issue_date else datetime.utcnow().date().isoformat(),
        # seller TIN is mandatory for the QR per the FIRS docs — ensure this is filled
        "seller_tin": (invoice.accounting_supplier_party or {}).get("tin"),
        # Total payable (use legal_monetary_total.payable_amount)
        "total_amount": (invoice.legal_monetary_total or {}).get("payable_amount", 0),
        "currency": invoice.document_currency_code or "NGN",
        # CSID or stamp may be required in some specs; include when available
        "csid": getattr(invoice, "csid", None)
    }
    # compact JSON (no whitespace)
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def make_sample_qr_payload():
    """
    Produce a sample payload (so you can scan/test without creating invoice).
    Replace field names below if FIRS requires different keys.
    """
    payload = {
        "irn": "ITW001-E9E0C0D3-20240619",
        "issue_date": "2024-06-19",
        "seller_tin": "24058123-0001",
        "total_amount": 150000.50,
        "currency": "NGN",
        # csid included in case FIRS requires it in the QR payload
        "csid": "SAMPLE-CSID-12345"
    }
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)

@app.context_processor
def inject_current_year():
    """Make current_year available in all templates"""
    return {'current_year': datetime.now().year}


def render_invoice_pdf(invoice, qr_data_uri, template_path='invoice_pdf.html'):
    """
    Use Flask's render_template so url_for and other context globals are available.
    Provide base_url to WeasyPrint so static assets resolve (works both in dev and prod
    if the server is accessible at request.url_root).
    Returns PDF bytes.
    """
    # render via Flask so url_for works inside the template
    html = render_template(template_path, invoice=invoice, qr_data_uri=qr_data_uri)

    # base_url is used by WeasyPrint to resolve relative URLs (e.g. /static/...)
    # request.url_root is an absolute URL which WeasyPrint can fetch from your running server.
    base_url = request.url_root.rstrip('/') + '/'

    # produce PDF bytes (this may raise WeasyPrint errors if native libs missing)
    pdf_bytes = HTML(string=html, base_url=base_url).write_pdf()
    return pdf_bytes

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

@app.route("/invoice/<int:invoice_id>")
@login_required
def invoice_detail(invoice_id):
    if not current_user.is_admin:
        abort(403)
    inv = Invoices.query.get_or_404(invoice_id)
    business = Business.query.filter_by(buisness_id=inv.business_id).first()
    # Generate inline data-uri for faster embed in some contexts, but keep qr.png endpoint for PDF and img tag
    qr_data_uri = None
    # Prefer to show a stable URL for the QR image (works in PDF rendering too)
    qr_url = url_for('invoice_qr', invoice_id=inv.id)

    # load crypto keys file (if present) and build encrypted QR
    keys_path = current_app.config.get("CRYPTO_KEYS_PATH") or os.path.join(current_app.root_path, "crypto_keys.txt")
    try:
        crypto_txt = None
        if os.path.exists(keys_path):
            with open(keys_path, "r", encoding="utf-8") as fh:
                crypto_txt = fh.read()
        if crypto_txt and inv.irn:
            from helpers import make_irn_qr_from_business
            # this returns (encrypted_b64, qr_data_uri)
            _, qr_data_uri = make_irn_qr_from_business(business, inv.irn)
    except Exception:
        current_app.logger.exception("Failed to build QR for invoice %s", invoice_id)

    return render_template("invoice_detail.html", invoice=inv, qr_url=qr_url, qr_data_uri=qr_data_uri)

@app.route("/invoice/<int:invoice_id>/qr.png")
def invoice_qr(invoice_id):
    inv = Invoices.query.get_or_404(invoice_id)
    use_encrypted = current_app.config.get("FIRS_QR_ENCRYPTED", True)
    business = Business.query.filter_by(buisness_id=inv.business_id).first()
    if use_encrypted:
        keys_path = current_app.config.get("CRYPTO_KEYS_PATH") or os.path.join(current_app.root_path, "crypto_keys.txt")
        try:
            with open(keys_path, "r", encoding="utf-8") as fh:
                crypto_txt = fh.read()
        except Exception as e:
            current_app.logger.exception("Failed to read crypto keys file: %s", e)
            return ("crypto keys not available on server", 500)

        try:
            irn = inv.irn or inv.irn_candidate
            if not irn:
                return ("No IRN available for this invoice", 400)

            _, qr_data_uri = make_irn_qr_from_business(
                business, irn
            )
            png_bytes = base64.b64decode(qr_data_uri.split(",", 1)[1])
            return send_file(
                BytesIO(png_bytes),
                mimetype="image/png",
                download_name=f"invoice_{invoice_id}_qr.png"
            )
        except Exception as e:
            current_app.logger.exception("QR generation failed for invoice %s: %s", invoice_id, e)
            return (f"QR generation failed: {str(e)}", 500)

    else:
        try:
            txt = inv.make_firs_qr_payload_text()
            data_uri = make_qr_datauri(txt, box_size=6, border=4)
            png_bytes = base64.b64decode(data_uri.split(",", 1)[1])
            return send_file(
                BytesIO(png_bytes),
                mimetype="image/png",
                download_name=f"invoice_{invoice_id}_qr.png"
            )
        except Exception as e:
            current_app.logger.exception("Plain QR generation failed for invoice %s: %s", invoice_id, e)
            return (f"QR generation failed: {str(e)}", 500)

@app.route("/invoice/<int:invoice_id>/pdf")
@login_required
def invoice_pdf(invoice_id):
    current_app.logger.info(f"Starting PDF generation for invoice {invoice_id}")
    
    inv = Invoices.query.get_or_404(invoice_id)
    business = Business.query.filter_by(buisness_id=inv.business_id).first()
    
    # Build QR data
    qr_data_uri = None
    keys_path = current_app.config.get("CRYPTO_KEYS_PATH") or os.path.join(current_app.root_path, "crypto_keys.txt")
    
    try:
        current_app.logger.info(f"Generating QR code for invoice {invoice_id}")
        crypto_txt = None
        if os.path.exists(keys_path):
            with open(keys_path, "r", encoding="utf-8") as fh:
                crypto_txt = fh.read()
        try:
            if crypto_txt and inv.irn:
                from helpers import make_irn_qr_from_business
                _, qr_data_uri = make_irn_qr_from_business(business, inv.irn)
        except Exception as e:
            current_app.logger.exception(f"QR failed: {e}")
            raise  #
    except Exception as e:
        current_app.logger.exception(f"Failed to build QR for invoice {invoice_id}: {str(e)}")
    
    # Try to render PDF with WeasyPrint
    try:
        current_app.logger.info(f"Starting WeasyPrint render for invoice {invoice_id}")
        pdf_bytes = render_invoice_pdf(inv, qr_data_uri, template_path='invoice_pdf.html')
        current_app.logger.info(f"PDF rendered successfully, size: {len(pdf_bytes)} bytes")
        
        buf = BytesIO(pdf_bytes)
        buf.seek(0)
        return send_file(
            buf,
            mimetype="application/pdf",
            download_name=f"invoice_{invoice_id}.pdf",
            as_attachment=True
        )
    except Exception as e:
        current_app.logger.exception(f"PDF generation failed for invoice {invoice_id}: {str(e)}")
        
        # Fallback: render HTML
        try:
            current_app.logger.info(f"Falling back to HTML for invoice {invoice_id}")
            return render_template(
                "invoice_detail.html", 
                invoice=inv, 
                qr_url=url_for('invoice_qr', invoice_id=inv.id), 
                qr_data_uri=qr_data_uri
            )
        except Exception as e2:
            current_app.logger.exception(f"HTML fallback also failed: {str(e2)}")
            # Last-resort minimal HTML
            return f"<h2>Invoice {inv.id}</h2><p>Error: {str(e)}</p>", 500
@app.route("/submission/<int:submission_id>/logs/view")
@login_required
def view_submission_logs(submission_id):
    if not current_user.is_admin:
        abort(403)
    
    logs = SubmissionLog.query.filter_by(submission_id=submission_id)\
        .order_by(SubmissionLog.created_at).all()
    return render_template("submission_logs.html", logs=logs, submission_id=submission_id)


@app.route("/client/submission/<int:submission_id>/logs/view")
@client_required
@login_required
def client_view_submission_logs(submission_id):
    # Normalize current user's business id (works if stored as string or int)
    user_biz = getattr(current_user, "business_id", None)
    
    submission = InvoiceSubmission.query.filter_by(
        id=submission_id,
        business_id=user_biz
    ).first_or_404()

    # Fetch logs ordered by created_at. Using submission.id ensures we don't leak data.
    logs = SubmissionLog.query.filter_by(submission_id=submission.id).order_by(SubmissionLog.created_at).all()

    return render_template("submission_logs.html", logs=logs, submission=submission)

# ---------------------------
# create client business and generate unique keys
# ---------------------------

@app.route("/admin/create-api-client", methods=["POST"])
@admin_required
def admin_create_api_client():
    if not (app.debug or current_app.config.get("FLASK_DEBUG")):
        return jsonify({"ok": False, "error": "Not allowed"}), 403
    data = request.get_json() or {}
    name = data.get("name", "dev-client")
    service_id = data.get("service_id") or current_app.config.get("FIRS_SERVICE_ID")
    api_key = data.get("api_key") or secrets.token_hex(16)
    raw_secret = data.get("api_secret") or secrets.token_urlsafe(36)

    hmac_key = current_app.config.get("API_SECRET_KEY") or current_app.config.get("SECRET_KEY")
    if not hmac_key:
        return jsonify({"ok": False, "error": "API_SECRET_KEY not configured"}), 500

    api_secret_hmac = hmac.new(hmac_key.encode(), raw_secret.encode(), hashlib.sha256).hexdigest()

    b = Business(name=name, service_id=service_id, api_key=api_key, api_secret_hmac=api_secret_hmac, is_active=True)
    db.session.add(b)
    db.session.commit()
    return jsonify({"ok": True, "api_key": api_key, "api_secret": raw_secret, "service_id": service_id, "business_id": b.id}), 201

# ---------------------------
# Business Routes
# ---------------------------

from flask import render_template, request, redirect, url_for, flash, current_app, abort
from flask_login import login_required, current_user
import secrets, hmac, hashlib
from sqlalchemy.exc import SQLAlchemyError
from math import ceil

# List businesses (GET)
@app.route("/admin/businesses", methods=["GET"])
@login_required
@admin_required
def list_businesses():
    # optional: only admin can view admin console
    if not current_user.is_admin:
        abort(403)

    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 20, type=int)
    q = (request.args.get("q") or "").strip()

    query = Business.query.order_by(Business.created_at.desc(), Business.id.desc())
    if q:
        like = f"%{q}%"
        query = query.filter(
            (Business.name.ilike(like)) |
            (Business.tin.ilike(like)) |
            (Business.service_id.ilike(like)) |
            (Business.api_key.ilike(like)) |
            (Business.buisness_id.ilike(like))
        )

    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    businesses = pagination.items
    total = pagination.total
    total_pages = pagination.pages

    all_partners = PartnerCompany.query.order_by(PartnerCompany.name).all()
    return render_template(
        "businesses.html",
        businesses=businesses,
        page=page,
        per_page=per_page,
        total=total,
        total_pages=total_pages,
        q=q,
        all_partners=all_partners,
    )


# Create business (POST from form)
@app.route("/admin/create-business", methods=["POST"])
@login_required
@admin_required
def create_business():
    # only admin may create API clients
    if not current_user.is_admin:
        abort(403)

    form = request.form
    name = (form.get("name") or "").strip()
    buisness_id = (form.get("business_id") or "").strip()   # note model field spelled 'buisness_id'
    tin = (form.get("tin") or "").strip() or None
    service_id = (form.get("service_id") or "").strip() or current_app.config.get("FIRS_SERVICE_ID")
    provided_api_key = (form.get("api_key") or "").strip() or None
    provided_api_secret = (form.get("api_secret") or "").strip() or None
    callback_url = (form.get("callback_url") or "").strip() or None
    certificate_pem = (form.get("certificate_pem") or "").strip() or None
    private_key_enc = (form.get("private_key") or "").strip() or None
    is_active = True if form.get("is_active") == "on" else False
    raw_pid = (form.get("referred_by_partner_id") or "").strip()
    referred_by_partner_id = int(raw_pid) if raw_pid.isdigit() else None
    new_tier = (form.get("tier") or "starter").strip()
    biz_tier = new_tier if new_tier in ('starter', 'professional', 'enterprise') else 'starter'

    if not name:
        flash("Business name is required.", "danger")
        return redirect(url_for("list_businesses"))

    # Choose/generate keys
    api_key = provided_api_key or secrets.token_hex(16)
    raw_secret = provided_api_secret or secrets.token_urlsafe(36)

    # HMAC with server secret
    hmac_key = current_app.config.get("API_SECRET_KEY") or current_app.config.get("SECRET_KEY")
    if not hmac_key:
        flash("Server not configured to generate API secrets (API_SECRET_KEY missing).", "danger")
        return redirect(url_for("list_businesses"))

    api_secret_hmac = hmac.new(hmac_key.encode(), raw_secret.encode(), hashlib.sha256).hexdigest()

    # persist
    try:
        b = Business(
            buisness_id=buisness_id or None,
            name=name,
            tin=tin,
            service_id=service_id,
            api_key=api_key,
            api_secret_hmac=api_secret_hmac,
            callback_url=callback_url,
            certificate_pem=certificate_pem,
            private_key_enc=private_key_enc,
            is_active=is_active,
            referred_by_partner_id=referred_by_partner_id,
            tier=biz_tier,
        )
        db.session.add(b)
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        current_app.logger.exception("Failed to create business")
        flash("Failed to create business. See server logs.", "danger")
        return redirect(url_for("list_businesses"))

    # IMPORTANT: show secret once (admin should copy it). Keep message short.
    flash(f"Business created. API key: {api_key}. Secret (copy now): {raw_secret}", "success")
    return redirect(url_for("list_businesses"))

# ── Schema migration (idempotent, runs on gunicorn startup) ──────────────────
def _migrate_schema():
    from sqlalchemy import inspect, text
    insp = inspect(db.engine)
    table_names = set(insp.get_table_names())

    with db.engine.connect() as conn:
        if 'businesses' in table_names:
            cols = {c['name'] for c in insp.get_columns('businesses')}
            if 'referred_by_partner_id' not in cols:
                conn.execute(text('ALTER TABLE businesses ADD COLUMN referred_by_partner_id INTEGER'))
        if 'complaints' in table_names:
            cols = {c['name'] for c in insp.get_columns('complaints')}
            for col, typedef in [
                ('business_id',          'INTEGER'),
                ('routed_to_partner_id', 'INTEGER'),
                ('updated_at',           'DATETIME'),
            ]:
                if col not in cols:
                    conn.execute(text(f'ALTER TABLE complaints ADD COLUMN {col} {typedef}'))
        if 'partner_tickets' in table_names:
            cols = {c['name'] for c in insp.get_columns('partner_tickets')}
            if 'source' not in cols:
                conn.execute(text("ALTER TABLE partner_tickets ADD COLUMN source VARCHAR(16) DEFAULT 'raised'"))
            if 'updated_at' not in cols:
                conn.execute(text('ALTER TABLE partner_tickets ADD COLUMN updated_at DATETIME'))
        if 'businesses' in table_names:
            cols = {c['name'] for c in insp.get_columns('businesses')}
            if 'tier' not in cols:
                conn.execute(text("ALTER TABLE businesses ADD COLUMN tier VARCHAR(16) DEFAULT 'starter'"))
        if 'partner_companies' in table_names:
            cols = {c['name'] for c in insp.get_columns('partner_companies')}
            if 'tier' not in cols:
                conn.execute(text("ALTER TABLE partner_companies ADD COLUMN tier VARCHAR(16) DEFAULT 'registered'"))
            if 'nrs_certified' not in cols:
                conn.execute(text('ALTER TABLE partner_companies ADD COLUMN nrs_certified BOOLEAN DEFAULT 0'))
        conn.commit()

    # ── Seed default tier configs if the table is empty ──────────────────────
    db.create_all()   # creates tier_configs table if it doesn't exist yet
    if TierConfig.query.count() == 0:
        for row in TIER_DEFAULTS:
            db.session.add(TierConfig(**row))
        db.session.commit()


with app.app_context():
    _migrate_schema()


# ── Helpdesk email helpers ────────────────────────────────────────────────────
def _partner_agent_emails(partner_company_id):
    """Return list of email addresses for all agents of a partner company."""
    agents = (PartnerAgent.query
              .filter_by(company_id=partner_company_id)
              .join(PartnerAgent.user)
              .all())
    return [a.user.email for a in agents if a.user and a.user.email]


def _send_helpdesk_mail(recipients, subject, html):
    if not recipients:
        return
    try:
        msg = Message(subject=subject,
                      recipients=recipients,
                      sender=current_app.config.get('MAIL_DEFAULT_SENDER'))
        msg.html = html
        mail.send(msg)
    except Exception:
        current_app.logger.exception('Helpdesk email failed to %s', recipients)


def _ticket_html(heading, complaint, submitter_name, business_name=None, extra_note=None):
    priority_color = {'critical': '#ef4444', 'high': '#f59e0b',
                      'normal': '#2ec5db', 'low': '#6b7280'}.get(complaint.priority, '#2ec5db')
    rows = f"""
    <tr><td style="padding:6px 0;color:#6b7280;font-size:13px">Ticket #</td>
        <td style="padding:6px 0;font-weight:600">#{complaint.id}</td></tr>
    <tr><td style="padding:6px 0;color:#6b7280;font-size:13px">Raised by</td>
        <td style="padding:6px 0">{submitter_name}</td></tr>
    {'<tr><td style="padding:6px 0;color:#6b7280;font-size:13px">Business</td><td style="padding:6px 0">' + business_name + '</td></tr>' if business_name else ''}
    <tr><td style="padding:6px 0;color:#6b7280;font-size:13px">Category</td>
        <td style="padding:6px 0">{complaint.category}</td></tr>
    <tr><td style="padding:6px 0;color:#6b7280;font-size:13px">Priority</td>
        <td style="padding:6px 0"><span style="color:{priority_color};font-weight:700">{complaint.priority.upper()}</span></td></tr>
    """
    extra = f'<p style="background:#fffbeb;border-left:3px solid #f59e0b;padding:.75rem 1rem;border-radius:6px;font-size:13px;color:#78350f;margin-top:1rem">{extra_note}</p>' if extra_note else ''
    return f"""<!doctype html><html><body style="font-family:Arial,sans-serif;background:#f0f4f8;padding:2rem">
<div style="max-width:560px;margin:auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 4px 20px rgba(0,0,0,.08)">
  <div style="background:#0b1220;padding:1.25rem 2rem">
    <span style="font-family:Arial,sans-serif;font-weight:900;font-size:1rem;color:#2ec5db;letter-spacing:.05em">LAYER21</span>
  </div>
  <div style="padding:1.75rem 2rem">
    <h2 style="margin:0 0 .5rem;font-size:1.1rem;color:#0b1220">{heading}</h2>
    <h3 style="margin:0 0 1.25rem;font-size:1.25rem;color:#111">{complaint.title}</h3>
    <table style="width:100%;border-collapse:collapse">{rows}</table>
    {('<p style="margin-top:1rem;font-size:13px;color:#374151;line-height:1.6"><strong>Description:</strong><br/>' + complaint.description + '</p>') if complaint.description else ''}
    {extra}
  </div>
  <div style="padding:.75rem 2rem;background:#f9fafb;font-size:11px;color:#9ca3af">
    Layer21 IT Services &nbsp;·&nbsp; <a href="mailto:support@layer21.com" style="color:#2ec5db">support@layer21.com</a>
  </div>
</div></body></html>"""


def _notify_partner_new_ticket(partner_id, complaint, submitter, business):
    """Email partner agents when a client ticket is auto-routed to them."""
    partner = PartnerCompany.query.get(partner_id)
    if not partner:
        return
    recipients = _partner_agent_emails(partner_id)
    html = _ticket_html(
        f'New client ticket routed to {partner.name}',
        complaint,
        f'{submitter.first_name or ""} {submitter.last_name or ""}'.strip() or submitter.username,
        business_name=business.name,
        extra_note='This ticket was raised by a client in your portfolio. Handle it in your partner helpdesk portal. Escalate to Layer21 if you need our support.'
    )
    _send_helpdesk_mail(
        recipients,
        f'[Partner Helpdesk #{complaint.id}] New ticket — {business.name}',
        html
    )


def _notify_l21_new_ticket(complaint, submitter):
    """Email Layer21 support when a ticket is NOT partner-routed."""
    name = f'{submitter.first_name or ""} {submitter.last_name or ""}'.strip() or submitter.username
    html = _ticket_html('New support ticket', complaint, name)
    _send_helpdesk_mail(
        ['support@layer21.com', 'bilikisu.muhammed@layer21.com'],
        f'[IT Ticket #{complaint.id}] New Support Request — {complaint.priority} Priority',
        html
    )
    if submitter.email:
        ack = _ticket_html('We received your support request', complaint, name,
                           extra_note='Our team will review your ticket and be in touch shortly.')
        _send_helpdesk_mail([submitter.email], 'We received your complaint', ack)


def _notify_partner_status_update(complaint):
    """Email partner agents when Layer21 updates a routed ticket's status."""
    if not complaint.routed_to_partner_id:
        return
    partner = PartnerCompany.query.get(complaint.routed_to_partner_id)
    if not partner:
        return
    recipients = _partner_agent_emails(complaint.routed_to_partner_id)
    html = _ticket_html(
        f'Ticket #{complaint.id} status updated by Layer21',
        complaint,
        complaint.created_by or 'Client',
        extra_note=f'Status is now: <strong>{complaint.status.replace("_", " ").title()}</strong>'
    )
    _send_helpdesk_mail(recipients,
                        f'[Partner Helpdesk #{complaint.id}] Status updated — {complaint.status}',
                        html)


def _notify_l21_escalation(complaint, partner_name):
    """Email Layer21 support when a partner escalates a routed ticket."""
    html = _ticket_html(
        f'[ESCALATED] Partner {partner_name} escalated a ticket',
        complaint,
        complaint.created_by or 'Client',
        extra_note=f'Partner <strong>{partner_name}</strong> could not resolve this ticket and has escalated it to Layer21 support.'
    )
    _send_helpdesk_mail(
        ['support@layer21.com', 'bilikisu.muhammed@layer21.com'],
        f'[ESCALATED #{complaint.id}] Partner escalation — {complaint.priority} Priority',
        html
    )


# Complaints routes
@app.route('/complaints', methods=['GET', 'POST'])
@login_required
def list_complaints():
    # business lookup (fixed possible typo)
    business_id = current_user.business_id
    bus = Business.query.filter_by(buisness_id=business_id).first()

    q = request.args.get('q', '').strip()
    try:
        page = max(int(request.args.get('page', 1)), 1)
    except ValueError:
        page = 1
    per_page = 20

    if current_user.user_type == 'admin':
        base_query = Complaint.query.order_by(desc(Complaint.created_at))
    else:
        base_query = Complaint.query.filter_by(
            user_id=current_user.id
            ).order_by(desc(Complaint.created_at))
    if q:
        base_query = base_query.filter(
            (Complaint.title.ilike(f'%{q}%')) |
            (Complaint.description.ilike(f'%{q}%'))
        )

    pagination = base_query.paginate(page=page, per_page=per_page, error_out=False)
    complaints = pagination.items

    if request.method == 'POST':
        # basic validation
        title = (request.form.get('title') or '').strip()
        priority = (request.form.get('priority') or 'normal').strip()
        category = (request.form.get('category') or '').strip()
        description = (request.form.get('description') or '').strip()

        if not title or not category:
            flash('Title and category are required.', 'danger')
            return redirect(url_for('list_complaints', page=page, q=q))

        # Resolve the business record for the current user
        biz_record = None
        if current_user.business_id:
            biz_record = Business.query.filter_by(buisness_id=current_user.business_id).first()

        new_complaint = Complaint(
            title=title,
            priority=priority,
            category=category,
            description=description,
            user_id=current_user.id,
            status='new',
            created_by=f'{current_user.first_name} {current_user.last_name}',
            business_id=biz_record.id if biz_record else None,
        )

        try:
            db.session.add(new_complaint)
            db.session.flush()  # get new_complaint.id before routing

            # ── Partner routing ───────────────────────────────────────────
            if biz_record and biz_record.referred_by_partner_id:
                partner_id = biz_record.referred_by_partner_id
                new_complaint.routed_to_partner_id = partner_id
                # Auto-create a PartnerTicket so the partner sees it in their portal
                pt = PartnerTicket(
                    title=title,
                    description=description or '(no description)',
                    status='open',
                    source='routed',
                    company_id=partner_id,
                    opened_by_id=current_user.id,
                    internal_complaint_id=new_complaint.id,
                )
                db.session.add(pt)
                db.session.commit()
                current_app.logger.info(
                    'Ticket #%s routed to partner company %s', new_complaint.id, partner_id)
                _notify_partner_new_ticket(partner_id, new_complaint, current_user, biz_record)
            else:
                # No partner attribution — goes straight to Layer21 support
                db.session.commit()
                _notify_l21_new_ticket(new_complaint, current_user)

        except Exception:
            db.session.rollback()
            current_app.logger.exception("Failed to create complaint")
            flash('Failed to save complaint. Try again.', 'danger')
            return redirect(url_for('list_complaints', page=page, q=q))

        flash('Complaint logged successfully.', 'success')
        return redirect(url_for('list_complaints', page=1, q=''))

    return render_template(
        'complaints.html',
        complaints=complaints,
        page=page,
        per_page=per_page,
        total_count=pagination.total,
        total_pages=pagination.pages,
        q=q,
        bus=bus
    )

# Create (form POST or JSON)
@app.route('/complaints', methods=['POST'])
def create_complaint():
    # JSON (AJAX) or form fallback
    if request.is_json:
        data = request.get_json() or {}
        title = (data.get('title') or '').strip()
        description = (data.get('description') or '').strip()
        priority = data.get('priority') or 'normal'
        created_by = data.get('created_by') or None
    else:
        title = (request.form.get('title') or '').strip()
        description = (request.form.get('description') or '').strip()
        priority = request.form.get('priority') or 'normal'
        created_by = request.form.get('created_by') or None

    if not title:
        if request.is_json:
            return jsonify(ok=False, error='Title is required'), 400
        flash('Title is required', 'error')
        return redirect(url_for('list_complaints'))

    c = Complaint(
        title=title,
        description=description,
        priority=priority,
        created_by=created_by,
        created_at=datetime.utcnow()
    )
    db.session.add(c)
    db.session.commit()

    if request.is_json:
        return jsonify(ok=True, complaint_id=c.id), 201

    flash('Complaint created', 'success')
    return redirect(url_for('list_complaints'))




# Lightweight JSON API (for JS)
@app.route('/complaints/api', methods=['GET'])
def complaints_api():
    q = request.args.get('q', '').strip()
    try:
        limit = min(int(request.args.get('limit', 100)), 500)
    except ValueError:
        limit = 100

    query = Complaint.query.order_by(desc(Complaint.created_at))
    if q:
        query = query.filter(
            (Complaint.title.ilike(f'%{q}%')) |
            (Complaint.description.ilike(f'%{q}%'))
        )

    items = query.limit(limit).all()
    out = [{
        'id': c.id,
        'title': c.title,
        'description': c.description,
        'status': c.status,
        'priority': c.priority,
        'created_at': c.created_at.isoformat(),
        'created_by': c.created_by
    } for c in items]
    return jsonify(ok=True, complaints=out)

    # health-check
@app.route('/health')
def health():
    return {"status": "ok", "db": bool(db.engine)}, 200

    #error handlers
@app.errorhandler(500)
def server_error(e):
    app.logger.exception("Server error")
    return render_template("500.html"), 500

@app.errorhandler(404)
def not_found(e):
    return render_template("errors/404.html"), 404

@app.errorhandler(403)
def forbidden(e):
    return render_template("errors/403.html"), 403

@app.errorhandler(400)
def bad_request(e):
    return render_template("errors/400.html"), 400

@app.errorhandler(Exception)
def handle_exception(e):
    # Log the error but don't expose details to user
    app.logger.exception("Unhandled exception")
    return render_template("errors/500.html"), 500

# ---------------------------
# User Routes
# ---------------------------

@app.route("/users")
@login_required
def list_users():
    # simple pagination
    if not current_user.is_admin:
        abort(403)
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 20, type=int)

    q = (request.args.get("q") or "").strip()
    query = User.query.order_by(User.created_at.desc(), User.id.desc())
    if q:
        like = f"%{q}%"
        query = query.filter(
            or_(
                User.email.ilike(like),
                User.username.ilike(like),
                User.first_name.ilike(like),
                User.last_name.ilike(like),
                User.company_name.ilike(like),
            )
        )

    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    users = pagination.items
    total = pagination.total
    total_pages = pagination.pages
    businesses = Business.query.all()

    return render_template(
        "users.html",
        users=users,
        page=page,
        per_page=per_page,
        total=total,
        total_pages=total_pages,
        q=q,
        businesses=businesses
    )

@app.route("/users/<int:user_id>", methods=["GET"])
@login_required
def user_detail(user_id):
    user = User.query.get_or_404(user_id)
    if user.id != current_user.id and not current_user.is_admin:
        abort(403)
    return render_template("user_detail.html", user=user)


@app.route("/users/<int:user_id>/toggle_active", methods=["POST"])
@login_required
def toggle_user_active(user_id):
    # only admins may enable/disable other users
    if not current_user.is_admin:
        abort(403)
    user = User.query.get_or_404(user_id)
    # Prevent self-lockout
    if user.id == current_user.id:
        flash("You cannot disable your own account.", "danger")
        return redirect(url_for("user_detail", user_id=user_id))

    user.is_active = not bool(user.is_active)
    try:
        db.session.add(user)
        db.session.commit()
        flash(f"User {'enabled' if user.is_active else 'disabled'} successfully.", "success")
    except SQLAlchemyError:
        db.session.rollback()
        current_app.logger.exception("Failed toggling user active state")
        flash("Failed to update user, try again.", "danger")
    return redirect(url_for("user_detail", user_id=user_id))


def _bool_from_form(val: Optional[str]) -> bool:
    if val is None:
        return False
    return str(val).lower() in ("1", "true", "yes", "on")

@app.route("/users/<int:user_id>/update", methods=["POST"])
@login_required
def update_user(user_id):
    user = User.query.get_or_404(user_id)
    if not current_user.is_admin:
        abort(403)

    first_name = (request.form.get("first_name") or "").strip() or None
    last_name = (request.form.get("last_name") or "").strip() or None
    company_name = (request.form.get("company_name") or "").strip() or None
    user_type = (request.form.get("user_type") or "").strip() or None

    # Parse is_admin from form (safe and permissive)
    is_admin_flag = _bool_from_form(request.form.get("is_admin"))

    try:
        user.first_name = first_name
        user.last_name = last_name
        user.company_name = company_name
        if current_user.is_admin:
            user.user_type = user_type
            user.is_admin = bool(is_admin_flag)

        db.session.add(user)
        db.session.commit()
        flash("User updated successfully.", "success")
    except SQLAlchemyError:
        db.session.rollback()
        current_app.logger.exception("Failed updating user")
        flash("Failed to update user. Try again.", "danger")

    return redirect(url_for("user_detail", user_id=user_id))

@app.route("/documentation/api_doc")
def api_doc():
    return render_template("pdf_viewer.html",
                           title="API Documentation",
                           pdf_url="/static/pdfs/api_doc.pdf")

@app.route("/documentation/sla_template")
def sla_template():
    return render_template("pdf_viewer.html",
                           title="SLA Template",
                           pdf_url="/static/pdfs/sla_template.pdf")

@app.route('/businesses/<int:business_id>', methods=['GET', 'POST'])
@login_required
def business_detail(business_id):
    business = Business.query.get_or_404(business_id)
 
    if request.method == 'POST':
        # ── Edit form submission ──────────────────
        business.name         = request.form.get('name', business.name).strip()
        business.buisness_id  = request.form.get('buisness_id', business.buisness_id).strip()
        business.tin          = request.form.get('tin', '').strip() or None
        business.service_id   = request.form.get('service_id', '').strip() or None
        business.callback_url = request.form.get('callback_url', '').strip() or None
        business.firs_api_key = request.form.get('firs_api_key', '').strip() or None
 
        firs_secret = request.form.get('firs_api_secret', '').strip()
        if firs_secret:
            business.firs_api_secret = firs_secret   # store / encrypt as needed
 
        cert_pem     = request.form.get('certificate_pem', '').strip()
        firs_cert    = request.form.get('firs_certificate', '').strip()
        private_key  = request.form.get('private_key_enc', '').strip()
 
        if cert_pem:
            business.certificate_pem = cert_pem
        if firs_cert:
            business.firs_certificate = firs_cert
        if private_key:
            # TODO: encrypt before storing
            business.private_key_enc = private_key
 
        business.is_active = bool(request.form.get('is_active'))

        # Partner attribution
        raw_pid = request.form.get('referred_by_partner_id', '').strip()
        business.referred_by_partner_id = int(raw_pid) if raw_pid.isdigit() else None

        # Billing tier
        new_tier = request.form.get('tier', '').strip()
        if new_tier in ('starter', 'professional', 'enterprise'):
            business.tier = new_tier

        db.session.commit()
        flash('Business updated successfully.', 'success')
        return redirect(url_for('business_detail', business_id=business.id))
 
    # Optional context extras (replace with real queries)
    invoice_count     = 0   # e.g. Invoice.query.filter_by(business_id=business.id).count()
    submissions_today = 0
    last_activity     = None
 
    all_partners = PartnerCompany.query.order_by(PartnerCompany.name).all()
    return render_template(
        'business_detail.html',
        business=business,
        invoice_count=invoice_count,
        submissions_today=submissions_today,
        last_activity=last_activity,
        all_partners=all_partners,
    )
 
 
@app.route('/admin/tier-config', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_tier_config():
    if request.method == 'POST':
        updated = 0
        for cfg in TierConfig.query.all():
            key_rpm   = f'rate_per_min_{cfg.id}'
            key_cap   = f'daily_invoice_cap_{cfg.id}'
            key_max   = f'max_businesses_{cfg.id}'
            key_sla   = f'sla_hours_{cfg.id}'
            key_label = f'display_name_{cfg.id}'
            if key_rpm in request.form:
                try:
                    cfg.rate_per_min      = int(request.form[key_rpm])
                    cfg.daily_invoice_cap = int(request.form[key_cap])
                    cfg.max_businesses    = int(request.form[key_max])
                    cfg.sla_hours         = int(request.form[key_sla])
                    cfg.display_name      = request.form[key_label].strip() or cfg.display_name
                    updated += 1
                except (ValueError, KeyError):
                    pass
        db.session.commit()
        flash(f'Tier configuration saved ({updated} tiers updated).', 'success')
        return redirect(url_for('admin_tier_config'))

    biz_tiers     = TierConfig.query.filter_by(tier_type='business').order_by(TierConfig.id).all()
    partner_tiers = TierConfig.query.filter_by(tier_type='partner').order_by(TierConfig.id).all()
    return render_template('admin_tier_config.html',
                           biz_tiers=biz_tiers,
                           partner_tiers=partner_tiers)


@app.route('/businesses/<int:business_id>/toggle', methods=['POST'])
@login_required
def toggle_business_active(business_id):
    business = Business.query.get_or_404(business_id)
    business.is_active = not business.is_active
    db.session.commit()
    state = 'activated' if business.is_active else 'deactivated'
    flash(f'Business {state}.', 'success')
    return redirect(url_for('business_detail', business_id=business.id))
 
 
@app.route('/businesses/<int:business_id>/rotate-key', methods=['POST'])
@login_required
def rotate_business_key(business_id):
    if not current_user.is_admin:
        abort(403)
 
    business = Business.query.get_or_404(business_id)
 
    from flask import current_app
    sk = current_app.config.get('API_SECRET_KEY') or current_app.config.get('SECRET_KEY')
    if not sk:
        flash('API_SECRET_KEY not configured.', 'danger')
        return redirect(url_for('business_detail', business_id=business.id))
 
    new_api_key = secrets.token_urlsafe(18)
    new_secret  = secrets.token_urlsafe(32)
    new_hmac    = hmac.new(sk.encode(), new_secret.encode(), hashlib.sha256).hexdigest()
 
    business.api_key         = new_api_key
    business.api_secret_hmac = new_hmac
    db.session.commit()
 
    # Surface the new raw secret ONCE via flash (never stored in plain text)
    flash(
        f'API key rotated. New API Key: {new_api_key} | New Secret (copy now): {new_secret}',
        'warning'
    )
    return redirect(url_for('business_detail', business_id=business.id))
 
 
# ══════════════════════════════════════════════
#  COMPLAINT / TICKET DETAIL
# ══════════════════════════════════════════════
 
@app.route('/helpdesk/<int:complaint_id>')
@login_required
def complaint_detail(complaint_id):
    complaint = Complaint.query.get_or_404(complaint_id)
 
    # If you have a ComplaintLog / ActivityLog model, query it here:
    # logs = ComplaintLog.query.filter_by(complaint_id=complaint_id).order_by('created_at').all()
    logs = []
 
    return render_template(
        'complaint_detail.html',
        complaint=complaint,
        logs=logs,
    )
 
 
@app.route('/helpdesk/<int:complaint_id>/status', methods=['POST'])
@login_required
def update_complaint_status(complaint_id):
    complaint = Complaint.query.get_or_404(complaint_id)
 
    new_status = request.form.get('status', '').strip()
    if new_status in ('open', 'in_progress', 'resolved', 'closed', 'escalated'):
        complaint.status = new_status
        db.session.commit()
        flash(f'Ticket status updated to "{new_status.replace("_", " ").title()}".', 'success')
        _notify_partner_status_update(complaint)

    return redirect(url_for('complaint_detail', complaint_id=complaint.id))
 
 
@app.route('/helpdesk/<int:complaint_id>/note', methods=['POST'])
@login_required
def add_complaint_note(complaint_id):
    complaint = Complaint.query.get_or_404(complaint_id)
 
    new_status   = request.form.get('status', '').strip()
    new_priority = request.form.get('priority', '').strip()
    note_text    = request.form.get('note', '').strip()
 
    changed = False
 
    if new_status and new_status in ('open', 'in_progress', 'resolved', 'closed', 'escalated'):
        complaint.status = new_status
        changed = True
 
    if new_priority and new_priority in ('low', 'normal', 'high', 'critical'):
        complaint.priority = new_priority
        changed = True
 
    if changed or note_text:
        db.session.commit()
        if changed:
            _notify_partner_status_update(complaint)

        # Optional: persist the note to a log table
        # log = ComplaintLog(
        #     complaint_id = complaint.id,
        #     status       = complaint.status,
        #     note         = note_text,
        #     changed_by   = current_user.username,
        #     created_at   = datetime.utcnow(),
        # )
        # db.session.add(log)
        # db.session.commit()
 
        flash('Ticket updated.', 'success')
 
    return redirect(url_for('complaint_detail', complaint_id=complaint.id))

if __name__ == "__main__":
    with app.app_context():
        db.create_all() 
    app.run(host="0.0.0.0", port=5000, debug=False)

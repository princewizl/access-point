# app/webhook_receiver.py
import hashlib
import json
from datetime import datetime

from flask import Blueprint, current_app, request, jsonify
from sqlalchemy import Column, Integer, String, DateTime, Text, Boolean
from sqlalchemy.exc import SQLAlchemyError

from models import db  # adjust import to match your codebase (SQLAlchemy instance)
import logging
import requests
from typing import Union, Optional

logger = logging.getLogger(__name__)
webhook_bp = Blueprint("webhook_receiver", __name__, url_prefix="/api/v1/invoice/transmit")

# -----------------------
# DB model for received webhook events
# -----------------------
class WebhookEvent(db.Model):
    __tablename__ = "webhook_events"
    id = Column(Integer, primary_key=True)
    irn = Column(String(256), index=True, nullable=True)
    message = Column(String(64), nullable=True)
    payload_raw = Column(Text, nullable=True)       # full JSON text
    payload_hash = Column(String(64), index=True)   # sha256 of payload to dedupe
    received_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    processed = Column(Boolean, default=False, nullable=False)   # whether business processing done
    processed_at = Column(DateTime, nullable=True)
    attempts = Column(Integer, default=0, nullable=False)        # processing attempts counter
    last_error = Column(Text, nullable=True)
    acked = Column(Boolean, default=False, nullable=False)       # whether ack was sent/backed
    acked_at = Column(DateTime, nullable=True)

    def to_dict(self):
        return {
            "id": self.id,
            "irn": self.irn,
            "message": self.message,
            "received_at": self.received_at.isoformat() if self.received_at else None,
            "processed": self.processed,
            "processed_at": self.processed_at.isoformat() if self.processed_at else None,
            "attempts": self.attempts,
            "last_error": self.last_error,
            "acked": self.acked,
            "acked_at": self.acked_at.isoformat() if self.acked_at else None,
            "payload": json.loads(self.payload_raw) if self.payload_raw else None,
        }

# -----------------------
# utils
# -----------------------
def _payload_hash(payload_obj) -> str:
    """Deterministic SHA256 hash of JSON payload for idempotency."""
    try:
        # canonicalize keys and whitespace
        canonical = json.dumps(payload_obj, separators=(",", ":"), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    except Exception:
        # fallback to text hash
        text = str(payload_obj)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

# -----------------------
# Receiver endpoint (the webhook that FIRS will POST to)
# POST /api/v1/invoice/transmit  (accepts JSON body)
# -----------------------
@webhook_bp.route("", methods=["POST"])
def receive_transmit_webhook():
    """
    Receive webhook notifications from FIRS Exchange.
    Expected JSON payload example:
      { "irn": "INV0990-088ED42R-20270920", "message": "TRANSMITTING", ... }
    Responds HTTP 200 immediately when the payload is accepted (even if duplicate).
    Stores event in webhook_events table for downstream processing.
    """
    try:
        payload = request.get_json(force=True, silent=True)
        if not payload:
            logger.warning("Webhook received with no JSON body from %s", request.remote_addr)
            # still return 200 per requirement (but store minimal record)
            payload = {}

        irn = payload.get("irn") or payload.get("IRN") or None
        message = payload.get("message") or payload.get("status") or None
        phash = _payload_hash(payload)

        # Deduplicate: check for exact payload hash (idempotent)
        existing = WebhookEvent.query.filter_by(payload_hash=phash).first()
        if existing:
            logger.info("Duplicate webhook event received (id=%s irn=%s message=%s) - recorded duplicate", existing.id, existing.irn, existing.message)
            # Optionally increment attempts or update last_received time
            try:
                existing.attempts = (existing.attempts or 0) + 1
                db.session.add(existing)
                db.session.commit()
            except SQLAlchemyError:
                db.session.rollback()
            # Per APP requirements, respond 200 OK to confirm receipt
            return ("", 200)

        # Persist new event
        we = WebhookEvent(
            irn=irn,
            message=message,
            payload_raw=json.dumps(payload, ensure_ascii=False),
            payload_hash=phash,
            received_at=datetime.utcnow(),
            processed=False,
            attempts=1,
        )
        db.session.add(we)
        db.session.commit()
        try:
            if current_app.config.get("FIRS_AUTO_ACK_ON_RECEIVE", True):
                # best-effort: call your acknowledge endpoint functionally (not HTTP) to ack this IRN
                # # Avoid re-entering the same request context; call the ack helper directly.
                # # WARNING: if the incoming payload is 'TRANSMITTING' you might want to wait for 'TRANSMITTED' before acking.
                from flask import copy_current_request_context
                # Option A: synchronous (simple, but stalls the webhook receiver)
                acknowledge_irn(irn)   # this will attempt to ack and optionally send to FIRS depending on config
                # Option B (recommended): schedule background job (Celery, RQ) to call the same ack logic
        except Exception:
            logger.exception("Auto-ack attempt failed for irn=%s", irn)
        logger.info("Received webhook event id=%s irn=%s message=%s from %s", we.id, irn, message, request.remote_addr)
        # return 200 OK immediately per spec
        return ("", 200)
    
    except Exception as exc:
        logger.exception("Unhandled exception in webhook receiver: %s", exc)
        # Return 200 anyway so the Exchange treats this as delivered.
        # Optionally you may return 500 to force retries, but spec asked for 200 OK on success.
        return ("", 200)

def _send_ack_to_firs(irn: str, ack_note: Optional[str], by: str, timeout: int = 30) -> dict:
    """
    Best-effort outbound ACK to the Exchange. Returns parsed response or error dict.
    """
    base = current_app.config.get("FIRS_API_BASE", "https://einvoice.firs.gov.ng").rstrip("/")
    ack_path = current_app.config.get("FIRS_ACK_PATH", "/api/v1/invoice/transmit/{irn}")  # default re-use
    if "{irn}" in ack_path:
        ack_path = ack_path.format(irn=irn)
    else:
        ack_path = ack_path.rstrip("/") + "/" + irn
    ack_url = base + "/" + ack_path.lstrip("/")

    headers = {"Content-Type": "application/json"}
    if current_app.config.get("FIRS_API_KEY"):
        headers["x-api-key"] = current_app.config["FIRS_API_KEY"]
    if current_app.config.get("FIRS_API_SECRET"):
        headers["x-api-secret"] = current_app.config["FIRS_API_SECRET"]

    payload = {"irn": irn, "message": "ACKNOWLEDGED", "ack_note": ack_note or "", "acked_by": by}
    try:
        r = requests.patch(ack_url, json=payload, headers=headers, timeout=timeout)
        r.raise_for_status()
        try:
            return {"ok": True, "status_code": r.status_code, "body": r.json()}
        except Exception:
            return {"ok": True, "status_code": r.status_code, "body": r.text}
    except requests.HTTPError:
        body_text = None
        try:
            body_text = r.json()
        except Exception:
            body_text = r.text
        return {"ok": False, "status_code": r.status_code, "body": body_text}
    except requests.RequestException as ex:
        return {"ok": False, "error": str(ex)}

@webhook_bp.route("/<string:irn>", methods=["PATCH"])
def acknowledge_irn(irn: str):
    """
    Acknowledge receipt/processing of all webhook events for an IRN.
    Body JSON (optional):
      {
        "ack_note": "processed by worker",
        "by": "consumer-service",
        "send_to_firs": true   # optional override, default from config FIRS_AUTO_ACK (False)
      }

    Returns: {"ok": True, "acked": <n>, "irn": irn, "firs_ack": <response or null>}
    """
    try:
        body = request.get_json(force=True, silent=True) or {}
        ack_note = body.get("ack_note")
        by = body.get("by", "unknown")
        # send_to_firs may be True in body or default via config
        send_to_firs = bool(body.get("send_to_firs")) or bool(current_app.config.get("FIRS_AUTO_ACK", False))

        # find unacked events for this IRN (idempotent)
        events = WebhookEvent.query.filter_by(irn=irn, acked=False).order_by(WebhookEvent.received_at.asc()).all()
        if not events:
            return jsonify({"ok": False, "message": "no_unacked_events", "irn": irn}), 404

        now = datetime.utcnow()
        updated = 0
        for ev in events:
            ev.acked = True
            ev.acked_at = now
            ev.last_error = (ev.last_error or "") + f"\nACK by={by} note={ack_note or ''} at={now.isoformat()}"
            # increment attempts or store who acked
            ev.attempts = (ev.attempts or 0)
            db.session.add(ev)
            updated += 1
        try:
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            logger.exception("Failed to commit ack updates for irn=%s", irn)
            return jsonify({"ok": False, "message": "db_error"}), 500

        logger.info("Acknowledged %s webhook events for IRN %s by=%s note=%s", updated, irn, by, ack_note)

        firs_ack_resp = None
        if send_to_firs:
            try:
                timeout = int(current_app.config.get("FIRS_REQUEST_TIMEOUT", 30))
                firs_ack_resp = _send_ack_to_firs(irn, ack_note, by, timeout=timeout)
                if firs_ack_resp.get("ok"):
                    logger.info("Outbound FIRS ACK successful for IRN %s: %r", irn, firs_ack_resp)
                else:
                    logger.error("Outbound FIRS ACK failed for IRN %s: %r", irn, firs_ack_resp)
            except Exception as ex:
                logger.exception("Outbound FIRS ACK request failed for IRN %s: %s", irn, ex)
                firs_ack_resp = {"ok": False, "error": str(ex)}

        return jsonify({"ok": True, "acked": updated, "irn": irn, "firs_ack": firs_ack_resp}), 200

    except Exception as exc:
        logger.exception("Unhandled exception in acknowledge_irn: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500

# -----------------------
# Pull endpoint: allow internal systems to fetch pending/unprocessed events
# GET /api/v1/invoice/transmit/pull?limit=25
# -----------------------
@webhook_bp.route("/pull", methods=["GET"])
def pull_events():
    """
    Return a list of pending (processed=False) webhook events for downstream processing.
    Query params:
      - limit (int) default 25
      - mark_taken (bool) optional: if true, increment attempts and set processed=True (use with care)
    """
    try:
        limit = int(request.args.get("limit", 25))
        mark_taken = request.args.get("mark_taken", "false").lower() in ("1", "true", "yes")

        q = WebhookEvent.query.filter_by(processed=False).order_by(WebhookEvent.received_at.asc()).limit(limit)
        events = q.all()
        resp = [e.to_dict() for e in events]

        if mark_taken and events:
            now = datetime.utcnow()
            for e in events:
                e.attempts = (e.attempts or 0) + 1
                # Do not mark processed=True here unless you truly want to — often better to mark processed AFTER work completes.
                # e.processed = True
                # e.processed_at = now
                db.session.add(e)
            try:
                db.session.commit()
            except SQLAlchemyError:
                db.session.rollback()
                logger.exception("Failed to update attempts while pulling events")
        return jsonify({"ok": True, "count": len(resp), "events": resp}), 200
    except Exception as exc:
        logger.exception("Unhandled exception in pull_events: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500

# -----------------------
# Utility: mark event processed (internal consumer calls after completing work)
# POST /api/v1/invoice/transmit/mark_processed
# Body: { "event_id": 123, "status": "ok", "error": null }
# -----------------------
@webhook_bp.route("/mark_processed", methods=["POST"])
def mark_processed():
    try:
        data = request.get_json(force=True, silent=True) or {}
        event_id = data.get("event_id")
        status = data.get("status", "ok")
        error = data.get("error")

        if not event_id:
            return jsonify({"ok": False, "error": "missing event_id"}), 400

        ev = WebhookEvent.query.get(event_id)
        if not ev:
            return jsonify({"ok": False, "error": "not_found"}), 404

        ev.processed = True
        ev.processed_at = datetime.utcnow()
        ev.last_error = error or ev.last_error
        db.session.add(ev)
        try:
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            logger.exception("Failed to commit mark_processed for event=%s", event_id)
            return jsonify({"ok": False, "error": "db_error"}), 500

        logger.info("Marked webhook event %s processed status=%s", event_id, status)
        return jsonify({"ok": True, "event_id": event_id}), 200
    except Exception as exc:
        logger.exception("Unhandled exception in mark_processed: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500


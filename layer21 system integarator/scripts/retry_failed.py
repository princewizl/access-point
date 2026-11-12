"""
Retry every failed invoice submission.

    venv/bin/python scripts/retry_failed.py                          list only, nothing is sent
    venv/bin/python scripts/retry_failed.py --run                    resubmit them to FIRS
    venv/bin/python scripts/retry_failed.py --run --skip-data-errors same, but leave invoices FIRS
                                                                     rejected for their data

Invoices that failed after signing (health check, TIN lookup, transmit) only retry the
transmit steps, so they are never re-validated or re-signed. --skip-data-errors leaves out
invoices whose last error is FIRS rejecting a field of the invoice (e.g. a 0 line amount or
a bad HSN code): only the sender can fix those, by resending, and retrying them just
repeats the failure and its alert email. The scheduled run uses it.
"""
import os
import re
import sys
import logging
from collections import Counter
from datetime import datetime

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_DIR)
os.chdir(APP_DIR)
logging.disable(logging.CRITICAL)   # keep the app's own logging out of this report

from app import app  # noqa: E402
from helpers import _process_invoice_irn_request, _process_invoice_transmit_only  # noqa: E402
from models import db, Invoices, InvoiceSubmission  # noqa: E402

# statuses only reachable after the invoice was signed -> retry transmit steps only
AFTER_SIGNING = {"signed", "issued", "health_check_failed", "tin_lookup_failed", "transmit_failed"}

# FIRS field validation errors look like "invoicerequest.invoice.<field> is required" /
# "... must be a valid ..."; nothing changes on a retry until the sender resends
DATA_ERROR = re.compile(r"invoicerequest\.invoice\.\S+ (is required|must be)", re.I)

RUN = "--run" in sys.argv
SKIP_DATA_ERRORS = "--skip-data-errors" in sys.argv


def short(err):
    err = str(err)
    m = re.search(r"'details': '([^']+)'", err)
    return (m.group(1) if m else err)[:120]


def main():
    with app.app_context():
        latest = {}
        for s in InvoiceSubmission.query.order_by(InvoiceSubmission.id).all():
            if s.invoice_id:
                latest[s.invoice_id] = s

        todo, skipped = [], 0
        for inv_id, s in sorted(latest.items()):
            inv = db.session.get(Invoices, inv_id)
            if not inv or (s.status == "transmitted" and inv.irn_status == "transmitted"):
                continue
            if not (s.status and "fail" in s.status):
                continue
            if SKIP_DATA_ERRORS and DATA_ERROR.search(s.error or ""):
                skipped += 1
                continue
            step = "transmit-only" if (s.status in AFTER_SIGNING or inv.irn_status in ("signed", "issued")) else "full"
            todo.append((inv, s, step))

        print(f"[{datetime.now():%Y-%m-%d %H:%M}] {len(todo)} failed invoices to retry"
              f"{f', {skipped} skipped (data errors - waiting for the sender to resend)' if skipped else ''}"
              f"  ({'RESUBMITTING' if RUN else 'list only - add --run to resubmit'})")
        if todo:
            print(dict(Counter(s.status for _, s, _ in todo)))

        results = Counter()
        for inv, s, step in todo:
            line = f"invoice {inv.id:<5} {inv.order_reference or '':<16} {s.status:<20} -> {step}"
            if not RUN:
                print(line)
                continue
            try:
                r = _process_invoice_transmit_only(inv.id) if step == "transmit-only" else _process_invoice_irn_request(inv.id)
            except Exception as exc:
                r = {"ok": False, "error": str(exc)}
            if r.get("ok"):
                results["OK"] += 1
                print(f"{line}   OK  irn={r.get('irn') or inv.irn}")
            else:
                why = short(r.get("details") or r.get("error"))
                results[why] += 1
                print(f"{line}   FAILED  {why}")

        if RUN and todo:
            print("Summary:")
            for k, n in results.most_common():
                print(f"  {n:>3}  {k}")


if __name__ == "__main__":
    main()

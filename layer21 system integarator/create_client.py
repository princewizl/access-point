import os, secrets, hmac, hashlib
from app import app
from models import db, Business
from flask import current_app

# set desired values here or read from env
api_key = os.environ.get("FIRS_API_KEY") or "6ae5780c-55f2-401b-9e15-a5bce69ede00"
raw_secret = os.environ.get("FIRS_API_SECRET") or "ri93RjTSwI3e5tjB6u61K3J4elepmLAmoDq996LkCB8Bcfr718gX2gfZHkW98b5YZwWkurYHDfC8c8DlrXh9o13g5Lu2Ru8j8g6Z"
name = "FIRS Sandbox"
service_id = os.environ.get("FIRS_SERVICE_ID") or "94ND90NR"

with app.app_context():
    # Make sure API_SECRET_KEY exists
    hmac_key = current_app.config.get("API_SECRET_KEY") or current_app.config.get("SECRET_KEY")
    if not hmac_key:
        raise RuntimeError("API_SECRET_KEY not set in app config; add it to .env and restart")

    secret_hmac = hmac.new(hmac_key.encode(), raw_secret.encode(), hashlib.sha256).hexdigest()

    # create or update existing record with same key
    b = Business.query.filter_by(api_key=api_key).first()
    if b:
        b.name = name
        b.service_id = service_id
        b.api_secret_hmac = secret_hmac
        b.is_active = True
    else:
        b = Business(name=name, service_id=service_id, api_key=api_key, api_secret_hmac=secret_hmac, is_active=True)
        db.session.add(b)

    db.session.commit()
    print("Created/updated business:", b.id, "api_key:", b.api_key)
    print(">>> Use this API key and this RAW secret in your client headers:")
    print("x-api-key:", b.api_key)
    print("x-api-secret:", raw_secret)
"""Gmail OAuth 2.0 + send, for the Outreach tab's approve-then-send flow.

Credentials come from Streamlit secrets (GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET,
GMAIL_REDIRECT_URI). The refresh token is persisted in Supabase (gmail_token
table) so you connect once. Scope is gmail.send ONLY -- the app can send mail
but cannot read your inbox. Every send is gated behind explicit per-email
approval in the UI; this module just performs the mechanics.
"""
from __future__ import annotations

import base64
import json
import os
from email.mime.text import MIMEText

# Allow http://localhost redirect during local testing; relax the harmless
# scope-order changes Google sometimes returns on token exchange.
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]


def _flow(client_id, client_secret, redirect_uri, state=None):
    from google_auth_oauthlib.flow import Flow
    cfg = {"web": {
        "client_id": client_id,
        "client_secret": client_secret,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": [redirect_uri],
    }}
    flow = Flow.from_client_config(cfg, scopes=SCOPES, state=state)
    flow.redirect_uri = redirect_uri
    # Disable PKCE. authorization_url() and the token exchange run as separate
    # Flow objects across a Streamlit redirect, so the auto-generated code_verifier
    # doesn't survive -> "invalid_grant: Missing code verifier". We're a
    # confidential web client (client_secret provides the security PKCE would),
    # so no verifier is needed as long as neither request uses PKCE.
    flow.autogenerate_code_verifier = False
    flow.code_verifier = None
    return flow


def auth_url(client_id, client_secret, redirect_uri):
    """Return (url, state). Send the user to url; Google redirects back with a code."""
    flow = _flow(client_id, client_secret, redirect_uri)
    url, state = flow.authorization_url(access_type="offline", prompt="consent")
    return url, state


def exchange_code(client_id, client_secret, redirect_uri, code, state=None):
    """Exchange the redirect ?code=... for a token; return token JSON string."""
    flow = _flow(client_id, client_secret, redirect_uri, state=state)
    flow.fetch_token(code=code)
    return flow.credentials.to_json()


def load_creds(token_json):
    """Rebuild Credentials from stored JSON, refreshing if expired.
    Returns (creds, possibly_updated_json)."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    creds = Credentials.from_authorized_user_info(json.loads(token_json), SCOPES)
    if (not creds.valid) and creds.refresh_token:
        creds.refresh(Request())
    return creds, creds.to_json()


def get_profile_email(creds):
    from googleapiclient.discovery import build
    svc = build("gmail", "v1", credentials=creds, cache_discovery=False)
    return svc.users().getProfile(userId="me").execute().get("emailAddress")


def send_email(creds, sender, to, subject, body):
    """Send one plain-text email; return the Gmail message id."""
    from googleapiclient.discovery import build
    svc = build("gmail", "v1", credentials=creds, cache_discovery=False)
    msg = MIMEText(body)
    msg["to"] = to
    if sender:
        msg["from"] = sender
    msg["subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    sent = svc.users().messages().send(userId="me", body={"raw": raw}).execute()
    return sent.get("id")

"""Find work emails for saved contacts via the Hunter.io API (free tier: ~25
lookups/month). The dashboard calls find_email() per contact that has a name +
company but no email; results include a confidence score (0-100) which we keep
so low-confidence guesses are visible before anyone hits send.

Setup: create a free account at hunter.io, copy the API key from
https://hunter.io/api-keys, then set HUNTER_API_KEY in .env (local) and in
Streamlit Cloud secrets (deployed).
"""
from __future__ import annotations

import os

import requests

API = "https://api.hunter.io/v2/email-finder"


def api_key() -> str | None:
    return os.environ.get("HUNTER_API_KEY") or None


def find_email(full_name: str, company: str, key: str,
               timeout: int = 20) -> dict:
    """Return {email, score, error}. Uses company name (Hunter resolves the
    domain); pass a domain in `company` for tighter matching if known."""
    name = (full_name or "").strip()
    if " " not in name or not company:
        return {"email": None, "score": None,
                "error": "need full name and company"}
    first, last = name.split()[0], name.split()[-1]
    params = {"first_name": first, "last_name": last, "api_key": key}
    # Hunter accepts either a domain or a company name.
    params["domain" if "." in company else "company"] = company.strip()
    try:
        r = requests.get(API, params=params, timeout=timeout)
        body = r.json()
    except Exception as e:
        return {"email": None, "score": None, "error": str(e)}
    if r.status_code != 200:
        errs = body.get("errors") or [{}]
        detail = errs[0].get("details") or errs[0].get("id") or f"HTTP {r.status_code}"
        return {"email": None, "score": None, "error": detail}
    data = body.get("data") or {}
    return {"email": data.get("email"), "score": data.get("score"), "error": None}

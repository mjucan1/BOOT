"""Build and email the weekly news digest. Run by the weekly scrape task.

Pulls Google News for Boot Barn + competitors and emails a clean HTML brief to
DIGEST_TO using the stored Gmail token. It sends only to your own configured
address -- it's a self-notification, not outreach.
"""
import datetime as dt
import os
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parent
if (PROJ / ".env").exists():
    for line in (PROJ / ".env").read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
sys.path.insert(0, str(PROJ))

from bbxray import db, gmail_send, news  # noqa: E402
from bbxray.utils import log             # noqa: E402


def main() -> None:
    to = os.environ.get("DIGEST_TO", "").strip()
    if not to:
        log("[digest] DIGEST_TO not set in .env; skipping.")
        return
    companies = [c.strip() for c in os.environ.get("DIGEST_COMPANIES", "").split(",")
                 if c.strip()] or None

    data = news.gather(companies)
    total = sum(len(v) for v in data.values())
    if total == 0:
        log("[digest] no news found this week; skipping send.")
        return

    tok = db.get_gmail_token()
    if not tok:
        log("[digest] Gmail not connected; connect it in the dashboard to receive "
            "the digest.")
        return
    try:
        creds, fresh = gmail_send.load_creds(tok)
        if fresh != tok:
            db.set_gmail_token(fresh)
    except Exception as e:
        log(f"[digest] Gmail token invalid ({e}); reconnect in the dashboard.")
        return

    subject = f"Western Retail Weekly — Boot Barn & peers ({dt.datetime.now():%b %d})"
    html_body = news.build_digest_html(data)
    try:
        gmail_send.send_email(creds, None, to, subject, html_body, subtype="html")
        log(f"[digest] sent to {to}: {total} items across {len(data)} companies.")
    except Exception as e:
        log(f"[digest] send failed: {e}")


if __name__ == "__main__":
    main()

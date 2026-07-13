"""Send any due scheduled emails. Run by Task Scheduler every ~20 minutes.

Reads the scheduled_emails queue from Supabase, sends those whose send_at has
passed (using the Gmail OAuth token stored in Supabase), and marks them
sent/failed. It only ever sends emails you already reviewed, approved, and
scheduled in the dashboard -- this script performs no composing of its own.
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

from bbxray import db, gmail_send  # noqa: E402
from bbxray.utils import log       # noqa: E402


def main() -> None:
    # naive-local now, matching how the dashboard stores send_at (both on this PC).
    now = dt.datetime.now().isoformat()
    due = db.due_scheduled(now)
    if not due:
        log("[schedule] nothing due.")
        return

    tok = db.get_gmail_token()
    if not tok:
        log(f"[schedule] {len(due)} due but Gmail isn't connected; "
            "reconnect in the dashboard.")
        return
    try:
        creds, fresh = gmail_send.load_creds(tok)
        if fresh != tok:
            db.set_gmail_token(fresh)
    except Exception as e:
        log(f"[schedule] Gmail token invalid ({e}); reconnect in the dashboard.")
        return

    sent_ts = dt.datetime.now(dt.timezone.utc).isoformat()
    ok = 0
    for e in due:
        try:
            gmail_send.send_email(creds, None, e["to_email"], e["subject"], e["body"])
            db.update_scheduled(e["id"], status="sent", sent_ts=sent_ts, error=None)
            ok += 1
            log(f"[schedule] sent -> {e['to_email']}")
        except Exception as ex:
            db.update_scheduled(e["id"], status="failed", error=str(ex)[:300])
            log(f"[schedule] FAILED {e['to_email']}: {ex}")
    log(f"[schedule] done: {ok}/{len(due)} sent.")


if __name__ == "__main__":
    main()

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

# Auto-send only if the PC is on within this many minutes of the scheduled time.
# If the machine was off longer than this (e.g. you next boot at midnight), the
# email is HELD as 'missed' rather than fired at an unintended hour.
GRACE_MIN = int(os.environ.get("SCHEDULE_GRACE_MINUTES", "120"))


def main() -> None:
    # naive-local now, matching how the dashboard stores send_at (both on this PC).
    now_dt = dt.datetime.now()
    due = db.due_scheduled(now_dt.isoformat())
    if not due:
        log("[schedule] nothing due.")
        return

    # Split timely (send) vs overdue-past-grace (hold as missed).
    to_send, missed = [], []
    for e in due:
        try:
            sched = dt.datetime.fromisoformat(e["send_at"])
        except Exception:
            sched = now_dt
        over = (now_dt - sched).total_seconds() / 60.0
        (missed if over > GRACE_MIN else to_send).append((e, over))

    for e, over in missed:
        db.update_scheduled(
            e["id"], status="missed",
            error=f"PC off near send time: {int(over)} min overdue "
                  f"(> {GRACE_MIN} min grace). Held, not auto-sent.")
        log(f"[schedule] MISSED (held, not sent) -> {e['to_email']} "
            f"({int(over)}m overdue)")

    if not to_send:
        log(f"[schedule] {len(missed)} held as missed; nothing timely to send.")
        return

    tok = db.get_gmail_token()
    if not tok:
        log(f"[schedule] {len(to_send)} due but Gmail isn't connected; reconnect.")
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
    for e, _ in to_send:
        try:
            gmail_send.send_email(creds, None, e["to_email"], e["subject"], e["body"])
            db.update_scheduled(e["id"], status="sent", sent_ts=sent_ts, error=None)
            ok += 1
            log(f"[schedule] sent -> {e['to_email']}")
        except Exception as ex:
            db.update_scheduled(e["id"], status="failed", error=str(ex)[:300])
            log(f"[schedule] FAILED {e['to_email']}: {ex}")
    log(f"[schedule] done: {ok}/{len(to_send)} sent, {len(missed)} held missed.")


if __name__ == "__main__":
    main()

"""Pull SEC EDGAR filings + financials for the public comp set into Supabase.

Run manually or from the weekly scrape. Data changes only ~quarterly, so a
weekly refresh is plenty. Private peers (Ariat, Tecovas, Cavender's) have no
filings and are simply absent.
"""
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

from bbxray import db, edgar  # noqa: E402
from bbxray.utils import log  # noqa: E402


def main() -> None:
    log("[ir] pulling SEC EDGAR filings + financials …")
    try:
        data = edgar.pull_all()
    except Exception as e:
        log(f"[ir] EDGAR fetch failed: {e}")
        return
    db.init_db()
    db.replace_financials(data["financials"])
    db.replace_sec_filings(data["filings"])
    log(f"[ir] stored {len(data['financials'])} financial points and "
        f"{len(data['filings'])} filings across "
        f"{len({r['ticker'] for r in data['financials']})} companies.")


if __name__ == "__main__":
    main()

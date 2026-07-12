"""Scrape the full Boot Barn store roster and snapshot it for diffing.

The `/stores-all` page renders the COMPLETE directory (all ~566 stores) in its
raw HTML as `<div class="store" store-id=.. store-name=..>` blocks -- no geo
gating once you read the raw markup. We parse every block, geocode the ZIP to
lat/long offline (pgeocode), and store a full snapshot. Comparing snapshots over
time reveals openings (new StoreIDs) and closures (vanished StoreIDs).
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
from bbxray import db  # noqa: E402
from bbxray.utils import PoliteSession, log  # noqa: E402

STORES_ALL_URL = f"{config.BASE_URL}/stores-all"
_ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")
_PHONE_RE = re.compile(r"\(\d{3}\)\s*\d{3}-\d{4}")


def _geocoder():
    """Return an offline ZIP->latlng geocoder, or None if pgeocode missing."""
    try:
        import pgeocode
        return pgeocode.Nominatim("us")
    except ImportError:
        log("  (pgeocode not installed -> no lat/long; `pip install pgeocode`)")
        return None


def parse_store_block(b) -> dict | None:
    sid = b.get("store-id")
    if not sid:
        return None
    addr = b.select_one(".address")
    txt = addr.get_text("\n", strip=True) if addr else ""
    lines = [ln for ln in txt.split("\n") if ln.strip()]
    city = addr.select_one(".address-city") if addr else None
    state = addr.select_one(".address-state") if addr else None
    zipm = _ZIP_RE.search(txt)
    phonem = _PHONE_RE.search(txt)
    return {
        "store_id": str(sid).strip(),
        "name": b.get("store-name"),
        "address": lines[0] if lines else None,
        "city": city.get_text(strip=True) if city else None,
        "state": state.get_text(strip=True) if state else None,
        "zip": zipm.group(1) if zipm else None,
        "phone": phonem.group(0) if phonem else None,
        "url": f"{config.BASE_URL}/stores?StoreID={sid}",
    }


def run() -> int:
    run_ts = dt.datetime.now(dt.timezone.utc).isoformat()
    sess = PoliteSession()
    db.init_db()

    log("[stores] fetching full directory /stores-all ...")
    r = sess.get(STORES_ALL_URL)
    if not r or r.status_code != 200:
        log(f"[stores] failed to fetch ({r.status_code if r else 'no resp'}); aborting.")
        return 0

    soup = BeautifulSoup(r.text, "html.parser")
    blocks = soup.select("div.store[store-id]")
    log(f"[stores] parsed {len(blocks)} store blocks.")

    geo = _geocoder()
    rows: dict[str, dict] = {}
    for b in blocks:
        rec = parse_store_block(b)
        if not rec:
            continue
        rec["run_ts"] = run_ts
        rec["hours_json"] = ""  # detail hours available per-store page if needed
        rec["lat"] = rec["lng"] = None
        if geo and rec["zip"]:
            g = geo.query_postal_code(rec["zip"])
            lat, lng = getattr(g, "latitude", None), getattr(g, "longitude", None)
            rec["lat"] = float(lat) if lat == lat and lat is not None else None  # NaN guard
            rec["lng"] = float(lng) if lng == lng and lng is not None else None
        rows[rec["store_id"]] = rec  # dedupe by StoreID

    out = list(rows.values())
    db.insert_stores(out)
    db.record_run("stores", run_ts, len(out),
                  notes=f"parsed {len(blocks)} blocks from /stores-all")
    log(f"[stores] done: {len(out)} unique stores snapshotted "
        f"({sum(1 for r in out if r['lat'] is not None)} geocoded).")
    return len(out)


if __name__ == "__main__":
    run()

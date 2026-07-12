"""Backfill historical Boot Barn pricing from the Wayback Machine (archive.org).

Why this shape: querying the CDX API per product is slow and mostly wasted (many
SKUs have zero captures). Instead we ask the archive for its ENTIRE list of Boot
Barn product-page captures in a few bulk CDX calls, dedupe to one per
(product, quarter), then fetch only pages that actually exist.

Each archived page is parsed with era-tolerant heuristics (markup changed over
the years): schema.org microdata -> JSON-LD -> price meta tags -> embedded JSON.
Rows land in price_snapshots with source='wayback' and run_ts = the CAPTURE date,
so they flow straight into the existing price-over-time and category-trend charts.

Best-effort: coverage is sparse and skewed to popular / long-lived products.

Usage:
    python -m bbxray.scrape_wayback            # default cap
    python -m bbxray.scrape_wayback 6000       # fetch up to 6000 snapshots
"""
from __future__ import annotations

import datetime as dt
import json
import random
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bbxray import db  # noqa: E402
from bbxray.scrape_prices import _clean_price, classify_category  # noqa: E402
from bbxray.utils import log  # noqa: E402

CDX = "http://web.archive.org/cdx/search/cdx"
UA = "Mozilla/5.0 (research; boot-barn-xray historical pricing)"
_PID_RE = re.compile(r"/(\d{5,})\.html")


def _http_get(url: str, timeout: int = 60, retries: int = 3) -> bytes:
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            return urllib.request.urlopen(req, timeout=timeout).read()
        except Exception as e:  # noqa: BLE001 -- archive.org is flaky; back off
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise last  # type: ignore[misc]


def cdx_product_snapshots() -> list[tuple[str, str]]:
    """Every archived Boot Barn product page as (url, timestamp), via paged CDX."""
    params = [
        ("url", "bootbarn.com"), ("matchType", "domain"),
        ("filter", "statuscode:200"), ("filter", "mimetype:text/html"),
        ("filter", r"original:.*[0-9]{5,}\.html.*"),   # product detail pages
        ("collapse", "digest"),                         # drop unchanged re-captures
        ("fl", "original,timestamp"), ("output", "json"),
    ]
    base = CDX + "?" + urllib.parse.urlencode(params)
    out: list[tuple[str, str]] = []
    resume = None
    page = 0
    while True:
        url = base + "&limit=15000&showResumeKey=true"
        if resume:
            url += "&resumeKey=" + urllib.parse.quote(resume)
        rows = json.loads(_http_get(url).decode("utf-8", "ignore"))
        resume = None
        added = 0
        for r in rows:
            if len(r) == 1:                       # trailing resumeKey row
                resume = r[0]
            elif len(r) >= 2 and str(r[1]).isdigit():   # skip header row
                out.append((r[0], r[1]))
                added += 1
        page += 1
        log(f"  cdx page {page}: +{added} (total {len(out)})"
            + (" [more]" if resume else " [done]"))
        if not resume:
            break
        time.sleep(0.4)
    return out


def _quarter(ts: str) -> str:
    return f"{ts[:4]}Q{(int(ts[4:6]) - 1) // 3 + 1}"


def select_snapshots(snaps: list[tuple[str, str]], cap: int) -> list[tuple[str, str]]:
    """One capture per (url, quarter); random-sample down to cap if needed."""
    seen: dict[tuple[str, str], tuple[str, str]] = {}
    for url, ts in snaps:
        key = (url, _quarter(ts))
        seen.setdefault(key, (url, ts))
    picks = list(seen.values())
    random.shuffle(picks)
    return picks[:cap] if cap and len(picks) > cap else picks


def _jsonld_price(soup) -> float | None:
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except Exception:  # noqa: BLE001
            continue
        for obj in (data if isinstance(data, list) else [data]):
            if not isinstance(obj, dict):
                continue
            if str(obj.get("@type", "")).lower() == "product":
                offers = obj.get("offers") or {}
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                p = _clean_price(offers.get("price") if isinstance(offers, dict) else None)
                if p:
                    return p
    return None


def parse_archived(html: str, url: str) -> dict | None:
    """Era-tolerant price/name extraction from an archived PDP."""
    soup = BeautifulSoup(html, "html.parser")

    price = None
    el = soup.select_one('[itemprop="price"]')           # 1) schema.org microdata
    if el:
        price = _clean_price(el.get("content") or el.get_text())
    if price is None:                                     # 2) JSON-LD
        price = _jsonld_price(soup)
    if price is None:                                     # 3) price meta tags
        for sel in ('meta[property="product:price:amount"]',
                    'meta[property="og:price:amount"]',
                    'meta[itemprop="price"]'):
            m = soup.select_one(sel)
            if m and m.get("content"):
                price = _clean_price(m["content"])
                if price:
                    break
    if price is None:                                     # 4) embedded JSON
        m = re.search(r'"(?:price|salePrice|unitPrice|current[Pp]rice)"\s*:\s*"?\$?'
                      r'([0-9]{1,4}\.[0-9]{2})', html)
        if m:
            price = _clean_price(m.group(1))
    if not price or price <= 0:
        return None

    name = None
    for sel in ('[itemprop="name"]', 'meta[property="og:title"]', "h1", "title"):
        e = soup.select_one(sel)
        if e:
            name = e.get("content") or e.get_text(strip=True)
            if name:
                break
    if name:
        name = re.sub(r"^\s*Product Name:\s*", "", name)
        name = re.sub(r"\s*\|\s*Boot Barn.*$", "", name)   # strip site suffix in <title>
        name = re.sub(r"\s+", " ", name.replace("\xa0", " ")).strip() or None

    # list price (best-effort across eras)
    list_price = None
    orig = soup.select_one(".price-original strong, .price-standard, "
                           '[itemprop="highPrice"]')
    if orig is not None:
        cents = orig.get("price-for-currency-conversion")
        list_price = (float(cents) / 100 if cents and str(cents).isdigit()
                      else _clean_price(orig.get_text()))
    if not list_price or list_price < price:
        list_price = price
    sale_price = price if list_price and price < list_price else None

    pid_m = _PID_RE.search(url)
    pid = pid_m.group(1) if pid_m else ""
    return {
        "product_id": pid, "sku": pid, "name": name, "brand": None,
        "category": classify_category(name, url), "url": url,
        "list_price": list_price, "sale_price": sale_price, "currency": "USD",
        "availability": None, "in_stock": None, "map_hidden": 0, "source": "wayback",
    }


def run(cap: int = 4000) -> int:
    db.init_db()
    log("[wayback] fetching CDX list of archived product pages...")
    snaps = cdx_product_snapshots()
    log(f"[wayback] {len(snaps)} archived product captures found.")
    picks = select_snapshots(snaps, cap)
    log(f"[wayback] fetching {len(picks)} snapshots (1 per product/quarter, "
        f"capped {cap})...")

    rows: list[dict] = []
    ok = miss = 0
    for i, (url, ts) in enumerate(picks, 1):
        try:
            html = _http_get(f"http://web.archive.org/web/{ts}id_/{url}",
                             timeout=45).decode("utf-8", "replace")
            rec = parse_archived(html, url)
        except Exception:  # noqa: BLE001
            rec = None
        if rec:
            rec["run_ts"] = (f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}T"
                             f"{ts[8:10]}:{ts[10:12]}:{ts[12:14]}+00:00")
            rows.append(rec)
            ok += 1
        else:
            miss += 1
        if i % 50 == 0:
            log(f"  [{i}/{len(picks)}] ok={ok} miss={miss}")
            db.insert_prices(rows)   # incremental commit so a long run is resumable
            rows = []
        time.sleep(0.35)

    if rows:
        db.insert_prices(rows)
    db.record_run("prices_wayback", dt.datetime.now(dt.timezone.utc).isoformat(), ok,
                  notes=f"wayback backfill: {ok} parsed, {miss} missed of {len(picks)}")
    log(f"[wayback] done: {ok} historical price rows (missed {miss}).")
    return ok


if __name__ == "__main__":
    run(int(sys.argv[1]) if len(sys.argv) > 1 else 4000)

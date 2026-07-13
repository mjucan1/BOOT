"""Scrape aggregated product pricing from bootbarn.com.

Strategy (robust against markup changes):
  1. Read sitemap_index.xml -> find product sitemaps.
  2. Collect product-detail-page (PDP) URLs.
  3. For each PDP, parse the embedded JSON-LD <script type="application/ld+json">
     Product object -> name, brand, sku, offers.price / availability.
JSON-LD is a stable, structured source; we fall back to HTML meta tags only if
JSON-LD is missing.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
from bbxray import db  # noqa: E402
from bbxray.utils import PoliteSession, log  # noqa: E402

_SM_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


def _clean_price(val) -> float | None:
    if val is None:
        return None
    try:
        return float(re.sub(r"[^0-9.]", "", str(val)))
    except ValueError:
        return None


def get_product_sitemaps(sess: PoliteSession) -> list[str]:
    r = sess.get(config.SITEMAP_INDEX)
    if not r or r.status_code != 200:
        log(f"  ! could not fetch sitemap index ({r.status_code if r else 'no resp'})")
        return []
    try:
        root = ET.fromstring(r.content)
    except ET.ParseError as e:
        log(f"  ! sitemap index parse error: {e}")
        return []
    urls = [loc.text.strip() for loc in root.iter(f"{_SM_NS}loc") if loc.text]
    # Product sitemaps typically contain 'product' or 'pdp' in the filename.
    prod = [u for u in urls if re.search(r"product|pdp|catalog", u, re.I)]
    log(f"  found {len(urls)} sitemaps ({len(prod)} look like product sitemaps)")
    return prod or urls  # fall back to all if naming is opaque


def get_product_urls(sess: PoliteSession, sitemaps: list[str],
                     limit: int) -> list[str]:
    all_urls: list[str] = []
    for sm in sitemaps:
        r = sess.get(sm)
        if not r or r.status_code != 200:
            continue
        try:
            root = ET.fromstring(r.content)
        except ET.ParseError:
            continue
        locs = [loc.text.strip() for loc in root.iter(f"{_SM_NS}loc") if loc.text]
        pdps = [u for u in locs if u.endswith(".html")]  # PDP = /<slug>/<id>.html
        all_urls.extend(pdps or locs)

    # When capped, stride-sample across the FULL catalog so we get category
    # variety rather than only the first (jeans-heavy) sitemap.
    if limit and len(all_urls) > limit:
        stride = len(all_urls) / limit
        out = [all_urls[int(i * stride)] for i in range(limit)]
    else:
        out = all_urls
    log(f"  collected {len(out)} product URLs (from {len(all_urls)} total)")
    return out


# Boot Barn has no product category in static HTML (breadcrumb is JS-rendered),
# so we classify by keywords in the name/URL. Order matters: first match wins.
_CATEGORY_RULES = [
    ("Work Boots", r"work boot|composite toe|steel toe|safety toe|waterproof work"),
    ("Western Boots", r"western boot|cowboy boot|cowgirl|roper|square toe|snip toe"),
    ("Boots", r"\bboot(s)?\b(?!\s*cut)"),   # not "boot cut [jeans]"
    ("Outerwear", r"jacket|vest|\bcoat\b|sportscoat|blazer|hoodie|pullover|outerwear"),
    ("Shirts", r"\bshirt|\btee\b|\bt-shirt|flannel|henley|polo|crop top|tank top"),
    ("Pants & Shorts", r"\bshort(s)?\b|trouser|chino|legging|\bpant(s)?\b"),
    ("Jeans", r"\bjean(s)?\b|denim"),
    ("Hats", r"\bhat(s)?\b|\bcap\b|beanie"),
    ("Belts & Buckles", r"belt|buckle"),
    ("Boot Care", r"boot care|conditioner|polish|cleaner"),
    ("Accessories", r"wallet|sock|glove|scarf|bandana|jewelry|sunglass|bag|backpack"),
    ("Footwear", r"shoe|sneaker|sandal|moccasin|slipper|clog"),
]


def classify_category(name: str | None, url: str) -> str | None:
    hay = f"{name or ''} {url}".lower()
    for label, pat in _CATEGORY_RULES:
        if re.search(pat, hay):
            return label
    return None


def _itemprop(soup, name: str):
    el = soup.select_one(f'[itemprop="{name}"]')
    if not el:
        return None
    return (el.get("content") or el.get_text(strip=True)) or None


def parse_pdp(html: str, url: str) -> dict | None:
    """Parse a Boot Barn PDP.

    Boot Barn has no JSON-LD; it exposes schema.org *microdata* plus a
    ``.price-original`` block with an exact cents attribute. Note: many products
    use MAP (Minimum Advertised Price) and hide the sale price until cart
    (``display-mode="HidePromotionAndSalesPricing"``), so the public signal is
    the displayed price + the original price.
    """
    soup = BeautifulSoup(html, "html.parser")

    price = _clean_price(_itemprop(soup, "price"))
    if price is None:
        return None  # no price microdata -> not a parseable PDP
    availability = (_itemprop(soup, "availability") or "").split("/")[-1]
    product_id = _itemprop(soup, "productID") or ""

    name = _itemprop(soup, "name") or ""
    name = re.sub(r"^\s*Product Name:\s*", "", name)
    name = re.sub(r"\s+", " ", name.replace("\xa0", " ")).strip() or None

    # Original (list) price from the dedicated block; the cents attr is exact.
    list_price = None
    orig = soup.select_one(".price-original strong")
    if orig is not None:
        cents = orig.get("price-for-currency-conversion")
        list_price = (float(cents) / 100 if cents and cents.isdigit()
                      else _clean_price(orig.get_text()))
    if list_price is None:
        list_price = price

    # If the displayed price is below the original, it's an (visible) sale.
    sale_price = price if (list_price and price < list_price) else None
    map_hidden = "HidePromotionAndSalesPricing" in html

    # Brand comes cleanly from the Affirm widget; category isn't in static HTML
    # so we derive it by keyword classification of the name/URL.
    affirm = soup.select_one(".affirm-as-low-as, [data-brand]")
    brand = affirm.get("data-brand") if affirm else None
    category = classify_category(name, url)
    sku = (affirm.get("data-sku") if affirm else None) or product_id

    return {
        "product_id": str(product_id),
        "sku": str(sku),
        "name": name,
        "brand": brand,
        "category": category,
        "url": url,
        "list_price": list_price,
        "sale_price": sale_price,
        "currency": _itemprop(soup, "priceCurrency") or "USD",
        "availability": (availability or None),
        "in_stock": 1 if availability.lower() == "instock" else 0,
        "map_hidden": 1 if map_hidden else 0,
        "source": "live",
    }


def run(limit: int | None = None) -> int:
    limit = config.MAX_PRODUCTS if limit is None else limit
    run_ts = dt.datetime.now(dt.timezone.utc).isoformat()
    sess = PoliteSession()
    db.init_db()

    log("[prices] discovering product sitemaps...")
    sitemaps = get_product_sitemaps(sess)
    if not sitemaps:
        log("[prices] no sitemaps; aborting.")
        return 0
    urls = get_product_urls(sess, sitemaps, limit)

    rows, ok, miss = [], 0, 0
    for i, url in enumerate(urls, 1):
        r = sess.get(url)
        if not r or r.status_code != 200:
            miss += 1
            continue
        rec = parse_pdp(r.content.decode("utf-8", "replace"), url)
        if rec:
            rec["run_ts"] = run_ts
            rows.append(rec)
            ok += 1
        else:
            miss += 1
        if i % 25 == 0:
            log(f"  [{i}/{len(urls)}] ok={ok} miss={miss}")

    db.insert_prices(rows)
    db.record_run("prices", run_ts, len(rows),
                  notes=f"{ok} parsed, {miss} missed of {len(urls)} urls")
    log(f"[prices] done: stored {len(rows)} products (missed {miss}).")
    return len(rows)


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else None
    run(n)

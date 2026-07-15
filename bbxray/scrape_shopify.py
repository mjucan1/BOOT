"""Scrape Boot Barn exclusive-brand DTC Shopify sites via /products.json.

Shopify exposes every product + variant + price as clean JSON at
    {site}/products.json?limit=250&page=N
-- no HTML parsing and no bot wall. We snapshot each configured brand site
(config.BRAND_SITES) into brand_prices, one row per product per run, so
private-label pricing gets the same over-time treatment as the main catalog.
Product price = the lowest variant price ("from"); compare_at = the highest
compare-at (Shopify's list/MSRP) so we can flag sales.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
from bbxray import db  # noqa: E402
from bbxray.utils import log  # noqa: E402

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) bootbarn-xray/0.1 (research)"


def _get(url: str, timeout: int = 25, retries: int = 3) -> bytes:
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            return urllib.request.urlopen(req, timeout=timeout).read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise last  # type: ignore[misc]


def _fnum(v) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def fetch_products(site: str, max_products: int | None = None) -> list[dict]:
    """All products for a Shopify site, paging until empty (or max_products)."""
    out: list[dict] = []
    page = 1
    while page <= 60:  # 60 * 250 = 15k product safety ceiling
        data = json.loads(_get(f"https://{site}/products.json?limit=250&page={page}"))
        prods = data.get("products", [])
        if not prods:
            break
        out.extend(prods)
        if max_products and len(out) >= max_products:
            return out[:max_products]
        page += 1
        time.sleep(config.REQUEST_DELAY_SEC * 0.3)
    return out


def product_row(p: dict, brand: str, site: str, run_ts: str) -> dict:
    variants = p.get("variants") or []
    prices = [x for x in (_fnum(v.get("price")) for v in variants) if x is not None]
    compares = [x for x in (_fnum(v.get("compare_at_price")) for v in variants)
                if x is not None]
    price = min(prices) if prices else None
    compare = max(compares) if compares else None
    return {
        "run_ts": run_ts, "brand": p.get("vendor") or brand, "site": site,
        "product_id": str(p.get("id")), "handle": p.get("handle"),
        "title": p.get("title"), "product_type": p.get("product_type") or None,
        "price": price, "compare_at_price": compare,
        "on_sale": 1 if (compare and price and compare > price) else 0,
        "available": 1 if any(v.get("available") for v in variants) else 0,
        "n_variants": len(variants),
        "url": f"https://{site}/products/{p.get('handle')}",
        "product_created_at": p.get("created_at"),
        "published_at": p.get("published_at"),
    }


def run() -> int:
    run_ts = dt.datetime.now(dt.timezone.utc).isoformat()
    db.init_db()
    total = 0
    for brand, site in config.BRAND_SITES.items():
        log(f"[brands] {brand} ({site}) ...")
        try:
            prods = fetch_products(site)
        except Exception as e:  # noqa: BLE001
            log(f"  ! failed to fetch {site}: {e}")
            continue
        rows = [product_row(p, brand, site, run_ts) for p in prods]
        db.insert_brand_prices(rows)
        total += len(rows)
        log(f"  + {len(rows)} products")
    db.record_run("brands", run_ts, total,
                  notes=f"{len(config.BRAND_SITES)} private-label Shopify sites")
    log(f"[brands] done: {total} products across {len(config.BRAND_SITES)} sites.")
    return total


def _competitor_row(p: dict, competitor: str, site: str, run_ts: str) -> dict:
    from bbxray.scrape_prices import bucket7
    base = product_row(p, competitor, site, run_ts)
    # Shopify tags usually carry the gender ("Womens", "Men's Boots") that thin
    # titles like "The Jane" lack -- fold them into the classification text.
    tags = p.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    hay = f"{base['title'] or ''} {base['product_type'] or ''} {' '.join(tags)}"
    return {
        "run_ts": run_ts, "competitor": competitor,
        "brand": p.get("vendor") or competitor, "site": site,
        "product_id": base["product_id"], "title": base["title"],
        "product_type": base["product_type"],
        "category": bucket7(hay, base["url"]),
        "price": base["price"], "compare_at_price": base["compare_at_price"],
        "on_sale": base["on_sale"], "available": base["available"],
        "url": base["url"],
    }


def run_competitors(max_per_site: int = 600) -> int:
    """Snapshot competitor DTC catalogs (config.COMPETITOR_SITES) for comparison."""
    run_ts = dt.datetime.now(dt.timezone.utc).isoformat()
    db.init_db()
    total = 0
    for comp, site in config.COMPETITOR_SITES.items():
        log(f"[competitors] {comp} ({site}) ...")
        try:
            prods = fetch_products(site, max_products=max_per_site)
        except Exception as e:  # noqa: BLE001
            log(f"  ! failed to fetch {site}: {e}")
            continue
        rows = [_competitor_row(p, comp, site, run_ts) for p in prods]
        db.insert_competitor_prices(rows)
        total += len(rows)
        log(f"  + {len(rows)} products")
    db.record_run("competitors", run_ts, total,
                  notes=f"{len(config.COMPETITOR_SITES)} competitor Shopify sites")
    log(f"[competitors] done: {total} products across "
        f"{len(config.COMPETITOR_SITES)} sites.")
    return total


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "competitors":
        run_competitors()
    else:
        run()

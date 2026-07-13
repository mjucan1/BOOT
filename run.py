"""CLI entry point for Boot Barn X-Ray scrapers.

Examples:
  python run.py prices           # scrape pricing (respects BBXRAY_MAX_PRODUCTS)
  python run.py prices 100       # scrape only 100 products (quick test)
  python run.py stores           # snapshot the full store roster
  python run.py brands           # snapshot private-label Shopify sites
  python run.py all              # prices + stores + brands
  python run.py initdb           # just create the SQLite schema
"""
from __future__ import annotations

import sys

from bbxray import db, scrape_prices, scrape_shopify, scrape_stores


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "all"
    if cmd == "initdb":
        db.init_db()
        print("DB initialized.")
    elif cmd == "prices":
        limit = int(argv[2]) if len(argv) > 2 else None
        scrape_prices.run(limit)
    elif cmd == "stores":
        scrape_stores.run()
    elif cmd == "brands":
        scrape_shopify.run()
    elif cmd == "competitors":
        scrape_shopify.run_competitors()
    elif cmd == "all":
        scrape_prices.run()
        scrape_stores.run()
        scrape_shopify.run()
        scrape_shopify.run_competitors()
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

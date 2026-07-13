"""Central configuration for the Boot Barn X-Ray scraper + dashboard."""
from __future__ import annotations

import os
from pathlib import Path

# --- Paths -------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "bootbarn.sqlite"

# --- Storage backend ---------------------------------------------------------
# Local dev defaults to SQLite. For the shared setup (PC scraper writes, cloud
# dashboard reads), set DATABASE_URL to your Supabase/Neon Postgres URL, e.g.
#   postgresql+psycopg2://user:pass@host:5432/dbname?sslmode=require
# Streamlit Cloud reads it from st.secrets (see dashboard.py).
DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{DB_PATH.as_posix()}")

# --- Target site -------------------------------------------------------------
BASE_URL = "https://www.bootbarn.com"
SITEMAP_INDEX = f"{BASE_URL}/sitemap_index.xml"

# SFCC (Salesforce Commerce Cloud / Demandware) store-finder controller.
# Boot Barn's site id is "bootbarn"; the finder returns JSON with a list of
# stores near a lat/long or postal code. We sweep a grid of points to get all.
STORE_FIND_URL = (
    f"{BASE_URL}/on/demandware.store/Sites-bootbarn-Site/default/Stores-FindStores"
)

# --- Politeness --------------------------------------------------------------
# Be a good citizen: real browser UA, gentle rate limit, retries with backoff.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 bootbarn-xray/0.1 "
    "(research; contact: %s)" % os.environ.get("BBXRAY_CONTACT", "m26jucan@gmail.com")
)
REQUEST_DELAY_SEC = float(os.environ.get("BBXRAY_DELAY", "0.75"))
# Randomized jitter added on top of the delay (fraction of delay, 0..this).
# Fixed intervals look robotic; jitter makes the crawl look human.
REQUEST_JITTER = float(os.environ.get("BBXRAY_JITTER", "0.5"))
REQUEST_TIMEOUT = 30
MAX_RETRIES = 3

# Optional proxy. Point this at a rotating *residential* proxy gateway (Bright
# Data, IPRoyal, Decodo, etc.) and every request exits from a different home IP
# -- the real way to get "multiple IPs" from one machine. One gateway URL is
# enough; the provider rotates the exit IP per request. Leave blank to go direct.
#   e.g. http://user:pass@gate.provider.com:7777
PROXY_URL = os.environ.get("BBXRAY_PROXY", "").strip()

# --- Pricing scrape limits ---------------------------------------------------
# Full catalog is large; cap per-run so a first run is quick. Set to 0 = no cap.
MAX_PRODUCTS = int(os.environ.get("BBXRAY_MAX_PRODUCTS", "500"))

# --- Private-label brand DTC sites (Shopify) ---------------------------------
# Boot Barn's exclusive brands run their own Shopify stores, which expose every
# product/price at {site}/products.json. brand -> domain. Add/remove freely.
BRAND_SITES = {
    "Idyllwind": "idyllwind.com",
    "Cody James": "codyjames.com",
    "Shyanne": "shyanne.com",
    "Moonshine Spirit": "moonshinespirit.com",
}

# Competitor DTC sites on Shopify (clean /products.json). Spans price tiers:
# value (Durango) -> mid (Tecovas/Dan Post/Twisted X) -> premium (Lucchese/Lane).
COMPETITOR_SITES = {
    "Tecovas": "tecovas.com",
    "Lucchese": "lucchese.com",
    "Durango": "durangoboots.com",
    "Dan Post": "danpostboots.com",
    "Twisted X": "twistedx.com",
    "Lane Boots": "laneboots.com",
    "Kimes Ranch": "kimesranch.com",
}

# --- Foot traffic (Dewey) ----------------------------------------------------
DEWEY_API_KEY = os.environ.get("DEWEY_API_KEY", "")
# Product path for the dataset you licensed on Dewey (e.g. Advan Monthly Patterns).
DEWEY_PRODUCT_PATH = os.environ.get("DEWEY_PRODUCT_PATH", "")
DEWEY_DOWNLOAD_DIR = DATA_DIR / "dewey_raw"

# Substring used to keep only Boot Barn POIs when filtering a patterns dataset.
BOOTBARN_NAME_MATCH = "boot barn"

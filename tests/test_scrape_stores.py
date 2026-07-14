"""Unit tests for bbxray.scrape_stores.parse_store_block (no network, no DB)."""
from bs4 import BeautifulSoup

import config
from bbxray.scrape_stores import parse_store_block


def _blocks(html: str):
    return BeautifulSoup(html, "html.parser").select("div.store")


def test_full_store_block(fixture_html):
    blocks = _blocks(fixture_html("stores_all_snippet.html"))
    rec = parse_store_block(blocks[0])
    assert rec == {
        "store_id": "083",
        "name": "Boot Barn El Paso",
        "address": "11855 Gateway Blvd W",
        "city": "El Paso",
        "state": "TX",
        "zip": "79936",                    # ZIP+4 trimmed to the 5-digit ZIP
        "phone": "(915) 555-0134",
        "url": f"{config.BASE_URL}/stores?StoreID=083",
    }


def test_store_id_whitespace_is_stripped(fixture_html):
    blocks = _blocks(fixture_html("stores_all_snippet.html"))
    rec = parse_store_block(blocks[1])
    assert rec["store_id"] == "512"
    assert rec["zip"] == "93309"
    assert rec["state"] == "CA"


def test_block_without_store_id_is_skipped(fixture_html):
    blocks = _blocks(fixture_html("stores_all_snippet.html"))
    assert parse_store_block(blocks[2]) is None


def test_block_without_address_yields_none_fields(fixture_html):
    blocks = _blocks(fixture_html("stores_all_snippet.html"))
    rec = parse_store_block(blocks[3])
    assert rec["store_id"] == "900"
    assert rec["address"] is None
    assert rec["city"] is None
    assert rec["state"] is None
    assert rec["zip"] is None
    assert rec["phone"] is None

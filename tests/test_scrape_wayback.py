"""Unit tests for bbxray.scrape_wayback parsing/selection (no network, no DB).

parse_archived() has a 4-tier fallback chain (microdata -> JSON-LD -> price meta
tags -> embedded JSON); each tier gets its own era fixture.
"""
from bbxray.scrape_wayback import _quarter, parse_archived, select_snapshots

URL = "https://www.bootbarn.com/some-product/2000123456.html"


class TestQuarter:
    def test_quarters(self):
        assert _quarter("20250115120000") == "2025Q1"
        assert _quarter("20250401000000") == "2025Q2"
        assert _quarter("20180930235959") == "2018Q3"
        assert _quarter("20251231000000") == "2025Q4"


class TestSelectSnapshots:
    def test_dedupes_to_one_capture_per_url_quarter(self):
        snaps = [
            (URL, "20250110000000"),   # Q1, first seen -> kept
            (URL, "20250315000000"),   # Q1 duplicate -> dropped
            (URL, "20250701000000"),   # Q3 -> kept
            ("https://www.bootbarn.com/other/2000999999.html", "20250110000000"),
        ]
        picks = select_snapshots(snaps, cap=0)
        assert len(picks) == 3
        assert set(picks) == {snaps[0], snaps[2], snaps[3]}

    def test_cap_limits_output(self):
        snaps = [(f"https://www.bootbarn.com/p/{10000 + i}.html", "20250110000000")
                 for i in range(10)]
        picks = select_snapshots(snaps, cap=3)
        assert len(picks) == 3
        assert set(picks) <= set(snaps)


class TestParseArchived:
    def test_tier1_microdata(self, fixture_html):
        rec = parse_archived(fixture_html("wayback_microdata.html"), URL)
        assert rec is not None
        assert rec["list_price"] == 199.99        # .price-standard era block
        assert rec["sale_price"] == 149.99        # displayed < list
        assert rec["name"] == "Ariat Roughstock Western Boot"
        assert rec["category"] == "Western Boots"
        assert rec["product_id"] == "2000123456"  # from the URL
        assert rec["source"] == "wayback"

    def test_tier2_jsonld(self, fixture_html):
        rec = parse_archived(fixture_html("wayback_jsonld.html"), URL)
        assert rec is not None
        assert rec["list_price"] == 89.50         # no list block -> list = price
        assert rec["sale_price"] is None
        # Non-Product JSON-LD (BreadcrumbList) must be skipped, offers-as-list handled.
        assert rec["name"] == "Corral Snip Toe Boot"   # "| Boot Barn" suffix stripped
        assert rec["category"] == "Western Boots"

    def test_tier3_price_meta_tag(self, fixture_html):
        rec = parse_archived(fixture_html("wayback_meta.html"), URL)
        assert rec is not None
        # A "list" below the displayed price is nonsense -> coerced to price.
        assert rec["list_price"] == 59.99
        assert rec["sale_price"] is None
        assert rec["name"] == "Wrangler Boot Cut Jeans"
        assert rec["category"] == "Jeans"         # boot-cut must not match Boots

    def test_tier4_embedded_json(self, fixture_html):
        rec = parse_archived(fixture_html("wayback_embedded_json.html"), URL)
        assert rec is not None
        assert rec["list_price"] == 79.99
        assert rec["name"] == "Cody James Leather Belt"  # <title>, suffix stripped
        assert rec["category"] == "Belts & Buckles"

    def test_no_price_anywhere_returns_none(self):
        assert parse_archived("<html><body><h1>Boot</h1></body></html>", URL) is None

    def test_zero_price_returns_none(self):
        html = '<html><body><span itemprop="price" content="0.00"></span></body></html>'
        assert parse_archived(html, URL) is None

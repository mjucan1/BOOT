"""Unit tests for bbxray.scrape_shopify row builders (no network, no DB)."""
from bbxray.scrape_shopify import _competitor_row, _fnum, product_row

RUN_TS = "2025-07-14T00:00:00+00:00"


class TestFnum:
    def test_conversions(self):
        assert _fnum("12.5") == 12.5
        assert _fnum(40) == 40.0
        assert _fnum("") is None
        assert _fnum(None) is None
        assert _fnum("n/a") is None


class TestProductRow:
    def test_multi_variant_sale_product(self):
        p = {
            "id": 123, "handle": "test-boot", "title": "Test Western Boot",
            "vendor": "Idyllwind", "product_type": "Boots",
            "created_at": "2024-01-01T00:00:00-05:00",
            "published_at": "2024-01-02T00:00:00-05:00",
            "variants": [
                {"price": "49.99", "compare_at_price": "59.99", "available": True},
                {"price": "39.99", "compare_at_price": None, "available": False},
            ],
        }
        row = product_row(p, "Idyllwind", "idyllwind.com", RUN_TS)
        assert row["price"] == 39.99              # lowest variant ("from" price)
        assert row["compare_at_price"] == 59.99   # highest compare-at (list/MSRP)
        assert row["on_sale"] == 1
        assert row["available"] == 1              # any variant available
        assert row["n_variants"] == 2
        assert row["brand"] == "Idyllwind"
        assert row["product_id"] == "123"
        assert row["url"] == "https://idyllwind.com/products/test-boot"
        assert row["run_ts"] == RUN_TS
        assert row["product_created_at"] == "2024-01-01T00:00:00-05:00"

    def test_no_variants(self):
        p = {"id": 1, "handle": "x", "title": "Empty", "vendor": None}
        row = product_row(p, "Cody James", "codyjames.com", RUN_TS)
        assert row["price"] is None
        assert row["compare_at_price"] is None
        assert row["on_sale"] == 0
        assert row["available"] == 0
        assert row["n_variants"] == 0
        assert row["brand"] == "Cody James"       # vendor missing -> brand arg

    def test_compare_at_equal_to_price_is_not_a_sale(self):
        p = {"id": 2, "handle": "y", "title": "Full Price",
             "variants": [{"price": "50.00", "compare_at_price": "50.00",
                           "available": True}]}
        row = product_row(p, "b", "s.com", RUN_TS)
        assert row["on_sale"] == 0

    def test_unparseable_variant_prices_are_ignored(self):
        p = {"id": 3, "handle": "z", "title": "Bad Data",
             "variants": [{"price": "", "compare_at_price": "", "available": True}]}
        row = product_row(p, "b", "s.com", RUN_TS)
        assert row["price"] is None
        assert row["on_sale"] == 0
        assert row["available"] == 1


class TestCompetitorRow:
    def test_adds_competitor_and_category(self):
        # Category uses the 7-bucket reporting scheme; gender comes from Shopify
        # tags (titles like "The Jane" rarely carry it).
        p = {"id": 9, "handle": "snip", "title": "Snip Toe Western Boot",
             "vendor": "Tecovas", "product_type": "Boots",
             "tags": ["Womens", "Exotic"],
             "variants": [{"price": "255.00", "compare_at_price": None,
                           "available": True}]}
        row = _competitor_row(p, "Tecovas", "tecovas.com", RUN_TS)
        assert row["competitor"] == "Tecovas"
        assert row["category"] == "Ladies' Western Boot"
        assert row["price"] == 255.0
        assert row["on_sale"] == 0
        assert "compare_at_price" in row and row["compare_at_price"] is None

    def test_ungendered_boot_falls_to_other(self):
        p = {"id": 10, "handle": "b", "title": "Snip Toe Western Boot",
             "product_type": "Boots",
             "variants": [{"price": "255.00", "compare_at_price": None,
                           "available": True}]}
        assert _competitor_row(p, "X", "x.com", RUN_TS)["category"] == "Other"

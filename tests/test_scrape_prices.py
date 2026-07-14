"""Unit tests for bbxray.scrape_prices parsing helpers (no network, no DB)."""
import pytest

from bbxray.scrape_prices import _clean_price, classify_category, parse_pdp


class TestCleanPrice:
    @pytest.mark.parametrize("raw,expected", [
        ("119.99", 119.99),
        ("$119.99", 119.99),
        ("$1,299.99", 1299.99),
        ("USD 59.50", 59.50),
        (79, 79.0),
        (149.5, 149.5),
    ])
    def test_parses_common_formats(self, raw, expected):
        assert _clean_price(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "Free", "Call for price", "12.5.3"])
    def test_unparseable_returns_none(self, raw):
        assert _clean_price(raw) is None


class TestClassifyCategory:
    @pytest.mark.parametrize("name,expected", [
        # Work rules must win over the generic Boots rule.
        ("Ariat Men's Steel Toe Work Boots", "Work Boots"),
        ("Justin WorkHog Composite Toe", "Work Boots"),
        # Western cues.
        ("Corral Women's Snip Toe Boots", "Western Boots"),
        ("El Dorado Handmade Cowboy Boots", "Western Boots"),
        # "boot cut" must NOT be swallowed by the Boots rule.
        ("Wrangler Men's Boot Cut Jeans", "Jeans"),
        ("Kid's Rain Boots", "Boots"),
        # Outerwear is checked before Jeans, so denim jackets stay Outerwear.
        ("Carhartt Denim Jacket", "Outerwear"),
        ("Men's Plaid Flannel Shirt", "Shirts"),
        ("Resistol Felt Hat", "Hats"),
        ("Tooled Leather Belt", "Belts & Buckles"),
        ("Mink Oil Leather Conditioner", "Boot Care"),
        ("Twisted X Slip-On Moccasin Shoe", "Footwear"),
    ])
    def test_name_classification(self, name, expected):
        assert classify_category(name, "https://www.bootbarn.com/p/123.html") == expected

    def test_falls_back_to_url_when_name_missing(self):
        got = classify_category(None, "https://www.bootbarn.com/mens-leather-wallet/123.html")
        assert got == "Accessories"

    def test_no_match_returns_none(self):
        assert classify_category("Gift Card", "https://www.bootbarn.com/gift/1.html") is None


class TestParsePdp:
    URL = "https://www.bootbarn.com/ariat-heritage-roughstock/2000123456.html"

    def test_full_sale_pdp(self, fixture_html):
        rec = parse_pdp(fixture_html("pdp_sale.html"), self.URL)
        assert rec is not None
        assert rec["product_id"] == "2000123456"
        assert rec["sku"] == "10021547"           # affirm data-sku wins over productID
        assert rec["brand"] == "Ariat"
        # "Product Name:" prefix stripped, nbsp + double spaces collapsed.
        assert rec["name"] == "Ariat Men's Heritage Roughstock Western Boots"
        assert rec["category"] == "Western Boots"
        assert rec["list_price"] == 159.99        # exact cents attr (15999) preferred
        assert rec["sale_price"] == 119.99        # displayed < original -> visible sale
        assert rec["currency"] == "USD"
        assert rec["availability"] == "InStock"
        assert rec["in_stock"] == 1
        assert rec["map_hidden"] == 0
        assert rec["source"] == "live"
        assert rec["url"] == self.URL

    def test_map_hidden_pdp(self, fixture_html):
        rec = parse_pdp(fixture_html("pdp_map_hidden.html"), self.URL)
        assert rec is not None
        assert rec["map_hidden"] == 1
        # No cents attr -> falls back to parsing the original-price text.
        assert rec["list_price"] == 189.99
        # Displayed price equals original -> no visible sale.
        assert rec["sale_price"] is None
        assert rec["availability"] == "OutOfStock"
        assert rec["in_stock"] == 0
        assert rec["brand"] is None               # no affirm widget
        assert rec["sku"] == "2000987654"         # falls back to productID
        assert rec["category"] == "Work Boots"    # "steel toe" beats generic Boots

    def test_page_without_price_microdata_is_rejected(self, fixture_html):
        assert parse_pdp(fixture_html("pdp_not_a_product.html"), self.URL) is None

    def test_minimal_pdp_defaults(self):
        html = '<html><body><span itemprop="price" content="25.00"></span></body></html>'
        rec = parse_pdp(html, "https://www.bootbarn.com/mens-leather-wallet/999.html")
        assert rec is not None
        assert rec["list_price"] == 25.0          # no original block -> list = displayed
        assert rec["sale_price"] is None
        assert rec["availability"] is None
        assert rec["in_stock"] == 0
        assert rec["name"] is None
        assert rec["currency"] == "USD"           # default when microdata absent
        assert rec["category"] == "Accessories"   # classified from the URL alone

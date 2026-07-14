"""Weekly news digest sourcing, merging Google News + Bing News RSS (both free).

Google News gives real publisher names (good source diversity); Bing News gives
a real 1-2 sentence summary and the true article URL. We merge by headline:
prefer Google's clean source name, enrich with Bing's summary/link, and filter
out finance-SEO junk by both source name and domain. Used by weekly_digest.py.
"""
from __future__ import annotations

import datetime as dt
import html
import re
import urllib.parse as up

import feedparser

DEFAULT_COMPANIES = ["Boot Barn", "Ariat", "Cavender's", "Tecovas",
                     "Tractor Supply", "Sheplers"]

# Finance-SEO / aggregator domains (Bing) to exclude.
BLOCK_DOMAINS = {
    "finance.yahoo.com", "yahoo.com", "msn.com", "aol.com", "fool.com",
    "zacks.com", "simplywall.st", "marketbeat.com", "benzinga.com", "moomoo.com",
    "tradingview.com", "stockstory.org", "nasdaq.com", "investorplace.com",
    "tipranks.com", "insidermonkey.com", "gurufocus.com", "247wallst.com",
    "24-7wallst.com", "seekingalpha.com", "barchart.com", "stocktwits.com",
    "wallstreetzen.com", "stocktitan.net", "kavout.com", "defenseworld.net",
    "stockanalysis.com", "marketscreener.com", "investing.com", "thestreet.com",
    "streetinsider.com", "donanimhaber.com", "barrons.com", "ad-hoc-news.de",
    "trefis.com", "fintel.io", "consumerthai.com", "dars.gov.et",
}
# Same idea for Google's publisher-name field.
BLOCK_SOURCE_NAMES = (
    "yahoo", "zacks", "simply wall", "stockstory", "marketbeat", "moomoo",
    "tradingview", "kavout", "ad hoc", "insider monkey", "motley fool",
    "benzinga", "tipranks", "gurufocus", "seeking alpha", "nasdaq", "msn",
    "aol", "24/7 wall", "stocktwits", "investorplace", "defense world",
    "barchart", "stocktitan", "trefis", "fintel", "the globe and mail",
    "simplywall", "consumerthai", "donanım", "donanimhaber", "dars.gov",
)


def _is_spam(title: str, company: str) -> bool:
    """Keyword-stuffed product-listing spam repeats the brand name many times."""
    return title.lower().count(company.lower()) > 2
STOCK_NOISE = re.compile(
    r"\b(options?\s+traders?|price target|top (growth )?stock|rocketed|"
    r"short interest|market cap|zacks|motley fool|buy or sell|is it time to buy|"
    r"overbought|oversold|\bRSI\b|moving average|hedge funds?\s+(are|were|buy)|"
    r"analyst (rating|price)|stock (forecast|prediction|to buy))\b", re.I)
SOURCE_NAMES = {
    "wwd.com": "WWD", "retaildive.com": "Retail Dive", "cnbc.com": "CNBC",
    "forbes.com": "Forbes", "reuters.com": "Reuters", "bloomberg.com": "Bloomberg",
    "wsj.com": "WSJ", "bizjournals.com": "The Business Journals",
    "businessjournals.com": "The Business Journals", "modernretail.co": "Modern Retail",
    "footwearnews.com": "Footwear News", "sourcingjournal.com": "Sourcing Journal",
    "chainstoreage.com": "Chain Store Age", "retailwire.com": "RetailWire",
    "businessoffashion.com": "Business of Fashion",
}


def _clean(t: str) -> str:
    return re.sub(r"\s+", " ", re.sub("<[^>]+>", " ", t or "")).strip()


def _norm(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()[:45]


def _pub(entry):
    if getattr(entry, "published_parsed", None):
        return dt.datetime(*entry.published_parsed[:6])
    return None


def _google(company: str) -> list[dict]:
    q = up.quote(f'"{company}" when:21d')
    feed = feedparser.parse(
        f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en")
    out = []
    for e in feed.entries:
        src = (getattr(getattr(e, "source", None), "title", "") or "").strip()
        title = _clean(getattr(e, "title", ""))
        if src and title.endswith(f" - {src}"):
            title = title[: -(len(src) + 3)].strip()
        if not title or STOCK_NOISE.search(title) or _is_spam(title, company):
            continue
        if any(b in src.lower() for b in BLOCK_SOURCE_NAMES):
            continue
        out.append({"title": title, "source": src or "news",
                    "link": getattr(e, "link", ""), "published": _pub(e),
                    "summary": ""})
    return out


def _bing(company: str) -> list[dict]:
    q = up.quote(f'"{company}"')
    feed = feedparser.parse(
        f"https://www.bing.com/news/search?q={q}&format=rss&count=40")
    out = []
    for e in feed.entries:
        title = _clean(getattr(e, "title", ""))
        real = up.parse_qs(up.urlparse(getattr(e, "link", "")).query).get(
            "url", [getattr(e, "link", "")])[0]
        dom = up.urlparse(real).netloc.replace("www.", "").lower()
        if not title or STOCK_NOISE.search(title) or _is_spam(title, company):
            continue
        if any(dom == b or dom.endswith("." + b) for b in BLOCK_DOMAINS):
            continue
        if any(seg in up.urlparse(real).path.lower()
               for seg in ("/market-data/", "/quote/", "/stocks/")):
            continue
        out.append({"title": title, "source": SOURCE_NAMES.get(dom, dom),
                    "link": real, "published": _pub(e),
                    "summary": _clean(getattr(e, "summary", ""))[:320]})
    return out


def fetch_news(company: str, days: int = 14, limit: int = 7) -> list[dict]:
    google, bing = _google(company), _bing(company)
    bidx = {}
    for b in bing:
        bidx.setdefault(_norm(b["title"]), b)

    out, seen = [], set()
    # 1) Google items (clean source names), enriched with a Bing summary if the
    #    same story is there; use Bing's real link when matched.
    for g in google:
        n = _norm(g["title"])
        if n in seen:
            continue
        b = bidx.get(n)
        out.append({
            "title": g["title"], "source": g["source"],
            "link": b["link"] if b else g["link"],
            "published": g["published"] or (b["published"] if b else None),
            "summary": b["summary"] if b else "",
        })
        seen.add(n)
    # 2) Bing-only quality items Google missed (these carry summaries).
    for b in bing:
        n = _norm(b["title"])
        if n not in seen:
            out.append(b)
            seen.add(n)

    cutoff = dt.datetime.now() - dt.timedelta(days=days)
    dated = [i for i in out if (i["published"] is None or i["published"] >= cutoff)]
    # summaries first (more useful), then by recency
    dated.sort(key=lambda i: (bool(i["summary"]),
                              i["published"] or dt.datetime.min), reverse=True)
    return dated[:limit]


def gather(companies: list[str] | None = None, days: int = 14,
           limit: int = 7) -> dict[str, list[dict]]:
    return {c: fetch_news(c, days, limit) for c in (companies or DEFAULT_COMPANIES)}


def _fmt_date(pub) -> str:
    return pub.strftime("%b %d") if isinstance(pub, dt.datetime) else ""


def build_digest_html(data: dict[str, list[dict]], days: int = 14) -> str:
    now = dt.datetime.now().strftime("%B %d, %Y")
    navy, amber, ink, mut = "#1f3a5f", "#c8891f", "#1a1a1a", "#6b7280"
    parts = [
        f'<div style="max-width:660px;margin:0 auto;font-family:Georgia,serif;'
        f'color:{ink};line-height:1.55;">',
        f'<div style="border-bottom:3px solid {amber};padding-bottom:10px;'
        f'margin-bottom:18px;">'
        f'<div style="font-size:22px;font-weight:bold;color:{navy};">Western &amp; '
        f'Workwear Retail — Weekly Brief</div>'
        f'<div style="font-size:13px;color:{mut};margin-top:3px;">Week of {now} '
        f'· Boot Barn (BOOT) &amp; competitors · industry coverage</div></div>',
    ]
    for company, items in data.items():
        parts.append(
            f'<div style="font-size:17px;font-weight:bold;color:{navy};'
            f'border-left:4px solid {amber};padding-left:8px;margin:20px 0 10px;">'
            f'{html.escape(company)}</div>')
        if not items:
            parts.append(f'<div style="font-size:13px;color:{mut};padding-left:12px;">'
                         'No notable industry news this week.</div>')
            continue
        for it in items:
            meta = " · ".join(x for x in (html.escape(str(it["source"])),
                                          _fmt_date(it["published"])) if x)
            summ = (f'<div style="font-size:13.5px;color:#374151;margin-top:2px;">'
                    f'{html.escape(it["summary"])}</div>') if it["summary"] else ""
            parts.append(
                f'<div style="margin:0 0 14px;padding-left:4px;">'
                f'<a href="{html.escape(it["link"])}" style="color:{navy};'
                f'font-size:15px;font-weight:bold;text-decoration:none;">'
                f'{html.escape(it["title"])}</a>'
                f'<div style="font-size:11px;color:{amber};margin:2px 0 1px;'
                f'text-transform:uppercase;letter-spacing:.3px;">{meta}</div>'
                f'{summ}</div>')
    parts.append(
        f'<div style="border-top:1px solid #e5e7eb;margin-top:22px;padding-top:10px;'
        f'font-size:11px;color:{mut};">Auto-generated by Boot Barn X-Ray from public '
        'news (Google News + Bing), finance-SEO sources filtered out. Headlines link '
        'to the original publisher.</div></div>')
    return "\n".join(parts)


def build_digest_text(data: dict[str, list[dict]]) -> str:
    lines = ["Western & Workwear Retail — Weekly Brief", ""]
    for company, items in data.items():
        lines.append(f"## {company}")
        if not items:
            lines.append("  (no notable industry news this week)")
        for it in items:
            meta = " · ".join(x for x in (str(it["source"]),
                                          _fmt_date(it["published"])) if x)
            lines.append(f"  - {it['title']} [{meta}]")
            if it["summary"]:
                lines.append(f"    {it['summary']}")
            lines.append(f"    {it['link']}")
        lines.append("")
    return "\n".join(lines)

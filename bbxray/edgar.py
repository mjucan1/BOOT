"""SEC EDGAR: filings feed + structured financials for the public comp set.

Free, authoritative, no scraping fragility. Private peers (Ariat, Tecovas,
Cavender's) file nothing, so they're absent by necessity. Earnings-call
transcripts are third-party/copyrighted and NOT reproduced here -- we surface
the 8-K earnings releases and the structured XBRL numbers instead.
"""
from __future__ import annotations

import datetime as dt

import requests

# Public western/workwear + adjacent comps. (Ariat, Tecovas, Cavender's are private.)
PUBLIC_COMPS = {
    "BOOT": "Boot Barn",
    "TSCO": "Tractor Supply",
    "KTB": "Kontoor Brands (Wrangler/Lee)",
    "DBI": "Designer Brands",
    "GCO": "Genesco",
}
UA = {"User-Agent": "BootBarnXray research (contact: research@example.com)"}

METRICS = {
    "Revenue": ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues"],
    "Gross profit": ["GrossProfit"],
    "Net income": ["NetIncomeLoss"],
    "Diluted EPS": ["EarningsPerShareDiluted"],
}


def _get(url: str):
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    return r.json()


def cik_map() -> dict[str, str]:
    data = _get("https://www.sec.gov/files/company_tickers.json")
    return {v["ticker"]: str(v["cik_str"]).zfill(10) for v in data.values()}


def fetch_filings(cik: str, forms=("8-K", "10-Q", "10-K"), limit=8) -> list[dict]:
    sub = _get(f"https://data.sec.gov/submissions/CIK{cik}.json")
    r = sub["filings"]["recent"]
    out = []
    for i in range(len(r["form"])):
        if r["form"][i] not in forms:
            continue
        acc = r["accessionNumber"][i].replace("-", "")
        doc = r["primaryDocument"][i]
        out.append({
            "form": r["form"][i], "filed": r["filingDate"][i],
            "title": r.get("primaryDocDescription", [""] * len(r["form"]))[i]
            or r["form"][i],
            "url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{doc}",
        })
        if len(out) >= limit:
            break
    return out


def _classify(start: str, end: str) -> str | None:
    d = (dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days
    if 80 <= d <= 100:
        return "Q"
    if 350 <= d <= 380:
        return "FY"
    return None


def fetch_financials(cik: str) -> list[dict]:
    """Quarterly (~90d) and annual (~365d) points per metric, YTD facts dropped."""
    cf = _get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json")
    usg = cf["facts"].get("us-gaap", {})
    rows: dict[tuple, dict] = {}
    for label, keys in METRICS.items():
        pts = []
        for k in keys:
            if k in usg:
                units = usg[k]["units"]
                pts = units[next(iter(units))]     # USD or USD/shares
                break
        for u in pts:
            if "start" not in u or "end" not in u:
                continue
            ptype = _classify(u["start"], u["end"])
            if not ptype:
                continue
            # last write wins -> most recent (restated) value for that period
            rows[(label, u["end"], ptype)] = {
                "metric": label, "period_end": u["end"], "period_type": ptype,
                "value": float(u["val"]), "fy": u.get("fy"), "fp": u.get("fp"),
            }
    return sorted(rows.values(), key=lambda r: (r["metric"], r["period_end"]))


def pull_all(comps: dict[str, str] | None = None) -> dict:
    """Filings + financials for every public comp. Returns tidy row lists."""
    comps = comps or PUBLIC_COMPS
    cmap = cik_map()
    filings, financials = [], []
    for ticker, name in comps.items():
        cik = cmap.get(ticker)
        if not cik:
            continue
        for f in fetch_filings(cik):
            filings.append({"ticker": ticker, "company": name, **f})
        for fin in fetch_financials(cik):
            financials.append({"ticker": ticker, "company": name, **fin})
    return {"filings": filings, "financials": financials}

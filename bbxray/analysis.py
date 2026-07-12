"""Store-level cannibalization analysis via difference-in-differences (DiD).

Uses the Advan-sourced per-store attributes stored in foot_traffic
(latitude/longitude/open_date) plus weekly visit counts to estimate whether a
NEW store opening depresses visits at NEARBY existing stores ("exposed"),
relative to FAR-AWAY existing stores ("control") over the same weeks.

    cannibalization signal = exposed %change - control %change   (per opening)

A negative number means nearby stores lost visits beyond the chain-wide trend
after the new store opened -- evidence of cannibalization. The control group
nets out seasonality and company-wide swings. All functions are pure (no
Streamlit) so they can be unit-tested directly.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

EARTH_MILES = 3958.7613


def haversine_miles(lat1, lon1, lat2, lon2):
    """Great-circle distance in miles. Scalars or numpy arrays."""
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = np.sin(dlat / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2) ** 2
    return EARTH_MILES * 2 * np.arcsin(np.sqrt(a))


def store_dim(foot: pd.DataFrame) -> pd.DataFrame:
    """One row per store with its (static) location + opening date."""
    d = foot.dropna(subset=["placekey"]).sort_values("date_range_start")
    dim = d.groupby("placekey").agg(
        location_name=("location_name", "first"),
        city=("city", "first"), region=("region", "first"),
        lat=("latitude", "last"), lng=("longitude", "last"),
        open_date=("open_date", "last")).reset_index()
    dim["open_dt"] = pd.to_datetime(dim["open_date"], errors="coerce")
    return dim


def weekly_visits(foot: pd.DataFrame) -> pd.DataFrame:
    """placekey x week -> total visits."""
    f = foot.dropna(subset=["placekey"]).copy()
    f["week"] = pd.to_datetime(f["date_range_start"], errors="coerce")
    return (f.dropna(subset=["week"])
            .groupby(["placekey", "week"])["raw_visit_counts"].sum()
            .reset_index())


def run_cannibalization(foot: pd.DataFrame, radius_miles: float = 10.0,
                        window_weeks: int = 8, control_buffer: float = 2.0) -> dict:
    """Return {openings, event_study, summary, neighbors}.

    openings     : per-opening DiD table
    event_study  : mean visit index (pre-period = 100) by week-relative-to-open,
                   for exposed vs control -- the money chart
    summary      : headline numbers
    neighbors    : {placekey: (opening_row, exposed_df)} for mapping
    """
    empty = {"openings": pd.DataFrame(), "event_study": pd.DataFrame(),
             "summary": {}, "neighbors": {}}
    dim = store_dim(foot)
    wk = weekly_visits(foot)
    geo = dim.dropna(subset=["lat", "lng"])
    if wk["week"].nunique() < 4 or dim["open_dt"].notna().sum() == 0 or geo.empty:
        return empty

    wmin, wmax = wk["week"].min(), wk["week"].max()
    openings = geo[(geo["open_dt"] >= wmin + pd.Timedelta(weeks=window_weeks)) &
                   (geo["open_dt"] <= wmax - pd.Timedelta(weeks=window_weeks))]
    if openings.empty:
        return empty

    per_open, es_rows, neighbors = [], [], {}
    for _, o in openings.iterrows():
        d = o["open_dt"]
        gd = geo.assign(dist=haversine_miles(o["lat"], o["lng"],
                                             geo["lat"].values, geo["lng"].values))
        pre_existing = gd["open_dt"].isna() | (gd["open_dt"] < d)
        not_self = gd["placekey"] != o["placekey"]
        exposed_ids = set(gd.loc[(gd["dist"] <= radius_miles) & not_self
                                 & pre_existing, "placekey"])
        control_ids = set(gd.loc[(gd["dist"] > radius_miles * control_buffer)
                                 & not_self & pre_existing, "placekey"])
        if not exposed_ids:
            continue
        neighbors[o["placekey"]] = (o, gd[gd["placekey"].isin(exposed_ids)].copy())

        sub = wk[wk["placekey"].isin(exposed_ids | control_ids)].copy()
        sub["rel"] = ((sub["week"] - d).dt.days // 7)
        sub = sub[(sub["rel"] >= -window_weeks) & (sub["rel"] <= window_weeks)]
        sub["grp"] = np.where(sub["placekey"].isin(exposed_ids), "exposed", "control")

        pre = sub[sub["rel"] < 0].groupby("placekey")["raw_visit_counts"].mean()
        post = sub[sub["rel"] > 0].groupby("placekey")["raw_visit_counts"].mean()
        pp = pd.concat([pre.rename("pre"), post.rename("post")], axis=1).dropna()
        pp = pp[pp["pre"] > 0]
        if pp.empty:
            continue
        pp["pct"] = (pp["post"] - pp["pre"]) / pp["pre"] * 100
        pp["grp"] = sub.groupby("placekey")["grp"].first()
        ex_pct = pp.loc[pp["grp"] == "exposed", "pct"].mean()
        ct_pct = pp.loc[pp["grp"] == "control", "pct"].mean()
        per_open.append({
            "store": f"{o['city']}, {o['region']}", "placekey": o["placekey"],
            "open_date": str(d.date()), "n_exposed": int((pp["grp"] == "exposed").sum()),
            "n_control": int((pp["grp"] == "control").sum()),
            "exposed_%chg": round(ex_pct, 1),
            "control_%chg": round(ct_pct, 1) if pd.notna(ct_pct) else None,
            "cannibalization_pts": round(ex_pct - ct_pct, 1)
            if pd.notna(ct_pct) else None})

        # event study: index each store's visits to its own pre-period mean.
        sub = sub.join(pre.rename("premean"), on="placekey")
        sub = sub[sub["premean"] > 0]
        sub["idx"] = sub["raw_visit_counts"] / sub["premean"] * 100
        es_rows.append(sub[["rel", "grp", "idx"]])

    if not per_open:
        return empty

    openings_df = pd.DataFrame(per_open).sort_values("cannibalization_pts")
    es = pd.concat(es_rows) if es_rows else pd.DataFrame(columns=["rel", "grp", "idx"])
    event_study = (es.groupby(["rel", "grp"])["idx"].mean().reset_index()
                   if not es.empty else es)
    did = openings_df["cannibalization_pts"].dropna()
    summary = {
        "n_openings": int(len(openings_df)),
        "avg_cannibalization_pts": round(did.mean(), 1) if len(did) else None,
        "share_negative": round((did < 0).mean() * 100, 0) if len(did) else None,
        "radius_miles": radius_miles, "window_weeks": window_weeks,
    }
    return {"openings": openings_df, "event_study": event_study,
            "summary": summary, "neighbors": neighbors}

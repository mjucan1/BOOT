"""Boot Barn X-Ray dashboard (Streamlit).

Run:  streamlit run dashboard.py
Reads the SQLite DB populated by the scrapers and visualizes:
  - Pricing: distribution, by brand/category, discount depth, changes over time
  - Stores: map, count by state, intra-quarter openings/closures (snapshot diff)
  - Foot traffic: visits over time (if Dewey data loaded)
"""
from __future__ import annotations

import calendar
import datetime as _dt
import os
import urllib.parse

import pandas as pd
import plotly.express as px
import streamlit as st


def _secret(key: str):
    """Read a Streamlit secret without crashing when no secrets file exists."""
    try:
        return st.secrets[key]
    except Exception:
        return None


# On Streamlit Cloud the Postgres URL comes from st.secrets; push it into the
# environment BEFORE importing config (which reads DATABASE_URL at import time).
_db_url = _secret("DATABASE_URL")
if _db_url:
    os.environ["DATABASE_URL"] = _db_url

import config  # noqa: E402
from bbxray import db  # noqa: E402

st.set_page_config(page_title="Boot Barn X-Ray", layout="wide", page_icon="🥾")


def check_password() -> bool:
    """Simple shared-password gate. If APP_PASSWORD isn't set, stay open (dev)."""
    pw = _secret("APP_PASSWORD")
    if not pw:
        return True
    if st.session_state.get("auth_ok"):
        return True
    st.title("🥾 Boot Barn X-Ray")
    with st.form("login"):
        entered = st.text_input("Password", type="password")
        if st.form_submit_button("Enter"):
            if entered == pw:
                st.session_state["auth_ok"] = True
                st.rerun()
            else:
                st.error("Incorrect password")
    return False


if not check_password():
    st.stop()


@st.cache_data(ttl=300)
def load(table: str) -> pd.DataFrame:
    try:
        return pd.read_sql(f"SELECT * FROM {table}", db.get_engine())
    except Exception:
        return pd.DataFrame()


def latest_snapshot(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "run_ts" not in df:
        return df
    return df[df["run_ts"] == df["run_ts"].max()]


def quarter(ts: pd.Series) -> pd.Series:
    d = pd.to_datetime(ts, errors="coerce", utc=True)
    return d.dt.year.astype("Int64").astype(str) + "-Q" + d.dt.quarter.astype("Int64").astype(str)


def store_first_seen(stores_df: pd.DataFrame) -> pd.DataFrame:
    """Per store_id, the earliest snapshot we ever captured it in. As weekly
    snapshots accrue, a store's first_seen marks when WE detected it (a proxy for
    opening, for stores that appear after we start tracking)."""
    fs = stores_df.groupby("store_id")["run_ts"].min().reset_index(name="first_seen")
    d = pd.to_datetime(fs["first_seen"], errors="coerce", utc=True)
    fs["first_seen_year"] = d.dt.year.astype("Int64")
    fs["first_seen_quarter"] = quarter(fs["first_seen"])
    return fs


@st.cache_data(ttl=300)
def load_open_dates() -> pd.DataFrame:
    """Optional enrichment: real store opening years from data/store_open_dates.csv
    (columns: store_id, and either opened_year or opened_date). Populate it from
    Advan OPEN_DATE, Boot Barn disclosures, or research to get TRUE vintages.
    Returns empty frame if the file is absent -- the dashboard then falls back to
    first-detected year."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "data", "store_open_dates.csv")
    try:
        df = pd.read_csv(path, dtype={"store_id": str})
        df["store_id"] = df["store_id"].str.strip()
        if "opened_year" not in df.columns and "opened_date" in df.columns:
            df["opened_year"] = pd.to_datetime(
                df["opened_date"], errors="coerce").dt.year
        df["opened_year"] = pd.to_numeric(df["opened_year"], errors="coerce").astype("Int64")
        return df[["store_id", "opened_year"]].dropna(subset=["opened_year"])
    except Exception:
        return pd.DataFrame(columns=["store_id", "opened_year"])


st.title("🥾 Boot Barn X-Ray")
st.caption("Competitive intelligence: pricing · store footprint · foot traffic")

prices = load("price_snapshots")
stores = load("store_snapshots")
foot = load("foot_traffic")
brands = load("brand_prices")
contacts = load("contacts")
runs = load("runs")

if prices.empty and stores.empty:
    st.warning("No data yet. Run the scrapers first:  `python run.py all`")
    st.stop()

(tab_price, tab_store, tab_foot, tab_cann, tab_brand,
 tab_out) = st.tabs(
    ["💲 Pricing", "📍 Stores", "🚶 Foot Traffic", "🧭 Cannibalization",
     "🏷️ Private Labels", "📇 Outreach"])

# ---------------------------------------------------------------- Pricing ----
with tab_price:
    if prices.empty:
        st.info("No pricing data. Run `python run.py prices`.")
    else:
        cur = latest_snapshot(prices).copy()
        cur["eff_price"] = cur["sale_price"].fillna(cur["list_price"])
        cur["discount_pct"] = (
            (cur["list_price"] - cur["eff_price"]) / cur["list_price"] * 100
        ).where(cur["list_price"] > 0)

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Products", f"{len(cur):,}")
        c2.metric("Median price", f"${cur['eff_price'].median():,.2f}")
        c3.metric("Visible sale", f"{(cur['sale_price'].notna()).mean()*100:.0f}%")
        disc = cur["discount_pct"][cur["discount_pct"] > 0]
        c4.metric("Avg discount", f"{disc.mean():.0f}%" if len(disc) else "—")
        if "map_hidden" in cur:
            c5.metric("MAP-hidden", f"{cur['map_hidden'].mean()*100:.0f}%",
                      help="Share of products whose sale price is hidden until "
                           "cart (Minimum Advertised Price policy).")

        st.subheader("Price distribution")
        st.plotly_chart(
            px.histogram(cur, x="eff_price", nbins=40,
                         labels={"eff_price": "Effective price ($)"}),
            width='stretch')

        left, right = st.columns(2)
        if cur["brand"].notna().any():
            by_brand = (cur.groupby("brand")["eff_price"]
                        .agg(["median", "count"]).reset_index()
                        .sort_values("count", ascending=False).head(20))
            left.subheader("Median price by brand (top 20 by count)")
            left.plotly_chart(px.bar(by_brand, x="brand", y="median",
                                     hover_data=["count"]), width='stretch')
        if cur["category"].notna().any():
            by_cat = (cur.groupby("category")["eff_price"]
                      .agg(["median", "count"]).reset_index()
                      .sort_values("count", ascending=False).head(20))
            right.subheader("Median price by category (top 20)")
            right.plotly_chart(px.bar(by_cat, x="category", y="median",
                                      hover_data=["count"]), width='stretch')

        # ---- Category deep-dive: pick a category, watch prices trend over time --
        st.divider()
        st.subheader("📂 Category deep-dive")
        cats = sorted(c for c in prices["category"].dropna().unique() if str(c).strip())
        if not cats:
            st.info("No category labels captured yet.")
        else:
            sel = st.selectbox("Category", cats,
                               index=cats.index("Jeans") if "Jeans" in cats else 0)
            sub = prices[prices["category"] == sel].copy()
            sub["eff_price"] = sub["sale_price"].fillna(sub["list_price"])
            sub["discount_pct"] = (
                (sub["list_price"] - sub["eff_price"]) / sub["list_price"] * 100
            ).where(sub["list_price"] > 0)
            latest_sub = latest_snapshot(sub)

            k1, k2, k3 = st.columns(3)
            k1.metric(f"{sel} products", f"{latest_sub['product_id'].nunique():,}")
            k2.metric("Median price", f"${latest_sub['eff_price'].median():,.2f}")
            d = latest_sub["discount_pct"][latest_sub["discount_pct"] > 0]
            k3.metric("Avg discount", f"{d.mean():.0f}%" if len(d) else "—")

            def _trim_mean(s, frac=0.2):
                s = s.dropna().sort_values()
                # drop at least one from each tail once we have >=5 points, so a
                # lone premium/misparsed item can't drag the average.
                k = max(1, int(len(s) * frac)) if len(s) >= 5 else 0
                return s.iloc[k:len(s) - k].mean() if len(s) > 2 * k else s.mean()

            trend = (sub.groupby("run_ts")
                     .agg(median_price=("eff_price", "median"),
                          trimmed_avg=("eff_price", _trim_mean),
                          products=("product_id", "nunique")).reset_index())
            # Thin periods (esp. sparse Wayback months) let a single mislabeled or
            # premium item spike the line -- the "$800 jeans" bug. Require a min
            # sample and use median + a 10%-trimmed mean, both outlier-resistant.
            min_n = st.slider("Min products per trend point", 1, 20, 5,
                              key=f"minn_{sel}")
            trend = trend[trend["products"] >= min_n]
            trend["date"] = pd.to_datetime(trend["run_ts"], errors="coerce", utc=True)
            if len(trend) > 1:
                st.markdown(f"**{sel} — median & trimmed-avg price over time** "
                            f"(periods with ≥{min_n} products)")
                melt = trend.melt(id_vars="date",
                                  value_vars=["median_price", "trimmed_avg"],
                                  var_name="metric", value_name="price")
                st.plotly_chart(px.line(melt, x="date", y="price", color="metric",
                                        markers=True), width='stretch')
            elif len(trend) == 1:
                st.caption(f"Only one period has ≥{min_n} products so far — the "
                           "trend builds as weekly data accrues.")

                st.markdown(f"**Biggest price moves in {sel}** (first → latest snapshot)")
                piv = sub.pivot_table(index=["product_id", "name"], columns="run_ts",
                                      values="eff_price", aggfunc="last")
                moves = piv[[piv.columns.min(), piv.columns.max()]].reset_index()
                moves.columns = ["product_id", "name", "first_price", "latest_price"]
                moves["change"] = moves["latest_price"] - moves["first_price"]
                moves = moves.dropna(subset=["change"])
                moves = moves[moves["change"] != 0].sort_values("change")
                if not moves.empty:
                    st.dataframe(moves, width='stretch', hide_index=True)
                else:
                    st.caption("No price changes detected in this category yet.")
            else:
                st.info(f"Only one snapshot so far — the **{sel}** price-trend line "
                        f"builds up as you collect weekly data. Right now: median "
                        f"${latest_sub['eff_price'].median():,.2f} across "
                        f"{latest_sub['product_id'].nunique()} products.")

            st.markdown(f"**Current {sel} products**")
            st.dataframe(
                latest_sub[["name", "brand", "list_price", "sale_price", "eff_price",
                            "discount_pct", "availability", "url"]]
                .sort_values("eff_price"), width='stretch', hide_index=True)

        # Price changes over time (needs >=2 snapshots).
        if prices["run_ts"].nunique() > 1:
            st.subheader("Median effective price over time")
            p = prices.copy()
            p["eff_price"] = p["sale_price"].fillna(p["list_price"])
            trend = p.groupby("run_ts")["eff_price"].median().reset_index()
            st.plotly_chart(px.line(trend, x="run_ts", y="eff_price", markers=True),
                            width='stretch')

        st.subheader("Product table")
        st.dataframe(cur[["name", "brand", "category", "list_price",
                          "sale_price", "eff_price", "discount_pct",
                          "availability", "url"]], width='stretch',
                     hide_index=True)

# ----------------------------------------------------------------- Stores ----
with tab_store:
    if stores.empty:
        st.info("No store data. Run `python run.py stores`.")
    else:
        cur = latest_snapshot(stores)
        snap_dates = sorted(stores["run_ts"].unique())

        c1, c2, c3 = st.columns(3)
        c1.metric("Stores (latest snapshot)", f"{cur['store_id'].nunique():,}")
        c2.metric("States", f"{cur['state'].nunique()}")
        c3.metric("Snapshots captured", f"{len(snap_dates)}")

        if {"lat", "lng"}.issubset(cur.columns) and cur["lat"].notna().any():
            st.subheader("Store map")
            st.plotly_chart(
                px.scatter_geo(cur.dropna(subset=["lat", "lng"]),
                               lat="lat", lon="lng", scope="usa",
                               hover_name="city", hover_data=["state", "store_id"]),
                width='stretch')

        st.subheader("Stores by state")
        by_state = cur.groupby("state")["store_id"].nunique().reset_index(
            name="stores").sort_values("stores", ascending=False)
        st.plotly_chart(px.bar(by_state, x="state", y="stores"),
                        width='stretch')

        # ---- Store vintages: cluster stores by opening year ----
        st.divider()
        st.subheader("🏗️ Store vintages")
        fs = store_first_seen(stores)
        opendates = load_open_dates()
        v = cur.merge(fs, on="store_id", how="left").merge(
            opendates, on="store_id", how="left")
        has_real = 0 if opendates.empty else int(opendates["store_id"].nunique())
        v["vintage"] = v["opened_year"].fillna(v["first_seen_year"]).astype("Int64")
        v["source"] = v["opened_year"].notna().map(
            {True: "year opened", False: "first detected"})
        if has_real:
            st.caption(f"{has_real} stores use a real opening year from "
                       "data/store_open_dates.csv; the rest fall back to the year "
                       "we first detected them.")
        else:
            st.warning(
                "No opening-date source yet, so vintages currently show the **year "
                "we first detected** each store (all the same until weekly history "
                "builds). For TRUE vintages, add `data/store_open_dates.csv` with "
                "columns `store_id,opened_year` (e.g. from Advan OPEN_DATE) — this "
                "chart switches to real opening years the moment that file exists.")
        by_vin = (v.groupby(["vintage", "source"])["store_id"].nunique()
                  .reset_index(name="stores"))
        by_vin["vintage"] = by_vin["vintage"].astype(str)
        st.plotly_chart(px.bar(by_vin, x="vintage", y="stores", color="source",
                               labels={"vintage": "Opening year (vintage)"}),
                        width='stretch')
        vint_options = sorted(int(x) for x in v["vintage"].dropna().unique())
        if vint_options:
            pick = st.selectbox("Inspect a vintage", vint_options,
                                index=len(vint_options) - 1)
            st.dataframe(
                v[v["vintage"] == pick][["store_id", "name", "city", "state",
                                         "zip", "source", "url"]]
                .sort_values(["state", "city"]), width='stretch', hide_index=True)

        # ---- New store openings detected over time ----
        st.divider()
        st.subheader("🆕 New store openings detected")
        if stores["run_ts"].nunique() < 2:
            st.info("As weekly snapshots accumulate, stores that newly appear get "
                    "logged here as openings, grouped by the quarter we first saw "
                    "them. (Needs 2+ snapshots on different dates to begin.)")
        else:
            first_snap = min(stores["run_ts"])
            newly = fs[fs["first_seen"] > first_snap]
            per_q = (newly.groupby("first_seen_quarter")["store_id"].nunique()
                     .reset_index(name="new_stores"))
            if not per_q.empty:
                st.plotly_chart(px.bar(per_q, x="first_seen_quarter", y="new_stores",
                                       labels={"first_seen_quarter": "Quarter first seen"}),
                                width='stretch')
            latest_ts = max(stores["run_ts"])
            just = newly[newly["first_seen"] == latest_ts]
            st.markdown(f"**Newly detected in the latest snapshot: "
                        f"{just['store_id'].nunique()}**")
            if not just.empty:
                st.dataframe(
                    cur[cur["store_id"].isin(just["store_id"])][
                        ["store_id", "name", "city", "state", "zip", "url"]],
                    width='stretch', hide_index=True)

        # ---- Openings / closures via snapshot diff ----
        st.divider()
        st.subheader("Openings & closures (snapshot diff)")
        if len(snap_dates) < 2:
            st.info("Need at least two snapshots on different dates to detect "
                    "openings/closures. Re-run `python run.py stores` "
                    "periodically (e.g. weekly) to build history.")
        else:
            colA, colB = st.columns(2)
            base = colA.selectbox("Baseline snapshot", snap_dates, index=0)
            comp = colB.selectbox("Compare snapshot", snap_dates, index=len(snap_dates)-1)
            base_ids = set(stores.loc[stores["run_ts"] == base, "store_id"])
            comp_ids = set(stores.loc[stores["run_ts"] == comp, "store_id"])
            opened = comp_ids - base_ids
            closed = base_ids - comp_ids
            m1, m2 = st.columns(2)
            m1.metric("Opened", len(opened))
            m2.metric("Closed", len(closed))
            det = cur[cur["store_id"].isin(opened)][
                ["store_id", "name", "city", "state", "zip", "url"]]
            st.write("**New stores (openings):**")
            st.dataframe(det, width='stretch', hide_index=True)
            if closed:
                cl = stores[(stores["run_ts"] == base) &
                            (stores["store_id"].isin(closed))][
                    ["store_id", "name", "city", "state", "zip"]]
                st.write("**Disappeared (closures):**")
                st.dataframe(cl, width='stretch', hide_index=True)

# ------------------------------------------------------------- Foot traffic --
with tab_foot:
    if foot.empty:
        st.info("No foot-traffic data. Load a Dewey patterns dataset:\n\n"
                "```\npip install \"git+https://github.com/Dewey-Data/deweydatapy\"\n"
                "set DEWEY_API_KEY=...\nset DEWEY_PRODUCT_PATH=...\n"
                "python -m bbxray.ingest_dewey download 2025-01-01 2025-06-30\n"
                "python -m bbxray.ingest_dewey load\n```")
    else:
        f = foot.copy()
        f["date"] = pd.to_datetime(f["date_range_start"], errors="coerce")
        c1, c2, c3 = st.columns(3)
        c1.metric("POIs", f"{f['placekey'].nunique():,}")
        c2.metric("Total visits", f"{f['raw_visit_counts'].sum():,.0f}")
        c3.metric("Months", f"{f['date'].dt.to_period('M').nunique()}")

        st.subheader("Total visits over time")
        ts = f.groupby("date")["raw_visit_counts"].sum().reset_index()
        st.plotly_chart(px.line(ts, x="date", y="raw_visit_counts", markers=True),
                        width='stretch')

        # ---- Year-over-year by month ----
        st.subheader("Year-over-year by month")
        fm = f.dropna(subset=["date"]).copy()
        fm["year"] = fm["date"].dt.year
        fm["month"] = fm["date"].dt.month
        # total visits + #weeks per (year, month); avg weekly = fair across months
        # that have different numbers of (or missing) weeks.
        g = (fm.groupby(["year", "month"])
             .agg(visits=("raw_visit_counts", "sum"), weeks=("date", "nunique"))
             .reset_index())
        g["avg_weekly"] = g["visits"] / g["weeks"]

        if g["year"].nunique() < 2:
            st.info("Year-over-year needs at least two calendar years of data. "
                    "It fills in as history accrues.")
        else:
            gp = g.copy()
            gp["year"] = gp["year"].astype(str)
            fig = px.line(gp.sort_values(["year", "month"]), x="month", y="avg_weekly",
                          color="year", markers=True,
                          labels={"avg_weekly": "Avg weekly visits", "month": "Month",
                                  "year": "Year"})
            fig.update_xaxes(tickmode="array", tickvals=list(range(1, 13)),
                             ticktext=[calendar.month_abbr[m] for m in range(1, 13)])
            st.plotly_chart(fig, width='stretch')
            st.caption("Each line is a year; uses **average weekly visits per month** "
                       "so months with missing or partial weeks compare fairly. "
                       "The current month may be partial.")

            # Y/Y % change: same month vs the prior year.
            piv = g.pivot(index="month", columns="year", values="avg_weekly")
            years = sorted(piv.columns)
            yy = []
            for prev_y, cur_y in zip(years, years[1:]):
                for m in piv.index:
                    cur, prev = piv.loc[m, cur_y], piv.loc[m, prev_y]
                    if pd.notna(cur) and pd.notna(prev) and prev > 0:
                        yy.append({"month": calendar.month_abbr[m], "month_num": m,
                                   "comparison": f"{cur_y} vs {prev_y}",
                                   "yoy_pct": (cur / prev - 1) * 100})
            yy = pd.DataFrame(yy)
            if not yy.empty:
                st.markdown("**Y/Y growth by month** (avg weekly visits vs the same "
                            "month a year earlier)")
                st.plotly_chart(
                    px.bar(yy.sort_values("month_num"), x="month", y="yoy_pct",
                           color="comparison", barmode="group",
                           labels={"yoy_pct": "Y/Y change (%)", "month": "Month",
                                   "comparison": ""}),
                    width='stretch')

        st.subheader("Visits by state")
        by_reg = f.groupby("region")["raw_visit_counts"].sum().reset_index(
            ).sort_values("raw_visit_counts", ascending=False)
        st.plotly_chart(px.bar(by_reg, x="region", y="raw_visit_counts"),
                        width='stretch')

        # ---- Visits over time, broken out by state ----
        st.subheader("Visits over time by state")
        all_states = by_reg["region"].dropna().tolist()          # already sorted desc
        default_states = all_states[:6]
        sel_states = st.multiselect("States to compare", all_states,
                                    default=default_states)
        if sel_states:
            ss = (f[f["region"].isin(sel_states)]
                  .groupby(["date", "region"])["raw_visit_counts"].sum()
                  .reset_index())
            st.plotly_chart(
                px.line(ss, x="date", y="raw_visit_counts", color="region",
                        markers=True, labels={"raw_visit_counts": "Visits",
                                              "region": "State"}),
                width='stretch')
            st.caption("Tip: index each state to its own first week to compare "
                       "*growth rates* rather than absolute volume.")

        st.subheader("Foot-traffic detail")
        st.dataframe(f, width='stretch', hide_index=True)

# ----------------------------------------------------------- Cannibalization --
with tab_cann:
    st.subheader("🧭 Store cannibalization (difference-in-differences)")
    st.caption("When Boot Barn opens a store, does it steal foot traffic from its "
               "own nearby stores? We compare nearby ('exposed') stores' visit "
               "change around each opening vs far-away ('control') stores over the "
               "same weeks — the difference nets out seasonality and chain trends.")
    need = (foot.empty
            or "open_date" not in foot.columns or "latitude" not in foot.columns
            or foot["open_date"].isna().all()
            or foot["latitude"].isna().all()
            or foot["date_range_start"].nunique() < 8)
    if need:
        st.info(
            "**Not enough data yet.** This activates once the filtered Advan feed is "
            "backfilled with store **OPEN_DATE** + **lat/lng** and a history spanning "
            "openings (pre & post weeks). Backfill ~a year of the BOOT-only feed and "
            "this fills in automatically.\n\n"
            "**Method:** for each new opening, nearby existing stores are the "
            "*exposed* group and far stores the *control*. The estimate is the "
            "exposed group's %-visit change minus the control's — a negative value "
            "means traffic was pulled from neighbors (cannibalization).")
    else:
        from bbxray import analysis
        c1, c2 = st.columns(2)
        radius = c1.slider("Neighbor radius (miles)", 2, 50, 15)
        window = c2.slider("Pre/post window (weeks)", 4, 26, 8)
        res = analysis.run_cannibalization(foot, radius_miles=radius,
                                           window_weeks=window)
        s = res["summary"]
        if not s or s.get("n_openings", 0) == 0:
            st.warning("No openings fall inside the data window with enough pre/post "
                       "weeks yet. Widen the window or backfill more history.")
        else:
            m1, m2, m3 = st.columns(3)
            m1.metric("Openings analyzed", s["n_openings"])
            avg = s.get("avg_cannibalization_pts")
            m2.metric("Avg cannibalization",
                      f"{avg:+.1f} pts" if avg is not None else "—",
                      help="Exposed stores' visit %-change minus control's. "
                           "Negative = nearby stores lost traffic after the opening.")
            m3.metric("Openings w/ negative effect",
                      f"{s.get('share_negative')}%" if s.get("share_negative")
                      is not None else "—")

            es = res["event_study"]
            if not es.empty:
                st.markdown("**Event study — visits around opening week "
                            "(pre-period = 100)**")
                st.plotly_chart(
                    px.line(es, x="rel", y="idx", color="grp", markers=True,
                            labels={"rel": "Weeks relative to opening",
                                    "idx": "Visit index", "grp": "Group"}),
                    width='stretch')
                st.caption("Exposed line dropping below control after week 0 is the "
                           "cannibalization signal.")

            st.markdown("**Per-opening detail** (most cannibalizing first)")
            st.dataframe(res["openings"], width='stretch', hide_index=True)

            labels = {f"{r['store']} — opened {r['open_date']}": r["placekey"]
                      for _, r in res["openings"].iterrows()
                      if r["placekey"] in res["neighbors"]}
            if labels:
                pick = st.selectbox("Map an opening + its exposed neighbors",
                                    list(labels))
                o, exposed = res["neighbors"][labels[pick]]
                mp = pd.concat([
                    pd.DataFrame([{"lat": o["lat"], "lng": o["lng"],
                                   "role": "new opening", "city": o["city"]}]),
                    exposed.assign(role="exposed neighbor")[
                        ["lat", "lng", "role", "city"]]], ignore_index=True)
                st.plotly_chart(
                    px.scatter_geo(mp.dropna(subset=["lat", "lng"]), lat="lat",
                                   lon="lng", color="role", scope="usa",
                                   hover_name="city"), width='stretch')

# ------------------------------------------------------------ Private labels --
with tab_brand:
    st.subheader("🏷️ Private-label brands (their own Shopify sites)")
    st.caption("Boot Barn's exclusive brands (Idyllwind, Cody James, Shyanne, "
               "Moonshine Spirit) run direct-to-consumer Shopify stores. This "
               "pulls their full catalog + prices straight from Shopify.")
    if brands.empty:
        st.info("No private-label data yet. Run `python run.py brands`.")
    else:
        cur = latest_snapshot(brands).copy()
        options = ["All"] + sorted(cur["brand"].dropna().unique())
        pick = st.selectbox("Brand", options)
        d = (cur if pick == "All" else cur[cur["brand"] == pick]).copy()
        d["disc"] = ((d["compare_at_price"] - d["price"]) / d["compare_at_price"]
                     * 100).where(d["on_sale"] == 1)

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Products", f"{d['product_id'].nunique():,}")
        c2.metric("Median price", f"${d['price'].median():,.2f}")
        c3.metric("On sale", f"{(d['on_sale'] == 1).mean() * 100:.0f}%")
        dd = d["disc"].dropna()
        c4.metric("Avg discount", f"{dd.mean():.0f}%" if len(dd) else "—")

        if pick == "All":
            by_brand = (d.groupby("brand")
                        .agg(products=("product_id", "nunique"),
                             median_price=("price", "median"),
                             on_sale=("on_sale", "mean")).reset_index()
                        .sort_values("products", ascending=False))
            by_brand["on_sale"] = (by_brand["on_sale"] * 100).round(0)
            st.subheader("Products & median price by brand")
            st.plotly_chart(px.bar(by_brand, x="brand", y="products",
                                   hover_data=["median_price", "on_sale"]),
                            width='stretch')

        left, right = st.columns(2)
        left.subheader("Price distribution")
        left.plotly_chart(px.histogram(d.dropna(subset=["price"]), x="price",
                                       nbins=40, labels={"price": "Price ($)"}),
                          width='stretch')
        by_type = (d.groupby("product_type")["price"].agg(["median", "count"])
                   .reset_index().sort_values("count", ascending=False).head(20))
        right.subheader("Median price by product type")
        right.plotly_chart(px.bar(by_type, x="product_type", y="median",
                                  hover_data=["count"]), width='stretch')

        if brands["run_ts"].nunique() > 1:
            st.subheader("Median price over time")
            b = brands if pick == "All" else brands[brands["brand"] == pick]
            trend = b.groupby("run_ts")["price"].median().reset_index()
            st.plotly_chart(px.line(trend, x="run_ts", y="price", markers=True),
                            width='stretch')

        # ---- Cross-channel: DTC site vs. Boot Barn catalog ----
        st.divider()
        st.subheader("🔀 DTC site vs. Boot Barn catalog")
        st.caption("Same brand, two channels — its own Shopify store vs. inside "
                   "bootbarn.com — showing whether Boot Barn prices its private "
                   "labels differently by channel.")
        from bbxray.scrape_prices import classify_category
        live = prices[prices["source"] != "wayback"] if "source" in prices else prices
        cat_latest = latest_snapshot(live)
        summ = []
        for c in config.BRAND_SITES:
            db_ = latest_snapshot(brands[brands["brand"].astype(str)
                                  .str.contains(c, case=False, na=False)])
            cb = cat_latest[cat_latest["brand"] == c]
            cb_eff = cb["sale_price"].fillna(cb["list_price"])
            if db_.empty and cb.empty:
                continue
            summ.append({
                "brand": c,
                "DTC median": round(db_["price"].median(), 2) if not db_.empty else None,
                "DTC n": int(db_["product_id"].nunique()),
                "Catalog median": round(cb_eff.median(), 2) if not cb.empty else None,
                "Catalog n": int(cb["product_id"].nunique()),
            })
        sdf = pd.DataFrame(summ)
        if sdf.empty:
            st.info("No overlapping brands between the DTC sites and the catalog yet.")
        else:
            sdf["Δ DTC−Catalog"] = (sdf["DTC median"] - sdf["Catalog median"]).round(2)
            st.dataframe(sdf, width='stretch', hide_index=True)
            st.caption("Small **Catalog n** = the main scrape samples only a few of "
                       "each brand; raise BBXRAY_MAX_PRODUCTS for a tighter read.")

            bsel = st.selectbox("Category breakdown", sdf["brand"].tolist())
            dbb = latest_snapshot(brands[brands["brand"].astype(str)
                                  .str.contains(bsel, case=False, na=False)]).copy()
            dbb["cat"] = [classify_category(t, u) for t, u in
                          zip(dbb["title"].fillna(""), dbb["url"].fillna(""))]
            dbb["eff"] = dbb["price"]
            dbb["channel"] = "DTC site"
            cbb = cat_latest[cat_latest["brand"] == bsel].copy()
            cbb["cat"] = cbb["category"]
            cbb["eff"] = cbb["sale_price"].fillna(cbb["list_price"])
            cbb["channel"] = "Catalog"
            combo = pd.concat([dbb[["cat", "eff", "channel"]],
                               cbb[["cat", "eff", "channel"]]], ignore_index=True
                              ).dropna(subset=["cat", "eff"])
            if not combo.empty:
                bycat = (combo.groupby(["cat", "channel"])["eff"].median()
                         .reset_index())
                st.plotly_chart(
                    px.bar(bycat, x="cat", y="eff", color="channel", barmode="group",
                           labels={"eff": "Median price ($)", "cat": "Category",
                                   "channel": ""}), width='stretch')

        st.subheader("Recently launched products")
        d2 = d.copy()
        d2["launched"] = pd.to_datetime(d2["product_created_at"], errors="coerce",
                                        utc=True)
        recent = d2.sort_values("launched", ascending=False).head(25)
        st.dataframe(recent[["launched", "brand", "title", "product_type", "price",
                             "compare_at_price", "on_sale", "url"]],
                     width='stretch', hide_index=True)

        st.subheader("Product table")
        st.dataframe(d[["brand", "title", "product_type", "price",
                        "compare_at_price", "on_sale", "available", "url"]]
                     .sort_values("price"), width='stretch', hide_index=True)

# ----------------------------------------------------------------- Outreach --
OUT_COMPANIES = ["Boot Barn", "Ariat", "Cavender's", "Tecovas", "Sheplers",
                 "Tractor Supply", "Rural King", "Cabela's", "Durango Boots",
                 "Justin Boots", "Georgia Boot", "Wrangler"]
OUT_PERSONAS = {
    "Merchandising / Buying": ["merchandiser", "buyer", "merchandising",
                               "category manager"],
    "Planning / Allocation": ["planner", "allocation", "inventory planning",
                              "demand planning"],
    "Store Operations": ["store manager", "district manager", "regional manager",
                         "retail operations"],
    "Supply Chain / DC": ["supply chain", "distribution center", "logistics",
                          "sourcing"],
    "Marketing / E-commerce": ["marketing", "ecommerce", "digital", "brand manager"],
    "Leadership": ["VP", "director", "chief", "head of"],
}
OUT_TEMPLATES = {
    "Former employee — industry perspective": (
        "Quick industry perspective — western/workwear retail",
        "Hi {first},\n\nI came across your background at {company} and I'm doing "
        "independent research to better understand the western & workwear retail "
        "industry. Would you be open to a short 20–30 minute call to share your "
        "perspective on the space? Happy to work around your schedule.\n\n"
        "If you'd rather not, no problem at all — just let me know and I won't "
        "follow up.\n\nBest,\n{me}"),
    "Competitor context": (
        "Industry research — a quick perspective?",
        "Hi {first},\n\nI'm researching the western & workwear retail landscape and "
        "your experience at {company} stood out. Could I ask you for 20 minutes to "
        "hear your read on the category — trends, competition, what's working?\n\n"
        "If now's not a good time, just say the word and I won't follow up.\n\n"
        "Thanks,\n{me}"),
}


def _gmail_link(to, subject, body):
    q = urllib.parse.urlencode({"view": "cm", "fs": "1", "to": to or "",
                                "su": subject, "body": body})
    return "https://mail.google.com/mail/?" + q


with tab_out:
    st.subheader("📇 Outreach & channel checks")
    st.warning("**Compliance:** Boot Barn is public (NYSE: BOOT). Keep questions to "
               "high-level *industry* perspective — never solicit confidential or "
               "material non-public information, especially from current employees. "
               "Templates include that disclaimer and an opt-out. You review and "
               "send every email yourself.")

    st.markdown("### 1 · Find people (LinkedIn X-ray search)")
    cc = st.multiselect("Companies", OUT_COMPANIES, default=["Boot Barn"])
    extra_co = st.text_input("…or add a company", "")
    if extra_co:
        cc = cc + [extra_co]
    personas = st.multiselect("Roles / personas", list(OUT_PERSONAS),
                              default=["Merchandising / Buying"])
    kw = [k for p in personas for k in OUT_PERSONAS[p]]
    loc = st.text_input("Location (optional, e.g. \"Texas\" or \"California\")", "")

    if cc and kw:
        co_q = " OR ".join(f'"{c}"' for c in cc)
        kw_q = " OR ".join(f'"{k}"' for k in kw)
        query = f'site:linkedin.com/in ({co_q}) ({kw_q})'
        if loc:
            query += f' "{loc}"'
        g_url = "https://www.google.com/search?q=" + urllib.parse.quote(query)
        li_url = ("https://www.linkedin.com/search/results/people/?keywords="
                  + urllib.parse.quote(" ".join(cc + kw + ([loc] if loc else []))))
        st.code(query, language="text")
        b1, b2 = st.columns(2)
        b1.link_button("🔎 Search on Google (X-ray)", g_url, width='stretch')
        b2.link_button("in  Search on LinkedIn", li_url, width='stretch')
        st.caption("Click through, open profiles, and add the good ones below. "
                   "Tip: for **former** Boot Barn staff, look for 'Past: Boot Barn' "
                   "on the profile.")

    st.divider()
    st.markdown("### 2 · Your contacts")
    cols = ["name", "title", "company", "relationship", "linkedin_url", "email",
            "status", "notes"]
    base = (contacts[cols] if not contacts.empty else
            pd.DataFrame(columns=cols))
    edited = st.data_editor(
        base, num_rows="dynamic", width='stretch', key="contacts_editor",
        column_config={
            "relationship": st.column_config.SelectboxColumn(
                options=["former_boot", "current_boot", "competitor", "other"]),
            "status": st.column_config.SelectboxColumn(
                options=["to_contact", "drafted", "sent", "replied", "passed"]),
            "linkedin_url": st.column_config.LinkColumn(),
        })
    if st.button("💾 Save contacts"):
        clean = edited.where(pd.notna(edited), None)
        rows = clean.to_dict("records")
        now = _dt.datetime.now(_dt.timezone.utc).isoformat()
        rows = [r for r in rows if r.get("name") or r.get("email")]
        for r in rows:
            r["added_ts"] = now
        db.replace_contacts(rows)
        load.clear()
        st.success(f"Saved {len(rows)} contacts.")
        st.rerun()

    # ---- Auto-find missing emails (Hunter.io, free tier ~25/month) ----
    from bbxray import email_finder
    hkey = _secret("HUNTER_API_KEY") or email_finder.api_key()
    missing = (contacts[(contacts["email"].isna() | (contacts["email"] == ""))
                        & contacts["name"].notna() & contacts["company"].notna()]
               if not contacts.empty else pd.DataFrame())
    if not hkey:
        st.caption("🔍 **Auto-find emails:** create a free hunter.io account, then "
                   "add `HUNTER_API_KEY` to your `.env` (and Streamlit secrets) to "
                   "enable one-click email lookup for saved contacts. Free tier is "
                   "~25 lookups/month.")
    elif missing.empty:
        st.caption("🔍 All saved contacts with a name + company have emails.")
    else:
        n = len(missing)
        if st.button(f"🔍 Find emails for {n} contact{'s' if n > 1 else ''} "
                     f"missing one (uses {n} of your free Hunter lookups)"):
            allrows = contacts.where(pd.notna(contacts), None).to_dict("records")
            found, misses = 0, []
            with st.spinner("Looking up emails…"):
                for r in allrows:
                    if r.get("email") or not (r.get("name") and r.get("company")):
                        continue
                    res = email_finder.find_email(r["name"], r["company"], hkey)
                    if res["email"]:
                        r["email"] = res["email"]
                        conf = f"email confidence {res['score']}%"
                        r["notes"] = f"{r['notes']} | {conf}" if r.get("notes") else conf
                        found += 1
                    else:
                        misses.append(f"{r['name']}: {res['error'] or 'not found'}")
            keep = [{k: r.get(k) for k in cols + ["added_ts"]} for r in allrows]
            db.replace_contacts(keep)
            load.clear()
            st.success(f"Found {found} of {n} emails (confidence saved in notes).")
            if misses:
                st.caption("No luck: " + "; ".join(misses[:5]))
            st.rerun()

    st.divider()
    st.markdown("### 3 · Compose outreach (you review & send)")
    if contacts.empty:
        st.info("Add a contact above (with an email) to draft outreach.")
    else:
        me = st.text_input("Your name (signature)", "")
        named = contacts[contacts["name"].notna()]
        who = st.selectbox("Contact", named["name"].tolist())
        row = named[named["name"] == who].iloc[0]
        tmpl = st.selectbox("Template", list(OUT_TEMPLATES))
        subj_t, body_t = OUT_TEMPLATES[tmpl]
        first = str(row["name"]).split()[0] if pd.notna(row["name"]) else "there"
        ctx = {"first": first, "company": row.get("company") or "your company",
               "me": me or "[your name]"}
        subject = st.text_input("Subject", subj_t.format(**ctx))
        body = st.text_area("Body", body_t.format(**ctx), height=280)
        to = row.get("email")
        if not to or pd.isna(to):
            st.warning("This contact has no email yet — add one in the table above "
                       "to enable sending.")
        else:
            st.link_button("✉️ Open in Gmail (review, then send)",
                           _gmail_link(to, subject, body), width='stretch')
            st.caption(f"Opens a pre-filled Gmail compose to {to}. Nothing sends "
                       "until you click Send in Gmail.")

    # ---- 4 · OAuth send: connect Gmail, approve a queue, bulk-send ----
    st.divider()
    st.markdown("### 4 · Send from the dashboard (Gmail OAuth)")
    g_cid, g_sec = _secret("GMAIL_CLIENT_ID"), _secret("GMAIL_CLIENT_SECRET")
    g_redir = _secret("GMAIL_REDIRECT_URI")
    if not (g_cid and g_sec and g_redir):
        st.info("**Not set up yet.** To send from inside the dashboard, create a "
                "Google Cloud OAuth client (send-only Gmail scope) and add "
                "`GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, and `GMAIL_REDIRECT_URI` "
                "to your secrets. Until then, use the **Open in Gmail** links above. "
                "(Ask me for the 15-min setup walkthrough.)")
    else:
        from bbxray import gmail_send
        qp = st.query_params
        if "code" in qp and not st.session_state.get("gmail_token"):
            try:
                tok = gmail_send.exchange_code(
                    g_cid, g_sec, g_redir, qp["code"],
                    st.session_state.get("oauth_state"))
                db.set_gmail_token(tok)
                st.session_state["gmail_token"] = tok
                st.query_params.clear()
                st.success("Gmail connected.")
            except Exception as e:
                st.error(f"OAuth exchange failed: {e}")

        tok = st.session_state.get("gmail_token") or db.get_gmail_token()
        creds = acct = None
        if tok:
            try:
                creds, fresh = gmail_send.load_creds(tok)
                if fresh != tok:
                    db.set_gmail_token(fresh)
                    st.session_state["gmail_token"] = fresh
            except Exception as e:
                st.warning(f"Gmail session expired or invalid ({e}). Reconnect.")
                creds = tok = None

        if not creds:
            url, state = gmail_send.auth_url(g_cid, g_sec, g_redir)
            st.session_state["oauth_state"] = state
            st.link_button("🔗 Connect Gmail (send-only)", url)
            st.caption("Grants send-only access; the app cannot read your inbox. "
                       "You approve every email before it sends.")
        else:
            top = st.columns([3, 1])
            top[0].success("Gmail connected (send-only). Emails send from the "
                           "account you authorized.")
            if top[1].button("Disconnect"):
                db.set_gmail_token("")
                st.session_state.pop("gmail_token", None)
                st.rerun()

            sendable = (contacts[contacts["email"].notna() &
                                 (contacts["email"] != "")]
                        if not contacts.empty else pd.DataFrame())
            if sendable.empty:
                st.info("No contacts with emails yet — add/find some above.")
            else:
                st.markdown("**Build a queue → approve each → send the approved.**")
                picks = st.multiselect("Contacts to email",
                                       sendable["name"].dropna().tolist())
                bt = st.selectbox("Template", list(OUT_TEMPLATES), key="bulk_tmpl")
                bme = st.text_input("Your name (signature)", key="bulk_me")
                bsub_t, bbody_t = OUT_TEMPLATES[bt]
                queue = []
                for nm in picks:
                    r = sendable[sendable["name"] == nm].iloc[0]
                    ctx = {"first": str(nm).split()[0],
                           "company": r.get("company") or "your company",
                           "me": bme or "[your name]"}
                    with st.expander(f"✉️  {nm}  <{r['email']}>"):
                        su = st.text_input("Subject", bsub_t.format(**ctx),
                                           key=f"su_{nm}")
                        bo = st.text_area("Body", bbody_t.format(**ctx),
                                          key=f"bo_{nm}", height=200)
                        ap = st.checkbox("✅ Approve this email", key=f"ap_{nm}")
                    queue.append({"name": nm, "to": r["email"], "subject": su,
                                  "body": bo, "approved": ap})
                appr = [q for q in queue if q["approved"]]
                if picks:
                    st.markdown(f"**{len(appr)} of {len(queue)} approved.**")
                mode = st.radio("Delivery", ["Send now", "Schedule for later"],
                                horizontal=True, key="send_mode")

                if mode == "Send now":
                    if appr and st.button(f"🚀 Send {len(appr)} approved email(s)"):
                        ok, errs = 0, []
                        prog = st.progress(0.0)
                        for i, q in enumerate(appr, 1):
                            try:
                                gmail_send.send_email(creds, None, q["to"],
                                                      q["subject"], q["body"])
                                ok += 1
                            except Exception as e:
                                errs.append(f"{q['name']}: {e}")
                            prog.progress(i / len(appr))
                        if ok:
                            allc = contacts.where(pd.notna(contacts), None).to_dict("records")
                            done = {q["name"] for q in appr}
                            for c in allc:
                                if c.get("name") in done:
                                    c["status"] = "sent"
                            db.replace_contacts(
                                [{k: c.get(k) for k in cols + ["added_ts"]} for c in allc])
                            load.clear()
                        st.success(f"Sent {ok} email(s); contacts marked 'sent'.")
                        if errs:
                            st.error("Errors: " + "; ".join(errs[:5]))
                        if ok:
                            st.rerun()
                else:
                    cA, cB = st.columns(2)
                    sd = cA.date_input("Send date", key="sch_date")
                    sti = cB.time_input("Send time (your local timezone)",
                                        key="sch_time")
                    when = _dt.datetime.combine(sd, sti)
                    past = when < _dt.datetime.now()
                    if past:
                        st.warning("That time is in the past — pick a future time.")
                    if appr and not past and st.button(
                            f"📅 Schedule {len(appr)} for {when:%b %d, %I:%M %p}"):
                        now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
                        db.insert_scheduled([
                            {"to_email": q["to"], "contact_name": q["name"],
                             "subject": q["subject"], "body": q["body"],
                             "send_at": when.isoformat(), "status": "scheduled",
                             "created_ts": now_iso, "sent_ts": None, "error": None}
                            for q in appr])
                        st.success(f"Scheduled {len(appr)} email(s) for "
                                   f"{when:%b %d, %I:%M %p}. The sender task delivers "
                                   "them within ~20 min of that time (your PC must be "
                                   "on and the send_scheduled task running).")
                        st.rerun()

                st.caption("Gmail caps sending (~500/day personal). Keep batches "
                           "small and personalized — this is channel-check outreach, "
                           "not a blast.")

                # ---- Scheduled queue: review / cancel before they fire ----
                pend = db.list_scheduled("scheduled")
                if pend:
                    with st.expander(f"📋 Scheduled queue ({len(pend)} pending)"):
                        for e in pend:
                            r1, r2 = st.columns([5, 1])
                            r1.markdown(
                                f"**{e['send_at'][:16].replace('T', ' ')}** — "
                                f"{e['contact_name']} `<{e['to_email']}>`  ·  "
                                f"{e['subject']}")
                            if r2.button("Cancel", key=f"cx_{e['id']}"):
                                db.update_scheduled(e["id"], status="canceled")
                                st.rerun()

                # ---- Missed: PC was off past the grace window; you decide ----
                miss = db.list_scheduled("missed")
                if miss:
                    with st.expander(f"⚠️ Missed — PC was off ({len(miss)})",
                                     expanded=True):
                        st.caption("These passed their send time while your PC was "
                                   "off, so they were held — **not** auto-sent. Send "
                                   "now or cancel, on your terms.")
                        for e in miss:
                            m1, m2, m3 = st.columns([4, 1, 1])
                            m1.markdown(
                                f"was **{e['send_at'][:16].replace('T', ' ')}** — "
                                f"{e['contact_name']} · {e['subject']}")
                            if m2.button("Send now", key=f"ms_{e['id']}"):
                                try:
                                    gmail_send.send_email(creds, None, e["to_email"],
                                                          e["subject"], e["body"])
                                    db.update_scheduled(
                                        e["id"], status="sent", error=None,
                                        sent_ts=_dt.datetime.now(
                                            _dt.timezone.utc).isoformat())
                                    st.success(f"Sent to {e['to_email']}.")
                                except Exception as ex:
                                    st.error(f"Send failed: {ex}")
                                st.rerun()
                            if m3.button("Cancel", key=f"mc_{e['id']}"):
                                db.update_scheduled(e["id"], status="canceled")
                                st.rerun()

with st.sidebar:
    st.header("Run log")
    if not runs.empty:
        st.dataframe(runs.sort_values("run_id", ascending=False)[
            ["run_ts", "kind", "n_rows", "notes"]],
            width='stretch', hide_index=True)
    st.caption("Data source: bootbarn.com (public) + Dewey Data (foot traffic).")

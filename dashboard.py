"""Boot Barn X-Ray dashboard (Streamlit).

Run:  streamlit run dashboard.py
Reads the SQLite DB populated by the scrapers and visualizes:
  - Pricing: distribution, by brand/category, discount depth, changes over time
  - Stores: map, count by state, intra-quarter openings/closures (snapshot diff)
  - Foot traffic: visits over time (if Dewey data loaded)
"""
from __future__ import annotations

import os

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


st.title("🥾 Boot Barn X-Ray")
st.caption("Competitive intelligence: pricing · store footprint · foot traffic")

prices = load("price_snapshots")
stores = load("store_snapshots")
foot = load("foot_traffic")
runs = load("runs")

if prices.empty and stores.empty:
    st.warning("No data yet. Run the scrapers first:  `python run.py all`")
    st.stop()

tab_price, tab_store, tab_foot = st.tabs(["💲 Pricing", "📍 Stores", "🚶 Foot Traffic"])

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

        # ---- Openings / closures via snapshot diff ----
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

        st.subheader("Visits by state")
        by_reg = f.groupby("region")["raw_visit_counts"].sum().reset_index(
            ).sort_values("raw_visit_counts", ascending=False)
        st.plotly_chart(px.bar(by_reg, x="region", y="raw_visit_counts"),
                        width='stretch')

        st.subheader("Foot-traffic detail")
        st.dataframe(f, width='stretch', hide_index=True)

with st.sidebar:
    st.header("Run log")
    if not runs.empty:
        st.dataframe(runs.sort_values("run_id", ascending=False)[
            ["run_ts", "kind", "n_rows", "notes"]],
            width='stretch', hide_index=True)
    st.caption("Data source: bootbarn.com (public) + Dewey Data (foot traffic).")

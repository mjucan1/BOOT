"""Boot Barn X-Ray dashboard (Streamlit).

Run:  streamlit run dashboard.py
Reads the SQLite DB populated by the scrapers and visualizes:
  - Pricing: distribution, by brand/category, discount depth, changes over time
  - Stores: map, count by state, intra-quarter openings/closures (snapshot diff)
  - Foot traffic: visits over time (if Dewey data loaded)
"""
from __future__ import annotations

import calendar
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
runs = load("runs")

if prices.empty and stores.empty:
    st.warning("No data yet. Run the scrapers first:  `python run.py all`")
    st.stop()

tab_price, tab_store, tab_foot, tab_cann, tab_brand = st.tabs(
    ["💲 Pricing", "📍 Stores", "🚶 Foot Traffic", "🧭 Cannibalization",
     "🏷️ Private Labels"])

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

            trend = (sub.groupby("run_ts")
                     .agg(median_price=("eff_price", "median"),
                          avg_price=("eff_price", "mean"),
                          products=("product_id", "nunique")).reset_index())
            trend["date"] = pd.to_datetime(trend["run_ts"], errors="coerce", utc=True)
            if len(trend) > 1:
                st.markdown(f"**{sel} — median & average price over time**")
                melt = trend.melt(id_vars="date",
                                  value_vars=["median_price", "avg_price"],
                                  var_name="metric", value_name="price")
                st.plotly_chart(px.line(melt, x="date", y="price", color="metric",
                                        markers=True), width='stretch')

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

with st.sidebar:
    st.header("Run log")
    if not runs.empty:
        st.dataframe(runs.sort_values("run_id", ascending=False)[
            ["run_ts", "kind", "n_rows", "notes"]],
            width='stretch', hide_index=True)
    st.caption("Data source: bootbarn.com (public) + Dewey Data (foot traffic).")

"""Unit tests for bbxray.analysis — haversine, aggregation, and the DiD engine.

The cannibalization test builds a synthetic foot-traffic panel with a known
answer: an existing store 5 miles from a new opening loses exactly 20% of its
visits post-open while a far-away control store is flat, so the DiD signal must
come out at -20.0 points.
"""
import math

import numpy as np
import pandas as pd
import pytest

from bbxray.analysis import (EARTH_MILES, haversine_miles, run_cannibalization,
                             store_dim, weekly_visits)

OPEN = pd.Timestamp("2025-04-07")
DEG_MILES = EARTH_MILES * math.pi / 180  # exact miles per degree of latitude


class TestHaversine:
    def test_zero_distance(self):
        assert haversine_miles(36.0, -100.0, 36.0, -100.0) == 0.0

    def test_one_degree_of_latitude(self):
        assert haversine_miles(0.0, 0.0, 1.0, 0.0) == pytest.approx(DEG_MILES, rel=1e-6)

    def test_quarter_meridian(self):
        assert haversine_miles(0.0, 0.0, 90.0, 0.0) == pytest.approx(
            EARTH_MILES * math.pi / 2, rel=1e-6)

    def test_symmetry(self):
        a = haversine_miles(34.05, -118.24, 40.71, -74.01)
        b = haversine_miles(40.71, -74.01, 34.05, -118.24)
        assert a == pytest.approx(b)

    def test_vectorized_over_arrays(self):
        d = haversine_miles(0.0, 0.0, np.array([1.0, 2.0]), np.array([0.0, 0.0]))
        assert isinstance(d, np.ndarray)
        assert d == pytest.approx([DEG_MILES, 2 * DEG_MILES], rel=1e-6)


def _foot_row(pk, city, lat, lng, open_date, week, visits):
    return {
        "placekey": pk, "location_name": f"Boot Barn {city}", "city": city,
        "region": "TX", "latitude": lat, "longitude": lng, "open_date": open_date,
        "date_range_start": week.strftime("%Y-%m-%d"), "raw_visit_counts": visits,
    }


def make_foot(exposed_lat=None, new_open_date="2025-04-07", open_dates=True,
              n_weeks_each_side=8):
    """Synthetic panel around a single opening at OPEN (2025-04-07).

    E: existing store ~5 mi from the opening; 100 visits/wk pre, 80 post (-20%).
    C: existing control ~45 mi away; flat 100 visits/wk.
    N: the new store; opens at OPEN, 50 visits/wk from open onward.
    """
    exposed_lat = 36.0 + (5.0 / DEG_MILES) if exposed_lat is None else exposed_lat
    weeks = [OPEN + pd.Timedelta(weeks=w)
             for w in range(-n_weeks_each_side, n_weeks_each_side + 1)]
    rows = []
    for w in weeks:
        rows.append(_foot_row("E", "Amarillo", exposed_lat, -100.0, None, w,
                              100 if w <= OPEN else 80))
        rows.append(_foot_row("C", "Abilene", 36.65, -100.0, None, w, 100))
        if w >= OPEN:
            rows.append(_foot_row("N", "Lubbock", 36.0, -100.0,
                                  new_open_date if open_dates else None, w, 50))
    return pd.DataFrame(rows)


class TestStoreDim:
    def test_one_row_per_store_with_latest_location(self):
        foot = pd.DataFrame([
            # Deliberately out of order: "last" must be last *by week*, not by row.
            _foot_row("P1", "Waco", 31.6, -97.1, "2019-05-01",
                      pd.Timestamp("2025-01-13"), 10),
            _foot_row("P1", "Waco", 31.5, -97.1, "2019-05-01",
                      pd.Timestamp("2025-01-06"), 12),
            _foot_row("P2", "Tyler", 32.4, -95.3, "not-a-date",
                      pd.Timestamp("2025-01-06"), 7),
        ])
        dim = store_dim(foot).set_index("placekey")
        assert len(dim) == 2
        assert dim.loc["P1", "lat"] == 31.6
        assert dim.loc["P1", "open_dt"] == pd.Timestamp("2019-05-01")
        assert pd.isna(dim.loc["P2", "open_dt"])   # unparseable date -> NaT


class TestWeeklyVisits:
    def test_sums_within_store_week_and_drops_bad_rows(self):
        wk_ts = pd.Timestamp("2025-01-06")
        foot = pd.DataFrame([
            _foot_row("P1", "Waco", 31.6, -97.1, None, wk_ts, 10),
            _foot_row("P1", "Waco", 31.6, -97.1, None, wk_ts, 5),
            _foot_row(None, "Ghost", 31.0, -97.0, None, wk_ts, 99),
        ])
        foot.loc[len(foot)] = _foot_row("P2", "Tyler", 32.4, -95.3, None, wk_ts, 3)
        foot.loc[len(foot) - 1, "date_range_start"] = "not-a-date"
        wk = weekly_visits(foot)
        assert len(wk) == 1                        # null placekey + bad date dropped
        assert wk.iloc[0]["raw_visit_counts"] == 15


class TestRunCannibalization:
    def test_known_did_signal(self):
        res = run_cannibalization(make_foot())

        assert res["summary"]["n_openings"] == 1
        assert res["summary"]["avg_cannibalization_pts"] == -20.0
        assert res["summary"]["share_negative"] == 100.0

        row = res["openings"].iloc[0]
        assert row["placekey"] == "N"
        assert row["store"] == "Lubbock, TX"
        assert row["open_date"] == "2025-04-07"
        assert row["n_exposed"] == 1
        assert row["n_control"] == 1
        assert row["exposed_%chg"] == -20.0
        assert row["control_%chg"] == 0.0
        assert row["cannibalization_pts"] == -20.0

        # Event study: pre-period indexed to 100; exposed drops to 80 post.
        es = res["event_study"].set_index(["grp", "rel"])["idx"]
        assert es.loc[("exposed", -4)] == pytest.approx(100.0)
        assert es.loc[("exposed", 4)] == pytest.approx(80.0)
        assert es.loc[("control", 4)] == pytest.approx(100.0)

        # Neighbors map: the opening's exposed set contains exactly E.
        assert set(res["neighbors"]) == {"N"}
        _, exposed_df = res["neighbors"]["N"]
        assert set(exposed_df["placekey"]) == {"E"}

    def _assert_empty(self, res):
        assert res["openings"].empty
        assert res["event_study"].empty
        assert res["summary"] == {}
        assert res["neighbors"] == {}

    def test_too_few_weeks_returns_empty(self):
        self._assert_empty(run_cannibalization(make_foot(n_weeks_each_side=1)))

    def test_no_open_dates_returns_empty(self):
        self._assert_empty(run_cannibalization(make_foot(open_dates=False)))

    def test_opening_too_close_to_data_edge_is_excluded(self):
        # Open date 7 weeks after the panel midpoint leaves < window_weeks of
        # post-period data, so the opening must be skipped entirely.
        self._assert_empty(run_cannibalization(make_foot(new_open_date="2025-05-26")))

    def test_no_stores_within_radius_returns_empty(self):
        # Move the "exposed" store out past the control buffer: nothing is
        # within the 10-mile radius, so there is no DiD to run.
        self._assert_empty(run_cannibalization(make_foot(exposed_lat=36.65)))

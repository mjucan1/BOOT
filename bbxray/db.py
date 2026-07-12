"""Storage layer: works with local SQLite *or* hosted Postgres.

Which backend is used is decided by `config.DATABASE_URL`:
  - unset            -> local SQLite file (data/bootbarn.sqlite)  [dev]
  - postgresql://... -> hosted Postgres (Supabase/Neon)           [shared/prod]

Same snapshot design either way: every run appends a date-stamped snapshot and
nothing is overwritten, so openings/closures and price trends come from diffing
snapshots over time.
"""
from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import (Column, Float, Integer, MetaData, Table, Text,
                        create_engine, delete, func, insert, select)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402

metadata = MetaData()

runs = Table(
    "runs", metadata,
    Column("run_id", Integer, primary_key=True, autoincrement=True),
    Column("run_ts", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("n_rows", Integer),
    Column("notes", Text),
)

price_snapshots = Table(
    "price_snapshots", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_ts", Text, nullable=False, index=True),
    Column("product_id", Text, index=True),
    Column("sku", Text),
    Column("name", Text),
    Column("brand", Text),
    Column("category", Text),
    Column("url", Text),
    Column("list_price", Float),
    Column("sale_price", Float),
    Column("currency", Text),
    Column("availability", Text),
    Column("in_stock", Integer),
    Column("map_hidden", Integer),
)

store_snapshots = Table(
    "store_snapshots", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_ts", Text, nullable=False, index=True),
    Column("store_id", Text, nullable=False, index=True),
    Column("name", Text),
    Column("address", Text),
    Column("city", Text),
    Column("state", Text),
    Column("zip", Text),
    Column("phone", Text),
    Column("lat", Float),
    Column("lng", Float),
    Column("hours_json", Text),
    Column("url", Text),
)

foot_traffic = Table(
    "foot_traffic", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("placekey", Text, index=True),
    Column("store_id", Text),
    Column("location_name", Text),
    Column("city", Text),
    Column("region", Text),
    Column("date_range_start", Text, index=True),
    Column("raw_visit_counts", Integer),
    Column("raw_visitor_counts", Integer),
    Column("source", Text),
)

_engine = None


def get_engine():
    """Return a cached SQLAlchemy engine for the configured backend."""
    global _engine
    if _engine is None:
        url = config.DATABASE_URL
        if url.startswith("sqlite"):
            config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(url, pool_pre_ping=True)
    return _engine


def init_db() -> None:
    metadata.create_all(get_engine())


def _insert(table: Table, rows: list[dict]) -> None:
    if not rows:
        return
    eng = get_engine()
    with eng.begin() as conn:
        conn.execute(insert(table), rows)


def record_run(kind: str, run_ts: str, n_rows: int, notes: str = "") -> None:
    _insert(runs, [{"run_ts": run_ts, "kind": kind,
                    "n_rows": n_rows, "notes": notes}])


def insert_prices(rows: list[dict]) -> None:
    _insert(price_snapshots, rows)


def insert_stores(rows: list[dict]) -> None:
    _insert(store_snapshots, rows)


def insert_foot_traffic(rows: list[dict]) -> None:
    _insert(foot_traffic, rows)


def max_foot_traffic_date() -> str | None:
    """Latest date_range_start already loaded (so sync only pulls newer data)."""
    with get_engine().connect() as conn:
        return conn.execute(
            select(func.max(foot_traffic.c.date_range_start))).scalar()


def delete_foot_traffic_dates(source: str, dates) -> None:
    """Remove existing rows for these weeks so a re-load replaces (no dupes)."""
    dates = [d for d in set(dates) if d]
    if not dates:
        return
    with get_engine().begin() as conn:
        conn.execute(delete(foot_traffic).where(
            foot_traffic.c.source == source,
            foot_traffic.c.date_range_start.in_(dates)))


if __name__ == "__main__":
    init_db()
    print(f"Initialized schema on {config.DATABASE_URL.split('@')[-1]}")

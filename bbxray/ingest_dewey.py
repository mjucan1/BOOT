"""Ingest Boot Barn foot-traffic from Dewey Data (Advan / SafeGraph patterns).

Prerequisites
-------------
1. A Dewey account with a licensed patterns dataset. As UCLA-affiliated, check
   whether the library provides Dewey access (many R1 libraries do) -- that
   gives you a login to https://app.deweydata.io.
2. Create an API key: Dewey platform -> Connections -> Add Connection -> API Key.
   Save it (shown once). Then set env vars before running:
       set DEWEY_API_KEY=...            (your key)
       set DEWEY_PRODUCT_PATH=...       (the dataset's API "product path")
   Find the product path on the dataset's "Get & Use Data" -> "API" tab.
3. Install the client:
       pip install "git+https://github.com/Dewey-Data/deweydatapy"

What this does
--------------
- Uses deweydatapy to download the dataset files for a date range.
- Filters rows to Boot Barn POIs (location_name contains 'boot barn').
- Normalizes to our foot_traffic schema and loads into SQLite.

Column names below match Advan Monthly/Weekly Patterns + SafeGraph Patterns;
adjust COLS if your licensed dataset differs (inspect one downloaded CSV).
"""
from __future__ import annotations

import datetime as dt
import glob
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
from bbxray import db  # noqa: E402
from bbxray.utils import log  # noqa: E402

# Map dataset columns -> our schema. Left = our field, right = candidates.
COLS = {
    "placekey": ["PLACEKEY", "placekey"],
    "location_name": ["LOCATION_NAME", "location_name"],
    "city": ["CITY", "city"],
    "region": ["REGION", "region", "STATE", "state"],
    "date_range_start": ["DATE_RANGE_START", "date_range_start", "MONTH", "spend_date_range_start"],
    "raw_visit_counts": ["RAW_VISIT_COUNTS", "raw_visit_counts"],
    "raw_visitor_counts": ["RAW_VISITOR_COUNTS", "raw_visitor_counts"],
}


def _first_col(df: pd.DataFrame, candidates: list[str]) -> str | None:
    for c in candidates:
        if c in df.columns:
            return c
    return None


def download(date_start: str, date_end: str) -> Path:
    """Download dataset files via deweydatapy into DEWEY_DOWNLOAD_DIR."""
    if not config.DEWEY_API_KEY or not config.DEWEY_PRODUCT_PATH:
        raise SystemExit(
            "Set DEWEY_API_KEY and DEWEY_PRODUCT_PATH env vars first "
            "(see this file's docstring).")
    try:
        import deweydatapy as ddp
    except ImportError:
        raise SystemExit(
            'deweydatapy not installed. Run:\n'
            '  pip install "git+https://github.com/Dewey-Data/deweydatapy"')

    config.DEWEY_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    log(f"[dewey] downloading {date_start}..{date_end} -> {config.DEWEY_DOWNLOAD_DIR}")
    # Current deweydatapy signature:
    #   download_files1(apikey, product_path, dest_folder, start_date, end_date, ...)
    # It fetches the file list AND downloads in one call, filtered by date range.
    # (download_files1 pages the downloads, best for large/long pulls.)
    ddp.download_files1(
        config.DEWEY_API_KEY, config.DEWEY_PRODUCT_PATH,
        str(config.DEWEY_DOWNLOAD_DIR),
        start_date=date_start, end_date=date_end, skip_exists=True)
    log(f"[dewey] downloaded into {config.DEWEY_DOWNLOAD_DIR}")
    return config.DEWEY_DOWNLOAD_DIR


def _iter_downloaded(folder: Path):
    for pat in ("*.csv.gz", "*.csv", "*.parquet"):
        for f in glob.glob(str(folder / pat)):
            yield Path(f)


def load_to_db(folder: Path | None = None, source: str = "dewey_patterns") -> int:
    folder = folder or config.DEWEY_DOWNLOAD_DIR
    files = list(_iter_downloaded(folder))
    if not files:
        raise SystemExit(f"No downloaded files in {folder}. Run download() first.")

    db.init_db()
    # Accumulate all Boot Barn rows across every partition file FIRST. A single
    # week is split across many files that share the same date_range_start, so we
    # must collect everything before touching the DB -- otherwise a per-file
    # delete-then-insert would wipe rows another file just added.
    all_rows: list[dict] = []
    for f in files:
        log(f"[dewey] reading {f.name}")
        reader = (pd.read_parquet(f) if f.suffix == ".parquet"
                  else pd.read_csv(f, compression="infer", low_memory=False))
        name_col = _first_col(reader, COLS["location_name"])
        if name_col is None:
            log(f"  ! no location_name column in {f.name}; skipping")
            continue
        bb = reader[reader[name_col].astype(str).str.lower()
                    .str.contains(config.BOOTBARN_NAME_MATCH, na=False)]
        if bb.empty:
            continue
        for _, r in bb.iterrows():
            rec = {"store_id": None, "source": source}
            for field, cands in COLS.items():
                col = _first_col(reader, cands)
                val = r[col] if col else None
                if field in ("raw_visit_counts", "raw_visitor_counts"):
                    try:
                        val = int(val) if pd.notna(val) else None
                    except (ValueError, TypeError):
                        val = None
                elif pd.isna(val):
                    val = None
                else:
                    val = str(val)
                rec[field] = val
            all_rows.append(rec)
        log(f"  + {len(bb)} Boot Barn rows")

    if not all_rows:
        log("[dewey] no Boot Barn rows found -- check column mapping (COLS).")
        return 0

    # Idempotent load: replace any existing rows for the weeks we're loading, so
    # re-running (or overlapping sync windows) never creates duplicates.
    dates = {r["date_range_start"] for r in all_rows}
    db.delete_foot_traffic_dates(source, dates)
    db.insert_foot_traffic(all_rows)
    log(f"[dewey] done: loaded {len(all_rows)} rows across {len(dates)} weeks.")
    return len(all_rows)


def sync(overlap_weeks: int = 2, first_run_weeks: int = 12,
         cleanup: bool = True) -> int:
    """Hands-off update: download only NEW weeks, load them, tidy up.

    Looks at the latest week already in the database and downloads from a little
    before that up to today (the small overlap catches late-arriving data, and
    the idempotent load dedupes it). On the very first run it grabs the last
    `first_run_weeks`. With `cleanup`, the bulky raw files are deleted after a
    successful load so disk usage stays flat -- because we only fetch new weeks,
    each run stays small.
    """
    end = dt.date.today()
    last = db.max_foot_traffic_date()
    if last:
        last_day = dt.date.fromisoformat(str(last)[:10])
        start = last_day - dt.timedelta(weeks=overlap_weeks)
        log(f"[dewey] sync: latest loaded week is {last_day}; "
            f"fetching {start}..{end}")
    else:
        start = end - dt.timedelta(weeks=first_run_weeks)
        log(f"[dewey] sync: first run, fetching last {first_run_weeks} weeks "
            f"({start}..{end})")

    download(start.isoformat(), end.isoformat())
    n = load_to_db()

    if cleanup:
        removed = 0
        for f in _iter_downloaded(config.DEWEY_DOWNLOAD_DIR):
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
        log(f"[dewey] cleaned up {removed} raw files.")
    return n


if __name__ == "__main__":
    # Usage:
    #   python -m bbxray.ingest_dewey sync                      (automated: new weeks only)
    #   python -m bbxray.ingest_dewey sync --no-cleanup         (keep raw files)
    #   python -m bbxray.ingest_dewey download 2025-01-01 2025-06-30
    #   python -m bbxray.ingest_dewey load
    cmd = sys.argv[1] if len(sys.argv) >= 2 else ""
    if cmd == "sync":
        sync(cleanup="--no-cleanup" not in sys.argv)
    elif cmd == "download":
        download(sys.argv[2], sys.argv[3])
    elif cmd == "load":
        load_to_db()
    else:
        print(__doc__)

# Boot Barn X-Ray 🥾

Competitive-intelligence scraper + dashboard for [bootbarn.com](https://www.bootbarn.com):

1. **Pricing** — aggregated product pricing across the catalog (price, availability, brand, derived category, MAP flag).
2. **Store footprint** — the full ~566-store roster, snapshotted over time so **intra-quarter openings/closures** fall out of snapshot diffs.
3. **Foot traffic** — ingested from [Dewey Data](https://www.deweydata.io) (Advan / SafeGraph patterns). Boot Barn publishes none of this itself, so it must come from a licensed dataset.

Data lands in **SQLite** (local dev) or **Postgres** (shared/hosted) — chosen by the
`DATABASE_URL` env var. The dashboard is **Streamlit**.

---

## How it works (and why)

Boot Barn runs on **Salesforce Commerce Cloud** behind **PerimeterX/Yottaa bot protection**. Two consequences shaped the design:

- **Plain `requests` is blocked** (TLS fingerprinting → "Access denied"). We use **`curl_cffi`** to impersonate a real Chrome TLS fingerprint, plus automatic session refresh + backoff when a challenge page appears mid-crawl. See `bbxray/utils.py`.
- **Pricing** comes from schema.org **microdata** on product pages (there is no JSON-LD). Many products use **MAP** (Minimum Advertised Price) and hide the sale price until cart — so the public signal is the *displayed* price + the *original* price. Products where sale pricing is hidden are flagged `map_hidden=1`.
- **Stores** are parsed from `/stores-all`, which renders the complete directory in raw HTML. ZIP → lat/long is geocoded **offline** via `pgeocode`.
- **Openings/closures** are derived by diffing store snapshots across dates — so this only works once you have **≥2 snapshots**. Run the store scrape on a schedule (e.g. weekly).

> ⚖️ **Use responsibly.** This scrapes only public pages, rate-limited and identified via a contact string in the User-Agent (`BBXRAY_CONTACT`). It's built for competitive research. Respect Boot Barn's Terms of Service and don't hammer the site.

---

## Setup

```bash
cd "bootbarn-xray"
python -m venv .venv
.venv\Scripts\activate            # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Run the scrapers

```bash
python run.py stores          # snapshot all ~566 stores
python run.py prices 400      # scrape 400 products (stride-sampled across catalog)
python run.py prices          # scrape up to BBXRAY_MAX_PRODUCTS (default 500)
python run.py all             # prices + stores
```

Tuning via env vars:

| Var | Default | Meaning |
|---|---|---|
| `BBXRAY_DELAY` | `0.75` | Seconds between requests (raise if you hit challenges) |
| `BBXRAY_MAX_PRODUCTS` | `500` | Cap per pricing run; `0` = whole catalog (~30k URLs) |
| `BBXRAY_CONTACT` | your email | Included in the User-Agent |

## Launch the dashboard

```bash
streamlit run dashboard.py
```

Tabs: **Pricing** (distribution, by brand/category, discount depth, trend), **Stores** (US map, by-state, openings/closures diff), **Foot Traffic**.

---

## Foot traffic via Dewey (Advan / SafeGraph patterns)

Boot Barn doesn't expose foot traffic. You license a *patterns* dataset on Dewey and we filter it to Boot Barn POIs.

**1. Get access.** As UCLA-affiliated, check whether the library provides a Dewey Data account (many R1 libraries do). Otherwise sign up at deweydata.io.

**2. Create an API key.** Dewey platform → **Connections → Add Connection → API Key** (shown once — save it). Find the dataset's **product path** on its *Get & Use Data → API* tab.

**3. Install the client + set env vars.**
```bash
pip install "git+https://github.com/Dewey-Data/deweydatapy"
set DEWEY_API_KEY=your_key_here
set DEWEY_PRODUCT_PATH=your_dataset_product_path
```

**4. Download + load.**
```bash
python -m bbxray.ingest_dewey download 2025-01-01 2025-06-30
python -m bbxray.ingest_dewey load
```
This downloads the patterns files, keeps rows where `location_name` contains "boot barn", normalizes to the `foot_traffic` table, and the dashboard's Foot Traffic tab lights up. Column mapping lives in `bbxray/ingest_dewey.py` (`COLS`) — tweak if your licensed dataset uses different column names.

---

## Deploy: weekly scrape + hosted website

Target architecture (all free tiers), chosen because Boot Barn's bot wall blocks
datacenter IPs far more than home IPs:

```
  Your PC (residential IP)                Cloud
  ┌─────────────────────┐                ┌──────────────────────────┐
  │ Task Scheduler       │   writes       │ Postgres (Supabase/Neon) │
  │  weekly -> run.py all │ ─────────────▶ │  shared snapshot store    │
  └─────────────────────┘                └───────────┬──────────────┘
                                                       │ reads
                                          ┌────────────▼─────────────┐
                                          │ Streamlit Community Cloud │
                                          │  password-gated dashboard │
                                          └──────────────────────────┘
```

### 1. Create the shared Postgres (Supabase)
1. Make a project at [supabase.com](https://supabase.com) (free tier).
2. Project → **Connect** → copy the connection string, and convert it to
   SQLAlchemy form:
   `postgresql+psycopg2://postgres:PASSWORD@HOST:5432/postgres?sslmode=require`
3. Create the schema once:
   ```bash
   set DATABASE_URL=postgresql+psycopg2://...   # your URL
   python -m bbxray.db
   ```

### 2. Point the weekly scraper at it (your PC)
1. `copy .env.example .env` and paste your `DATABASE_URL` (+ tuning) into `.env`.
2. Test manually: `powershell -File scrape_weekly.ps1` (writes to Postgres, logs to `data/scrape.log`).
3. Schedule it weekly (Mondays 06:00):
   ```powershell
   $ps = (Get-Command powershell).Source
   schtasks /Create /SC WEEKLY /D MON /TN "BootBarnXray" /ST 06:00 /TR `
     "$ps -NoProfile -ExecutionPolicy Bypass -File `"$PWD\scrape_weekly.ps1`""
   ```
   (Set *"Run whether user is logged on or not"* and *"Wake the computer"* in Task Scheduler's UI if you want it to run unattended.)

### 3. Host the dashboard (Streamlit Community Cloud)
1. Push this folder to a **GitHub** repo (see below).
2. At [share.streamlit.io](https://share.streamlit.io), **New app** → pick the repo,
   main file `dashboard.py`.
3. In the app's **Settings → Secrets**, paste (see `.streamlit/secrets.toml.example`):
   ```toml
   DATABASE_URL = "postgresql+psycopg2://postgres:PASSWORD@HOST:5432/postgres?sslmode=require"
   APP_PASSWORD = "your-shared-team-password"
   ```
4. Deploy → you get a public URL. Share the link + password with your team.

Each weekly run appends a new dated snapshot; the dashboard diffs any two you pick
to show intra-quarter openings/closures.

### Push to GitHub
```bash
git init && git add . && git commit -m "Boot Barn X-Ray"
git branch -M main
git remote add origin https://github.com/<you>/bootbarn-xray.git
git push -u origin main
```
`.gitignore` already excludes `.env`, `.streamlit/secrets.toml`, the SQLite file,
and Dewey downloads, so no secrets or bulky data get committed.

---

## Project layout

```
bootbarn-xray/
  config.py              # URLs, DATABASE_URL, politeness, limits, Dewey settings
  run.py                 # CLI: prices / stores / all / initdb
  dashboard.py           # Streamlit dashboard (+ password gate, Postgres/SQLite)
  scrape_weekly.ps1      # Task Scheduler entry: loads .env, runs run.py all
  .env.example           # scraper secrets template (-> copy to .env)
  .streamlit/
    secrets.toml.example # dashboard secrets template (DB URL + password)
  .claude/launch.json    # local dashboard launch config
  bbxray/
    utils.py             # curl_cffi session w/ anti-bot-wall handling
    db.py                # SQLAlchemy schema, SQLite-or-Postgres, insert helpers
    scrape_prices.py     # sitemap -> PDP microdata -> pricing
    scrape_stores.py     # /stores-all -> full roster + geocode
    ingest_dewey.py      # Dewey patterns -> foot_traffic
  data/
    bootbarn.sqlite      # local snapshots (created on first run; gitignored)
```

## Avoiding bot-wall blocks (one machine)

Blocks are driven by **volume + speed**, not by using a single machine, so for a
weekly capped run you shouldn't need anything fancy. In order of effort:

1. **Stay gentle (free, default).** `BBXRAY_DELAY=1.0` + `BBXRAY_JITTER=0.5`
   randomizes the gap between requests (fixed cadence looks robotic), and the
   session auto warms-up, rotates Chrome fingerprints, and backs off on a
   challenge. This got 119/120 in testing.
2. **Cap the catalog.** `BBXRAY_MAX_PRODUCTS` limits each run; products are
   stride-sampled across the whole catalog, so even a cap gives representative
   coverage. Don't crawl all ~30k weekly unless you actually need it.
3. **Rotating residential proxy (paid, only if needed).** If you scale to the
   full catalog and one IP starts getting blocked, set `BBXRAY_PROXY` to a
   rotating residential gateway (Bright Data, IPRoyal, Decodo…) — every request
   then exits from a different home IP. This is the real "multiple IPs from one
   device" answer. One gateway URL is enough; wiring is already in place.

> VPNs and Tor usually make things **worse** here — their exit ranges are
> datacenter/flagged and PerimeterX blocks them harder than your home IP.

## Known limitations

- **Sale prices** are frequently hidden by MAP policy; where hidden you get list price only (`map_hidden=1`).
- **Category** is keyword-derived from product names (no category in static HTML), so it's coarse.
- **Openings/closures** require ≥2 snapshots over time — one run can't show change.
- **Foot traffic** quality depends entirely on the Dewey dataset you license.

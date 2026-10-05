# Local Leads

Find high-street independents near any town — garages, florists, butchers,
tattoo shops, and more — with owner name, phone, email, and Companies House
number where available, then export to CSV.

Discovery uses **Google Maps via Apify** (live open/closed status), then
website contact crawl, then Companies House for legal owners and company
health. Franchises, retail parks, and head offices are filtered out.

## For the person using this (non-technical setup)

**Requirements:**

- **Python 3** — Windows/Mac: https://www.python.org/downloads/  
  On Windows, tick **"Add Python to PATH"** on the first install screen.
- **Apify API token** — required for Google Maps discovery. Put it in
  `backend/.env` as `APIFY_API_TOKEN=...` (see `backend/.env.example`).
- **Companies House API key** (optional) — for owner / company-number
  fallback. Put it in `backend/.env` as `COMPANIES_HOUSE_API_KEY=...`.

**Every time you want to use the tool:**

1. Unzip this folder anywhere (Desktop is fine).
2. Double-click:
   - **Windows:** `start_windows.bat`
   - **Mac/Linux:** `start_mac_linux.sh` (right-click → Open if macOS warns
     about an unidentified developer)
3. A browser opens at `http://127.0.0.1:8000`.
4. Enter a town/city, set **search radius** and **target lead count**, pick
   business types (or use “Also match by name”), click **Generate Leads**.
5. Watch progress and the live remaining ETA. When finished (or if you **Stop** /
   hit an error after some rows exist), use the **four dial-window downloads**
   (09:00–11:30, 11:30–13:00, 13:00–15:00, 15:00–17:00) — each lead goes in
   only one window (earliest they are open), or **All leads**. Browse the full table with search, filters, and pages.

**Business types (built in):** Garages, Fabric shops, Fish and chips,
Florists, Tattoo shops, Jewellery, Butchers, Tanning shops, Wool and craft.

**Target lead count** is best-effort — Google Maps may return fewer open
independents than you ask for, or up to about **30 more** when a dense batch
comes back. For larger targets the app widens radius,
runs satellite area crawls, and (if enabled) a surrounding county pass.

The first run can take a few minutes (Python packages + a one-time
~150–300MB headless browser for JS-heavy sites). Later starts are faster.

To stop the app, close the terminal window that opened with the browser.

## What's inside (for whoever maintains this)

```
local-leads/
├── start_windows.bat / start_mac_linux.sh
├── backend/
│   ├── app.py              <- FastAPI: jobs, estimate, geocode, CSV download
│   ├── jobs.py             <- discovery loop + enrich + job JSON persist
│   ├── apify_maps.py       <- Google Maps via Apify (phrases, bbox, geo)
│   ├── geocode.py          <- Nominatim (lat/lon, bbox, parent county)
│   ├── contact_enricher.py <- owner / phone / email from websites
│   ├── companies_house.py  <- PSC/director + company number + office
│   ├── chain_filter.py     <- chains, head offices, retail parks
│   ├── opening_hours.py    <- parse Maps hours + dial-window matching
│   ├── estimate.py         <- rough ETA for the UI
│   ├── config.py           <- categories, synonyms, limits, API keys
│   ├── overpass_search.py  <- legacy OSM helper (not used for discovery)
│   ├── outputs/            <- CSVs + jobs/*.json snapshots
│   └── static/             <- built frontend (served in one-click mode)
└── frontend/               <- React + TypeScript + Tailwind source
    └── src/
        ├── App.tsx
        └── components/     <- LeadDetailPanel, LocationMap, LeadPinMap, ui/
```

The frontend is **already built** into `backend/static/` for one-click users.
After changing `frontend/src`, rebuild:

```bash
cd frontend
npm install
npm run build
```

### Dev: frontend + backend in two terminals

```bash
# terminal 1 — backend (exclude outputs so CSV/job writes don't restart mid-job)
cd backend
uvicorn app:app --host 127.0.0.1 --port 8000 --reload \
  --reload-exclude "outputs/*" --reload-exclude "__pycache__/*" --reload-exclude ".venv/*"

# terminal 2 — frontend (proxies /api to :8000)
cd frontend
npm run dev
```

Open `http://127.0.0.1:5173`.

# Run the application with DOCKER

If you want to run with docker, You must install docker software and follow the step.

- Frontend
  - cd frontend
  - npm install
  - npm run build

- backend
  - docker compose up -d --build

Note : If you have anything changes in frontend, you need to build again,

- npm install
- npm build

## How discovery works

1. **Geocode** the place (Nominatim) — centre, bounding box, optional parent
   county/region.
2. **Google Maps (Apify)** — related search phrases per niche, place footprint
   / radius, widen if short of target, satellite offsets, then optional
   surrounding-county pass. Closed places skipped.
3. **Quality filters** — national franchises, head-office / HQ names, and
   retail-park / shopping-centre addresses are dropped (high-street focus).
4. **Website enrich** — owner, phone, email where the site exposes them.
5. **Companies House** — live companies only; company number + registered
   office; website owner kept when found.
6. **CSV + UI** — opening hours from Maps; four dial-window CSVs plus All leads;
   full paginated/filterable table and detail panel. Partial downloads after
   Stop or mid-run error.

Job status is snapshotted under `backend/outputs/jobs/` so a reload does not
leave the UI spinning forever.

## Architecture notes / scaling

Sized for **one person locally**:

- Jobs live in memory and on disk (`outputs/jobs/{id}.json`). Not multi-tenant.
- Enrichment uses a thread pool (`ENRICHMENT_WORKERS` in `config.py`).
- For a hosted multi-user service: persist jobs in a DB, use a real queue
  (Celery/RQ), put uvicorn behind a reverse proxy with TLS.

## Data & compliance

- Listings come from Google Maps (via Apify) and each business’s public site.
- Companies House Public Data API is used when a key is configured.
- Owner detection is heuristic (About/Contact pages + CH PSC/directors) — it
  leaves fields blank rather than inventing a name.
- Unsolicited commercial contact (UK PECR/GDPR, etc.) depends on your use
  case — check what applies before outreach. This tool only finds contacts;
  it does not send messages.

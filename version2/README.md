# Village Pond Planning System — Setup & Run Guide

This zip is the **complete, correct, up-to-date** state of the project as of Step 5
(village search + rainfall + terrain/contours). Ignore any older zips you downloaded
earlier — use only this one.

## What's included so far — THIS IS THE COMPLETE CORE APPLICATION
- Village search (Open-Meteo geocoding)
- Historical rainfall stats (Open-Meteo archive)
- Terrain analysis: slope + contour lines from real SRTM elevation data (OpenTopography)
- Catchment delineation: click any point and see the watershed area draining to it,
  using a D8 flow-direction/accumulation algorithm we implemented ourselves in NumPy
- Runoff estimation using the SCS Curve Number method, applied per-day across the
  historical rainfall record for realism (not a naive annual-total shortcut)
- Pond sizing recommendation: depth, surface area, and achievable storage capacity
  using the standard trapezoidal-prism earthwork volume formula, constrained by
  available site area and an honest sufficiency check (flags when the site is too
  small for the target capture fraction, rather than silently under-delivering)
- ONE combined endpoint (`/api/pond/recommend`) that chains all of the above into a
  single "Site Summary" — this matches the assignment's overlay requirement (Section 8)
- A Leaflet map frontend showing everything together: catchment polygon overlay,
  rainfall stats, runoff volume, and recommended pond dimensions/capacity, all
  updating live when you click a site

## IMPORTANT: always run uvicorn like this
Some machines have a stray global `uvicorn` install that ignores your venv and causes
`ModuleNotFoundError: No module named 'app'` or similar. Always run it as:
```bash
python -m uvicorn app.main:app --reload --port 8000
```
NOT just `uvicorn app.main:app ...`. This forces it to use your venv's Python.

## Why we wrote our own D8 algorithm instead of using a watershed library
We initially tried `pysheds`, but it depends on an old NumPy function
(`np.in1d`) that was removed in NumPy 2.0 — and NumPy 2.0 is required by
`rasterio`/`opencv` (our other dependencies), creating an unresolvable version
conflict. Rather than pin everything to old, unmaintained versions, we implemented
D8 flow direction, flow accumulation, and catchment delineation ourselves in
`app/services/catchment_engine.py`. This is also better for the assignment's
"understand every component" requirement — every line of the watershed algorithm
is ours to explain, not a black-box library call. See the docstrings in that file
for the algorithm details (priority-flood depression filling, D8 steepest-descent
flow direction, topological flow accumulation, reverse-BFS catchment tracing).

## One-time setup

### 1. Get your free OpenTopography API key (skip if you already have one)
- Sign up at https://portal.opentopography.org/newUser
- Verify your email, log in
- Generate a key at https://portal.opentopography.org/myopentopo

### 2. Backend setup
Open a terminal **inside this folder's `backend/` directory**:

```bash
cd backend
cp .env.example .env
```

Now open `backend/.env` in your editor and replace `your_key_here` with your real
OpenTopography key. Save the file.

```bash
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

If `pip install` fails on `rasterio` or `scikit-image`, tell your instructor/Claude
the exact error — these sometimes need system-level GDAL, especially on Windows.

### 3. Run the backend
Still inside `backend/`, with the venv active (prompt shows `(venv)`):

```bash
python -m uvicorn app.main:app --reload --port 8000
```
(Always use `python -m uvicorn ...`, not bare `uvicorn` — see note above.)

Leave this terminal running. Confirm it works by opening in a browser:
- http://127.0.0.1:8000/api/health  → should show `{"status":"ok",...}`
- http://127.0.0.1:8000/docs → interactive API documentation

### 4. Run the frontend
Open a **second, separate terminal tab** (don't close the backend one).
This time, go to the **project root** (the folder containing both `backend/` and
`frontend/`), NOT into backend:

```bash
cd /path/to/this/unzipped/folder
python3 -m http.server 5500
```

### 5. Open the app
In your browser:
```
http://127.0.0.1:5500/frontend/
```
(Note the `/frontend/` at the end — this matters because we served from the project
root, not from inside the frontend folder.)

## How to verify everything works
1. Type a village name (e.g. `Nagpur`) in the search box, press Search → map recenters,
   a pin appears, and the "Show Contours" button becomes clickable.
2. Click "Show Contours" → wait 10-20 seconds (downloading + processing real elevation
   data) → brown contour lines should appear on the map, and the status text should show
   elevation range, slope, and suitable-land percentage.
3. Click anywhere on the map (after searching a village) → wait 15-40 seconds (this
   now runs the FULL pipeline: DEM fetch, catchment delineation, rainfall history,
   SCS-CN runoff estimation, and pond sizing) → the sidebar shows a complete Site
   Summary (rainfall, catchment area, curve number, runoff volume, recommended pond
   depth/area/capacity) and a blue catchment boundary appears on the map.

## Why the numbers might look extreme sometimes
The default land cover assumption is "cultivated_land" (curve number 78) and the
default available site area is 20,000 m² — both are placeholder defaults. If you
click a point with a huge catchment (e.g. near a real river valley) and a small
assumed site area, you'll correctly see "site not sufficient for target" — this is
the tool being honest about a real engineering constraint, not a bug. You can adjust
`land_cover` and `available_site_area_m2` as query parameters on `/api/pond/recommend`
(see the Swagger docs at /docs) once you're ready to make these adjustable in the UI.

## Folder structure
```
backend/
  app/
    main.py              -- FastAPI entrypoint, wires all routers together
    config.py             -- loads .env (API keys)
    routers/
      village.py           -- GET /api/village/search
      rainfall.py           -- GET /api/rainfall
      terrain.py             -- GET /api/terrain/analyze
      catchment.py             -- GET /api/catchment/delineate
      pond.py                    -- GET /api/pond/recommend (full pipeline, chains everything)
    services/
      geocode_client.py       -- calls Open-Meteo geocoding API
      rainfall_client.py       -- calls Open-Meteo historical weather API
      elevation_client.py       -- calls OpenTopography, caches DEM GeoTIFFs
      terrain_engine.py          -- slope + contour math (rasterio/numpy/skimage)
      catchment_engine.py         -- D8 flow direction/accumulation/catchment (our own impl)
      runoff_engine.py             -- SCS Curve Number runoff estimation
      pond_sizing_engine.py         -- trapezoidal volume + depth/area recommendation
  requirements.txt
  .env.example            -- copy to .env and fill in your API key
frontend/
  index.html
  css/style.css
  js/api.js                -- talks to the backend
  js/map.js                 -- Leaflet map logic
```

## What's next (not yet built — nice-to-haves for the final Sep 5 submission)
- Land-use/government-vacant-land overlay (OpenStreetMap Overpass API) to auto-suggest
  candidate sites, rather than requiring the user to click manually
- PostgreSQL/PostGIS persistence + caching layer (currently DEM is cached to disk,
  but rainfall/catchment results are recomputed each time)
- Adjustable land-cover and site-area inputs in the UI (currently only via API params)
- Multi-village comparison and a proper technical report

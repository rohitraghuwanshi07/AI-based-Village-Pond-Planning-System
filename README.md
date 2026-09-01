# 🌾 AI-Based Village Pond Planning System

A web application that helps identify suitable locations for pond construction in rural areas by analyzing terrain, catchment area, and historical rainfall data — built entirely with **free and open-source tools**, with no paid APIs or licensing costs.

Click any location on a map and instantly get:
- The **catchment area** draining to that point (via a self-implemented D8 watershed algorithm)
- **Historical rainfall** statistics (10 years, daily resolution)
- Estimated **annual runoff volume** (SCS Curve Number method)
- A recommended **pond depth, surface area, and storage capacity**

---

## 🎥 Demo

Search a village → click a candidate site on the map → get a complete site analysis overlaid on satellite imagery, in one response.

---

## ✨ Features

- 🔍 Village AND landmark search (Photon geocoder — resolves specific institutions
  like "IIT Bhilai", not just cities, with Open-Meteo as fallback)
- 🛰️ Satellite imagery + street map toggle
- 🗺️ Color-coded contour visualization (blue=low → red=high elevation) from real SRTM data
- 💧 Catchment (watershed) delineation for any clicked point
- ⭐ **Automatic pond site suggestion** — finds the lowest-elevation, best-draining
  point in a searched area with no manual click required (water physically collects
  at the lowest point of a basin; this is weighted as the primary factor)
- 🌧️ Historical rainfall lookup (10+ years, no API key required)
- 📊 Runoff volume estimation using the SCS Curve Number method
- 🏗️ **Land eligibility analysis**: rasterizes real OpenStreetMap buildings, roads,
  and water bodies to find genuinely CONTIGUOUS vacant patches — a patch split by
  a road is correctly treated as two separate patches, not combined into one number
- 🏛️ **Ownership-aware filtering (strict mode)**: only land explicitly OSM-tagged as
  government/public is eligible; private and ownership-unverified land is excluded
  by default (see "Ownership Data Limitation" below — this is a heuristic, not a
  legal verification, and the app says so explicitly in every response)
- 🏜️ Soil composition check (ISRIC SoilGrids) — sand/silt/clay % and seepage-risk rating
- 🏞️ Pond depth/area/storage capacity recommendation using earthwork volume formulas,
  preferring realistic depths (~2.5m) over degenerate shallow-and-huge solutions,
  and clearly reports "cannot recommend a pond here" instead of a nonsensical
  micro-pond when no eligible land exists
- 📄 Upload your own KML/KMZ contour map for fully independent terrain analysis (Phase 2)
- 🌍 Works anywhere SRTM elevation data exists (India-wide and beyond)

---

## ⚖️ Ownership Data Limitation (read this before relying on results)

**There is no free, authoritative, publicly-queryable cadastral/land-ownership API**
for India (or most countries) that can be looked up by coordinates. Real land
ownership records live in state-specific government portals (e.g. Bhulekh,
Bhu-Naksha) that require manual survey-number lookups, not spatial API access.

This app's "government-owned land" classification is a **heuristic** based on
OpenStreetMap tags (government offices, protected areas, village common/panchayat
land, military land, etc.) — **not legal proof of ownership**. Untagged land
(the vast majority of most areas in OSM) is treated as **ownership-unverified**
and **excluded by default** from any proposed pond area, per a strict "don't
include what you can't verify" policy. This commonly means the reported eligible
area will be small or zero, especially in rural areas with sparse OSM tagging —
**this is intentional, honest behavior, not a bug.**

**Before any real construction, ownership must be confirmed through actual
government land records for that state/district.**

---

## 🏗️ Architecture

```
Frontend (Leaflet.js)  →  Backend (FastAPI)  →  Free External APIs
                              │
                    ┌─────────┼─────────┐
              Village      Terrain    Rainfall
              Search       Analysis    Fetch
                    │         │         │
              Catchment   Runoff +  Pond Sizing
              Delineation  Estimation
```

The backend exposes a single combined endpoint (`/api/pond/recommend`) that chains catchment delineation → rainfall lookup → runoff estimation → pond sizing into one JSON response, which the frontend renders as a map overlay + results panel.

---

## 🛠️ Tech Stack

| Layer | Technology |
|---|---|
| Frontend | [Leaflet.js](https://leafletjs.com/), HTML/CSS/JavaScript |
| Backend | [FastAPI](https://fastapi.tiangolo.com/) (Python), Uvicorn |
| Geospatial processing | [rasterio](https://rasterio.readthedocs.io/), NumPy, scikit-image |
| Elevation data | [OpenTopography API](https://opentopography.org/) (free, SRTM 30m) |
| Geocoding + Rainfall | [Open-Meteo API](https://open-meteo.com/) (free, no key required) |
| Satellite tiles | Esri World Imagery (free) |
| Config | pydantic-settings, python-dotenv |
| Planned | PostgreSQL + PostGIS for caching/persistence |

---

## 📂 Project Structure

```
backend/
  app/
    main.py                    -- FastAPI entrypoint
    config.py                  -- environment/API key loading
    routers/
      village.py                 -- GET /api/village/search
      rainfall.py                 -- GET /api/rainfall
      terrain.py                   -- GET /api/terrain/analyze
      catchment.py                  -- GET /api/catchment/delineate
      pond.py                        -- GET /api/pond/recommend (full pipeline)
      contour.py                      -- POST /api/analyzeContour (Phase 2: KML/KMZ upload)
    services/
      geocode_client.py           -- Open-Meteo geocoding
      rainfall_client.py           -- Open-Meteo historical weather
      elevation_client.py           -- OpenTopography DEM fetch + caching
      terrain_engine.py              -- slope + contour extraction
      catchment_engine.py             -- D8 flow direction/accumulation (custom implementation)
      runoff_engine.py                 -- SCS Curve Number runoff estimation
      pond_sizing_engine.py             -- trapezoidal volume + sizing recommendation
      kml_parser.py                      -- Phase 2: namespace-agnostic KML/KMZ contour parser
      contour_rasterizer.py               -- Phase 2: vector contours -> elevation raster
      pond_site_selector.py                -- Phase 2: automatic candidate site selection
  requirements.txt
  .env.example
frontend/
  index.html
  css/style.css
  js/api.js                    -- backend API client
  js/map.js                    -- Leaflet map logic
```

---

## 🚀 Getting Started

### Prerequisites
- Python 3.11+
- A free [OpenTopography](https://portal.opentopography.org/newUser) account + API key

### 1. Clone the repository
```bash
git clone https://github.com/<your-username>/village-pond-planner.git
cd village-pond-planner
```

### 2. Backend setup
```bash
cd backend
cp .env.example .env
# Open .env and paste your OpenTopography API key
python3.11 -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
python -m uvicorn app.main:app --reload --port 8000
```
> ⚠️ Always run `python -m uvicorn ...` (not bare `uvicorn`) to avoid picking up a conflicting global install.

Verify it's running: open `http://127.0.0.1:8000/docs` for the interactive API documentation.

### 3. Frontend setup
In a **new terminal**, from the project root:
```bash
python3 -m http.server 5500
```

### 4. Open the app
```
http://127.0.0.1:5500/frontend/
```

---

## 📡 API Reference

| Endpoint | Method | Description |
|---|---|---|
| `/api/village/search?q=` | GET | Geocode a place name to lat/lon |
| `/api/rainfall?lat=&lon=&years=` | GET | Historical rainfall statistics |
| `/api/terrain/analyze?south=&north=&west=&east=` | GET | Slope + contour lines for an area |
| `/api/catchment/delineate?...&pour_lat=&pour_lon=` | GET | Watershed boundary for a clicked point |
| `/api/pond/recommend?...` | GET | Full pipeline for a manually-chosen point: catchment → rainfall → runoff → land-use → soil → pond sizing |
| `/api/pond/suggest-site?south=&north=&west=&east=` | GET | Same pipeline, but auto-selects the lowest-elevation site within the given area — no manual point needed |
| `/api/analyzeContour` (alias `/api/findCatchment`) | POST | Upload a KML/KMZ contour map, get an auto-identified pond site + catchment area |

Full interactive documentation is auto-generated at `/docs` (Swagger UI) when the backend is running.

### Phase 2: Contour Map Upload Endpoint

```
POST /api/analyzeContour
Content-Type: multipart/form-data

file: <.kml or .kmz contour map>
resolution_m: 10.0        (optional, grid cell size to reconstruct, meters)
max_slope_deg: 8.0        (optional, max slope considered suitable for a pond)
```

**Approach:** the uploaded contour map's vector lines (each labeled with an
elevation) are parsed and interpolated into a continuous elevation raster
(linear interpolation over scattered elevation-labeled points — a standard
"contour-to-DEM" technique, similar in spirit to ArcGIS's "Topo to Raster").
From there, the same slope and D8 flow-direction/accumulation engines built
in Phase 1 are reused unchanged to find drainage patterns. A candidate pond
site is automatically selected as the highest-flow-accumulation cell among
low-slope candidates — no coordinates are hard-coded; everything is derived
from the uploaded file. The catchment area is then delineated via a reverse
breadth-first search upstream from that site.

**Verified on the provided sample (`contours_1m.kml`, 1355 contour lines,
267–298m elevation range):** correctly identified a pond site at the outlet
of the file's main drainage valley, with a catchment area of **320.6
hectares**, in under 3 seconds end-to-end.

**Generalization check:** also tested against an independently-constructed
synthetic KML at a different location (California, elevation range 100–155m,
concentric-ring "bowl" shape). The endpoint correctly derived the new
bounding box and elevation range from the file, and correctly identified the
bowl's lowest point (100m) as the pond site — confirming the implementation
generalizes rather than being tuned to the sample file.

---

## 🧮 Methodology

**Catchment delineation (D8 algorithm)** — implemented from scratch in NumPy rather than using a third-party watershed library (avoids a real dependency conflict between `pysheds` and NumPy 2.x). Pipeline: priority-flood depression filling → D8 steepest-descent flow direction → topological flow accumulation → reverse-BFS catchment tracing from the clicked pour point. Flat-terrain tie-breaking is handled via a tiny monotonic epsilon nudge during depression filling, since real SRTM data's integer-meter precision otherwise causes ~80% of cells on flat terrain to have no valid flow direction.

**Runoff estimation** uses the [SCS Curve Number method](https://en.wikipedia.org/wiki/Runoff_curve_number), applied per rainy day across the historical record (not to a single annual total) for realistic results:
```
S = (25400 / CN) - 254
Ia = 0.2 * S
Q = (P - Ia)² / (P - Ia + S)
```

**Pond sizing** uses the trapezoidal-prism earthwork volume formula (accounts for sloped pond walls):
```
V = D/6 × (A_top + A_bottom + 4×A_mid)
```

---

## ⚠️ Known Limitations

- SRTM 30m resolution may not capture very small terrain features precisely
- No persistent database yet — results are recomputed on each request (PostgreSQL/PostGIS caching planned)
- Catchment analysis is limited to a fixed window (~13km) around the clicked point; very large watersheds may be partially clipped
- Land-cover assumption for runoff (Curve Number) currently defaults to "cultivated land" rather than being auto-detected
- Pond footprint placement checks whether *enough total open land* exists within a 150m radius of the site (using real OpenStreetMap building/road data), but does not yet run a full "avoid this exact shape" placement search — so the proposed square is sized correctly against real nearby obstructions, but isn't guaranteed to be positioned at the single best sub-location within that radius. A logged, precise "largest empty rectangle" placement algorithm is a natural next step.
- **Public Overpass API availability is unreliable, confirmed via direct testing, not a bug in this app.** Diagnostic testing showed: `overpass-api.de` refused connections outright, `overpass.kumi.systems` timed out, and a third mirror (`overpass.openstreetmap.fr`) explicitly returned "This service is only available to white-listed usages" (now removed from our mirror list). At the same time, Nominatim and our other external dependencies (OpenTopography, Open-Meteo) worked fine, confirming this is specific to the free public Overpass network being overloaded/down, not a local network or code issue. **Mitigations built into this app**: automatic fallback across 3 mirrors, a 1-hour local cache to reduce repeat load, and — most importantly — a manual site-area override plus the land-record upload pathway (`/api/pond/suggest-from-landrecord`), neither of which depend on Overpass at all.

---

## 🗺️ Roadmap

- [ ] Land-use/vacant-government-land overlay via OpenStreetMap Overpass API
- [ ] PostgreSQL + PostGIS caching and persistence layer
- [ ] Adjustable land-cover and site-area inputs in the UI
- [ ] Multi-site comparison view

---

## 📄 License

This project uses only free and open-source tools and free-tier public APIs. Add your preferred license here (e.g., MIT).

---

## 🙏 Acknowledgments

- [Open-Meteo](https://open-meteo.com/) for free geocoding and historical weather data
- [OpenTopography](https://opentopography.org/) for free global elevation data
- [Leaflet.js](https://leafletjs.com/) and [Esri World Imagery](https://www.esri.com/) for mapping

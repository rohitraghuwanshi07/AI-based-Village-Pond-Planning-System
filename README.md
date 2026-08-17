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

- 🛰️ Satellite imagery + street map toggle
- 🗺️ Contour line visualization from real SRTM elevation data
- 💧 Catchment (watershed) delineation for any clicked point
- 🌧️ Historical rainfall lookup (10+ years, no API key required)
- 📊 Runoff volume estimation using the SCS Curve Number method
- 🏞️ Pond depth/area/storage capacity recommendation using earthwork volume formulas
- 🌍 Works anywhere SRTM elevation data exists (India-wide and beyond)

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
    services/
      geocode_client.py           -- Open-Meteo geocoding
      rainfall_client.py           -- Open-Meteo historical weather
      elevation_client.py           -- OpenTopography DEM fetch + caching
      terrain_engine.py              -- slope + contour extraction
      catchment_engine.py             -- D8 flow direction/accumulation (custom implementation)
      runoff_engine.py                 -- SCS Curve Number runoff estimation
      pond_sizing_engine.py             -- trapezoidal volume + sizing recommendation
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
| `/api/pond/recommend?...` | GET | Full pipeline: catchment → rainfall → runoff → pond sizing |

Full interactive documentation is auto-generated at `/docs` (Swagger UI) when the backend is running.

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

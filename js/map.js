// ---- Initialize the Leaflet map ----
// Default view: zoomed out to show all of India, so you can click anywhere
// without needing to search a village first.
const map = L.map("map").setView([22.9734, 78.6569], 5);

// Street layer (free, OpenStreetMap tiles)
const streetLayer = L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  attribution: "&copy; OpenStreetMap contributors",
  maxZoom: 19,
});

// Satellite layer (free, Esri World Imagery tiles, no API key required)
const satelliteLayer = L.tileLayer(
  "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
  {
    attribution: "Tiles &copy; Esri &mdash; Source: Esri, Maxar, Earthstar Geographics",
    maxZoom: 19,
  }
);

streetLayer.addTo(map);
let currentLayer = "street";

let villageMarker = null;
let siteMarker = null;
let contourLayer = null;
let catchmentLayer = null;
let pondFootprintLayer = null;
let vacantLandLayer = null;
let lastVillageBbox = null;

// ---- Layer toggle buttons ----
document.getElementById("layer-street").addEventListener("click", () => {
  if (currentLayer === "street") return;
  map.removeLayer(satelliteLayer);
  streetLayer.addTo(map);
  currentLayer = "street";
  setActiveLayerButton("layer-street");
});

document.getElementById("layer-satellite").addEventListener("click", () => {
  if (currentLayer === "satellite") return;
  map.removeLayer(streetLayer);
  satelliteLayer.addTo(map);
  currentLayer = "satellite";
  setActiveLayerButton("layer-satellite");
});

function setActiveLayerButton(activeId) {
  document.querySelectorAll(".layer-btn").forEach((btn) => btn.classList.remove("active"));
  document.getElementById(activeId).classList.add("active");
}

// ---- Village search ----
const searchInput = document.getElementById("village-search");
const searchBtn = document.getElementById("search-btn");
const searchStatus = document.getElementById("search-status");

async function handleSearch() {
  const query = searchInput.value.trim();
  if (!query) return;

  searchStatus.textContent = "Searching...";
  searchStatus.classList.remove("error");

  try {
    const result = await searchVillage(query);

    if (villageMarker) map.removeLayer(villageMarker);
    villageMarker = L.marker([result.lat, result.lon])
      .addTo(map)
      .bindPopup(`<strong>${result.name}</strong>`)
      .openPopup();

    map.setView([result.lat, result.lon], 13);
    searchStatus.textContent = `Found: ${result.name}`;
    lastVillageBbox = result.bbox;
    document.getElementById("contours-btn").disabled = false;
    document.getElementById("suggest-site-btn").disabled = false;
  } catch (err) {
    searchStatus.textContent = err.message;
    searchStatus.classList.add("error");
  }
}

searchBtn.addEventListener("click", handleSearch);
searchInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") handleSearch();
});

// ---- Click-to-select a candidate pond site ----
const resultsPanel = document.getElementById("results-panel");
const resultsContent = document.getElementById("results-content");

// Build an analysis bounding box CENTERED ON THE CLICKED POINT, not the
// searched village. This is what makes the tool work anywhere -- India-wide,
// or globally wherever SRTM elevation data exists -- instead of only within
// a fixed radius of whatever village name was searched.
//
// Trade-off: a bigger box catches bigger watersheds but means a bigger DEM
// download + slower catchment computation. 0.06 degrees (~13km box, ~6.5km
// radius) is a reasonable default for small-to-medium village catchments.
const ANALYSIS_HALF_SIZE_DEG = 0.06;

function bboxAroundPoint(lat, lon, halfSize = ANALYSIS_HALF_SIZE_DEG) {
  return {
    south: lat - halfSize,
    north: lat + halfSize,
    west: lon - halfSize,
    east: lon + halfSize,
  };
}

map.on("click", async (e) => {
  const { lat, lng } = e.latlng;
  await analyzeAndRender(lat, lng, /*isAutoSuggested=*/ false);
});

// Shared rendering used by both a manual map click and the auto "Suggest Best Site" button.
async function analyzeAndRender(lat, lng, isAutoSuggested, autoSelectedInfo = null) {
  if (siteMarker) map.removeLayer(siteMarker);
  siteMarker = L.marker([lat, lng], {
    icon: L.divIcon({ className: "site-marker", html: isAutoSuggested ? "⭐" : "📍", iconSize: [24, 24] }),
  }).addTo(map);

  resultsPanel.classList.remove("hidden");
  resultsContent.innerHTML = `<p class="hint">Analyzing catchment, rainfall, runoff, soil, and pond sizing... (this can take 15-40s)</p>`;

  try {
    const { south, north, west, east } = bboxAroundPoint(lat, lng);
    const rec = await getPondRecommendation(south, north, west, east, lat, lng);
    renderSiteSummary(rec, lat, lng, autoSelectedInfo);
  } catch (err) {
    resultsContent.innerHTML = `<p class="status-text error">${err.message}</p>`;
  }
}

function renderSiteSummary(rec, lat, lng, autoSelectedInfo) {
  if (catchmentLayer) map.removeLayer(catchmentLayer);
  catchmentLayer = L.geoJSON(rec.catchment.boundary_geojson, {
    style: { color: "#2c5a3d", weight: 2, fillColor: "#4a90d9", fillOpacity: 0.25 },
  }).addTo(map);

  const p = rec.pond_recommendation;

  // Draw the REAL usable vacant land boundary (buildings/roads/water already
  // excluded, and separate patches split by a road are shown as separate
  // shapes, not merged) -- this is the actual buildable area, distinct from
  // both the huge catchment polygon and the small proposed pond square.
  if (vacantLandLayer) map.removeLayer(vacantLandLayer);
  if (rec.vacant_land_boundary_geojson) {
    vacantLandLayer = L.geoJSON(rec.vacant_land_boundary_geojson, {
      style: { color: "#8a5a2b", weight: 2, fillColor: "#e8d5a8", fillOpacity: 0.35, dashArray: "6,4" },
    }).addTo(map).bindPopup("Usable vacant land (buildings/roads/water excluded)");
  }

  if (pondFootprintLayer) map.removeLayer(pondFootprintLayer);
  const sideMeters = Math.sqrt(p.recommended_surface_area_m2);
  const halfSideDegLat = (sideMeters / 2) / 111320;
  const halfSideDegLon = (sideMeters / 2) / (111320 * Math.cos(lat * Math.PI / 180));
  pondFootprintLayer = L.rectangle(
    [[lat - halfSideDegLat, lng - halfSideDegLon], [lat + halfSideDegLat, lng + halfSideDegLon]],
    { color: "#d9534f", weight: 2, fillColor: "#d9534f", fillOpacity: 0.4 }
  ).addTo(map).bindPopup(`Proposed pond footprint: ${sideMeters.toFixed(0)}m × ${sideMeters.toFixed(0)}m`);

  const sufficiencyNote = p.site_area_sufficient_for_target
    ? `<span style="color:#2c5a3d;">✓ Site is large enough for the ${(p.target_capture_fraction*100).toFixed(0)}% capture target</span>`
    : `<span style="color:#b3413a;">⚠ Site can only capture ${p.percent_of_annual_runoff_captured}% of target — consider a larger site or a second pond</span>`;

  const catchmentClippedNote = rec.catchment.area_hectares > (2 * ANALYSIS_HALF_SIZE_DEG * 111) * (2 * ANALYSIS_HALF_SIZE_DEG * 111) * 0.9
    ? `<p class="hint" style="color:#b3841a;">Note: this catchment may extend beyond our ${(ANALYSIS_HALF_SIZE_DEG*2*111).toFixed(0)}km analysis window and could be larger than shown.</p>`
    : "";

  const sc = rec.site_check;
  const siteCheckNote = sc && sc.total_separate_patches_found !== undefined
    ? `<p class="hint">Found ${sc.total_separate_patches_found} separate open patch(es) nearby (${sc.buildings_found_nearby} building(s), ${sc.roads_found_nearby} road(s), ${sc.water_bodies_found_nearby || 0} water body/ies excluded). Using the ${sc.available_area_m2.toLocaleString()} m² patch closest to this site.</p>`
    : sc && sc.note
    ? `<p class="hint" style="color:#b3413a;">⚠ ${sc.note}</p>`
    : "";

  const soil = rec.soil_check;
  const soilNote = soil && soil.query_succeeded
    ? `<div class="result-row"><span class="label">Soil (sand/silt/clay)</span><span class="value">${soil.sand_pct}% / ${soil.silt_pct}% / ${soil.clay_pct}%</span></div>
       <div class="result-row"><span class="label">Seepage risk</span><span class="value">${soil.seepage_risk}</span></div>`
    : soil
    ? `<p class="hint" style="color:#b3413a;">⚠ ${soil.note}</p>`
    : "";

  const autoNote = autoSelectedInfo
    ? `<p class="hint" style="color:#2c5a3d;">⭐ Auto-suggested lowest-elevation site (elevation ${autoSelectedInfo.elevation_at_site_m}m, lower than ${autoSelectedInfo.elevation_percentile_among_candidates}% of nearby candidates).</p>`
    : "";

  resultsContent.innerHTML = `
    ${autoNote}
    <div class="result-row"><span class="label">Location</span><span class="value">${lat.toFixed(4)}, ${lng.toFixed(4)}</span></div>
    <div class="result-row"><span class="label">Avg annual rainfall</span><span class="value">${rec.rainfall.annual_average_mm} mm</span></div>
    <div class="result-row"><span class="label">Catchment area</span><span class="value">${rec.catchment.area_hectares} ha</span></div>
    <div class="result-row"><span class="label">Curve number (land cover)</span><span class="value">${rec.runoff.curve_number_used} (${rec.runoff.land_cover_assumed})</span></div>
    <div class="result-row"><span class="label">Avg annual runoff</span><span class="value">${rec.runoff.avg_annual_runoff_volume_m3.toLocaleString()} m³</span></div>
    <div class="result-row"><span class="label">Recommended depth</span><span class="value">${p.recommended_depth_m} m</span></div>
    <div class="result-row"><span class="label">Recommended surface area</span><span class="value">${p.recommended_surface_area_m2.toLocaleString()} m²</span></div>
    <div class="result-row"><span class="label">Storage capacity</span><span class="value">${p.achievable_storage_capacity_m3.toLocaleString()} m³</span></div>
    ${soilNote}
    <p class="hint" style="margin-top:12px;">${sufficiencyNote}</p>
    ${siteCheckNote}
    ${catchmentClippedNote}
  `;
}

// ---- Auto-suggest the best (lowest-elevation) pond site within the searched village ----
document.getElementById("suggest-site-btn").addEventListener("click", async () => {
  if (!lastVillageBbox) return;
  const btn = document.getElementById("suggest-site-btn");
  const originalText = btn.textContent;
  btn.textContent = "Finding best site...";
  btn.disabled = true;

  resultsPanel.classList.remove("hidden");
  resultsContent.innerHTML = `<p class="hint">Fetching elevation data and finding the lowest-elevation drainage point... (this can take 20-50s)</p>`;

  try {
    const { south, north, west, east } = lastVillageBbox;
    const rec = await suggestPondSite(south, north, west, east);
    const { lat, lon } = rec.location;
    if (siteMarker) map.removeLayer(siteMarker);
    siteMarker = L.marker([lat, lon], {
      icon: L.divIcon({ className: "site-marker", html: "⭐", iconSize: [24, 24] }),
    }).addTo(map);
    map.setView([lat, lon], 14);
    renderSiteSummary(rec, lat, lon, rec.auto_selected);
  } catch (err) {
    resultsContent.innerHTML = `<p class="status-text error">${err.message}</p>`;
  } finally {
    btn.textContent = originalText;
    btn.disabled = false;
  }
});

// Maps a value from 0-1 through a blue -> yellow -> orange -> red gradient
// (low elevation to high elevation), like a standard terrain color ramp.
function elevationColorRamp(t) {
  // Stops: blue, yellow, orange, red
  const stops = [
    { t: 0.0, color: [33, 102, 172] },   // blue
    { t: 0.4, color: [255, 255, 51] },   // yellow
    { t: 0.7, color: [255, 140, 0] },    // orange
    { t: 1.0, color: [214, 39, 40] },    // red
  ];
  t = Math.max(0, Math.min(1, t));

  for (let i = 0; i < stops.length - 1; i++) {
    const a = stops[i], b = stops[i + 1];
    if (t >= a.t && t <= b.t) {
      const localT = (t - a.t) / (b.t - a.t);
      const r = Math.round(a.color[0] + (b.color[0] - a.color[0]) * localT);
      const g = Math.round(a.color[1] + (b.color[1] - a.color[1]) * localT);
      const bl = Math.round(a.color[2] + (b.color[2] - a.color[2]) * localT);
      return `rgb(${r},${g},${bl})`;
    }
  }
  return `rgb(${stops[stops.length - 1].color.join(",")})`;
}

// ---- Load contours for the currently searched village ----
document.getElementById("contours-btn").addEventListener("click", async () => {
  if (!lastVillageBbox) return;
  const btn = document.getElementById("contours-btn");
  const originalText = btn.textContent;
  btn.textContent = "Loading contours...";
  btn.disabled = true;

  try {
    const { south, north, west, east } = lastVillageBbox;
    const terrain = await getTerrain(south, north, west, east);

    const elevMin = terrain.elevation_min_m;
    const elevMax = terrain.elevation_max_m;
    const elevRange = Math.max(elevMax - elevMin, 0.001); // avoid divide-by-zero on flat terrain

    if (contourLayer) map.removeLayer(contourLayer);
    contourLayer = L.geoJSON(terrain.contours, {
      style: (feature) => {
        const elev = feature.properties.elevation_m;
        const t = (elev - elevMin) / elevRange;
        return { color: elevationColorRamp(t), weight: 1.5, opacity: 0.85 };
      },
      onEachFeature: (feature, layer) => {
        layer.bindPopup(`Elevation: ${feature.properties.elevation_m} m`);
      },
    }).addTo(map);

    document.getElementById("contour-legend").classList.remove("hidden");

    searchStatus.textContent =
      `Terrain: ${terrain.elevation_min_m}-${terrain.elevation_max_m}m, ` +
      `avg slope ${terrain.mean_slope_deg}°, ${terrain.percent_suitable_land}% suitable land`;
  } catch (err) {
    searchStatus.textContent = `Contour load failed: ${err.message}`;
    searchStatus.classList.add("error");
  } finally {
    btn.textContent = originalText;
    btn.disabled = false;
  }
});

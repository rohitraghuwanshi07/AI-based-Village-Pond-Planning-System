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

  if (siteMarker) map.removeLayer(siteMarker);
  siteMarker = L.marker([lat, lng], {
    icon: L.divIcon({ className: "site-marker", html: "📍", iconSize: [24, 24] }),
  }).addTo(map);

  resultsPanel.classList.remove("hidden");
  resultsContent.innerHTML = `<p class="hint">Analyzing catchment, rainfall, runoff, and pond sizing... (this can take 15-40s)</p>`;

  try {
    const { south, north, west, east } = bboxAroundPoint(lat, lng);
    const rec = await getPondRecommendation(south, north, west, east, lat, lng);

    if (catchmentLayer) map.removeLayer(catchmentLayer);
    catchmentLayer = L.geoJSON(rec.catchment.boundary_geojson, {
      style: { color: "#2c5a3d", weight: 2, fillColor: "#4a90d9", fillOpacity: 0.25 },
    }).addTo(map);

    const p = rec.pond_recommendation;

    // Draw the actual proposed pond footprint (a small square centered on the
    // click) so you can visually see, on satellite imagery, whether it lands
    // on open land or overlaps something -- this is the piece that was
    // missing before, making the mismatch invisible until you looked closely.
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
    const siteCheckNote = sc && sc.note
      ? `<p class="hint" style="color:#b3413a;">⚠ ${sc.note}</p>`
      : sc
      ? `<p class="hint">Checked ${sc.buildings_found_nearby} building(s) and ${sc.roads_found_nearby} road(s) within ${sc.search_radius_m}m — ${sc.percent_of_search_area_open}% of nearby land is open (OpenStreetMap data).</p>`
      : "";

    resultsContent.innerHTML = `
      <div class="result-row"><span class="label">Location</span><span class="value">${lat.toFixed(4)}, ${lng.toFixed(4)}</span></div>
      <div class="result-row"><span class="label">Avg annual rainfall</span><span class="value">${rec.rainfall.annual_average_mm} mm</span></div>
      <div class="result-row"><span class="label">Catchment area</span><span class="value">${rec.catchment.area_hectares} ha</span></div>
      <div class="result-row"><span class="label">Curve number (land cover)</span><span class="value">${rec.runoff.curve_number_used} (${rec.runoff.land_cover_assumed})</span></div>
      <div class="result-row"><span class="label">Avg annual runoff</span><span class="value">${rec.runoff.avg_annual_runoff_volume_m3.toLocaleString()} m³</span></div>
      <div class="result-row"><span class="label">Recommended depth</span><span class="value">${p.recommended_depth_m} m</span></div>
      <div class="result-row"><span class="label">Recommended surface area</span><span class="value">${p.recommended_surface_area_m2.toLocaleString()} m²</span></div>
      <div class="result-row"><span class="label">Storage capacity</span><span class="value">${p.achievable_storage_capacity_m3.toLocaleString()} m³</span></div>
      <p class="hint" style="margin-top:12px;">${sufficiencyNote}</p>
      ${siteCheckNote}
      ${catchmentClippedNote}
    `;
  } catch (err) {
    resultsContent.innerHTML = `<p class="status-text error">${err.message}</p>`;
  }
});

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

    if (contourLayer) map.removeLayer(contourLayer);
    contourLayer = L.geoJSON(terrain.contours, {
      style: { color: "#8a5a2b", weight: 1, opacity: 0.7 },
    }).addTo(map);

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

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
let ownershipGovLayer = null;
let ownershipPrivateLayer = null;
let ownershipUnverifiedLayer = null;
let ownershipEligibleLayer = null;
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
  if (rec.catchment && rec.catchment.boundary_geojson) {
    catchmentLayer = L.geoJSON(rec.catchment.boundary_geojson, {
      style: { color: "#2c5a3d", weight: 2, fillColor: "#4a90d9", fillOpacity: 0.25 },
    }).addTo(map);
  }

  const p = rec.pond_recommendation || {};

  // Draw ALL ownership/exclusion layers, per the strict eligibility pipeline:
  // government-owned (green), private (red hatch, excluded), unverified
  // (gray, excluded by default), and the final eligible patch (gold).
  [ownershipGovLayer, ownershipPrivateLayer, ownershipUnverifiedLayer, ownershipEligibleLayer].forEach(l => {
    if (l) map.removeLayer(l);
  });
  ownershipGovLayer = ownershipPrivateLayer = ownershipUnverifiedLayer = ownershipEligibleLayer = null;

  const ol = rec.ownership_layers;
  const isFromLandRecord = rec.site_check && rec.site_check.source === "user-uploaded land record (not OSM heuristic)";
  const govPopupText = isFromLandRecord
    ? "Government-owned land (from your uploaded land record)"
    : "Government/public land (OSM-tag heuristic, not verified cadastral record)";
  if (ol) {
    if (ol.unverified_ownership) {
      ownershipUnverifiedLayer = L.geoJSON(ol.unverified_ownership, {
        style: { color: "#888888", weight: 1, fillColor: "#cccccc", fillOpacity: 0.25, dashArray: "3,3" },
      }).addTo(map).bindPopup("Ownership unverified — excluded by default");
    }
    if (ol.private) {
      ownershipPrivateLayer = L.geoJSON(ol.private, {
        style: { color: "#b3413a", weight: 1, fillColor: "#e08c85", fillOpacity: 0.3 },
      }).addTo(map).bindPopup("Private land — excluded");
    }
    if (ol.government_owned) {
      ownershipGovLayer = L.geoJSON(ol.government_owned, {
        style: { color: "#2c5a3d", weight: 1.5, fillColor: "#a8d5b0", fillOpacity: 0.25 },
      }).addTo(map).bindPopup(govPopupText);
    }
    if (ol.final_eligible) {
      ownershipEligibleLayer = L.geoJSON(ol.final_eligible, {
        style: { color: "#c9962c", weight: 2.5, fillColor: "#f0d264", fillOpacity: 0.45 },
      }).addTo(map).bindPopup("Final eligible land: government-owned, vacant, unoccupied");
    }
  }

  // Also draw the specific selected patch boundary (subset of the eligible layer)
  if (vacantLandLayer) map.removeLayer(vacantLandLayer);
  if (rec.vacant_land_boundary_geojson) {
    vacantLandLayer = L.geoJSON(rec.vacant_land_boundary_geojson, {
      style: { color: "#8a5a2b", weight: 2, fillColor: "#e8d5a8", fillOpacity: 0.35, dashArray: "6,4" },
    }).addTo(map).bindPopup("Selected eligible patch (closest to this site)");
  }

  if (pondFootprintLayer) map.removeLayer(pondFootprintLayer);
  if (p.recommended_surface_area_m2) {
    const sideMeters = Math.sqrt(p.recommended_surface_area_m2);
    const halfSideDegLat = (sideMeters / 2) / 111320;
    const halfSideDegLon = (sideMeters / 2) / (111320 * Math.cos(lat * Math.PI / 180));
    pondFootprintLayer = L.rectangle(
      [[lat - halfSideDegLat, lng - halfSideDegLon], [lat + halfSideDegLat, lng + halfSideDegLon]],
      { color: "#d9534f", weight: 2, fillColor: "#d9534f", fillOpacity: 0.4 }
    ).addTo(map).bindPopup(`Proposed pond footprint: ${sideMeters.toFixed(0)}m × ${sideMeters.toFixed(0)}m`);
  }

  const sufficiencyNote = p.cannot_recommend
    ? `<span style="color:#b3413a;">✗ ${p.reason}</span>`
    : p.site_area_sufficient_for_target
    ? `<span style="color:#2c5a3d;">✓ Site is large enough for the ${(p.target_capture_fraction*100).toFixed(0)}% capture target</span>`
    : `<span style="color:#b3413a;">⚠ Site can only capture ${p.percent_of_annual_runoff_captured}% of target — consider a larger site or a second pond</span>`;

  const catchmentClippedNote = rec.catchment && rec.catchment.area_hectares > (2 * ANALYSIS_HALF_SIZE_DEG * 111) * (2 * ANALYSIS_HALF_SIZE_DEG * 111) * 0.9
    ? `<p class="hint" style="color:#b3841a;">Note: this catchment may extend beyond our ${(ANALYSIS_HALF_SIZE_DEG*2*111).toFixed(0)}km analysis window and could be larger than shown.</p>`
    : "";

  const sc = rec.site_check;
  let siteCheckNote = "";
  if (sc && sc.area_breakdown) {
    const b = sc.area_breakdown;
    siteCheckNote = `
      <div class="result-row"><span class="label">Government-owned land nearby</span><span class="value">${b.government_owned_area_m2.toLocaleString()} m²</span></div>
      <div class="result-row"><span class="label">— occupied by development</span><span class="value">-${b.government_area_occupied_by_development_m2.toLocaleString()} m²</span></div>
      <div class="result-row"><span class="label">Private land (excluded)</span><span class="value">${b.private_area_m2.toLocaleString()} m²</span></div>
      <div class="result-row"><span class="label">Ownership unverified (excluded)</span><span class="value">${b.unverified_ownership_area_m2.toLocaleString()} m²</span></div>
      <div class="result-row"><span class="label"><strong>Final eligible area</strong></span><span class="value"><strong>${b.final_eligible_vacant_government_area_m2.toLocaleString()} m²</strong></span></div>
      <p class="hint" style="margin-top:6px;">${sc.limitation}</p>
    `;
  } else if (sc && sc.note) {
    siteCheckNote = `<p class="hint" style="color:#b3413a;">⚠ ${sc.note}</p>`;
  }

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

  const depthDisplay = p.recommended_depth_m !== null && p.recommended_depth_m !== undefined ? `${p.recommended_depth_m} m` : "—";
  const areaDisplay = p.recommended_surface_area_m2 !== null && p.recommended_surface_area_m2 !== undefined ? `${p.recommended_surface_area_m2.toLocaleString()} m²` : "—";
  const capacityDisplay = p.achievable_storage_capacity_m3 !== null && p.achievable_storage_capacity_m3 !== undefined ? `${p.achievable_storage_capacity_m3.toLocaleString()} m³` : "—";

  const rainfallRow = rec.rainfall
    ? `<div class="result-row"><span class="label">Avg annual rainfall</span><span class="value">${rec.rainfall.annual_average_mm} mm</span></div>`
    : "";
  const catchmentRow = rec.catchment
    ? `<div class="result-row"><span class="label">Catchment area</span><span class="value">${rec.catchment.area_hectares} ha</span></div>`
    : "";
  const runoffRows = rec.runoff
    ? `<div class="result-row"><span class="label">Curve number (land cover)</span><span class="value">${rec.runoff.curve_number_used} (${rec.runoff.land_cover_assumed})</span></div>
       <div class="result-row"><span class="label">Avg annual runoff</span><span class="value">${rec.runoff.avg_annual_runoff_volume_m3.toLocaleString()} m³</span></div>`
    : "";

  resultsContent.innerHTML = `
    ${autoNote}
    ${lat !== null ? `<div class="result-row"><span class="label">Location</span><span class="value">${lat.toFixed(4)}, ${lng.toFixed(4)}</span></div>` : ""}
    ${rainfallRow}
    ${catchmentRow}
    ${runoffRows}
    <div class="result-row"><span class="label">Recommended depth</span><span class="value">${depthDisplay}</span></div>
    <div class="result-row"><span class="label">Recommended surface area</span><span class="value">${areaDisplay}</span></div>
    <div class="result-row"><span class="label">Storage capacity</span><span class="value">${capacityDisplay}</span></div>
    ${soilNote}
    <p class="hint" style="margin-top:12px;">${sufficiencyNote}</p>
    <h3 style="margin:14px 0 6px 0; font-size:0.9rem;">Land eligibility (ownership + land-use)</h3>
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

// ---- Enable the land record button once a file is chosen ----
document.getElementById("landrecord-file").addEventListener("change", (e) => {
  document.getElementById("landrecord-btn").disabled = !e.target.files.length;
});

// ---- Find best pond site from an uploaded land record (real ownership data,
// no dependency on OSM/Overpass at all) ----
document.getElementById("landrecord-btn").addEventListener("click", async () => {
  const fileInput = document.getElementById("landrecord-file");
  const file = fileInput.files[0];
  if (!file) return;

  const btn = document.getElementById("landrecord-btn");
  const originalText = btn.textContent;
  btn.textContent = "Analyzing land record...";
  btn.disabled = true;

  resultsPanel.classList.remove("hidden");
  resultsContent.innerHTML = `<p class="hint">Parsing land record, classifying parcels, and finding the best eligible site... (this can take 20-60s)</p>`;

  try {
    const rec = await suggestFromLandRecord(file);

    if (rec.pond_recommendation && rec.pond_recommendation.cannot_recommend && !rec.location) {
      // No eligible government land found anywhere in the record at all --
      // still show the ownership layers (so the user sees WHY), but there's
      // no site/catchment/pond to render.
      renderSiteSummary(rec, null, null, null);
      const counts = rec.land_record_summary.classification_counts;
      const countsText = Object.entries(counts).map(([k, v]) => `${k}: ${v}`).join(", ");
      resultsContent.innerHTML =
        `<p class="status-text error">✗ ${rec.pond_recommendation.reason}</p>` +
        `<p class="hint">Parcels found: ${countsText}</p>`;
      return;
    }

    const { lat, lon } = rec.location;
    if (siteMarker) map.removeLayer(siteMarker);
    siteMarker = L.marker([lat, lon], {
      icon: L.divIcon({ className: "site-marker", html: "📄", iconSize: [24, 24] }),
    }).addTo(map);
    map.setView([lat, lon], 15);
    renderSiteSummary(rec, lat, lon, rec.auto_selected);

    // Prepend a note about the land record source, since this result is
    // grounded in real uploaded data rather than an OSM heuristic.
    const counts = rec.land_record_summary.classification_counts;
    const countsText = Object.entries(counts).map(([k, v]) => `${k}: ${v}`).join(", ");
    resultsContent.innerHTML =
      `<p class="hint" style="color:#2c5a3d;">📄 From uploaded land record "${rec.land_record_summary.filename}" (${rec.land_record_summary.parcels_parsed} parcels: ${countsText})</p>` +
      resultsContent.innerHTML;
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

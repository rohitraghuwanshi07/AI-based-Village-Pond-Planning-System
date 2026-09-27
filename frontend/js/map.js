// ---- Initialize the Leaflet map ----
// Default view: zoomed out to show all of India, so you can click anywhere
// without needing to search a village first.
const map = L.map("map").setView([22.9734, 78.6569], 5);
// Ensure Leaflet knows the container size after CSS loads
setTimeout(() => map.invalidateSize(), 500);

// Default view: zoomed out to show all of India, so you can click anywhere
// without needing to search a village first.

// Street layer.
// NOT served from tile.openstreetmap.org. That server's own tile usage
// policy (operations.osmfoundation.org/policies/tiles/) explicitly states:
// "Heavy use (e.g. distributing an app that uses tiles from
// openstreetmap.org) is forbidden without prior permission" -- which is
// exactly what this app does once it's shared with more than one person.
// OSMF enforces that by silently rate-limiting or blocking the offending
// app's requests (a 403 "Referer is required", or just dropped/throttled
// tiles) with NO error surfaced to the app, which is precisely what
// "the map is sometimes fully blank/grey, sometimes has white patches"
// looks like: full block = every tile fails = grey; partial
// throttling = only some tiles fail = white patches where they should be.
// It's not a bug in this code, it's this code hitting a server it was
// never allowed to hit at app-distribution volume.
//
// Esri's World_Street_Map is used instead: same free, no-API-key,
// no-signup ArcGIS REST tile service this app already relies on for the
// satellite layer below (so it's already proven to work reliably here),
// and Esri's terms permit exactly this kind of embedded, no-key use.
const streetLayer = L.tileLayer(
  "https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}",
  {
    attribution: "Tiles &copy; Esri &mdash; Source: Esri, HERE, Garmin, OpenStreetMap contributors",
    maxZoom: 19,
    maxNativeZoom: 19,
  }
);

// Satellite layer (free, Esri World Imagery tiles, no API key required).
// maxNativeZoom is set below maxZoom on purpose: Esri's free imagery has
// genuinely no high-resolution coverage at all for a lot of rural areas
// (exactly where this app is used) -- requesting tiles past whatever zoom
// IS actually available for a given spot returns blank/white tiles, which
// looks like a bug but is a real data gap in the free tileset. Capping
// maxNativeZoom makes Leaflet re-use (and upscale) the best real imagery it
// has instead of requesting tiles that don't exist for that location.
const satelliteLayer = L.tileLayer(
  "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
  {
    attribution: "Tiles &copy; Esri &mdash; Source: Esri, Maxar, Earthstar Geographics",
    maxZoom: 19,
    maxNativeZoom: 17,
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
    document.getElementById("suggest-top-btn").disabled = false;
    
    // Clear any previous selection when searching new village
    cancelBoundarySelection();
  } catch (err) {
    searchStatus.textContent = err.message;
    searchStatus.classList.add("error");
  }
}

searchBtn.addEventListener("click", handleSearch);
searchInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") handleSearch();
});

// ---- Boundary Selection Logic (4 Corners) ----
const resultsPanel = document.getElementById("results-panel");
const resultsContent = document.getElementById("results-content");
const selectBoundaryBtn = document.getElementById("select-boundary-btn");
const selectionHint = document.getElementById("selection-hint");

// #map is a flex sibling of #results-panel (flex: 1, so it fills whatever
// width is left over) -- opening the results panel for the first time
// changes #results-panel from display:none to visible, which shrinks #map's
// actual on-screen width. Leaflet caches the pixel size of its container at
// init time and has NO way to know the DOM just resized around it unless
// told explicitly, so every "show the results panel" call used to leave
// Leaflet's internal tile grid sized for the OLD (usually wider) container --
// which is exactly what produces random blank/white gaps where it thinks
// tiles already cover the area but the actual visible map doesn't, along
// with panning/zooming looking subtly "glitched" relative to reality. This
// helper is the single place that opens the panel, so the fix (recalculating
// right after) can't be missed by a future call site the way the 4 separate
// `resultsPanel.classList.remove("hidden")` calls it replaces could be.
function showResultsPanel() {
  resultsPanel.classList.remove("hidden");
  // setTimeout(0) queues this for right after the browser finishes the
  // layout reflow the classList change just triggered -- calling
  // invalidateSize() synchronously would still see the OLD size.
  setTimeout(() => map.invalidateSize(), 0);
}

let isSelectingBoundary = false;
let boundaryCorners = [];
let cornerMarkers = [];
let boundaryPolygon = null;
let lastRankedSites = null;  // cached ranked-sites list, so "View Full Analysis" can return to it without re-running the search

// Monotonically-increasing token identifying the "current" in-flight result
// render. There are 3 independent places that can update the results
// panel/map (whole-village "Suggest Top 3", boundary-based "Select 4
// Corners", and single-site "View Full Analysis") -- each is an async
// fetch, so if you start one and then start ANOTHER before the first
// finishes, whichever network response happened to arrive LAST used to win
// and overwrite the screen, regardless of which action you'd actually
// moved on to. That's what caused "I started drawing a boundary and it
// snapped back to the old top-3 results" -- a stale, already-abandoned
// fetch finishing late and clobbering the newer flow's state. Every flow
// below grabs a fresh token before it starts and checks it's still current
// before touching the DOM; a flow whose token has been superseded quietly
// discards its result instead of rendering it.
let currentRequestId = 0;

selectBoundaryBtn.addEventListener("click", () => {
  isSelectingBoundary = !isSelectingBoundary;
  if (isSelectingBoundary) {
    startBoundarySelection();
  } else {
    cancelBoundarySelection();
  }
});

function startBoundarySelection() {
  cancelBoundarySelection(); // Clear any previous selection
  currentRequestId++;  // invalidate any in-flight "Suggest Top 3" (whole-village) fetch immediately
  isSelectingBoundary = true;
  selectBoundaryBtn.textContent = "Cancel Selection";
  selectBoundaryBtn.classList.add("selecting");
  selectionHint.textContent = "Click corner #1 on the map...";
  map.getContainer().style.cursor = "crosshair";
}

function cancelBoundarySelection() {
  isSelectingBoundary = false;
  boundaryCorners = [];
  cornerMarkers.forEach(m => map.removeLayer(m));
  cornerMarkers = [];
  if (boundaryPolygon) map.removeLayer(boundaryPolygon);
  boundaryPolygon = null;
  selectBoundaryBtn.textContent = "📍 Select Boundary (4 Corners)";
  selectBoundaryBtn.classList.remove("selecting");
  selectionHint.textContent = "Click the button above then select 4 corners on the map to define your search area.";
  map.getContainer().style.cursor = "";
}

// Sort points by angle around their centroid, so any 4 clicks describing a
// roughly convex quadrilateral -- regardless of the order they were clicked
// in -- form a valid, simple (non-self-intersecting) polygon.
function orderCornersByAngle(corners) {
  const cLat = corners.reduce((s, c) => s + c[0], 0) / corners.length;
  const cLng = corners.reduce((s, c) => s + c[1], 0) / corners.length;
  return [...corners].sort((a, b) => {
    const angleA = Math.atan2(a[0] - cLat, a[1] - cLng);
    const angleB = Math.atan2(b[0] - cLat, b[1] - cLng);
    return angleA - angleB;
  });
}

map.on("click", async (e) => {
  const { lat, lng } = e.latlng;

  if (!isSelectingBoundary) {
    // Plain click anywhere on the map — no village search or boundary
    // selection needed. Runs the exact same full analysis as clicking a
    // ranked-site card, just for whatever point the user picked directly.
    await analyzeAndRender(lat, lng, false);
    return;
  }

  boundaryCorners.push([lat, lng]);

  const marker = L.marker([lat, lng], {
    icon: L.divIcon({ 
      className: "corner-marker", 
      html: boundaryCorners.length, 
      iconSize: [20, 20],
      iconAnchor: [10, 10],  // center the icon on the actual clicked point -- without this
                             // Leaflet anchors at the icon's top-left corner, so the number
                             // visibly drifts away from the real corner as you zoom
    })
  }).addTo(map);
  cornerMarkers.push(marker);

  if (boundaryCorners.length < 4) {
    selectionHint.textContent = `Click corner #${boundaryCorners.length + 1} on the map...`;
  } else {
    // 4 corners selected!
    isSelectingBoundary = false;
    selectBoundaryBtn.classList.remove("selecting");
    selectBoundaryBtn.textContent = "📍 Select Boundary (4 Corners)";
    selectionHint.textContent = "Analyzing area...";
    map.getContainer().style.cursor = "";

    // Order the 4 clicked points around their centroid (by angle) before
    // building a polygon from them. Without this, clicking corners in
    // anything other than strict perimeter order (e.g. the two diagonals
    // first, which is an easy, natural way to click "the 4 corners of an
    // area") produces a self-intersecting "bowtie" polygon -- which has
    // near-ZERO real area even though it looks roughly like the intended
    // shape on screen. That degenerate shape is what actually got sent to
    // the backend, so the ranked sites came back from almost none of the
    // area the user thought they'd selected -- looking like wrong/random
    // site placement. Sorting by angle guarantees a valid, simple polygon
    // for any convex-ish quadrilateral, regardless of click order.
    const orderedCorners = orderCornersByAngle(boundaryCorners);

    // Draw the selection polygon (using the corrected order)
    boundaryPolygon = L.polygon(orderedCorners, { color: "#d9534f", weight: 2, fillOpacity: 0.1 }).addTo(map);

    // Calculate bbox for API
    const lats = orderedCorners.map(c => c[0]);
    const lngs = orderedCorners.map(c => c[1]);
    const south = Math.min(...lats);
    const north = Math.max(...lats);
    const west = Math.min(...lngs);
    const east = Math.max(...lngs);

    const myRequestId = ++currentRequestId;  // claim this render slot -- see currentRequestId comment above

    showResultsPanel();
    resultsContent.innerHTML = `<p class="hint">Finding top 3 pond sites strictly within your boundary... (very fast)</p>`;
    
    // Clear existing result layers
    if (siteMarker) map.removeLayer(siteMarker);
    topSiteMarkers.forEach(m => map.removeLayer(m));
    topSiteMarkers = [];
    topSiteFootprints.forEach(l => map.removeLayer(l));
    topSiteFootprints = [];
    topSitePondFootprints.forEach(l => map.removeLayer(l));
    topSitePondFootprints = [];
    if (catchmentLayer) map.removeLayer(catchmentLayer);
    [ownershipGovLayer, ownershipPrivateLayer, ownershipUnverifiedLayer, ownershipEligibleLayer].forEach(l => {
      if (l) map.removeLayer(l);
    });
    if (vacantLandLayer) map.removeLayer(vacantLandLayer);
    if (pondFootprintLayer) map.removeLayer(pondFootprintLayer);
    document.getElementById("explain-panel").classList.add("hidden");

    try {
      // Format polygon for API: lon,lat;lon,lat...
      // Close the polygon by repeating the first point
      const polyStr = [...orderedCorners, orderedCorners[0]]
        .map(c => `${c[1]},${c[0]}`)
        .join(";");
        
      const rec = await suggestTopPondSites(south, north, west, east, { boundaryPolygon: polyStr });
      if (myRequestId !== currentRequestId) return;  // a newer action started while this was in flight -- discard
      renderTop5Sites(rec.ranked_sites);
      selectionHint.textContent = "Selection complete. See results below.";
    } catch (err) {
      if (myRequestId !== currentRequestId) return;
      resultsContent.innerHTML = `<p class="status-text error">${err.message}</p>`;
      selectionHint.textContent = "Error during analysis.";
    }
  }
});

// Build an analysis bounding box CENTERED ON THE CLICKED POINT, not the
// searched village. 
const ANALYSIS_HALF_SIZE_DEG = 0.06;

function bboxAroundPoint(lat, lon, halfSize = ANALYSIS_HALF_SIZE_DEG) {
  return {
    south: lat - halfSize,
    north: lat + halfSize,
    west: lon - halfSize,
    east: lon + halfSize,
  };
}

// Shared rendering used by both a manual map click and the auto "Suggest Best Site" button.
async function analyzeAndRender(lat, lng, isAutoSuggested, autoSelectedInfo = null) {
  if (siteMarker) map.removeLayer(siteMarker);

  // If this call is drilling into one specific ranked site (from a rank
  // card's "View Full Analysis" button), keep the other ranked
  // markers/footprints on the map and in memory -- clearing them here was
  // exactly why picking rank #1 used to make ranks #2/#3 vanish, with no
  // way back short of redrawing the whole boundary and re-running the
  // search. Only a genuinely fresh single-point analysis (a plain map
  // click, or "Suggest Best Site") should clear the ranked-site layers.
  const viewingRankedSite = !!(autoSelectedInfo && autoSelectedInfo.rank && lastRankedSites);
  if (!viewingRankedSite && typeof topSiteMarkers !== "undefined") {
    topSiteMarkers.forEach(m => map.removeLayer(m));
    topSiteMarkers = [];
    topSiteFootprints.forEach(l => map.removeLayer(l));
    topSiteFootprints = [];
    topSitePondFootprints.forEach(l => map.removeLayer(l));
    topSitePondFootprints = [];
    lastRankedSites = null;
  }

  const myRequestId = ++currentRequestId;  // claim this render slot -- see currentRequestId comment above

  siteMarker = L.marker([lat, lng], {
    icon: L.divIcon({ className: "site-marker", html: isAutoSuggested ? "⭐" : "📍", iconSize: [24, 24], iconAnchor: [12, 12] }),
  }).addTo(map);

  showResultsPanel();
  const backButtonHtml = viewingRankedSite
    ? `<button id="back-to-ranked-btn" class="layer-btn full-width" style="margin-bottom:12px;">&larr; Back to Top 3 Ranked Sites</button>`
    : "";
  resultsContent.innerHTML = `
    ${backButtonHtml}
    <div class="analysis-status">
      <p class="hint">🚀 Running fast parallel analysis...</p>
      <ul class="status-list">
        <li>📡 Fetching terrain and delineating catchment...</li>
        <li>🌧️ Analyzing historical rainfall (10 years)...</li>
        <li>🌱 Checking soil composition and seepage risk...</li>
        <li>🏢 Identifying buildings, roads, and land ownership...</li>
        <li>📐 Sizing pond for optimal runoff capture...</li>
      </ul>
      <p class="hint" style="margin-top:10px;">Usually takes 5-15 seconds now.</p>
    </div>
  `;
  if (viewingRankedSite) {
    document.getElementById("back-to-ranked-btn").addEventListener("click", () => restoreRankedSitesView());
  }

  try {
    const { south, north, west, east } = bboxAroundPoint(lat, lng);
    const siteAreaInput = document.getElementById("site-area-input").value;
    const rec = await getPondRecommendation(south, north, west, east, lat, lng, { siteAreaM2: siteAreaInput });
    if (myRequestId !== currentRequestId) return;  // a newer action started while this was in flight -- discard
    renderSiteSummary(rec, lat, lng, autoSelectedInfo, viewingRankedSite);
  } catch (err) {
    if (myRequestId !== currentRequestId) return;
    resultsContent.innerHTML = `${backButtonHtml}<p class="status-text error">${err.message}</p>`;
    if (viewingRankedSite) {
      document.getElementById("back-to-ranked-btn").addEventListener("click", () => restoreRankedSitesView());
    }
  }
}

function renderSiteSummary(rec, lat, lng, autoSelectedInfo, viewingRankedSite = false) {
  if (catchmentLayer) map.removeLayer(catchmentLayer);
  if (rec.catchment && rec.catchment.boundary_geojson) {
    catchmentLayer = L.geoJSON(rec.catchment.boundary_geojson, {
      style: { color: "#2c5a3d", weight: 2, fillColor: "#4a90d9", fillOpacity: 0.25 },
    }).addTo(map).bindTooltip(
      `Catchment: ${rec.catchment.area_hectares ? rec.catchment.area_hectares + ' ha' : 'area draining to this site'}`,
      { className: "pond-tooltip", sticky: true }
    );
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
      }).addTo(map)
        .bindTooltip("Ownership unverified — excluded", { className: "pond-tooltip", sticky: true })
        .bindPopup("Ownership unverified — excluded by default");
    }
    if (ol.private) {
      ownershipPrivateLayer = L.geoJSON(ol.private, {
        style: { color: "#b3413a", weight: 1, fillColor: "#e08c85", fillOpacity: 0.3 },
      }).addTo(map)
        .bindTooltip("Private land — excluded", { className: "pond-tooltip", sticky: true })
        .bindPopup("Private land — excluded");
    }
    if (ol.government_owned) {
      ownershipGovLayer = L.geoJSON(ol.government_owned, {
        style: { color: "#2c5a3d", weight: 1.5, fillColor: "#a8d5b0", fillOpacity: 0.25 },
      }).addTo(map)
        .bindTooltip("Government/public land", { className: "pond-tooltip", sticky: true })
        .bindPopup(govPopupText);
    }
    if (ol.final_eligible) {
      ownershipEligibleLayer = L.geoJSON(ol.final_eligible, {
        style: { color: "#c9962c", weight: 2.5, fillColor: "#f0d264", fillOpacity: 0.45 },
      }).addTo(map)
        .bindTooltip("Final eligible land", { className: "pond-tooltip", sticky: true })
        .bindPopup("Final eligible land: government-owned, vacant, unoccupied");
    }
  }

  // Also draw the specific selected patch boundary (subset of the eligible layer)
  if (vacantLandLayer) map.removeLayer(vacantLandLayer);
  if (rec.vacant_land_boundary_geojson) {
    vacantLandLayer = L.geoJSON(rec.vacant_land_boundary_geojson, {
      style: { color: "#8a5a2b", weight: 2, fillColor: "#e8d5a8", fillOpacity: 0.35, dashArray: "6,4" },
    }).addTo(map)
      .bindTooltip("Selected eligible patch", { className: "pond-tooltip", sticky: true })
      .bindPopup("Selected eligible patch (closest to this site)");
  }

  if (pondFootprintLayer) map.removeLayer(pondFootprintLayer);
  if (p.recommended_surface_area_m2) {
    const sideMeters = Math.sqrt(p.recommended_surface_area_m2);
    const halfSideDegLat = (sideMeters / 2) / 111320;
    const halfSideDegLon = (sideMeters / 2) / (111320 * Math.cos(lat * Math.PI / 180));
    pondFootprintLayer = L.rectangle(
      [[lat - halfSideDegLat, lng - halfSideDegLon], [lat + halfSideDegLat, lng + halfSideDegLon]],
      { color: "#d9534f", weight: 2, fillColor: "#d9534f", fillOpacity: 0.4 }
    ).addTo(map)
      .bindTooltip(`Pond: ${sideMeters.toFixed(0)}m × ${sideMeters.toFixed(0)}m`, { className: "pond-tooltip", sticky: true })
      .bindPopup(`Proposed pond footprint: ${sideMeters.toFixed(0)}m × ${sideMeters.toFixed(0)}m`);
  }

  const sufficiencyNote = p.cannot_recommend
    ? `<span style="color:${p.data_unavailable ? '#8a6d1a' : '#b3413a'};">${p.data_unavailable ? '⚠' : '✗'} ${p.reason}</span>`
    : p.site_area_sufficient_for_target
      ? `<span style="color:#2c5a3d;">✓ Site is large enough for the ${(p.target_capture_fraction * 100).toFixed(0)}% capture target</span>`
      : `<span style="color:#b3413a;">⚠ Site can only capture ${p.percent_of_annual_runoff_captured}% of target — consider a larger site or a second pond</span>`;

  const catchmentClippedNote = rec.catchment && rec.catchment.area_hectares > (2 * ANALYSIS_HALF_SIZE_DEG * 111) * (2 * ANALYSIS_HALF_SIZE_DEG * 111) * 0.9
    ? `<p class="hint" style="color:#b3841a;">Note: this catchment may extend beyond our ${(ANALYSIS_HALF_SIZE_DEG * 2 * 111).toFixed(0)}km analysis window and could be larger than shown.</p>`
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
      <p class="hint" style="margin-top:6px;">${sc.limitation || sc.source || ""}</p>
    `;
  } else if (sc && sc.note) {
    siteCheckNote = `<p class="hint" style="color:#b3413a;">⚠ ${sc.note}</p>`;
  } else {
    siteCheckNote = `<p class="hint">Using manually entered site area — live ownership/obstruction check was skipped.</p>`;
  }

  const soil = rec.soil_check;
  const soilNote = soil && soil.query_succeeded
    ? `<div class="result-row"><span class="label">Soil (sand/silt/clay)</span><span class="value">${soil.sand_pct}% / ${soil.silt_pct}% / ${soil.clay_pct}%</span></div>
       <div class="result-row"><span class="label">Seepage risk</span><span class="value">${soil.seepage_risk}</span></div>`
    : soil
      ? `<p class="hint" style="color:#b3413a;">⚠ ${soil.note}</p>`
      : "";

  // Same "why this ranking" explanation and obstacle breakdown shown on the
  // ranked-list cards (renderTop5Sites), rendered here too -- this used to
  // just show a bare one-line "Score: X/100" summary, which is a visible
  // downgrade from the rich card someone just clicked "View Full Analysis"
  // from. rec.manual_score_info is now populated for BOTH a fresh manual
  // click and a ranked site's detail view (see _full_recommendation).
  const msi = rec.manual_score_info;
  const badgeHtml = autoSelectedInfo && autoSelectedInfo.rank
    ? `<div class="rank-badge" style="display:inline-block; margin-bottom:10px;">Rank #${autoSelectedInfo.rank} Site</div>`
    : autoSelectedInfo
      ? "" // auto-suggested-lowest-elevation path has its own note below
      : `<div class="rank-badge" style="display:inline-block; margin-bottom:10px; background-color: #6b7a70;">Manual Selection</div>`;

  const msiObs = msi ? (msi.nearby_obstacles || {}) : {};
  const msiDataUnavailable = !!msiObs.data_unavailable;
  const msiHasObs = (msiObs.buildings_nearby || msiObs.roads_nearby || msiObs.water_bodies_nearby);
  const msiObsText = msiDataUnavailable
    ? `<span style="color:#8a6d1a">Obstacle check unavailable in this hosting environment — using a conservative safety margin</span>`
    : msiHasObs
      ? `<span style="color:#b3413a">Bldgs: ${msiObs.buildings_nearby || 0}, Rds: ${msiObs.roads_nearby || 0}, Water: ${msiObs.water_bodies_nearby || 0}</span>`
      : `<span style="color:#2c5a3d">Clear of obstacles</span>`;

  // Same real-zero-vs-unavailable distinction as the ranked cards: available
  // area of exactly 0 is a real, meaningful result and must say "0 m²", not
  // be confused with "not checked yet".
  const siteAvailableArea = sc && sc.available_area_m2 !== undefined ? sc.available_area_m2 : null;
  const areaDisplayRow = msi
    ? `<div class="result-row"><span class="label">Available Area</span><span class="value">${
        msiDataUnavailable
          ? "Not verified (public map data blocked from this host)"
          : (siteAvailableArea !== null ? siteAvailableArea.toLocaleString() + ' m²' : 'Unknown')
      }</span></div>`
    : "";

  const explanationHtml = msi
    ? `${badgeHtml}
       <div class="rank-explanation">${msi.explanation}</div>
       ${areaDisplayRow}
       <div class="result-row"><span class="label">Obstacles</span><span class="value" style="font-size:0.8rem">${msiObsText}</span></div>`
    : "";

  const autoNote = autoSelectedInfo && !autoSelectedInfo.rank
    ? `<p class="hint" style="color:#2c5a3d;">⭐ Auto-suggested lowest-elevation site (elevation ${autoSelectedInfo.elevation_at_site_m}m, lower than ${autoSelectedInfo.elevation_percentile_among_candidates}% of nearby candidates).</p>${explanationHtml}`
    : explanationHtml;

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

  const backButtonHtml = viewingRankedSite
    ? `<button id="back-to-ranked-btn" class="layer-btn full-width" style="margin-bottom:12px;">&larr; Back to Top 3 Ranked Sites</button>`
    : "";

  resultsContent.innerHTML = `
    ${backButtonHtml}
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
  if (viewingRankedSite) {
    document.getElementById("back-to-ranked-btn").addEventListener("click", () => restoreRankedSitesView());
  }

  renderExplanationPanel(rec);
}

// Returns to the previously-fetched top-3 ranked sites view WITHOUT
// re-running the search: clears the layers specific to the single-site
// detail view (catchment outline, ownership layers, pond footprint for that
// one site, the pin marker), then just re-renders the ranked sites list and
// markers that were kept on the map/in memory all along.
function restoreRankedSitesView() {
  if (!lastRankedSites) return;
  currentRequestId++;  // invalidate any in-flight fetch from the site detail view we're leaving
  if (siteMarker) { map.removeLayer(siteMarker); siteMarker = null; }
  if (catchmentLayer) { map.removeLayer(catchmentLayer); catchmentLayer = null; }
  if (vacantLandLayer) { map.removeLayer(vacantLandLayer); vacantLandLayer = null; }
  if (pondFootprintLayer) { map.removeLayer(pondFootprintLayer); pondFootprintLayer = null; }
  [ownershipGovLayer, ownershipPrivateLayer, ownershipUnverifiedLayer, ownershipEligibleLayer].forEach(l => {
    if (l) map.removeLayer(l);
  });
  ownershipGovLayer = ownershipPrivateLayer = ownershipUnverifiedLayer = ownershipEligibleLayer = null;
  document.getElementById("explain-panel").classList.add("hidden");
  renderTop5Sites(lastRankedSites);
}

// Builds the plain-language "Why this result?" panel on the right, explaining
// WHY the recommended area is the size it is -- what got subtracted and why
// (roads, buildings, water, private land, unverified ownership), rather than
// just restating the numbers already in the Site Summary.
function renderExplanationPanel(rec) {
  const panel = document.getElementById("explain-panel");
  const content = document.getElementById("explain-content");
  const sc = rec.site_check;
  const p = rec.pond_recommendation || {};

  let html = "";

  if (rec.manual_score_info) {
    html += `
      <div class="explain-block">
        <h3>Site Scoring</h3>
        <p>${rec.manual_score_info.explanation}</p>
      </div>
    `;
  }


  // Block 1: catchment context, if available
  if (rec.catchment) {
    html += `
      <div class="explain-block">
        <h3>Catchment</h3>
        <p>This site collects rainfall runoff from <strong>${rec.catchment.area_hectares} hectares</strong> of surrounding land — the terrain naturally drains here, which is why it was chosen (or why you clicked here).</p>
      </div>
    `;
  }

  // Block 2: the actual "why only this much area" breakdown
  if (sc && sc.area_breakdown) {
    const b = sc.area_breakdown;
    const isLandRecord = sc.source === "user-uploaded land record (not OSM heuristic)";
    html += `
      <div class="explain-block">
        <h3>Why the eligible area is limited</h3>
        <p>${isLandRecord
        ? "Your uploaded land record was checked parcel-by-parcel. Only parcels explicitly classified as government/public land count as eligible — everything else is subtracted below."
        : "OpenStreetMap data was checked for real buildings, roads, water bodies, and land tagged as government/public. Only land meeting ALL of these is eligible — everything else is subtracted below."}</p>
        <div class="explain-stat-row"><span>Government-owned land found</span><span class="val">${b.government_owned_area_m2.toLocaleString()} m²</span></div>
        <div class="explain-stat-row subtract"><span>Occupied by buildings/roads/water</span><span class="val">${b.government_area_occupied_by_development_m2.toLocaleString()} m²</span></div>
        <div class="explain-stat-row total"><span>Final eligible land</span><span class="val">${b.final_eligible_vacant_government_area_m2.toLocaleString()} m²</span></div>
        <p style="margin-top:8px;">For reference, nearby land also includes <strong>${b.private_area_m2.toLocaleString()} m²</strong> of private property and <strong>${b.unverified_ownership_area_m2.toLocaleString()} m²</strong> of land whose ownership couldn't be confirmed — both are excluded on principle, since a pond can only be built on land that's actually available for public use.</p>
      </div>
    `;
  } else if (sc && sc.manually_entered_area_m2 !== undefined) {
    html += `
      <div class="explain-block">
        <h3>Why the area was adjusted</h3>
        <p>You entered <strong>${sc.manually_entered_area_m2.toLocaleString()} m²</strong> as an estimate. We checked OpenStreetMap for real buildings, roads, and water bodies within 150m of this exact point.</p>
        ${sc.real_vacant_patch_area_m2 !== undefined ? `
          <div class="explain-stat-row"><span>Your estimate</span><span class="val">${sc.manually_entered_area_m2.toLocaleString()} m²</span></div>
          <div class="explain-stat-row"><span>Real open land found nearby</span><span class="val">${sc.real_vacant_patch_area_m2.toLocaleString()} m²</span></div>
          <div class="explain-stat-row total"><span>Used for sizing (smaller of the two)</span><span class="val">${sc.available_area_m2.toLocaleString()} m²</span></div>
          <p style="margin-top:8px;">${(sc.buildings_found_nearby || 0)} building(s), ${(sc.roads_found_nearby || 0)} road(s), and ${(sc.water_bodies_found_nearby || 0)} water body/ies were found nearby — these are excluded from the buildable area, which is why the number was reduced.</p>
        ` : `<p style="color:#b3413a;">${sc.note}</p>`}
      </div>
    `;
  } else if (sc && sc.note) {
    html += `
      <div class="explain-block">
        <h3>Land check unavailable</h3>
        <p style="color:#b3413a;">${sc.note}</p>
      </div>
    `;
  } else {
    html += `
      <div class="explain-block">
        <h3>No land eligibility check performed</h3>
        <p>This result uses your manually entered area directly, with no automatic check against real buildings, roads, or water bodies. Verify the site on satellite imagery before construction.</p>
      </div>
    `;
  }

  // Block 3: what the sizing means in practice
  if (!p.cannot_recommend && p.recommended_surface_area_m2) {
    const capturedPct = p.percent_of_annual_runoff_captured;
    html += `
      <div class="explain-block">
        <h3>What this means for the pond</h3>
        <p>With <strong>${(sc && sc.available_area_m2 !== undefined ? sc.available_area_m2 : sc && sc.area_breakdown ? sc.area_breakdown.final_eligible_vacant_government_area_m2 : "the available").toLocaleString()} m²</strong> of usable land, the largest practical pond here is <strong>${p.recommended_surface_area_m2.toLocaleString()} m²</strong> at <strong>${p.recommended_depth_m}m</strong> deep — capturing about <strong>${capturedPct}%</strong> of this site's annual runoff.</p>
        ${!p.site_area_sufficient_for_target ? `<p style="color:#b3413a;">This falls short of the ${(p.target_capture_fraction * 100).toFixed(0)}% target because the catchment is large relative to the available land. A larger site, or a second pond elsewhere in the catchment, would capture more.</p>` : ""}
      </div>
    `;
  } else if (p.cannot_recommend) {
    html += `
      <div class="explain-block">
        <h3>Why no pond is recommended</h3>
        <p style="color:#b3413a;">${p.reason || "No eligible land was found at this location."}</p>
      </div>
    `;
  }

  content.innerHTML = html;
  panel.classList.remove("hidden");
}



let topSiteMarkers = [];
let topSiteFootprints = [];
let topSitePondFootprints = [];

// ---- Auto-suggest top 5 pond sites ----
document.getElementById("suggest-top-btn").addEventListener("click", async () => {
  if (!lastVillageBbox) return;
  const btn = document.getElementById("suggest-top-btn");
  const originalText = btn.textContent;
  btn.textContent = "Finding top 3 sites...";
  btn.disabled = true;

  const myRequestId = ++currentRequestId;  // claim this render slot -- see currentRequestId comment above

  showResultsPanel();
  resultsContent.innerHTML = `<p class="hint">Fetching elevation data and ranking best candidate sites... (this can take 10-20s)</p>`;

  // clear existing layers
  if (siteMarker) map.removeLayer(siteMarker);
  topSiteMarkers.forEach(m => map.removeLayer(m));
  topSiteMarkers = [];
  topSiteFootprints.forEach(l => map.removeLayer(l));
  topSiteFootprints = [];
  topSitePondFootprints.forEach(l => map.removeLayer(l));
  topSitePondFootprints = [];

  if (catchmentLayer) map.removeLayer(catchmentLayer);
  [ownershipGovLayer, ownershipPrivateLayer, ownershipUnverifiedLayer, ownershipEligibleLayer].forEach(l => {
    if (l) map.removeLayer(l);
  });
  if (vacantLandLayer) map.removeLayer(vacantLandLayer);
  if (pondFootprintLayer) map.removeLayer(pondFootprintLayer);

  document.getElementById("explain-panel").classList.add("hidden");

  try {
    const { south, north, west, east } = lastVillageBbox;
    const siteAreaInput = document.getElementById("site-area-input").value;
    const rec = await suggestTopPondSites(south, north, west, east, { siteAreaM2: siteAreaInput });

    if (myRequestId !== currentRequestId) return;  // a newer action (e.g. starting a boundary selection) started while this was in flight -- discard
    renderTop5Sites(rec.ranked_sites);
  } catch (err) {
    if (myRequestId !== currentRequestId) return;
    resultsContent.innerHTML = `<p class="status-text error">${err.message}</p>`;
  } finally {
    btn.textContent = originalText;
    btn.disabled = false;
  }
});

function renderTop5Sites(sites) {
  let html = "";

  if (!sites || sites.length === 0) {
    resultsContent.innerHTML = `<p class="status-text error">No suitable sites found.</p>`;
    return;
  }

  lastRankedSites = sites;  // cache so "View Full Analysis" can return here without re-fetching

  const coords = sites.map(s => [s.location.lat, s.location.lon]);

  const RANK_MARKER_COLORS = { 1: "#d4af37", 2: "#9aa0a6", 3: "#b08d57" }; // gold / silver / bronze
  const WELL_EMOJI = "💧";

  sites.forEach((site) => {
    const { lat, lon } = site.location;

    const markerColor = RANK_MARKER_COLORS[site.rank] || "#2c5a3d";
    const iconHtml = `
      <div class="rank-marker" style="background:${markerColor}">
        <span class="rank-marker-emoji">${WELL_EMOJI}</span>
        <span class="rank-marker-num">${site.rank}</span>
      </div>`;
    const marker = L.marker([lat, lon], {
      icon: L.divIcon({ className: "site-marker rank-icon", html: iconHtml, iconSize: [30, 30], iconAnchor: [15, 15] }),
    }).addTo(map);

    marker.bindTooltip(`Rank #${site.rank} (Score: ${site.composite_score})`, { className: "pond-tooltip", sticky: true });
    topSiteMarkers.push(marker);

    if (site.vacant_land_boundary_geojson) {
      const footprint = L.geoJSON(site.vacant_land_boundary_geojson, {
        style: { color: "#8a5a2b", weight: 2, fillColor: "#e8d5a8", fillOpacity: 0.35, dashArray: "6,4" },
      }).addTo(map)
        .bindTooltip(`Rank #${site.rank}: available land (${(site.available_area_m2 !== undefined && site.available_area_m2 !== null) ? site.available_area_m2.toLocaleString() + ' m²' : 'unknown'})`, { className: "pond-tooltip", sticky: true });
      topSiteFootprints.push(footprint);
    }

    // The actual recommended POND size for this site (a sub-boundary inside
    // the available-land patch above), sized from this site's own catchment
    // -- not just how much land happens to be free -- so it's clear how big
    // the pond itself should actually be here.
    const sizing = site.pond_sizing;
    if (sizing && sizing.recommended_surface_area_m2) {
      const sideMeters = Math.sqrt(sizing.recommended_surface_area_m2);
      const halfSideDegLat = (sideMeters / 2) / 111320;
      const halfSideDegLon = (sideMeters / 2) / (111320 * Math.cos(lat * Math.PI / 180));
      const pondColor = RANK_MARKER_COLORS[site.rank] || "#d9534f";
      const pondFootprint = L.rectangle(
        [[lat - halfSideDegLat, lon - halfSideDegLon], [lat + halfSideDegLat, lon + halfSideDegLon]],
        { color: pondColor, weight: 2, fillColor: pondColor, fillOpacity: 0.45 }
      ).addTo(map)
        .bindTooltip(
          `Rank #${site.rank} pond: ${sideMeters.toFixed(0)}m × ${sideMeters.toFixed(0)}m, depth ${sizing.recommended_depth_m}m`,
          { className: "pond-tooltip", sticky: true }
        );
      topSitePondFootprints.push(pondFootprint);
    }

    const obs = site.nearby_obstacles || {};
    const dataUnavailable = !!obs.data_unavailable;
    const hasObs = (obs.buildings_nearby || obs.roads_nearby || obs.water_bodies_nearby);
    const obsText = dataUnavailable
      ? `<span style="color:#8a6d1a">Obstacle check unavailable in this hosting environment — using a conservative safety margin</span>`
      : hasObs
        ? `<span style="color:#b3413a">Bldgs: ${obs.buildings_nearby || 0}, Rds: ${obs.roads_nearby || 0}, Water: ${obs.water_bodies_nearby || 0}</span>`
        : `<span style="color:#2c5a3d">Clear of obstacles</span>`;

    // available_area_m2 can legitimately BE 0 (site really is fully built up) --
    // that's a real, meaningful number and must be shown as "0 m²", not
    // treated the same as "we have no idea" just because 0 is falsy in JS.
    // The only time this should say "Unknown"/"Not verified" is when the
    // live obstruction query actually failed.
    const areaText = dataUnavailable
      ? "Not verified (public map data blocked from this host)"
      : (site.available_area_m2 !== undefined && site.available_area_m2 !== null)
        ? site.available_area_m2.toLocaleString() + ' m²'
        : 'Unknown';

    const sizingRow = (sizing && sizing.recommended_surface_area_m2)
      ? `<div class="result-row"><span class="label">Recommended Pond</span><span class="value">${sizing.recommended_surface_dimensions_m || (Math.sqrt(sizing.recommended_surface_area_m2).toFixed(0) + 'm x ' + Math.sqrt(sizing.recommended_surface_area_m2).toFixed(0) + 'm')}, ${sizing.recommended_depth_m}m deep</span></div>`
      : `<div class="result-row"><span class="label">Recommended Pond</span><span class="value" style="color:${sizing && sizing.data_unavailable ? '#8a6d1a' : '#b3413a'}">${(sizing && sizing.reason) || 'Not enough data to size this site'}</span></div>`;

    html += `
      <div class="rank-card" data-lat="${lat}" data-lon="${lon}" data-rank="${site.rank}">
        <div class="rank-card-header">
          <span class="rank-badge">${WELL_EMOJI} #${site.rank}</span>
          <span class="rank-score">Score: <strong>${site.composite_score.toFixed(1)}</strong>/100</span>
        </div>
        <div class="rank-scores">
          <span>Elev: ${site.scores.elevation_score.toFixed(0)}%</span>
          <span>Drainage: ${site.scores.accumulation_score.toFixed(0)}%</span>
          <span>Flatness: ${site.scores.slope_score.toFixed(0)}%</span>
        </div>
        <div class="rank-explanation">${site.explanation}</div>
        <div class="result-row"><span class="label">Available Area</span><span class="value">${areaText}</span></div>
        ${sizingRow}
        <div class="result-row"><span class="label">Obstacles</span><span class="value" style="font-size:0.8rem">${obsText}</span></div>
        <button class="layer-btn full-width" style="margin-top:10px; font-size:0.8rem;">View Full Analysis & Pond Plan</button>
      </div>
    `;
  });

  map.fitBounds(coords, { padding: [30, 30] });

  resultsContent.innerHTML = `<h3 style="margin-bottom:15px; color:#2c5a3d;">Top Ranked Pond Sites</h3>` + html;

  document.querySelectorAll(".rank-card").forEach(card => {
    card.addEventListener("click", async () => {
      const lat = parseFloat(card.getAttribute("data-lat"));
      const lon = parseFloat(card.getAttribute("data-lon"));
      const rank = card.getAttribute("data-rank");

      // Visual feedback
      document.querySelectorAll(".rank-card").forEach(c => c.classList.remove("active"));
      card.classList.add("active");

      // Detailed analysis for this specific site
      map.setView([lat, lon], 16);
      await analyzeAndRender(lat, lon, true, { rank: rank });
    });
  });
}

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

  const myRequestId = ++currentRequestId;  // claim this render slot -- see currentRequestId comment above

  showResultsPanel();
  resultsContent.innerHTML = `<p class="hint">Parsing land record, classifying parcels, and finding the best eligible site... (this can take 20-60s)</p>`;

  try {
    const rec = await suggestFromLandRecord(file);
    if (myRequestId !== currentRequestId) return;  // a newer action started while this was in flight -- discard

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

    if (typeof topSiteMarkers !== "undefined") {
      topSiteMarkers.forEach(m => map.removeLayer(m));
      topSiteMarkers = [];
      topSiteFootprints.forEach(l => map.removeLayer(l));
      topSiteFootprints = [];
      topSitePondFootprints.forEach(l => map.removeLayer(l));
      topSitePondFootprints = [];
    }

    siteMarker = L.marker([lat, lon], {
      icon: L.divIcon({ className: "site-marker", html: "📄", iconSize: [24, 24], iconAnchor: [12, 12] }),
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
    if (myRequestId !== currentRequestId) return;
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
        layer.bindTooltip(`${feature.properties.elevation_m} m`, { className: "pond-tooltip", sticky: true });
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

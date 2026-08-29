// Small wrapper around our FastAPI backend.
// Change this if your backend runs on a different host/port.
const API_BASE = "http://127.0.0.1:8000";

async function searchVillage(name) {
  const url = `${API_BASE}/api/village/search?q=${encodeURIComponent(name)}`;
  const resp = await fetch(url);
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    throw new Error(body.detail || `Search failed (${resp.status})`);
  }
  return resp.json();
}

async function getRainfall(lat, lon, years = 10) {
  const url = `${API_BASE}/api/rainfall?lat=${lat}&lon=${lon}&years=${years}`;
  const resp = await fetch(url);
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    throw new Error(body.detail || `Rainfall lookup failed (${resp.status})`);
  }
  return resp.json();
}

async function getTerrain(south, north, west, east) {
  const url = `${API_BASE}/api/terrain/analyze?south=${south}&north=${north}&west=${west}&east=${east}`;
  const resp = await fetch(url);
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    throw new Error(body.detail || `Terrain analysis failed (${resp.status})`);
  }
  return resp.json();
}

async function getCatchment(south, north, west, east, pourLat, pourLon) {
  const url =
    `${API_BASE}/api/catchment/delineate?south=${south}&north=${north}` +
    `&west=${west}&east=${east}&pour_lat=${pourLat}&pour_lon=${pourLon}`;
  const resp = await fetch(url);
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    throw new Error(body.detail || `Catchment delineation failed (${resp.status})`);
  }
  return resp.json();
}

async function getPondRecommendation(south, north, west, east, pourLat, pourLon, options = {}) {
  const params = new URLSearchParams({
    south, north, west, east,
    pour_lat: pourLat, pour_lon: pourLon,
    land_cover: options.landCover || "cultivated_land",
    target_capture_fraction: options.targetFraction || 0.5,
  });
  const url = `${API_BASE}/api/pond/recommend?${params.toString()}`;
  const resp = await fetch(url);
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    throw new Error(body.detail || `Pond recommendation failed (${resp.status})`);
  }
  return resp.json();
}

async function suggestPondSite(south, north, west, east, options = {}) {
  const params = new URLSearchParams({
    south, north, west, east,
    land_cover: options.landCover || "cultivated_land",
    target_capture_fraction: options.targetFraction || 0.5,
  });
  const url = `${API_BASE}/api/pond/suggest-site?${params.toString()}`;
  const resp = await fetch(url);
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    throw new Error(body.detail || `Site suggestion failed (${resp.status})`);
  }
  return resp.json();
}

async function suggestFromLandRecord(file, options = {}) {
  const params = new URLSearchParams({
    land_cover: options.landCover || "cultivated_land",
    target_capture_fraction: options.targetFraction || 0.5,
  });
  const formData = new FormData();
  formData.append("file", file);

  const url = `${API_BASE}/api/pond/suggest-from-landrecord?${params.toString()}`;
  const resp = await fetch(url, { method: "POST", body: formData });
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    throw new Error(body.detail || `Land record analysis failed (${resp.status})`);
  }
  return resp.json();
}

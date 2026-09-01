"""
Phase 2 endpoint: accepts an uploaded KML/KMZ contour map, reconstructs a
terrain surface from it, automatically identifies a suitable pond site, and
returns the delineated catchment area.

Pipeline:
    1. Parse contour lines from the uploaded file
    2. Rasterize contour lines into a continuous elevation grid
    3. Compute slope
    4. Compute D8 flow direction + accumulation
    5. Automatically select a candidate pond site
    6. Delineate the catchment for that site
"""

import time
from pathlib import Path
from tempfile import SpooledTemporaryFile

import numpy as np
from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.responses import HTMLResponse
from skimage import measure

from app.services.kml_parser import parse_contour_file
from app.services.contour_rasterizer import rasterize_contours
from app.services.terrain_engine import compute_slope_degrees
from app.services.catchment_engine import (
    delineate_catchment,
    fill_depressions,
    flow_accumulation,
    flow_direction_d8,
)
from app.services.pond_site_selector import select_pond_site
from app.services.rainfall_client import get_historical_rainfall
from app.services.runoff_engine import (
    estimate_daily_series_runoff,
    DEFAULT_CURVE_NUMBER,
    CURVE_NUMBERS,
)
from app.services.land_use_client import fetch_obstructions
from app.services.ownership_client import fetch_ownership_zones
from app.services.site_suitability import find_eligible_patches
from app.services.pond_sizing_engine import recommend_pond
from app.services.soil_client import fetch_soil_composition


router = APIRouter(tags=["contour-analysis"])


def _rowcol_to_latlon(transform, row: int, col: int):
    lon, lat = transform * (col, row)
    return lat, lon


def _build_catchment_geojson(
    catchment_mask: np.ndarray,
    transform,
) -> dict:
    boundary_paths = measure.find_contours(
        catchment_mask.astype(float),
        level=0.5,
    )

    polygons = []

    for path in boundary_paths:
        coords = [
            _rowcol_to_latlon(transform, r, c)
            for r, c in path
        ]

        polygons.append(
            [[lon, lat] for lat, lon in coords]
        )

    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [poly],
                },
            }
            for poly in polygons
            if len(poly) >= 4
        ],
    }


async def _analyze_contour_impl(
    file: UploadFile,
    resolution_m: float,
    max_slope_deg: float,
    include_external_checks: bool = True,
):
    timings = {}
    t_start = time.time()

    # ---------------------------------------------------------
    # 1. READ + PARSE CONTOUR FILE
    # ---------------------------------------------------------
    file_bytes = await file.read()

    if not file_bytes:
        raise HTTPException(
            status_code=400,
            detail="Uploaded file is empty.",
        )

    try:
        parse_result = parse_contour_file(
            file_bytes,
            file.filename or "upload.kml",
        )
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Failed to parse contour file: {e}",
        )

    if not parse_result.lines:
        raise HTTPException(
            status_code=422,
            detail=(
                "No usable contour lines with elevation "
                "values were found in this file."
            ),
        )

    timings["parse_seconds"] = round(
        time.time() - t_start,
        2,
    )

    # ---------------------------------------------------------
    # 2. RASTERIZE CONTOURS
    # ---------------------------------------------------------
    t = time.time()

    try:
        elevation, transform, raster_meta = rasterize_contours(
            parse_result.lines,
            target_resolution_m=resolution_m,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=422,
            detail=str(e),
        )

    timings["rasterize_seconds"] = round(
        time.time() - t,
        2,
    )

    # ---------------------------------------------------------
    # 3. SLOPE
    # ---------------------------------------------------------
    t = time.time()

    slope = compute_slope_degrees(
        elevation,
        transform,
    )

    timings["slope_seconds"] = round(
        time.time() - t,
        2,
    )

    # ---------------------------------------------------------
    # 4. FLOW DIRECTION + ACCUMULATION
    # ---------------------------------------------------------
    t = time.time()

    filled = fill_depressions(elevation)

    px_m = abs(transform.a) * 111320
    py_m = abs(transform.e) * 111320

    downstream_r, downstream_c = flow_direction_d8(
        filled,
        px_m,
        py_m,
    )

    acc = flow_accumulation(
        filled,
        downstream_r,
        downstream_c,
    )

    timings["flow_analysis_seconds"] = round(
        time.time() - t,
        2,
    )

    # ---------------------------------------------------------
    # 5. AUTOMATIC POND SITE SELECTION
    # ---------------------------------------------------------
    t = time.time()

    row, col, site_info = select_pond_site(
        slope,
        acc,
        elevation=elevation,
        max_slope_deg=max_slope_deg,
    )

    timings["site_selection_seconds"] = round(
        time.time() - t,
        2,
    )

    # ---------------------------------------------------------
    # 6. CATCHMENT DELINEATION
    # ---------------------------------------------------------
    t = time.time()

    catchment_mask = delineate_catchment(
        downstream_r,
        downstream_c,
        row,
        col,
    )

    catchment_geojson = _build_catchment_geojson(
        catchment_mask,
        transform,
    )

    timings["catchment_delineation_seconds"] = round(
        time.time() - t,
        2,
    )

    site_lat, site_lon = _rowcol_to_latlon(
        transform,
        row,
        col,
    )

    cell_area_m2 = px_m * py_m

    catchment_area_m2 = (
        float(catchment_mask.sum())
        * cell_area_m2
    )

    # ---------------------------------------------------------
    # 7. LAND + OWNERSHIP CHECKS
    #
    # Full API:
    #     include_external_checks=True
    #
    # Browser demo:
    #     include_external_checks=False
    # ---------------------------------------------------------
    t = time.time()

    vacant_land_boundary_geojson = None
    ownership_layers = None

    if include_external_checks:
        obstruction_data = await fetch_obstructions(
            site_lat,
            site_lon,
            radius_m=150.0,
        )

        ownership_data = await fetch_ownership_zones(
            site_lat,
            site_lon,
            radius_m=150.0,
        )

        if (
            obstruction_data["query_succeeded"]
            and ownership_data["query_succeeded"]
        ):
            patch_result = find_eligible_patches(
                site_lat,
                site_lon,
                buildings=obstruction_data["buildings"],
                roads=obstruction_data["roads"],
                water=obstruction_data["water"],
                government_zones=ownership_data["government_zones"],
                private_zones=ownership_data["private_zones"],
                search_radius_m=150.0,
            )

            ownership_layers = patch_result[
                "layer_boundaries"
            ]

            if (
                patch_result["patches"]
                and patch_result["selected_patch_index"]
                is not None
            ):
                selected_patch = patch_result["patches"][
                    patch_result["selected_patch_index"]
                ]

                available_area_m2 = selected_patch[
                    "area_m2"
                ]

                vacant_land_boundary_geojson = (
                    selected_patch["boundary_geojson"]
                )

                site_check = {
                    "available_area_m2": available_area_m2,
                    "area_breakdown": patch_result[
                        "area_breakdown"
                    ],
                    "total_separate_eligible_patches_found": len(
                        patch_result["patches"]
                    ),
                    "government_zones_found_nearby": patch_result[
                        "government_zones_found_nearby"
                    ],
                    "private_zones_found_nearby": patch_result[
                        "private_zones_found_nearby"
                    ],
                    "buildings_found_nearby": patch_result[
                        "buildings_found_nearby"
                    ],
                    "roads_found_nearby": patch_result[
                        "roads_found_nearby"
                    ],
                    "water_bodies_found_nearby": patch_result[
                        "water_bodies_found_nearby"
                    ],
                    "limitation": patch_result[
                        "ownership_data_limitation"
                    ],
                }

            else:
                available_area_m2 = 0.0

                site_check = {
                    "available_area_m2": 0.0,
                    "area_breakdown": patch_result[
                        "area_breakdown"
                    ],
                    "total_separate_eligible_patches_found": 0,
                    "government_zones_found_nearby": patch_result[
                        "government_zones_found_nearby"
                    ],
                    "private_zones_found_nearby": patch_result[
                        "private_zones_found_nearby"
                    ],
                    "buildings_found_nearby": patch_result[
                        "buildings_found_nearby"
                    ],
                    "roads_found_nearby": patch_result[
                        "roads_found_nearby"
                    ],
                    "water_bodies_found_nearby": patch_result[
                        "water_bodies_found_nearby"
                    ],
                    "limitation": patch_result[
                        "ownership_data_limitation"
                    ],
                }

        else:
            failed_reasons = []

            if not obstruction_data[
                "query_succeeded"
            ]:
                failed_reasons.append(
                    "obstruction check failed "
                    f"({obstruction_data['error']})"
                )

            if not ownership_data[
                "query_succeeded"
            ]:
                failed_reasons.append(
                    "ownership check failed "
                    f"({ownership_data['error']})"
                )

            available_area_m2 = 0.0

            site_check = {
                "available_area_m2": 0.0,
                "note": (
                    "Could not verify land "
                    "ownership/availability "
                    f"({'; '.join(failed_reasons)})."
                ),
            }

    else:
        # Fast browser demo mode
        available_area_m2 = 0.0

        site_check = {
            "available_area_m2": None,
            "status": "skipped",
            "note": (
                "External land-use and ownership "
                "checks were skipped in fast demo mode."
            ),
        }

    timings["land_use_check_seconds"] = round(
        time.time() - t,
        2,
    )

    # ---------------------------------------------------------
    # 8. RAINFALL + RUNOFF
    # ---------------------------------------------------------
    t = time.time()

    rainfall_result = await get_historical_rainfall(
        site_lat,
        site_lon,
        years=10,
    )

    dates = rainfall_result["daily_series"]["dates"]

    daily_values = rainfall_result[
        "daily_series"
    ]["precipitation_mm"]

    years_seen = sorted(
        set(d[:4] for d in dates)
    )

    complete_years = (
        years_seen[1:-1]
        or years_seen
    )

    curve_number = CURVE_NUMBERS.get(
        "cultivated_land",
        DEFAULT_CURVE_NUMBER,
    )

    annual_runoff_depths = []

    for year in complete_years:
        year_values = [
            v
            for d, v in zip(
                dates,
                daily_values,
            )
            if d.startswith(year)
        ]

        runoff_result = (
            estimate_daily_series_runoff(
                year_values,
                curve_number=curve_number,
            )
        )

        annual_runoff_depths.append(
            runoff_result[
                "total_runoff_depth_mm"
            ]
        )

    avg_annual_runoff_depth_mm = (
        sum(annual_runoff_depths)
        / len(annual_runoff_depths)
        if annual_runoff_depths
        else 0.0
    )

    avg_annual_runoff_volume_m3 = (
        avg_annual_runoff_depth_mm / 1000
    ) * catchment_area_m2

    timings["rainfall_runoff_seconds"] = round(
        time.time() - t,
        2,
    )

    # ---------------------------------------------------------
    # 9. SOIL CHECK
    # ---------------------------------------------------------
    t = time.time()

    if include_external_checks:
        soil_check = await fetch_soil_composition(
            site_lat,
            site_lon,
        )
    else:
        soil_check = {
            "query_succeeded": False,
            "status": "skipped",
            "note": (
                "External soil check skipped "
                "in fast demo mode."
            ),
        }

    timings["soil_check_seconds"] = round(
        time.time() - t,
        2,
    )

    # ---------------------------------------------------------
    # 10. POND SIZING
    # ---------------------------------------------------------
    pond_sizing = recommend_pond(
        required_volume_m3=avg_annual_runoff_volume_m3,
        available_site_area_m2=available_area_m2,
        target_capture_fraction=0.5,
    )

    timings["total_seconds"] = round(
        time.time() - t_start,
        2,
    )

    # ---------------------------------------------------------
    # 11. RESPONSE
    # ---------------------------------------------------------
    return {
        "input_file": {
            "filename": file.filename,
            "format_detected": parse_result.source_format,
            "contour_lines_parsed": len(
                parse_result.lines
            ),
            "contour_lines_skipped": (
                parse_result.lines_skipped
            ),
        },

        "terrain_summary": {
            "bounding_box": raster_meta["bbox"],
            "raster_shape_rows_cols": (
                raster_meta["raster_shape"]
            ),
            "raster_resolution_m": (
                raster_meta["resolution_m_used"]
            ),
            "elevation_range_m": (
                raster_meta["elevation_range_m"]
            ),
            "mean_slope_deg": round(
                float(np.nanmean(slope)),
                2,
            ),
        },

        "recommended_pond_site": {
            "lat": site_lat,
            "lon": site_lon,
            "elevation_m": round(
                float(elevation[row, col]),
                1,
            ),
            "selection_method": site_info,
        },

        "catchment": {
            "area_m2": round(
                catchment_area_m2,
                1,
            ),
            "area_hectares": round(
                catchment_area_m2 / 10000,
                2,
            ),
            "cell_count": int(
                catchment_mask.sum()
            ),
            "boundary_geojson": catchment_geojson,
        },

        "rainfall_and_runoff": {
            "years_analyzed": len(
                complete_years
            ),
            "annual_average_rainfall_mm": (
                rainfall_result[
                    "annual_average_mm"
                ]
            ),
            "curve_number_used": curve_number,
            "avg_annual_runoff_volume_m3": round(
                avg_annual_runoff_volume_m3,
                1,
            ),
        },

        "site_check": site_check,

        "vacant_land_boundary_geojson": (
            vacant_land_boundary_geojson
        ),

        "ownership_layers": ownership_layers,

        "soil_check": soil_check,

        "pond_sizing_recommendation": pond_sizing,

        "methodology": (
            "Contour lines were parsed from the uploaded "
            "file and interpolated into a continuous "
            "elevation raster (linear interpolation over "
            "scattered elevation points sampled along each "
            "line). Slope was computed via finite-difference "
            "gradient. Flow direction/accumulation were "
            "computed using a D8 algorithm (priority-flood "
            "depression filling + steepest-descent routing). "
            "The pond site was chosen as the highest-"
            "flow-accumulation cell among low-slope "
            "candidates. The catchment was delineated via "
            "reverse breadth-first search over the "
            "flow-direction graph, upstream from the "
            "selected site. Real building/road data from "
            "OpenStreetMap can be checked near the selected "
            "site in full-analysis mode. No coordinates or "
            "results are hard-coded -- everything is derived "
            "from the uploaded file."
        ),

        "processing_time_seconds": timings,
    }


# =============================================================
# ACTUAL API: UPLOAD ANY KML/KMZ
# =============================================================

@router.post("/api/analyzeContour")
async def analyze_contour(
    file: UploadFile = File(
        ...,
        description="A KML or KMZ contour map file",
    ),
    resolution_m: float = Query(
        10.0,
        ge=2.0,
        le=50.0,
        description=(
            "Raster cell size to reconstruct, "
            "in meters"
        ),
    ),
    max_slope_deg: float = Query(
        8.0,
        ge=1.0,
        le=45.0,
        description=(
            "Maximum slope in degrees considered "
            "suitable for pond excavation"
        ),
    ),
):
    """
    Analyze an uploaded contour map (KML/KMZ).
    """

    return await _analyze_contour_impl(
        file,
        resolution_m,
        max_slope_deg,
        include_external_checks=True,
    )


@router.post("/api/findCatchment")
async def find_catchment(
    file: UploadFile = File(
        ...,
        description="A KML or KMZ contour map file",
    ),
    resolution_m: float = Query(
        10.0,
        ge=2.0,
        le=50.0,
    ),
    max_slope_deg: float = Query(
        8.0,
        ge=1.0,
        le=45.0,
    ),
):
    """
    Main catchment-estimation API.

    Accepts any KML/KMZ contour map.
    """

    return await _analyze_contour_impl(
        file,
        resolution_m,
        max_slope_deg,
        include_external_checks=True,
    )


# =============================================================
# BROWSER DEMO USING PROVIDED SAMPLE FILE
# =============================================================

@router.get(
    "/api/demo/catchment",
    tags=["contour-analysis"],
)
async def demo_catchment():
    """
    Browser-friendly demonstration endpoint.

    Automatically processes the sample contour map:
    backend/test_data/contours_1m.kml
    """

    sample_file = (
        Path(__file__).resolve().parents[2]
        / "test_data"
        / "contours_1m.kml"
    )

    if not sample_file.exists():
        raise HTTPException(
            status_code=404,
            detail=(
                "Sample contour file not found: "
                f"{sample_file}"
            ),
        )

    with open(sample_file, "rb") as f:
        file_bytes = f.read()

    temp_file = SpooledTemporaryFile()

    temp_file.write(file_bytes)
    temp_file.seek(0)

    upload = UploadFile(
        file=temp_file,
        filename="contours_1m.kml",
    )

    return await _analyze_contour_impl(
        upload,
        resolution_m=10.0,
        max_slope_deg=8.0,
        include_external_checks=False,
    )


# =============================================================
# BROWSER UPLOAD INTERFACE
# =============================================================

@router.get(
    "/api/catchment/upload",
    response_class=HTMLResponse,
    tags=["contour-analysis"],
)
async def catchment_upload_page():
    """
    Browser-based contour map upload interface.

    Users can upload any KML or KMZ file directly from
    their browser.
    """

    return """
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport"
              content="width=device-width, initial-scale=1">

        <title>Village Pond Planning System</title>

        <style>
            * {
                box-sizing: border-box;
            }

            body {
                font-family: Arial, sans-serif;
                margin: 0;
                padding: 40px 20px;
                background: #f4f7f9;
                color: #222;
            }

            .container {
                max-width: 850px;
                margin: 0 auto;
            }

            .card {
                background: white;
                padding: 32px;
                border-radius: 14px;
                box-shadow:
                    0 4px 20px rgba(0, 0, 0, 0.08);
            }

            h1 {
                margin-top: 0;
                margin-bottom: 10px;
            }

            .subtitle {
                color: #666;
                margin-bottom: 30px;
                line-height: 1.5;
            }

            .field {
                margin-top: 18px;
            }

            label {
                display: block;
                font-weight: bold;
                margin-bottom: 8px;
            }

            input[type="file"],
            input[type="number"] {
                width: 100%;
                padding: 12px;
                border: 1px solid #ccc;
                border-radius: 7px;
                background: white;
            }

            button {
                margin-top: 25px;
                padding: 13px 22px;
                border: none;
                border-radius: 8px;
                background: #2563eb;
                color: white;
                font-size: 16px;
                font-weight: bold;
                cursor: pointer;
            }

            button:disabled {
                background: #999;
                cursor: not-allowed;
            }

            #status {
                margin-top: 25px;
                padding: 15px;
                background: #f1f5f9;
                border-radius: 8px;
                white-space: pre-wrap;
                line-height: 1.5;
            }

            .spinner {
                display: inline-block;
                width: 14px;
                height: 14px;
                border: 2px solid #ccc;
                border-top-color: #2563eb;
                border-radius: 50%;
                animation: spin 0.8s linear infinite;
                margin-right: 8px;
            }

            @keyframes spin {
                to {
                    transform: rotate(360deg);
                }
            }

            #result {
                margin-top: 25px;
            }

            .results-grid {
                display: grid;
                grid-template-columns:
                    repeat(auto-fit, minmax(200px, 1fr));
                gap: 15px;
                margin-top: 15px;
            }

            .result-card {
                background: #f8fafc;
                padding: 18px;
                border-radius: 9px;
                border: 1px solid #e2e8f0;
            }

            .result-title {
                color: #64748b;
                font-size: 13px;
                margin-bottom: 7px;
            }

            .result-value {
                font-size: 21px;
                font-weight: bold;
            }

            details {
                margin-top: 20px;
            }

            summary {
                cursor: pointer;
                font-weight: bold;
            }

            pre {
                background: #111827;
                color: #e5e7eb;
                padding: 18px;
                border-radius: 8px;
                overflow-x: auto;
                white-space: pre-wrap;
                word-break: break-word;
                font-size: 12px;
            }

            .error {
                background: #fef2f2;
                color: #991b1b;
                border: 1px solid #fecaca;
            }

            .success {
                background: #f0fdf4;
                color: #166534;
                border: 1px solid #bbf7d0;
            }
        </style>
    </head>

    <body>

        <div class="container">

            <div class="card">

                <h1>
                    Village Pond Planning System
                </h1>

                <div class="subtitle">
                    Upload any KML or KMZ contour map to
                    reconstruct terrain, identify a suitable
                    pond location, and estimate the catchment.
                </div>

                <form id="uploadForm">

                    <div class="field">
                        <label for="file">
                            Contour Map (.kml / .kmz)
                        </label>

                        <input
                            type="file"
                            id="file"
                            name="file"
                            accept=".kml,.kmz"
                            required
                        >
                    </div>

                    <div class="field">
                        <label for="resolution_m">
                            Raster Resolution (meters)
                        </label>

                        <input
                            type="number"
                            id="resolution_m"
                            value="10"
                            min="2"
                            max="50"
                            step="1"
                        >
                    </div>

                    <div class="field">
                        <label for="max_slope_deg">
                            Maximum Suitable Slope (degrees)
                        </label>

                        <input
                            type="number"
                            id="max_slope_deg"
                            value="8"
                            min="1"
                            max="45"
                            step="1"
                        >
                    </div>

                    <button
                        type="submit"
                        id="analyzeButton"
                    >
                        Analyze Catchment
                    </button>

                </form>

                <div id="status"></div>

                <div id="result"></div>

            </div>

        </div>

        <script>
            const form =
                document.getElementById("uploadForm");

            const fileInput =
                document.getElementById("file");

            const resolutionInput =
                document.getElementById("resolution_m");

            const slopeInput =
                document.getElementById("max_slope_deg");

            const button =
                document.getElementById("analyzeButton");

            const status =
                document.getElementById("status");

            const result =
                document.getElementById("result");


            form.addEventListener(
                "submit",
                async function(event) {

                    event.preventDefault();

                    if (!fileInput.files.length) {
                        status.textContent =
                            "Please select a KML or KMZ file.";

                        status.className = "error";

                        return;
                    }

                    const file =
                        fileInput.files[0];

                    const filename =
                        file.name.toLowerCase();

                    if (
                        !filename.endsWith(".kml")
                        &&
                        !filename.endsWith(".kmz")
                    ) {
                        status.textContent =
                            "Please upload a .kml or .kmz file.";

                        status.className = "error";

                        return;
                    }

                    const formData =
                        new FormData();

                    formData.append(
                        "file",
                        file
                    );

                    const resolution =
                        resolutionInput.value;

                    const maxSlope =
                        slopeInput.value;

                    button.disabled = true;

                    result.innerHTML = "";

                    status.className = "";

                    const start =
                        Date.now();

                    const timer =
                        setInterval(
                            function() {

                                const elapsed =
                                    Math.floor(
                                        (Date.now() - start)
                                        / 1000
                                    );

                                status.innerHTML =
                                    '<span class="spinner"></span>' +
                                    'Analyzing contour map... ' +
                                    elapsed +
                                    ' seconds elapsed.';

                            },
                            500
                        );

                    status.innerHTML =
                        '<span class="spinner"></span>' +
                        'Uploading and analyzing contour map...';


                    try {

                        const response =
                            await fetch(
                                "/api/findCatchment"
                                + "?resolution_m="
                                + encodeURIComponent(resolution)
                                + "&max_slope_deg="
                                + encodeURIComponent(maxSlope),
                                {
                                    method: "POST",
                                    body: formData
                                }
                            );

                        const data =
                            await response.json();

                        clearInterval(timer);

                        if (!response.ok) {
                            throw new Error(
                                data.detail
                                ||
                                "API request failed."
                            );
                        }

                        const totalSeconds =
                            (
                                Date.now() - start
                            ) / 1000;


                        const contourLines =
                            data.input_file
                            ?.contour_lines_parsed
                            ?? "N/A";

                        const minElevation =
                            data.terrain_summary
                            ?.elevation_range_m
                            ?.min
                            ?? "N/A";

                        const maxElevation =
                            data.terrain_summary
                            ?.elevation_range_m
                            ?.max
                            ?? "N/A";

                        const meanSlope =
                            data.terrain_summary
                            ?.mean_slope_deg
                            ?? "N/A";

                        const siteLat =
                            data.recommended_pond_site
                            ?.lat
                            ?? "N/A";

                        const siteLon =
                            data.recommended_pond_site
                            ?.lon
                            ?? "N/A";

                        const siteElevation =
                            data.recommended_pond_site
                            ?.elevation_m
                            ?? "N/A";

                        const areaHectares =
                            data.catchment
                            ?.area_hectares
                            ?? "N/A";

                        const areaM2 =
                            data.catchment
                            ?.area_m2
                            ?? "N/A";


                        status.innerHTML =
                            "Analysis completed successfully "
                            + "in "
                            + totalSeconds.toFixed(1)
                            + " seconds.";

                        status.className = "success";


                        result.innerHTML = `

                            <h2>Analysis Results</h2>

                            <div class="results-grid">

                                <div class="result-card">
                                    <div class="result-title">
                                        Contour Lines
                                    </div>
                                    <div class="result-value">
                                        ${contourLines}
                                    </div>
                                </div>

                                <div class="result-card">
                                    <div class="result-title">
                                        Elevation Range
                                    </div>
                                    <div class="result-value">
                                        ${minElevation}
                                        –
                                        ${maxElevation} m
                                    </div>
                                </div>

                                <div class="result-card">
                                    <div class="result-title">
                                        Mean Slope
                                    </div>
                                    <div class="result-value">
                                        ${meanSlope}°
                                    </div>
                                </div>

                                <div class="result-card">
                                    <div class="result-title">
                                        Catchment Area
                                    </div>
                                    <div class="result-value">
                                        ${areaHectares}
                                        ha
                                    </div>
                                </div>

                                <div class="result-card">
                                    <div class="result-title">
                                        Catchment Area
                                    </div>
                                    <div class="result-value">
                                        ${areaM2}
                                        m²
                                    </div>
                                </div>

                                <div class="result-card">
                                    <div class="result-title">
                                        Pond Site Elevation
                                    </div>
                                    <div class="result-value">
                                        ${siteElevation}
                                        m
                                    </div>
                                </div>

                            </div>

                            <div class="result-card"
                                 style="margin-top:15px;">

                                <div class="result-title">
                                    Recommended Pond Site
                                </div>

                                <div class="result-value">
                                    ${siteLat},
                                    ${siteLon}
                                </div>

                            </div>

                            <details>
                                <summary>
                                    View Full API Response
                                </summary>

                                <pre>${JSON.stringify(
                                    data,
                                    null,
                                    2
                                )}</pre>

                            </details>
                        `;

                    } catch (error) {

                        clearInterval(timer);

                        status.textContent =
                            "Analysis failed: "
                            + error.message;

                        status.className = "error";

                    } finally {

                        button.disabled = false;

                    }

                }
            );
        </script>

    </body>
    </html>
    """
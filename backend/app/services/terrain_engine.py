
"""
Terrain engine: given a DEM GeoTIFF, compute slope (degrees) and extract
contour lines as GeoJSON.

Why slope matters for pond siting: cells with low slope (<~8 degrees) are
easier/cheaper to excavate into a basin and hold water without heavy
embankment work, so we flag them as more suitable.
"""

import math

import numpy as np
import rasterio
from rasterio.transform import Affine
from skimage import measure


def load_dem(path):
    """Load a DEM GeoTIFF and return (elevation array, affine transform, crs)."""
    with rasterio.open(path) as src:
        elevation = src.read(1).astype(float)
        transform = src.transform
        crs = src.crs
        nodata = src.nodata

    if nodata is not None:
        elevation[elevation == nodata] = np.nan

    return elevation, transform, crs


def pixel_size_meters(transform, elevation_shape):
    """
    Convert the DEM's pixel size in degrees to approximate meters.

    Longitude conversion uses the mean latitude of the raster, because
    longitude degrees become smaller toward the poles.
    """
    px_deg = abs(transform.a)
    py_deg = abs(transform.e)

    rows, cols = elevation_shape
    center_row = rows // 2

    # Get the latitude at the center of the raster.
    _, center_lat = transform * (0, center_row)

    meters_per_deg_lat = 111_320.0
    meters_per_deg_lon = 111_320.0 * math.cos(math.radians(center_lat))

    px_m = px_deg * meters_per_deg_lon
    py_m = py_deg * meters_per_deg_lat

    return px_m, py_m


# Backward-compatible alias.
_pixel_size_in_meters = pixel_size_meters


def compute_slope_degrees(elevation: np.ndarray, transform) -> np.ndarray:
    """
    Compute slope in degrees at every cell using a finite-difference gradient.

    Returns:
        Array with the same shape as elevation.
        0 degrees = flat terrain.
        90 degrees = vertical cliff.
    """
    px_m, py_m = _pixel_size_in_meters(transform, elevation.shape)

    # Avoid NaN values causing the entire gradient calculation to become NaN.
    valid = np.isfinite(elevation)

    if not np.any(valid):
        return np.full_like(elevation, np.nan, dtype=float)

    # Fill missing cells temporarily using the mean valid elevation.
    # The final result restores NaN at the original nodata cells.
    filled = elevation.copy()

    mean_elevation = float(np.nanmean(filled))
    filled[~valid] = mean_elevation

    dz_dy, dz_dx = np.gradient(filled, py_m, px_m)

    slope_rad = np.arctan(np.sqrt(dz_dx**2 + dz_dy**2))
    slope_deg = np.degrees(slope_rad)

    # Preserve original nodata locations.
    slope_deg[~valid] = np.nan

    return slope_deg


def classify_suitability(
    slope_deg: np.ndarray,
    max_slope_deg: float = 8.0,
) -> np.ndarray:
    """
    Boolean mask showing terrain gentle enough for pond construction.

    True  = slope is at or below the allowed threshold.
    False = slope is too steep.
    """
    return np.isfinite(slope_deg) & (slope_deg <= max_slope_deg)


def generate_contours(
    elevation: np.ndarray,
    transform,
    interval_m: float = 10.0,
) -> list[dict]:
    """
    Extract contour lines from the elevation raster.

    The DEM is downsampled before contour extraction when it is very large.
    This significantly reduces contour-processing time and GeoJSON size while
    keeping the contours suitable for map visualization.

    Returns:
        [
            {
                "elevation": <float>,
                "coordinates": [[lon, lat], ...]
            },
            ...
        ]
    """

    # ---------------------------------------------------------
    # 1. Check that the DEM contains valid elevation data.
    # ---------------------------------------------------------
    valid = elevation[np.isfinite(elevation)]

    if valid.size == 0:
        return []

    z_min = float(np.nanmin(elevation))
    z_max = float(np.nanmax(elevation))

    if z_max <= z_min:
        return []

    # ---------------------------------------------------------
    # 2. Downsample large DEMs.
    #
    # Contours are being generated for map visualization, so
    # processing the complete high-resolution DEM is unnecessary.
    # ---------------------------------------------------------
    max_dimension = 800

    scale = max(elevation.shape) / max_dimension

    if scale > 1:
        step = int(np.ceil(scale))

        elevation = elevation[::step, ::step]

        # The pixel spacing becomes larger after downsampling.
        transform = Affine(
            transform.a * step,
            transform.b,
            transform.c,
            transform.d,
            transform.e * step,
            transform.f,
        )

    # ---------------------------------------------------------
    # 3. Recalculate elevation range after downsampling.
    # ---------------------------------------------------------
    valid = elevation[np.isfinite(elevation)]

    if valid.size == 0:
        return []

    z_min = float(np.nanmin(elevation))
    z_max = float(np.nanmax(elevation))

    if z_max <= z_min:
        return []

    # ---------------------------------------------------------
    # 4. Create contour elevation levels.
    #
    # 10 m interval means fewer contour lines than the old
    # 5 m interval, which makes the map substantially faster.
    # ---------------------------------------------------------
    start_level = math.floor(z_min / interval_m) * interval_m

    levels = np.arange(
        start_level,
        z_max,
        interval_m,
    )

    if len(levels) == 0:
        return []

    # ---------------------------------------------------------
    # 5. Replace NaN values.
    #
    # skimage.measure.find_contours cannot directly process NaN.
    # Values far below the real DEM range prevent artificial
    # contours from appearing inside nodata areas.
    # ---------------------------------------------------------
    filled = np.nan_to_num(
        elevation,
        nan=z_min - 1000.0,
    )

    # ---------------------------------------------------------
    # 6. Extract contours.
    # ---------------------------------------------------------
    contours = []

    for level in levels:

        paths = measure.find_contours(
            filled,
            level=float(level),
        )

        for path in paths:

            # path contains:
            # [row, column]
            #
            # Rasterio transform expects:
            # (column, row)
            coords = [
                transform * (col, row)
                for row, col in path
            ]

            # Ignore extremely short contour fragments.
            if len(coords) < 2:
                continue

            contours.append(
                {
                    "elevation": float(level),
                    "coordinates": [
                        [float(x), float(y)]
                        for x, y in coords
                    ],
                }
            )

    return contours
```

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


def _pixel_size_in_meters(transform, elevation_shape):
    """
    Convert the DEM's pixel size (in degrees, since SRTM is in EPSG:4326)
    to approximate meters, using the mean latitude of the raster for the
    longitude-to-meters conversion (longitude degrees shrink toward the poles).
    """
    px_deg = abs(transform.a)  # pixel width in degrees longitude
    py_deg = abs(transform.e)  # pixel height in degrees latitude

    rows, cols = elevation_shape
    center_row = rows // 2
    # latitude at the center row
    _, center_lat = transform * (0, center_row)

    meters_per_deg_lat = 111_320.0
    meters_per_deg_lon = 111_320.0 * math.cos(math.radians(center_lat))

    px_m = px_deg * meters_per_deg_lon
    py_m = py_deg * meters_per_deg_lat
    return px_m, py_m


def compute_slope_degrees(elevation: np.ndarray, transform) -> np.ndarray:
    """
    Compute slope in degrees at every cell using a finite-difference gradient.

    Returns an array the same shape as `elevation`, with slope in degrees
    (0 = flat, 90 = vertical cliff). NaNs are preserved where elevation is NaN.
    """
    px_m, py_m = _pixel_size_in_meters(transform, elevation.shape)

    dz_dy, dz_dx = np.gradient(elevation, py_m, px_m)
    slope_rad = np.arctan(np.sqrt(dz_dx**2 + dz_dy**2))
    slope_deg = np.degrees(slope_rad)
    return slope_deg


def classify_suitability(slope_deg: np.ndarray, max_slope_deg: float = 8.0) -> np.ndarray:
    """
    Boolean mask: True where terrain is gentle enough to be a good pond site
    candidate (low slope). Threshold is adjustable; 8 degrees (~14% grade)
    is a common rule-of-thumb ceiling for small earthen pond construction.
    """
    return slope_deg <= max_slope_deg


def generate_contours(elevation: np.ndarray, transform, interval_m: float = 5.0) -> list[dict]:
    """
    Extract contour lines from the elevation raster at a fixed interval.

    Returns a list of dicts: [{"elevation": <float>, "coordinates": [[lon,lat], ...]}, ...]
    ready to be wrapped into a GeoJSON FeatureCollection by the router.
    """
    valid = elevation[~np.isnan(elevation)]
    if valid.size == 0:
        return []

    z_min, z_max = float(np.nanmin(elevation)), float(np.nanmax(elevation))
    if z_max <= z_min:
        return []

    levels = np.arange(math.floor(z_min / interval_m) * interval_m, z_max, interval_m)

    # skimage needs NaNs replaced with something outside the level range
    filled = np.nan_to_num(elevation, nan=z_min - 1000)

    contours = []
    for level in levels:
        for path in measure.find_contours(filled, level=float(level)):
            # path is an array of (row, col) pixel coordinates; convert to (lon, lat)
            coords = [transform * (col, row) for row, col in path]
            if len(coords) >= 2:
                contours.append({"elevation": float(level), "coordinates": coords})

    return contours

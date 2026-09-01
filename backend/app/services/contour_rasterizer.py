"""
Converts a set of parsed contour lines (vector data, each line at a known
elevation) into a continuous elevation raster (a DEM), so we can reuse our
existing slope/catchment analysis code (which expects a raster grid) exactly
as-is on data that started life as vector contour lines.

Method: treat every point along every contour line as a scattered
(lon, lat, elevation) sample, then interpolate a regular grid over those
samples using linear interpolation (Delaunay triangulation under the hood,
via scipy). This is a standard, well-established "contour-to-DEM"
technique -- conceptually the same idea used by tools like ArcGIS's
"Topo to Raster".

This is deliberately generic: it makes no assumptions about the specific
sample file's location, elevation range, or extent -- everything is derived
from whatever contour lines are passed in, so it generalizes to other
contour maps as required by the assignment.
"""

import math

import numpy as np
from affine import Affine
from scipy.interpolate import griddata


def _subsample_line(points: list, max_points_per_line: int) -> list:
    """Evenly subsample a line's points if it has more than max_points_per_line,
    to keep the total scatter-point count (and therefore interpolation time)
    manageable on dense, real-world contour files."""
    if len(points) <= max_points_per_line:
        return points
    step = len(points) / max_points_per_line
    indices = [int(i * step) for i in range(max_points_per_line)]
    return [points[i] for i in indices]


def rasterize_contours(
    contour_lines: list,
    target_resolution_m: float = 10.0,
    max_total_points: int = 40000,
):
    """
    Build an elevation raster from a list of ContourLine objects.

    Args:
        contour_lines: list of objects with .elevation (float) and
                        .points (list of (lon, lat) tuples)
        target_resolution_m: desired grid cell size in meters. Smaller = more
                              detail but slower and more memory.
        max_total_points: cap on total scatter points fed into the
                           interpolator, to bound runtime on very dense files
                           (points are subsampled per-line, proportionally,
                           if the raw total exceeds this).

    Returns:
        (elevation_grid, transform, metadata_dict)
        elevation_grid: 2D numpy array (rows x cols)
        transform: affine.Affine mapping (col, row) -> (lon, lat), same
                   convention used elsewhere in this codebase (terrain_engine,
                   catchment_engine), so all existing analysis code works
                   unchanged on this raster.
    """
    if not contour_lines:
        raise ValueError("No contour lines provided -- cannot build a raster from nothing.")

    all_lons, all_lats = [], []
    for line in contour_lines:
        for lon, lat in line.points:
            all_lons.append(lon)
            all_lats.append(lat)

    west, east = min(all_lons), max(all_lons)
    south, north = min(all_lats), max(all_lats)

    if east <= west or north <= south:
        raise ValueError("Contour data has degenerate (zero-area) bounding box.")

    # Subsample dense lines proportionally so total points stay under the cap
    total_raw_points = sum(len(line.points) for line in contour_lines)
    if total_raw_points > max_total_points:
        per_line_cap = max(2, int(max_total_points / len(contour_lines)))
    else:
        per_line_cap = max(len(line.points) for line in contour_lines)

    scatter_lons, scatter_lats, scatter_elevs = [], [], []
    for line in contour_lines:
        pts = _subsample_line(line.points, per_line_cap)
        for lon, lat in pts:
            scatter_lons.append(lon)
            scatter_lats.append(lat)
            scatter_elevs.append(line.elevation)

    scatter_lons = np.array(scatter_lons)
    scatter_lats = np.array(scatter_lats)
    scatter_elevs = np.array(scatter_elevs)

    # Convert desired resolution (meters) to degrees, using the area's mean
    # latitude for the longitude conversion (longitude degrees shrink toward
    # the poles -- same approach used in terrain_engine.py).
    mean_lat = (north + south) / 2
    meters_per_deg_lat = 111_320.0
    meters_per_deg_lon = 111_320.0 * math.cos(math.radians(mean_lat))

    px_deg = target_resolution_m / meters_per_deg_lon
    py_deg = target_resolution_m / meters_per_deg_lat

    cols = max(2, int((east - west) / px_deg))
    rows = max(2, int((north - south) / py_deg))

    # Cap grid size to keep interpolation + downstream D8 processing tractable
    MAX_CELLS = 250_000
    if rows * cols > MAX_CELLS:
        scale = math.sqrt(MAX_CELLS / (rows * cols))
        rows = max(2, int(rows * scale))
        cols = max(2, int(cols * scale))
        px_deg = (east - west) / cols
        py_deg = (north - south) / rows

    grid_lons = np.linspace(west, east, cols)
    grid_lats = np.linspace(north, south, rows)  # north to south = row 0 is north, matches raster convention
    mesh_lon, mesh_lat = np.meshgrid(grid_lons, grid_lats)

    # Linear interpolation (Delaunay-based) for the interior, filling any
    # points outside the convex hull of our scatter data with nearest-neighbor
    # so the whole grid still has a value (no NaN holes at the raster edges).
    grid_elev = griddata(
        (scatter_lons, scatter_lats), scatter_elevs,
        (mesh_lon, mesh_lat), method="linear",
    )
    nan_mask = np.isnan(grid_elev)
    if nan_mask.any():
        grid_elev_nearest = griddata(
            (scatter_lons, scatter_lats), scatter_elevs,
            (mesh_lon, mesh_lat), method="nearest",
        )
        grid_elev[nan_mask] = grid_elev_nearest[nan_mask]

    transform = Affine(px_deg, 0.0, west, 0.0, -py_deg, north)

    metadata = {
        "bbox": {"south": south, "north": north, "west": west, "east": east},
        "raster_shape": [rows, cols],
        "resolution_m_used": target_resolution_m,
        "scatter_points_used": len(scatter_elevs),
        "scatter_points_available": total_raw_points,
        "contour_lines_used": len(contour_lines),
        "elevation_range_m": {"min": float(scatter_elevs.min()), "max": float(scatter_elevs.max())},
    }

    return grid_elev.astype(float), transform, metadata

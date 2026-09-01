"""
Parses a KML or KMZ contour map file into a list of contour lines, each with
its elevation and a list of (lon, lat) points.

Designed to be generalizable to OTHER contour maps (not hard-coded to the
sample file), by trying several ways to find each line's elevation, in order
of preference:
    1. The Z (altitude) value in the coordinate tuples themselves, if present
       (some contour generators embed elevation directly as lon,lat,alt).
    2. The Placemark's <name> tag, if it parses as a number (this is how our
       sample file, and many contour-generation tools, store elevation).
    3. An ExtendedData/SchemaData field whose name looks like an elevation
       field (e.g. "elev", "elevation", "contour", "height", "z").

If a line's elevation can't be determined by any of these, it's skipped
(logged in the returned metadata) rather than guessed.

KML uses XML namespaces that vary between tools, so we parse tag names by
their local part (after the "}") rather than requiring an exact namespace
match -- this is what makes the parser tool-agnostic.
"""

import io
import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field


@dataclass
class ContourLine:
    elevation: float
    points: list  # list of (lon, lat) tuples


@dataclass
class ParseResult:
    lines: list = field(default_factory=list)  # list of ContourLine
    lines_skipped: int = 0
    source_format: str = "kml"


def _local_tag(elem) -> str:
    """Return an element's tag name without its namespace prefix."""
    tag = elem.tag
    return tag.split("}", 1)[1] if "}" in tag else tag


def _try_float(value: str):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


_ELEVATION_FIELD_HINTS = ("elev", "elevation", "contour", "height", "z", "alt")


def _extract_elevation_from_placemark(placemark) -> float | None:
    """Try, in order, the strategies described in the module docstring."""

    # Strategy 1 is applied later, once we've parsed the coordinates (needs
    # the Z value from the coordinate tuples themselves).

    # Strategy 2: <name> tag as a number
    for child in placemark:
        if _local_tag(child) == "name":
            val = _try_float((child.text or "").strip())
            if val is not None:
                return val

    # Strategy 3: ExtendedData / SchemaData SimpleData fields with a
    # plausible elevation-like field name
    for extended in placemark:
        if _local_tag(extended) != "ExtendedData":
            continue
        for schema_data in extended:
            for simple_data in schema_data:
                if _local_tag(simple_data) != "SimpleData":
                    continue
                field_name = (simple_data.get("name") or "").lower()
                if any(hint in field_name for hint in _ELEVATION_FIELD_HINTS):
                    val = _try_float((simple_data.text or "").strip())
                    if val is not None:
                        return val

    return None


def _parse_coordinates_text(text: str):
    """
    Parse a KML <coordinates> block into a list of (lon, lat, alt_or_None).
    KML coordinate tuples are "lon,lat" or "lon,lat,alt", whitespace-separated.
    """
    points = []
    for token in text.strip().split():
        parts = token.split(",")
        if len(parts) < 2:
            continue
        lon = _try_float(parts[0])
        lat = _try_float(parts[1])
        alt = _try_float(parts[2]) if len(parts) >= 3 else None
        if lon is not None and lat is not None:
            points.append((lon, lat, alt))
    return points


def _find_linestrings(placemark):
    """A Placemark may contain a LineString directly, or nested inside a
    MultiGeometry -- collect all LineString elements either way."""
    result = []
    for elem in placemark.iter():
        if _local_tag(elem) == "LineString":
            result.append(elem)
    return result


def parse_kml_bytes(kml_bytes: bytes) -> ParseResult:
    root = ET.fromstring(kml_bytes)
    result = ParseResult(source_format="kml")

    for placemark in root.iter():
        if _local_tag(placemark) != "Placemark":
            continue

        name_elevation = _extract_elevation_from_placemark(placemark)

        for linestring in _find_linestrings(placemark):
            coords_text = None
            for child in linestring:
                if _local_tag(child) == "coordinates":
                    coords_text = child.text
            if not coords_text:
                continue

            raw_points = _parse_coordinates_text(coords_text)
            if len(raw_points) < 2:
                continue

            # Strategy 1: use the Z/altitude value from the coordinates
            # themselves, if every point in the line has one and they agree
            # (a contour line should be at a single constant elevation).
            alts = [p[2] for p in raw_points if p[2] is not None]
            elevation = None
            if len(alts) == len(raw_points) and len(set(round(a, 3) for a in alts)) == 1:
                elevation = alts[0]
            elif name_elevation is not None:
                elevation = name_elevation

            if elevation is None:
                result.lines_skipped += 1
                continue

            points_2d = [(p[0], p[1]) for p in raw_points]
            result.lines.append(ContourLine(elevation=elevation, points=points_2d))

    return result


def parse_contour_file(file_bytes: bytes, filename: str) -> ParseResult:
    """
    Entry point: parses either a raw .kml file or a .kmz (zipped KML) file.
    Detects format by file signature (zip magic bytes) rather than trusting
    the filename extension, since uploads can be renamed/misnamed.
    """
    is_zip = file_bytes[:2] == b"PK"

    if is_zip or filename.lower().endswith(".kmz"):
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as z:
            kml_names = [n for n in z.namelist() if n.lower().endswith(".kml")]
            if not kml_names:
                raise ValueError("KMZ file does not contain any .kml file inside it.")
            # Prefer a file literally named doc.kml (the usual convention), else take the first
            preferred = next((n for n in kml_names if n.lower() == "doc.kml"), kml_names[0])
            kml_bytes = z.read(preferred)
        result = parse_kml_bytes(kml_bytes)
        result.source_format = "kmz"
        return result

    return parse_kml_bytes(file_bytes)

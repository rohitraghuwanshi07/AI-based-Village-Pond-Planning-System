"""
Parses a user-supplied land record document (e.g. a GeoJSON or KML export
derived from a Bhu-Naksha / Bhulekh style cadastral map) into classified
parcels: government-owned, private, or a specific exclusion type (existing
building/habitation, road, water body).

WHY THIS EXISTS: there is no free, live, queryable API for authoritative land
ownership (see ownership_client.py for the full explanation). Rather than
guess from OpenStreetMap tags, this lets the user supply the ACTUAL land
record for their village -- even a simplified, manually-prepared extract for
prototype purposes -- which is a much stronger basis for the "government
land-record source" the assignment specification asks for.

Expected input format (GeoJSON, the simplest to hand-prepare for a prototype):
{
  "type": "FeatureCollection",
  "features": [
    {
      "type": "Feature",
      "properties": {
        "khasra_no": "123/2",         # survey/parcel number (any label)
        "land_type": "Sarkari Zamin",  # the classification text from the record
        "owner_name": "Gram Panchayat" # optional
      },
      "geometry": {"type": "Polygon", "coordinates": [[[lon,lat], ...]]}
    },
    ...
  ]
}

KML is also supported: each Placemark's <name> is treated as the khasra
number, and an ExtendedData/SimpleData field (or the <description>) is
searched for classification keywords.

Classification uses real Indian revenue-record terminology (Hindi/English),
case-insensitive substring matching:
    Government-eligible: sarkari, government, nazul, gram panchayat, panchayat,
        charagah, gauchar, shamlat, van vibhag, forest department, forest
    Existing development (excluded, treated as "buildings"): aabadi, abadi,
        makan, ghar, residential, building, school, hospital, office
    Road (excluded): sarak, sadak, road, rasta, marg
    Water body (excluded): talab, talao, naala, nala, drain, pond, river, nadi
    Private (excluded, per strict policy): krishi, khasra, bhumiswami,
        khatedar, private, agricultural, farmland

Anything not matching any of these is "unverified" and excluded by default,
same strict policy as the rest of this project.
"""

import io
import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from shapely.geometry import Polygon

_GOVERNMENT_KEYWORDS = (
    "sarkari", "government", "govt", "nazul", "gram panchayat", "panchayat",
    "charagah", "gauchar", "shamlat", "van vibhag", "forest department", "forest",
    "municipal", "nagar palika", "nagar nigam",
)
_BUILDING_KEYWORDS = (
    "aabadi", "abadi", "makan", "ghar", "residential", "building",
    "school", "hospital", "office", "housing",
)
_ROAD_KEYWORDS = ("sarak", "sadak", "road", "rasta", "marg", "highway")
_WATER_KEYWORDS = ("talab", "talao", "naala", "nala", "drain", "pond", "river", "nadi", "canal")
_PRIVATE_KEYWORDS = (
    "krishi", "khasra", "bhumiswami", "khatedar", "private",
    "agricultural", "farmland", "individual",
)


@dataclass
class LandParcel:
    khasra_no: str
    land_type_raw: str
    classification: str  # "government", "private", "building", "road", "water", "unverified"
    polygon: object  # shapely Polygon
    owner_name: str = ""


@dataclass
class LandRecordResult:
    parcels: list = field(default_factory=list)  # list of LandParcel
    parcels_skipped: int = 0
    source_format: str = "geojson"


def _classify_land_type(text: str) -> str:
    text_lower = (text or "").lower()
    # Order matters: check specific exclusion categories before the broader
    # government/private buckets, since e.g. "government school" should be
    # excluded as a building, not counted as eligible government land.
    if any(kw in text_lower for kw in _BUILDING_KEYWORDS):
        return "building"
    if any(kw in text_lower for kw in _ROAD_KEYWORDS):
        return "road"
    if any(kw in text_lower for kw in _WATER_KEYWORDS):
        return "water"
    if any(kw in text_lower for kw in _GOVERNMENT_KEYWORDS):
        return "government"
    if any(kw in text_lower for kw in _PRIVATE_KEYWORDS):
        return "private"
    return "unverified"


def _parse_geojson(file_bytes: bytes) -> LandRecordResult:
    data = json.loads(file_bytes)
    result = LandRecordResult(source_format="geojson")

    features = data.get("features", [])
    for feat in features:
        props = feat.get("properties", {}) or {}
        geom = feat.get("geometry", {}) or {}

        if geom.get("type") != "Polygon":
            result.parcels_skipped += 1
            continue

        coords = geom.get("coordinates", [])
        if not coords or len(coords[0]) < 3:
            result.parcels_skipped += 1
            continue

        try:
            polygon = Polygon(coords[0])
            if not polygon.is_valid or polygon.area == 0:
                result.parcels_skipped += 1
                continue
        except Exception:
            result.parcels_skipped += 1
            continue

        land_type_raw = str(props.get("land_type", ""))
        classification = _classify_land_type(land_type_raw)

        result.parcels.append(LandParcel(
            khasra_no=str(props.get("khasra_no", "unknown")),
            land_type_raw=land_type_raw,
            classification=classification,
            polygon=polygon,
            owner_name=str(props.get("owner_name", "")),
        ))

    return result


def _local_tag(elem) -> str:
    tag = elem.tag
    return tag.split("}", 1)[1] if "}" in tag else tag


def _parse_kml(file_bytes: bytes) -> LandRecordResult:
    root = ET.fromstring(file_bytes)
    result = LandRecordResult(source_format="kml")

    for placemark in root.iter():
        if _local_tag(placemark) != "Placemark":
            continue

        khasra_no = "unknown"
        land_type_raw = ""

        for child in placemark:
            tag = _local_tag(child)
            if tag == "name":
                khasra_no = (child.text or "unknown").strip()
            elif tag == "description":
                land_type_raw += " " + (child.text or "")
            elif tag == "ExtendedData":
                for schema_data in child:
                    for simple_data in schema_data:
                        if _local_tag(simple_data) == "SimpleData":
                            field_name = (simple_data.get("name") or "").lower()
                            if "type" in field_name or "class" in field_name:
                                land_type_raw += " " + (simple_data.text or "")

        polygon_coords = None
        for elem in placemark.iter():
            if _local_tag(elem) == "coordinates":
                text = (elem.text or "").strip()
                points = []
                for token in text.split():
                    parts = token.split(",")
                    if len(parts) >= 2:
                        points.append((float(parts[0]), float(parts[1])))
                if len(points) >= 3:
                    polygon_coords = points
                break

        if polygon_coords is None:
            result.parcels_skipped += 1
            continue

        try:
            polygon = Polygon(polygon_coords)
            if not polygon.is_valid or polygon.area == 0:
                result.parcels_skipped += 1
                continue
        except Exception:
            result.parcels_skipped += 1
            continue

        classification = _classify_land_type(land_type_raw)
        result.parcels.append(LandParcel(
            khasra_no=khasra_no,
            land_type_raw=land_type_raw.strip(),
            classification=classification,
            polygon=polygon,
        ))

    return result


def parse_land_record_file(file_bytes: bytes, filename: str) -> LandRecordResult:
    """
    Entry point: parses a .geojson/.json or .kml land record file.
    Detects format from content, not just the filename extension.
    """
    stripped = file_bytes.lstrip()
    if stripped[:1] in (b"{", b"["):
        return _parse_geojson(file_bytes)
    return _parse_kml(file_bytes)

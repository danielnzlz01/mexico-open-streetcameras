#!/usr/bin/env python3
"""
Build an automated Mexico speed & red-light camera feed (GATSO_ALL.csv) for Open Zeekr.

Data sources:
  - Source A: OpenStreetMap (nationwide Mexico via Overpass API, ODbL 1.0)
  - Source B: Official CDMX Open Data — Fotocívicas (datos.cdmx.gob.mx, Libre Uso MX)
              with automatic fallback to a committed local cache when unreachable.

Output format (Garmin-POI / Open-GATSO-POI / Open Zeekr):
  longitude,latitude,"name","comment"
  - No header row, UTF-8 (no BOM), LF line endings, comma delimiter.
  - Packaged into csv_files.zip -> GATSO_ALL.csv for GitHub Releases, matching
    borconi/openzeekr (GatsoUpdater.java) and apgneto/br-open-speedcameras.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import math
import re
import sys
import time
import zipfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import requests

LOGGER = logging.getLogger("build_feed")

# Mexico nationwide bounding box (strict validation bounds)
MX_LAT_MIN = 14.5
MX_LAT_MAX = 32.8
MX_LON_MIN = -118.5
MX_LON_MAX = -86.5

# CDMX bounding box (for reporting & sanity checks)
CDMX_LAT_MIN = 19.04
CDMX_LAT_MAX = 19.60
CDMX_LON_MIN = -99.37
CDMX_LON_MAX = -98.94

USER_AGENT = "mexico-open-streetcameras/1.0 (+https://github.com/danielnzlz01/mexico-open-streetcameras)"

OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
]

# Official CDMX CKAN endpoints for Fotocívicas (Ubicación puntos)
CDMX_CKAN_PACKAGE_URLS = [
    "https://datos.cdmx.gob.mx/api/3/action/package_show?id=f5932908-edbd-4983-bbf0-be482b6b8d80",
    "https://datos.cdmx.gob.mx/api/3/action/package_show?id=informacion-fotocivicas",
]
CDMX_DIRECT_RESOURCE_URLS = [
    "https://datos.cdmx.gob.mx/dataset/f5932908-edbd-4983-bbf0-be482b6b8d80/resource/0af26679-e5fc-4cad-aa7c-7d4884ef6a23/download",
]

# Implicit OSM speed limits for Mexico (km/h)
MX_IMPLICIT_MAXSPEED = {
    "mx:urban": 50,
    "mx:rural": 80,
    "mx:trunk": 90,
    "mx:motorway": 110,
    "mx:living_street": 20,
}

CARDINAL_TO_DEG = {
    "N": 0, "NNE": 22, "NE": 45, "ENE": 67,
    "E": 90, "ESE": 112, "SE": 135, "SSE": 157,
    "S": 180, "SSW": 202, "SW": 225, "WSW": 247,
    "W": 270, "WNW": 292, "NW": 315, "NNW": 337,
}

# Approximate state/region bounding boxes for summary reporting (CDMX checked first)
MX_REGION_BOXES: list[tuple[str, float, float, float, float]] = [
    ("Ciudad de México (CDMX)", CDMX_LAT_MIN, CDMX_LAT_MAX, CDMX_LON_MIN, CDMX_LON_MAX),
    ("Estado de México", 18.36, 20.29, -100.61, -98.59),
    ("Morelos", 18.33, 19.13, -99.49, -98.63),
    ("Puebla", 17.86, 20.84, -99.07, -96.72),
    ("Tlaxcala", 19.10, 19.73, -98.71, -97.62),
    ("Hidalgo", 19.60, 21.40, -99.86, -97.98),
    ("Querétaro", 20.01, 21.67, -100.59, -99.04),
    ("Guanajuato", 19.91, 21.84, -102.10, -99.67),
    ("Aguascalientes", 21.62, 22.46, -102.87, -101.83),
    ("Jalisco", 18.92, 22.75, -105.70, -101.51),
    ("Nuevo León", 23.16, 27.80, -101.21, -98.42),
    ("Coahuila", 24.54, 29.88, -103.96, -99.84),
    ("Tamaulipas", 22.20, 27.68, -100.14, -97.14),
    ("San Luis Potosí", 21.16, 24.49, -102.29, -98.33),
    ("Veracruz", 17.13, 22.47, -98.68, -93.60),
    ("Michoacán", 17.91, 20.39, -103.74, -100.06),
    ("Baja California", 28.00, 32.72, -117.13, -112.76),
    ("Baja California Sur", 22.87, 28.00, -115.22, -109.41),
    ("Sonora", 26.29, 32.50, -115.05, -108.42),
    ("Chihuahua", 25.56, 31.79, -109.08, -103.30),
    ("Sinaloa", 22.47, 27.04, -109.45, -105.39),
    ("Durango", 22.34, 26.84, -107.21, -102.47),
    ("Zacatecas", 21.04, 25.13, -104.35, -100.74),
    ("Nayarit", 20.60, 23.09, -105.76, -103.72),
    ("Colima", 18.65, 19.52, -104.70, -103.48),
    ("Guerrero", 16.31, 18.89, -102.18, -98.00),
    ("Oaxaca", 15.65, 18.67, -98.55, -93.86),
    ("Chiapas", 14.53, 17.99, -94.14, -90.37),
    ("Tabasco", 17.25, 18.65, -94.13, -90.99),
    ("Campeche", 17.81, 20.85, -92.47, -89.12),
    ("Yucatán", 19.55, 21.63, -90.41, -87.53),
    ("Quintana Roo", 17.89, 21.60, -89.30, -86.71),
]

VALID_CAMERA_TYPES = {"fixed_speed", "redlight", "redlight_speed", "average_speed"}


@dataclass
class CameraPoint:
    """Normalized representation of a speed or red-light camera."""

    lat: float
    lon: float
    camera_type: str  # fixed_speed | redlight | redlight_speed | average_speed
    maxspeed_kmh: int | None = None
    direction_deg: int | None = None
    road_name: str = ""
    source: str = "osm"
    osm_id: str = ""


def is_valid_mexico_coord(lat: Any, lon: Any) -> bool:
    """Return True if (lat, lon) are finite floats strictly within Mexico's bounding box."""
    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except (TypeError, ValueError):
        return False
    if not (math.isfinite(lat_f) and math.isfinite(lon_f)):
        return False
    if lat_f == 0.0 or lon_f == 0.0:
        return False
    return (MX_LAT_MIN <= lat_f <= MX_LAT_MAX) and (MX_LON_MIN <= lon_f <= MX_LON_MAX)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters between two WGS84 coordinates."""
    earth_radius_m = 6_371_000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2.0) ** 2
    return 2.0 * earth_radius_m * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
    """Initial compass bearing in degrees [0, 359] from point 1 to point 2."""
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return int(round((math.degrees(math.atan2(x, y)) + 360.0) % 360.0)) % 360


def parse_maxspeed(raw: Any) -> int | None:
    """Parse OSM/official maxspeed strings into an integer km/h in [10, 150]."""
    if raw is None:
        return None
    text = str(raw).strip().lower()
    if not text or text in {"none", "nan", "null", "signals", "variable", "unknown"}:
        return None

    if text in MX_IMPLICIT_MAXSPEED:
        return MX_IMPLICIT_MAXSPEED[text]

    # Handle semicolon-separated multi-lane values (e.g. "50;80" -> pick lowest conservative limit)
    if ";" in text:
        candidates = [parse_maxspeed(part) for part in text.split(";")]
        valid = [c for c in candidates if c is not None]
        return min(valid) if valid else None

    is_mph = "mph" in text
    match = re.search(r"(\d+(?:\.\d+)?)", text)
    if not match:
        return None
    val = float(match.group(1))
    if is_mph:
        val = val * 1.60934
    speed = int(round(val))
    if 10 <= speed <= 150:
        return speed
    return None


def parse_direction(raw: Any) -> int | None:
    """Parse numeric bearings, compass cardinals, or Spanish CDMX sentido strings."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None

    upper = re.sub(r"\s+", " ", text.upper())
    if upper in CARDINAL_TO_DEG:
        return CARDINAL_TO_DEG[upper]

    # Spanish CDMX Fotocívicas sentido strings
    sentido_map = {
        "NORTE A SUR": 180,
        "SUR A NORTE": 0,
        "ORIENTE A PONIENTE": 270,
        "PONIENTE A ORIENTE": 90,
        "ESTE A OESTE": 270,
        "OESTE A ESTE": 90,
    }
    if upper in sentido_map:
        return sentido_map[upper]

    # Numeric degrees (avoid treating latitude/longitude floats like 19.473673 as direction
    # if clearly out of normal integer-bearing style? Wait: 0..360 numeric is valid in OSM).
    m = re.match(r"^(\d+(?:\.\d+)?)$", upper)
    if m:
        val = float(m.group(1))
        if 0.0 <= val <= 360.0:
            return int(round(val)) % 360
    return None


def build_overpass_query(timeout_sec: int = 90) -> str:
    """Build Overpass QL query for all traffic enforcement cameras in Mexico."""
    return f"""
[out:json][timeout:{timeout_sec}];
area["ISO3166-1"="MX"]["admin_level"="2"]->.mx;
(
  node["highway"="speed_camera"](area.mx);
  node["enforcement"="maxspeed"](area.mx);
  node["enforcement"="traffic_signals"](area.mx);
  node["enforcement"="average_speed"](area.mx);
  relation["type"="enforcement"]["enforcement"~"^(maxspeed|traffic_signals|average_speed)$"](area.mx);
  relation["type"="enforcement"][!"enforcement"](area.mx);
)->.cams;
(.cams; .cams >;)->.camnodes;
way(bn.camnodes)["highway"]->.parentways;
(
  .cams;
  .camnodes;
  .parentways;
);
out body;
>;
out skel qt;
""".strip()


def fetch_overpass_json(
    mirrors: list[str] | None = None,
    timeout_sec: int = 90,
    max_rounds: int = 3,
) -> dict[str, Any]:
    """Query Overpass API across mirrors with retry and exponential backoff."""
    endpoints = mirrors or OVERPASS_MIRRORS
    ql = build_overpass_query(timeout_sec=timeout_sec)
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    last_err: Exception | None = None
    for round_idx in range(1, max_rounds + 1):
        for url in endpoints:
            try:
                LOGGER.info("Querying Overpass mirror %s (round %d/%d)...", url, round_idx, max_rounds)
                resp = session.post(
                    url,
                    data={"data": ql},
                    timeout=(10, timeout_sec + 25),
                )
                if resp.status_code == 200:
                    payload = resp.json()
                    if "elements" in payload:
                        LOGGER.info(
                            "Overpass mirror %s succeeded (%d elements).",
                            url,
                            len(payload["elements"]),
                        )
                        return payload
                    raise ValueError("Overpass JSON response missing 'elements' key")
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:160].strip()}")
            except Exception as exc:
                last_err = exc
                LOGGER.warning("Overpass mirror %s failed (round %d): %s", url, round_idx, exc)

        if round_idx < max_rounds:
            backoff = 5 * (2 ** (round_idx - 1))
            LOGGER.info("Retrying Overpass mirrors after %ds backoff...", backoff)
            time.sleep(backoff)

    raise RuntimeError(f"All Overpass mirrors failed after {max_rounds} rounds: {last_err}")


def _classify_osm_camera_type(node_tags: dict[str, str], rel_tags: dict[str, str]) -> str | None:
    """Determine normalized camera type from OSM node and parent relation tags."""
    enf_vals = {
        (node_tags.get("enforcement") or "").strip().lower(),
        (rel_tags.get("enforcement") or "").strip().lower(),
    } - {""}

    # Ignore non-traffic surveillance relations/nodes unless explicitly a speed camera
    is_speed_cam_node = (node_tags.get("highway") or "").strip().lower() == "speed_camera"
    if enf_vals and enf_vals.issubset({"surveillance", "toll", "access", "weight", "height"}):
        if not is_speed_cam_node:
            return None

    has_avg = "average_speed" in enf_vals
    has_redlight = "traffic_signals" in enf_vals or "red_light" in enf_vals
    has_maxspeed = "maxspeed" in enf_vals or is_speed_cam_node

    if has_avg:
        return "average_speed"
    if has_redlight and (has_maxspeed and ("maxspeed" in enf_vals or node_tags.get("maxspeed") or rel_tags.get("maxspeed"))):
        return "redlight_speed"
    if has_redlight:
        return "redlight"
    if has_maxspeed:
        return "fixed_speed"
    return None


def extract_osm_cameras(raw: dict[str, Any]) -> list[CameraPoint]:
    """Extract normalized CameraPoint records from an Overpass JSON response."""
    elements = raw.get("elements", [])
    nodes_by_id: dict[int, dict[str, Any]] = {}
    ways_by_node: dict[int, list[dict[str, Any]]] = {}
    relations: list[dict[str, Any]] = []

    for el in elements:
        etype = el.get("type")
        if etype == "node":
            existing = nodes_by_id.get(el["id"])
            if existing is None or (el.get("tags") and not existing.get("tags")):
                nodes_by_id[el["id"]] = el
        elif etype == "way":
            if "highway" in el.get("tags", {}):
                for nid in el.get("nodes", []):
                    ways_by_node.setdefault(nid, []).append(el)
        elif etype == "relation":
            relations.append(el)

    # Index parent enforcement relations -> device nodes (with inherited tags and heading)
    rel_info_by_node: dict[int, dict[str, Any]] = {}
    for rel in relations:
        rtags = rel.get("tags", {})
        rel_enf = (rtags.get("enforcement") or "").strip().lower()
        if rel_enf and rel_enf not in {"maxspeed", "traffic_signals", "average_speed", "red_light"}:
            continue

        from_node = None
        to_node = None
        device_ids: list[int] = []
        for mem in rel.get("members", []):
            if mem.get("type") != "node":
                continue
            role = (mem.get("role") or "").strip().lower()
            ref = mem.get("ref")
            if role == "from":
                from_node = nodes_by_id.get(ref)
            elif role == "to":
                to_node = nodes_by_id.get(ref)
            elif role == "device":
                device_ids.append(ref)

        # If untagged relation, keep only device nodes that explicitly carry camera tags
        if not rel_enf:
            device_ids = [
                did for did in device_ids
                if (nodes_by_id.get(did, {}).get("tags", {}).get("highway") == "speed_camera")
                or (nodes_by_id.get(did, {}).get("tags", {}).get("enforcement") in {"maxspeed", "traffic_signals", "average_speed"})
            ]
        elif not device_ids and from_node is not None:
            device_ids = [from_node["id"]]

        rel_heading: int | None = None
        if from_node and to_node and is_valid_mexico_coord(from_node.get("lat"), from_node.get("lon")) and is_valid_mexico_coord(to_node.get("lat"), to_node.get("lon")):
            rel_heading = bearing_deg(
                float(from_node["lat"]),
                float(from_node["lon"]),
                float(to_node["lat"]),
                float(to_node["lon"]),
            )

        for did in device_ids:
            entry = rel_info_by_node.setdefault(did, {"tags": {}, "heading": None, "from_id": None})
            entry["tags"].update(rtags)
            if rel_heading is not None:
                entry["heading"] = rel_heading
            if from_node is not None:
                entry["from_id"] = from_node["id"]

    cameras: list[CameraPoint] = []
    for nid, node in nodes_by_id.items():
        ntags = node.get("tags", {})
        rinfo = rel_info_by_node.get(nid)
        rtags = rinfo["tags"] if rinfo else {}

        is_direct_camera = (
            ntags.get("highway") == "speed_camera"
            or ntags.get("enforcement") in {"maxspeed", "traffic_signals", "average_speed"}
        )
        if not is_direct_camera and not rinfo:
            continue

        cam_type = _classify_osm_camera_type(ntags, rtags)
        if cam_type is None:
            continue

        lat = node.get("lat")
        lon = node.get("lon")
        if not is_valid_mexico_coord(lat, lon):
            continue

        # Look up parent highway way (directly or via relation's `from` node)
        way_anchor = nid
        parent_ways = ways_by_node.get(nid, [])
        if not parent_ways and rinfo and rinfo.get("from_id") is not None:
            way_anchor = rinfo["from_id"]
            parent_ways = ways_by_node.get(way_anchor, [])
        parent_way = parent_ways[0] if parent_ways else None
        wtags = parent_way.get("tags", {}) if parent_way else {}

        # Priority: camera node maxspeed -> relation maxspeed -> parent way maxspeed (for speed cams)
        speed = parse_maxspeed(ntags.get("maxspeed")) or parse_maxspeed(rtags.get("maxspeed"))
        if speed is None and cam_type in {"fixed_speed", "average_speed", "redlight_speed"}:
            speed = parse_maxspeed(wtags.get("maxspeed"))

        direction = (
            parse_direction(ntags.get("direction"))
            or parse_direction(rtags.get("direction"))
            or (rinfo["heading"] if rinfo else None)
        )
        road_name = (
            ntags.get("name")
            or rtags.get("name")
            or wtags.get("name")
            or wtags.get("ref")
            or ""
        ).strip()

        cameras.append(
            CameraPoint(
                lat=round(float(lat), 6),
                lon=round(float(lon), 6),
                camera_type=cam_type,
                maxspeed_kmh=speed,
                direction_deg=direction,
                road_name=road_name,
                source="osm",
                osm_id=f"node/{nid}",
            )
        )

    return cameras


def _classify_fotocivicas_type(row: dict[str, str]) -> str:
    """Map CDMX Fotocívicas metadata to normalized camera_type."""
    explicit_type = " ".join(
        row.get(k, "")
        for k in ("tipo", "tipo_equipo", "tipo_camara", "tipo_dispositivo", "clasificacion", "infraccion")
    ).lower()

    if explicit_type.strip():
        is_speed = any(tok in explicit_type for tok in ("radar", "velocidad", "fotorradar"))
        is_redlight = any(
            tok in explicit_type
            for tok in ("semaforo", "semáforo", "luz roja", "cruce", "crucero", "carril", "confinado", "vuelta")
        )
        if is_speed and is_redlight:
            return "redlight_speed"
        if is_redlight:
            return "redlight"
        if is_speed:
            return "fixed_speed"

    # Official SEMOVI 'Fotocivicas (Ubicación puntos)' schema (ubi, no, via_princi, ubicacion, sentido, y, x):
    #   ubi 47..99  -> speed radars on expressways & primary corridors (Periférico, AUN, Viaducto, Circuito, etc.)
    #   ubi 1..46 & >=100 -> intersection / red-light / confined-lane cameras
    ubi_raw = row.get("ubi", "")
    try:
        ubi_num = int(round(float(ubi_raw))) if ubi_raw else 0
    except ValueError:
        ubi_num = 0

    if 47 <= ubi_num <= 99:
        return "fixed_speed"
    if ubi_num > 0:
        return "redlight"

    # Fallback heuristic based on road name if `ubi` is absent
    road_text = f"{row.get('via_princi', '')} {row.get('ubicacion', '')}".lower()
    if any(tok in road_text for tok in ("perif", "viaducto", "autopista urbana", "aun", "circuito bicentenario")):
        return "fixed_speed"
    return "redlight"


def _extract_coords_from_fotocivicas_row(row: dict[str, str]) -> tuple[float | None, float | None]:
    """Extract (lat, lon) from CDMX Fotocívicas CSV row or embedded geo_shape JSON."""
    lat_s = row.get("latitud") or row.get("lat") or row.get("y") or ""
    lon_s = row.get("longitud") or row.get("lon") or row.get("lng") or row.get("x") or ""

    if lat_s and lon_s:
        try:
            return float(lat_s.replace(",", ".")), float(lon_s.replace(",", "."))
        except ValueError:
            pass

    geo_shape = row.get("geo_shape") or row.get("geo_point_2d") or ""
    if geo_shape:
        if geo_shape.startswith("{"):
            try:
                geom = json.loads(geo_shape)
                coords = geom.get("coordinates")
                if isinstance(coords, list) and len(coords) >= 2:
                    return float(coords[1]), float(coords[0])
            except (ValueError, TypeError):
                pass
        elif "," in geo_shape:
            parts = [p.strip() for p in geo_shape.split(",")]
            if len(parts) == 2:
                try:
                    return float(parts[0]), float(parts[1])
                except ValueError:
                    pass
    return None, None


def parse_fotocivicas_csv(text: str) -> list[CameraPoint]:
    """Parse CDMX Fotocívicas CSV content into CameraPoint records."""
    cleaned = text.lstrip("\ufeff")
    if not cleaned.strip():
        return []

    sample = cleaned[:2048]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;")
    except csv.Error:
        dialect = csv.excel

    reader = csv.DictReader(io.StringIO(cleaned), dialect=dialect)
    cameras: list[CameraPoint] = []
    for raw_row in reader:
        row = {
            (k or "").strip().lower(): (v or "").strip()
            for k, v in raw_row.items()
            if k is not None
        }
        lat, lon = _extract_coords_from_fotocivicas_row(row)
        if not is_valid_mexico_coord(lat, lon):
            continue

        # Handle known shifted-column anomaly in official CDMX file (ubi=46: via_princi holds sentido)
        via = row.get("via_princi") or row.get("vialidad") or ""
        ubic = row.get("ubicacion") or ""
        sentido = row.get("sentido") or row.get("direccion") or ""
        if parse_direction(via) is not None and parse_direction(sentido) is None:
            sentido = via
            via = ubic

        road_name = (ubic or via).strip()
        speed = parse_maxspeed(
            row.get("maxspeed_kmh")
            or row.get("velocidad")
            or row.get("limite_velocidad")
            or row.get("maxspeed")
        )
        direction = parse_direction(sentido)
        cam_type = _classify_fotocivicas_type(row)

        cameras.append(
            CameraPoint(
                lat=round(float(lat), 6),
                lon=round(float(lon), 6),
                camera_type=cam_type,
                maxspeed_kmh=speed,
                direction_deg=direction,
                road_name=road_name,
                source="cdmx_fotocivicas",
                osm_id="",
            )
        )
    return cameras


def parse_fotocivicas_geojson(payload: dict[str, Any]) -> list[CameraPoint]:
    """Parse CDMX Fotocívicas GeoJSON FeatureCollection into CameraPoint records."""
    cameras: list[CameraPoint] = []
    for feat in payload.get("features", []):
        geom = feat.get("geometry") or {}
        if geom.get("type") != "Point":
            continue
        coords = geom.get("coordinates") or []
        if len(coords) < 2:
            continue
        lon, lat = coords[0], coords[1]
        if not is_valid_mexico_coord(lat, lon):
            continue
        props = {
            str(k).strip().lower(): "" if v is None else str(v).strip()
            for k, v in (feat.get("properties") or {}).items()
        }
        road_name = (props.get("ubicacion") or props.get("via_princi") or "").strip()
        speed = parse_maxspeed(
            props.get("maxspeed_kmh")
            or props.get("velocidad")
            or props.get("limite_velocidad")
            or props.get("maxspeed")
        )
        direction = parse_direction(props.get("sentido") or props.get("direccion"))
        cam_type = _classify_fotocivicas_type(props)
        cameras.append(
            CameraPoint(
                lat=round(float(lat), 6),
                lon=round(float(lon), 6),
                camera_type=cam_type,
                maxspeed_kmh=speed,
                direction_deg=direction,
                road_name=road_name,
                source="cdmx_fotocivicas",
                osm_id="",
            )
        )
    return cameras


def fetch_cdmx_fotocivicas(
    cache_path: Path | None = None,
    skip_live: bool = False,
) -> tuple[list[CameraPoint], str]:
    """
    Fetch official CDMX Fotocívicas points from datos.cdmx.gob.mx.

    Returns (cameras, status_label) where status_label is one of:
      - "live" (fetched directly from datos.cdmx.gob.mx)
      - "cache" (fell back cleanly to local cached CSV after warning)
      - "unavailable" (portal unreachable and no cache available)
    """
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    if not skip_live:
        candidate_urls: list[tuple[str, str]] = []  # (url, format_hint)

        # 1. Discover live resource URLs via CKAN API
        for api_url in CDMX_CKAN_PACKAGE_URLS:
            try:
                resp = session.get(api_url, timeout=(8, 15))
                if resp.status_code == 200:
                    data = resp.json()
                    resources = data.get("result", {}).get("resources", [])
                    for res in resources:
                        fmt = (res.get("format") or "").strip().lower()
                        url = (res.get("url") or "").strip()
                        name = (res.get("name") or "").lower()
                        if url and fmt in {"csv", "geojson", "json"}:
                            # Prefer location / ubicacion resources first
                            if "ubicaci" in name or "punto" in name or "fotocivic" in name:
                                candidate_urls.insert(0, (url, fmt))
                            else:
                                candidate_urls.append((url, fmt))
            except Exception as exc:
                LOGGER.warning("CDMX CKAN metadata lookup failed (%s): %s", api_url, exc)

        # 2. Always include known direct CSV resource URL as fallback
        for direct_url in CDMX_DIRECT_RESOURCE_URLS:
            if all(u != direct_url for u, _ in candidate_urls):
                candidate_urls.append((direct_url, "csv"))

        # 3. Try downloading from discovered/direct URLs
        for url, fmt in candidate_urls:
            try:
                LOGGER.info("Fetching CDMX Fotocívicas resource: %s", url)
                resp = session.get(url, timeout=(8, 25))
                resp.raise_for_status()
                content_type = (resp.headers.get("Content-Type") or "").lower()
                if fmt in {"geojson", "json"} or "json" in content_type:
                    cameras = parse_fotocivicas_geojson(resp.json())
                else:
                    # Decode UTF-8 with latin-1 fallback for older government CSV exports
                    try:
                        text = resp.content.decode("utf-8-sig")
                    except UnicodeDecodeError:
                        text = resp.content.decode("latin-1")
                    cameras = parse_fotocivicas_csv(text)
                if cameras:
                    LOGGER.info("Loaded %d CDMX Fotocívicas points live from %s", len(cameras), url)
                    return cameras, "live"
                LOGGER.warning("CDMX resource %s returned 0 valid camera points", url)
            except Exception as exc:
                LOGGER.warning("Failed to download CDMX Fotocívicas resource %s: %s", url, exc)

        LOGGER.warning(
            "CDMX open data portal (datos.cdmx.gob.mx) is unreachable or returned no data; "
            "falling back to local cache if present."
        )

    if cache_path and cache_path.exists():
        text = cache_path.read_text(encoding="utf-8-sig")
        cameras = parse_fotocivicas_csv(text)
        LOGGER.warning(
            "Using local cached CDMX Fotocívicas dataset (%s): %d points.",
            cache_path,
            len(cameras),
        )
        return cameras, "cache"

    LOGGER.warning("No local CDMX Fotocívicas cache found; continuing with OSM-only data.")
    return [], "unavailable"


def _merge_camera_types(t1: str, t2: str) -> str:
    """Combine two camera types preserving the richest enforcement semantics."""
    if t1 == t2:
        return t1
    pair = {t1, t2}
    if "average_speed" in pair:
        return "average_speed"
    if "redlight_speed" in pair or pair == {"fixed_speed", "redlight"}:
        return "redlight_speed"
    return t1


def _metadata_richness(cam: CameraPoint) -> tuple[int, int, int, int, int]:
    """Higher tuple means richer metadata (preferred as cluster anchor)."""
    return (
        1 if cam.maxspeed_kmh is not None else 0,
        1 if cam.camera_type in {"redlight_speed", "average_speed"} else 0,
        1 if cam.source == "osm" else 0,
        1 if cam.direction_deg is not None else 0,
        1 if bool(cam.road_name) else 0,
    )


def deduplicate_cameras(
    cameras: Iterable[CameraPoint],
    radius_m: float = 25.0,
) -> list[CameraPoint]:
    """
    Deduplicate camera points within `radius_m` meters using Haversine distance.

    When merging duplicates, preserves the richest metadata (known maxspeed,
    combined camera type, heading, road name, and merged source attribution).
    Guarantees every pair of returned points is strictly > `radius_m` meters apart.
    """
    # Filter invalid coordinates and process richest points first deterministically
    valid = [c for c in cameras if is_valid_mexico_coord(c.lat, c.lon)]
    valid.sort(
        key=lambda c: (
            -_metadata_richness(c)[0],
            -_metadata_richness(c)[1],
            -_metadata_richness(c)[2],
            -_metadata_richness(c)[3],
            -_metadata_richness(c)[4],
            c.lat,
            c.lon,
            c.camera_type,
        )
    )

    # Spatial grid cell size (~0.0005 deg ≈ 55m > 25m) guarantees checking 3x3 neighbors finds all pairs <= 25m
    cell_deg = max(0.0005, (radius_m / 111_000.0) * 1.5)
    grid: dict[tuple[int, int], list[CameraPoint]] = {}
    kept: list[CameraPoint] = []

    for cam in valid:
        gx = int(math.floor(cam.lat / cell_deg))
        gy = int(math.floor(cam.lon / cell_deg))

        nearest: CameraPoint | None = None
        nearest_dist = radius_m + 1.0
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for existing in grid.get((gx + dx, gy + dy), ()):
                    dist = haversine_m(cam.lat, cam.lon, existing.lat, existing.lon)
                    if dist <= radius_m and dist < nearest_dist:
                        nearest = existing
                        nearest_dist = dist

        if nearest is not None:
            # Merge richer metadata into existing kept record
            if nearest.maxspeed_kmh is None and cam.maxspeed_kmh is not None:
                nearest.maxspeed_kmh = cam.maxspeed_kmh
            nearest.camera_type = _merge_camera_types(nearest.camera_type, cam.camera_type)
            if nearest.direction_deg is None and cam.direction_deg is not None:
                nearest.direction_deg = cam.direction_deg
            if not nearest.road_name and cam.road_name:
                nearest.road_name = cam.road_name
            if not nearest.osm_id and cam.osm_id:
                nearest.osm_id = cam.osm_id
            existing_sources = set(nearest.source.split("+"))
            existing_sources.update(cam.source.split("+"))
            nearest.source = "+".join(sorted(existing_sources))
            continue

        cloned = CameraPoint(
            lat=round(cam.lat, 6),
            lon=round(cam.lon, 6),
            camera_type=cam.camera_type if cam.camera_type in VALID_CAMERA_TYPES else "fixed_speed",
            maxspeed_kmh=cam.maxspeed_kmh,
            direction_deg=cam.direction_deg,
            road_name=cam.road_name,
            source=cam.source,
            osm_id=cam.osm_id,
        )
        kept.append(cloned)
        grid.setdefault((gx, gy), []).append(cloned)

    # Deterministic sort for minimal git diffs across weekly updates
    kept.sort(key=lambda c: (c.lat, c.lon, c.camera_type, c.maxspeed_kmh or 0))
    return kept


def format_gatso_fields(cam: CameraPoint) -> tuple[str, str]:
    """
    Format (name, comment) columns for GATSO_ALL.csv matching Open-GATSO-POI & Open Zeekr.

    Open Zeekr's CameraStore.java parses:
      lon,lat,"name","comment"
    and extracts speed limits via regex `@\\s*(\\d{2,3})` from everything after the 2nd comma.
    """
    speed = cam.maxspeed_kmh
    if cam.camera_type == "average_speed":
        name = f"average @{speed}" if speed is not None else "average"
    elif cam.camera_type == "redlight":
        name = f"max @{speed}" if speed is not None else "stop"
    elif cam.camera_type == "redlight_speed":
        name = f"max @{speed}" if speed is not None else "stop"
    else:
        name = f"max @{speed}" if speed is not None else "max"

    comment = f"MX Radar {cam.camera_type}"
    return name, comment


def _escape_csv_text(value: str) -> str:
    """RFC 4180 double-quoted CSV text field with formula-injection protection (matches Open-GATSO-POI)."""
    clean = value.replace("\r", " ").replace("\n", " ").strip()
    if clean and clean[0] in ("=", "+", "-", "@", "\t"):
        clean = "'" + clean
    return '"' + clean.replace('"', '""') + '"'


def write_gatso_csv(cameras: Iterable[CameraPoint], path: Path) -> int:
    """Write GATSO_ALL.csv in exact Garmin-POI / Open-GATSO-POI format (UTF-8, LF, no header)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="") as f:
        for cam in cameras:
            name, comment = format_gatso_fields(cam)
            lon_s = f"{cam.lon:.6f}".rstrip("0").rstrip(".")
            lat_s = f"{cam.lat:.6f}".rstrip("0").rstrip(".")
            f.write(f"{lon_s},{lat_s},{_escape_csv_text(name)},{_escape_csv_text(comment)}\n")
            count += 1
    return count


def write_enriched_csv(cameras: Iterable[CameraPoint], path: Path) -> int:
    """Write companion mx_speed_cameras.csv with full metadata (mirrors br_speed_cameras.csv)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["osm_id", "lat", "lon", "maxspeed_kmh", "direction_deg", "type", "road_name", "source"]
    count = 0
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for cam in cameras:
            writer.writerow(
                {
                    "osm_id": cam.osm_id,
                    "lat": f"{cam.lat:.6f}",
                    "lon": f"{cam.lon:.6f}",
                    "maxspeed_kmh": "" if cam.maxspeed_kmh is None else cam.maxspeed_kmh,
                    "direction_deg": "" if cam.direction_deg is None else cam.direction_deg,
                    "type": cam.camera_type,
                    "road_name": cam.road_name,
                    "source": cam.source,
                }
            )
            count += 1
    return count


def write_release_zip(
    zip_path: Path,
    gatso_path: Path,
    enriched_path: Path,
    attribution_path: Path | None = None,
) -> None:
    """Package csv_files.zip matching borconi/openzeekr GatsoUpdater.java expectations."""
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(gatso_path, arcname="GATSO_ALL.csv")
        zf.write(enriched_path, arcname="mx_speed_cameras.csv")
        if attribution_path and attribution_path.exists():
            zf.write(attribution_path, arcname="ATTRIBUTION.md")


def infer_region(lat: float, lon: float) -> str:
    """Assign an approximate Mexican state/region name for summary reporting."""
    for name, lat_min, lat_max, lon_min, lon_max in MX_REGION_BOXES:
        if lat_min <= lat <= lat_max and lon_min <= lon <= lon_max:
            return name
    return "Other Mexico"


def build_summary(
    osm_raw_count: int,
    cdmx_raw_count: int,
    cdmx_status: str,
    final_cameras: list[CameraPoint],
) -> dict[str, Any]:
    """Compute structured build metrics for logging and CI summaries."""
    by_type = Counter(c.camera_type for c in final_cameras)
    by_source = Counter(c.source for c in final_cameras)
    by_region = Counter(infer_region(c.lat, c.lon) for c in final_cameras)
    with_speed = sum(1 for c in final_cameras if c.maxspeed_kmh is not None)
    cdmx_bbox_count = sum(
        1
        for c in final_cameras
        if CDMX_LAT_MIN <= c.lat <= CDMX_LAT_MAX and CDMX_LON_MIN <= c.lon <= CDMX_LON_MAX
    )
    return {
        "osm_extracted": osm_raw_count,
        "cdmx_fotocivicas_extracted": cdmx_raw_count,
        "cdmx_fotocivicas_status": cdmx_status,
        "final_deduplicated_count": len(final_cameras),
        "with_known_maxspeed": with_speed,
        "cdmx_bbox_count": cdmx_bbox_count,
        "by_type": dict(by_type.most_common()),
        "by_source": dict(by_source.most_common()),
        "top_regions": by_region.most_common(12),
    }


def print_summary(summary: dict[str, Any]) -> None:
    """Print a human-readable pipeline summary to stdout."""
    print("\n=== Mexico Speed & Red-Light Camera Feed Summary ===")
    print(f"  OSM extracted (Mexico)        : {summary['osm_extracted']}")
    print(
        f"  CDMX Fotocívicas extracted    : {summary['cdmx_fotocivicas_extracted']} "
        f"(status: {summary['cdmx_fotocivicas_status']})"
    )
    print(f"  Final deduplicated count      : {summary['final_deduplicated_count']}")
    print(f"  Points with known maxspeed    : {summary['with_known_maxspeed']}")
    print(f"  Points inside CDMX bbox       : {summary['cdmx_bbox_count']}")
    print("  Breakdown by camera type:")
    for ctype, cnt in summary["by_type"].items():
        print(f"    - {ctype:<16}: {cnt}")
    print("  Breakdown by merged source:")
    for src, cnt in summary["by_source"].items():
        print(f"    - {src:<24}: {cnt}")
    print("  Top states / regions (approx. bbox):")
    for region, cnt in summary["top_regions"]:
        print(f"    - {region:<26}: {cnt}")


def main(argv: list[str] | None = None) -> int:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description="Build Mexico GATSO_ALL.csv speed & red-light camera feed for Open Zeekr."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=repo_root / "GATSO_ALL.csv",
        help="Path to output GATSO_ALL.csv (default: ./GATSO_ALL.csv)",
    )
    parser.add_argument(
        "--dist-dir",
        type=Path,
        default=None,
        help="Optional directory to also write csv_files.zip, mx_speed_cameras.csv, and GATSO_ALL.csv",
    )
    parser.add_argument(
        "--cdmx-cache",
        type=Path,
        default=repo_root / "data" / "cdmx_fotocivicas_cache.csv",
        help="Fallback local CDMX Fotocívicas CSV cache",
    )
    parser.add_argument(
        "--skip-live-cdmx",
        action="store_true",
        help="Skip live datos.cdmx.gob.mx request and use the local cache directly",
    )
    parser.add_argument(
        "--dedupe-radius-m",
        type=float,
        default=25.0,
        help="Haversine deduplication radius in meters (default: 25.0)",
    )
    parser.add_argument(
        "--summary-json",
        type=Path,
        default=None,
        help="Optional path to write JSON build summary",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    # Source A: OpenStreetMap nationwide Mexico
    osm_raw = fetch_overpass_json()
    osm_cameras = extract_osm_cameras(osm_raw)
    LOGGER.info("Extracted %d valid camera points from OpenStreetMap.", len(osm_cameras))

    # Source B: CDMX Fotocívicas (with resilient cache fallback)
    cdmx_cameras, cdmx_status = fetch_cdmx_fotocivicas(
        cache_path=args.cdmx_cache,
        skip_live=args.skip_live_cdmx,
    )

    # Merge & deduplicate
    combined = osm_cameras + cdmx_cameras
    deduped = deduplicate_cameras(combined, radius_m=args.dedupe_radius_m)

    # Emit primary GATSO_ALL.csv
    written = write_gatso_csv(deduped, args.output)
    LOGGER.info("Wrote %d rows to %s", written, args.output)

    # Optionally emit release bundle (dist/csv_files.zip + mx_speed_cameras.csv + GATSO_ALL.csv)
    if args.dist_dir is not None:
        args.dist_dir.mkdir(parents=True, exist_ok=True)
        dist_gatso = args.dist_dir / "GATSO_ALL.csv"
        if dist_gatso.resolve() != args.output.resolve():
            write_gatso_csv(deduped, dist_gatso)
        enriched_path = args.dist_dir / "mx_speed_cameras.csv"
        write_enriched_csv(deduped, enriched_path)
        zip_path = args.dist_dir / "csv_files.zip"
        write_release_zip(
            zip_path=zip_path,
            gatso_path=dist_gatso,
            enriched_path=enriched_path,
            attribution_path=repo_root / "ATTRIBUTION.md",
        )
        LOGGER.info("Packaged release bundle: %s", zip_path)

    summary = build_summary(
        osm_raw_count=len(osm_cameras),
        cdmx_raw_count=len(cdmx_cameras),
        cdmx_status=cdmx_status,
        final_cameras=deduped,
    )
    print_summary(summary)

    if args.summary_json is not None:
        args.summary_json.parent.mkdir(parents=True, exist_ok=True)
        args.summary_json.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

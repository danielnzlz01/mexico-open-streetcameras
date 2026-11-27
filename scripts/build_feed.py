#!/usr/bin/env python3
"""
Build an automated Mexico speed & red-light camera feed (GATSO_ALL.csv) for Open Zeekr.

Data sources & precedence (highest -> lowest):
  1. Curated manual override/suppression layer (data/manual_mx.csv)
  2. Official CDMX Open Data — Fotocívicas (datos.cdmx.gob.mx, Libre Uso MX)
     with automatic fallback to a committed local snapshot when unreachable.
  3. OpenStreetMap nationwide Mexico (Overpass API, ODbL 1.0)
     including speed_camera, maxspeed, traffic_signals, average_speed, and
     enforcement relations (including man_made=surveillance device nodes).

Secondary enrichment (metadata-only; never inserts standalone points):
  - CDMX legacy Fotomultas (Ubicaciones) for backfilling missing maxspeed/type <= 25 m.
  - Batched Overpass nearest-road maxspeed inference (way(around:15)["highway"]["maxspeed"])
    preferring highest road class (motorway > trunk > primary > secondary > tertiary > residential).

Output format (Garmin-POI / Open-GATSO-POI / Open Zeekr):
  longitude,latitude,"name","comment"
  - No header row, UTF-8 (no BOM), LF line endings, comma delimiter.
  - Packaged into csv_files.zip -> GATSO_ALL.csv AND standalone GATSO_ALL.csv.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import math
import re
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

# Key metro/state bounding boxes for reporting & validation
CDMX_LAT_MIN = 19.04
CDMX_LAT_MAX = 19.60
CDMX_LON_MIN = -99.37
CDMX_LON_MAX = -98.94

EDOMEX_LAT_MIN = 18.36
EDOMEX_LAT_MAX = 20.29
EDOMEX_LON_MIN = -100.61
EDOMEX_LON_MAX = -98.59

USER_AGENT = "mexico-open-streetcameras/2.0 (+https://github.com/danielnzlz01/mexico-open-streetcameras)"

OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

# Official CDMX CKAN endpoints for Fotocívicas (Ubicación puntos)
CDMX_CKAN_PACKAGE_URLS = [
    "https://datos.cdmx.gob.mx/api/3/action/package_show?id=f5932908-edbd-4983-bbf0-be482b6b8d80",
    "https://datos.cdmx.gob.mx/api/3/action/package_show?id=informacion-fotocivicas",
]
CDMX_DIRECT_RESOURCE_URLS = [
    "https://datos.cdmx.gob.mx/dataset/f5932908-edbd-4983-bbf0-be482b6b8d80/resource/0af26679-e5fc-4cad-aa7c-7d4884ef6a23/download",
]

# Official CDMX CKAN endpoints for legacy Fotomultas (Ubicaciones) — secondary metadata enrichment only
CDMX_FOTOMULTAS_CKAN_URLS = [
    "https://datos.cdmx.gob.mx/api/3/action/package_show?id=fotomultas-ubicaciones",
    "https://datos.cdmx.gob.mx/api/3/action/package_show?id=fotomultas",
    "https://datos.cdmx.gob.mx/api/3/action/package_search?q=fotomultas",
]

# Implicit OSM speed limits for Mexico (km/h)
MX_IMPLICIT_MAXSPEED = {
    "mx:urban": 50,
    "mx:rural": 80,
    "mx:trunk": 90,
    "mx:motorway": 110,
    "mx:living_street": 20,
}

# Road-class hierarchy for nearest-road maxspeed tie-breaking:
# motorway > trunk > primary > secondary > tertiary > residential
ROAD_CLASS_PRIORITY: dict[str, int] = {
    "motorway": 100,
    "motorway_link": 95,
    "trunk": 90,
    "trunk_link": 85,
    "primary": 80,
    "primary_link": 75,
    "secondary": 70,
    "secondary_link": 65,
    "tertiary": 60,
    "tertiary_link": 55,
    "unclassified": 40,
    "residential": 30,
    "living_street": 20,
    "service": 10,
}

CARDINAL_TO_DEG = {
    "N": 0, "NNE": 22, "NE": 45, "ENE": 67,
    "E": 90, "ESE": 112, "SE": 135, "SSE": 157,
    "S": 180, "SSW": 202, "SW": 225, "WSW": 247,
    "W": 270, "WNW": 292, "NW": 315, "NNW": 337,
}

# Approximate state/region bounding boxes for summary reporting (CDMX checked before Edomex)
MX_REGION_BOXES: list[tuple[str, float, float, float, float]] = [
    ("Ciudad de México (CDMX)", CDMX_LAT_MIN, CDMX_LAT_MAX, CDMX_LON_MIN, CDMX_LON_MAX),
    ("Estado de México", EDOMEX_LAT_MIN, EDOMEX_LAT_MAX, EDOMEX_LON_MIN, EDOMEX_LON_MAX),
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

# Merge precedence: manual_mx.csv (3) > CDMX Fotocívicas (2) > OSM (1)
SOURCE_PRIORITY = {
    "manual": 3,
    "cdmx_fotocivicas": 2,
    "osm": 1,
}


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
    priority: int = 1


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


def point_to_segment_distance_m(
    lat: float,
    lon: float,
    lat1: float,
    lon1: float,
    lat2: float,
    lon2: float,
) -> float:
    """Approximate planar distance in meters from (lat, lon) to segment (lat1, lon1)-(lat2, lon2)."""
    cos_lat = math.cos(math.radians(lat))
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * max(0.1, cos_lat)

    ax = (lon1 - lon) * m_per_deg_lon
    ay = (lat1 - lat) * m_per_deg_lat
    bx = (lon2 - lon) * m_per_deg_lon
    by = (lat2 - lat) * m_per_deg_lat

    dx = bx - ax
    dy = by - ay
    seg_len_sq = dx * dx + dy * dy
    if seg_len_sq <= 1e-12:
        return math.hypot(ax, ay)

    t = max(0.0, min(1.0, -(ax * dx + ay * dy) / seg_len_sq))
    proj_x = ax + t * dx
    proj_y = ay + t * dy
    return math.hypot(proj_x, proj_y)


def point_to_polyline_distance_m(
    lat: float,
    lon: float,
    geometry: list[dict[str, float]],
) -> float:
    """Minimum distance in meters from (lat, lon) to an Overpass way geometry list."""
    if not geometry:
        return float("inf")
    if len(geometry) == 1:
        return haversine_m(lat, lon, float(geometry[0]["lat"]), float(geometry[0]["lon"]))

    best = float("inf")
    for idx in range(len(geometry) - 1):
        p1 = geometry[idx]
        p2 = geometry[idx + 1]
        d = point_to_segment_distance_m(
            lat,
            lon,
            float(p1["lat"]),
            float(p1["lon"]),
            float(p2["lat"]),
            float(p2["lon"]),
        )
        if d < best:
            best = d
    return best


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
    if not text or text in {"none", "nan", "null", "signals", "variable", "unknown", "walk"}:
        return None

    if text in MX_IMPLICIT_MAXSPEED:
        return MX_IMPLICIT_MAXSPEED[text]

    # Ignore conditional/ambiguous multi-value strings unless simple semicolon numeric limits
    if ";" in text:
        candidates = [parse_maxspeed(part) for part in text.split(";")]
        valid = [c for c in candidates if c is not None]
        return min(valid) if valid else None

    # Accept strict numeric formats: "50", "50 km/h", "50 kmh", "50 kph", "50 mph"
    match = re.match(r"^(\d+(?:\.\d+)?)\s*(?:km/?h|kmh|kph|mph)?$", text)
    if not match:
        return None
    val = float(match.group(1))
    if "mph" in text:
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

    m = re.match(r"^(\d+(?:\.\d+)?)$", upper)
    if m:
        val = float(m.group(1))
        if 0.0 <= val <= 360.0:
            return int(round(val)) % 360
    return None


def normalize_manual_type(raw: str | None) -> str:
    """Map manual_mx.csv type values (`speed`, `redlight`, `average_speed`) to internal types."""
    val = (raw or "").strip().lower()
    mapping = {
        "speed": "fixed_speed",
        "fixed_speed": "fixed_speed",
        "maxspeed": "fixed_speed",
        "radar": "fixed_speed",
        "redlight": "redlight",
        "red_light": "redlight",
        "traffic_signals": "redlight",
        "redlight_speed": "redlight_speed",
        "average_speed": "average_speed",
        "average": "average_speed",
    }
    return mapping.get(val, "fixed_speed")


_PREFERRED_OVERPASS_MIRROR: str | None = None


def _post_overpass_query(
    ql: str,
    mirrors: list[str] | None = None,
    timeout_sec: int = 90,
    max_rounds: int = 3,
) -> dict[str, Any]:
    """Execute an Overpass QL query across fallback mirrors with retry/backoff."""
    global _PREFERRED_OVERPASS_MIRROR
    base_endpoints = list(mirrors or OVERPASS_MIRRORS)
    if _PREFERRED_OVERPASS_MIRROR and _PREFERRED_OVERPASS_MIRROR in base_endpoints:
        endpoints = [_PREFERRED_OVERPASS_MIRROR] + [u for u in base_endpoints if u != _PREFERRED_OVERPASS_MIRROR]
    else:
        endpoints = base_endpoints

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
                    timeout=(8, timeout_sec + 10),
                )
                if resp.status_code == 200:
                    payload = resp.json()
                    if "elements" in payload:
                        _PREFERRED_OVERPASS_MIRROR = url
                        LOGGER.info(
                            "Overpass mirror %s succeeded (%d elements).",
                            url,
                            len(payload["elements"]),
                        )
                        return payload
                    raise ValueError("Overpass JSON response missing 'elements' key")
                if resp.status_code == 429:
                    raise RuntimeError("HTTP 429 Too Many Requests")
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:160].strip()}")
            except Exception as exc:
                last_err = exc
                LOGGER.warning("Overpass mirror %s failed (round %d): %s", url, round_idx, exc)

        if round_idx < max_rounds:
            backoff = 5 * (2 ** (round_idx - 1))
            LOGGER.info("Retrying Overpass mirrors after %ds backoff...", backoff)
            time.sleep(backoff)

    raise RuntimeError(f"All Overpass mirrors failed after {max_rounds} rounds: {last_err}")


def build_overpass_query(timeout_sec: int = 60) -> str:
    """Build lean Overpass QL query for all traffic enforcement cameras in Mexico."""
    return f"""
[out:json][timeout:{timeout_sec}];
area["ISO3166-1"="MX"]["admin_level"="2"]->.mx;
(
  node["highway"="speed_camera"](area.mx);
  node["enforcement"="maxspeed"](area.mx);
  node["enforcement"="traffic_signals"](area.mx);
  node["enforcement"="average_speed"](area.mx);
  relation["type"="enforcement"](area.mx);
);
out body;
>;
out body;
""".strip()


def fetch_overpass_json(
    mirrors: list[str] | None = None,
    timeout_sec: int = 60,
    max_rounds: int = 3,
) -> dict[str, Any]:
    """Query Overpass API for Mexico traffic enforcement cameras."""
    ql = build_overpass_query(timeout_sec=timeout_sec)
    return _post_overpass_query(ql, mirrors=mirrors, timeout_sec=timeout_sec, max_rounds=max_rounds)


def _classify_osm_camera_type(
    node_tags: dict[str, str],
    rel_tags: dict[str, str],
    in_traffic_enforcement_rel: bool = False,
) -> str | None:
    """Determine normalized camera type from OSM node and parent relation tags."""
    enf_vals = {
        (node_tags.get("enforcement") or "").strip().lower(),
        (rel_tags.get("enforcement") or "").strip().lower(),
    } - {""}

    is_speed_cam_node = (node_tags.get("highway") or "").strip().lower() == "speed_camera"
    is_surveillance_device = (node_tags.get("man_made") or "").strip().lower() == "surveillance"

    # Exclude pure non-traffic surveillance/toll/weight relations unless explicitly speed_camera
    if enf_vals and enf_vals.issubset({"surveillance", "toll", "access", "weight", "height"}):
        if not is_speed_cam_node:
            return None

    has_avg = "average_speed" in enf_vals
    has_redlight = "traffic_signals" in enf_vals or "red_light" in enf_vals
    has_maxspeed = "maxspeed" in enf_vals or is_speed_cam_node

    if has_avg:
        return "average_speed"
    if has_redlight and (
        "maxspeed" in enf_vals
        or (is_speed_cam_node and (node_tags.get("maxspeed") or rel_tags.get("maxspeed")))
    ):
        return "redlight_speed"
    if has_redlight:
        return "redlight"
    if has_maxspeed:
        return "fixed_speed"
    # Accept man_made=surveillance device nodes inside a traffic enforcement relation
    if in_traffic_enforcement_rel and is_surveillance_device:
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

    # Index parent enforcement relations -> device nodes (with inherited tags & heading)
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

        # If untagged type=enforcement relation, accept device nodes tagged as speed_camera,
        # traffic enforcement, OR man_made=surveillance (excluding general town CCTV relations above)
        if not rel_enf:
            filtered_devices: list[int] = []
            for did in device_ids:
                dtags = nodes_by_id.get(did, {}).get("tags", {})
                if (
                    dtags.get("highway") == "speed_camera"
                    or dtags.get("enforcement") in {"maxspeed", "traffic_signals", "average_speed"}
                    or dtags.get("man_made") == "surveillance"
                ):
                    filtered_devices.append(did)
            device_ids = filtered_devices
        elif not device_ids and from_node is not None:
            device_ids = [from_node["id"]]

        rel_heading: int | None = None
        if (
            from_node
            and to_node
            and is_valid_mexico_coord(from_node.get("lat"), from_node.get("lon"))
            and is_valid_mexico_coord(to_node.get("lat"), to_node.get("lon"))
        ):
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

        cam_type = _classify_osm_camera_type(
            ntags,
            rtags,
            in_traffic_enforcement_rel=rinfo is not None,
        )
        if cam_type is None:
            continue

        lat = node.get("lat")
        lon = node.get("lon")
        if not is_valid_mexico_coord(lat, lon):
            continue

        # Look up parent highway way (directly or via relation's `from` node)
        parent_ways = ways_by_node.get(nid, [])
        if not parent_ways and rinfo and rinfo.get("from_id") is not None:
            parent_ways = ways_by_node.get(rinfo["from_id"], [])

        # Pick highest road-class parent way if multiple ways share the node
        parent_way = None
        if parent_ways:
            parent_way = max(
                parent_ways,
                key=lambda w: (
                    ROAD_CLASS_PRIORITY.get((w.get("tags", {}).get("highway") or "").lower(), 0),
                    1 if parse_maxspeed(w.get("tags", {}).get("maxspeed")) is not None else 0,
                ),
            )
        wtags = parent_way.get("tags", {}) if parent_way else {}

        # Priority: camera node maxspeed -> relation maxspeed -> parent way maxspeed
        speed = parse_maxspeed(ntags.get("maxspeed")) or parse_maxspeed(rtags.get("maxspeed"))
        if speed is None:
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
                priority=SOURCE_PRIORITY["osm"],
            )
        )

    return cameras


def select_best_road_maxspeed(
    cam_lat: float,
    cam_lon: float,
    candidate_ways: Iterable[dict[str, Any]],
    radius_m: float = 15.0,
) -> tuple[int | None, str]:
    """
    Pick the best nearest-road maxspeed from candidate Overpass ways within `radius_m`.

    Tie-breaking order when multiple ways match within `radius_m`:
      1. Highest road class (motorway > trunk > primary > secondary > tertiary > residential)
      2. Closest distance in meters to the way geometry
      3. Conservative (lower) speed limit if class and distance are equal
    """
    best_key: tuple[int, float, int] | None = None
    best_speed: int | None = None
    best_road_name = ""

    for way in candidate_ways:
        tags = way.get("tags") or {}
        speed = parse_maxspeed(tags.get("maxspeed"))
        if speed is None:
            continue
        highway = (tags.get("highway") or "").strip().lower()
        road_rank = ROAD_CLASS_PRIORITY.get(highway, 0)
        if road_rank == 0:
            continue

        geom = way.get("geometry") or []
        dist_m = point_to_polyline_distance_m(cam_lat, cam_lon, geom)
        if dist_m > radius_m:
            continue

        # Maximize road_rank, minimize dist_m, prefer conservative speed on exact tie
        score = (road_rank, -round(dist_m, 2), -speed)
        if best_key is None or score > best_key:
            best_key = score
            best_speed = speed
            best_road_name = (tags.get("name") or tags.get("ref") or "").strip()

    return best_speed, best_road_name


def enrich_nearest_road_maxspeed(
    cameras: list[CameraPoint],
    radius_m: float = 15.0,
    chunk_size: int = 50,
    mirrors: list[str] | None = None,
) -> int:
    """
    Batch-query Overpass for surrounding roads (`way(around:15)["highway"]["maxspeed"]`)
    for all cameras missing a valid numeric speed limit.

    Mutates `cameras` in place and returns the number of cameras enriched.
    """
    targets = [c for c in cameras if c.maxspeed_kmh is None]
    if not targets:
        return 0

    LOGGER.info(
        "Running batched nearest-road maxspeed enrichment for %d cameras (chunk_size=%d, radius=%.0fm)...",
        len(targets),
        chunk_size,
        radius_m,
    )

    enriched = 0
    # Slight matching tolerance (+1.0m) accounts for Overpass spherical vs local equirectangular rounding
    match_tolerance_m = radius_m + 1.0

    for start in range(0, len(targets), chunk_size):
        chunk = targets[start : start + chunk_size]
        ql_lines = ["[out:json][timeout:35];", "("]
        for cam in chunk:
            ql_lines.append(
                f'  way(around:{int(round(radius_m))},{cam.lat:.6f},{cam.lon:.6f})["highway"]["maxspeed"];'
            )
        ql_lines.append(");")
        ql_lines.append("out body geom;")
        ql = "\n".join(ql_lines)

        try:
            payload = _post_overpass_query(ql, mirrors=mirrors, timeout_sec=35, max_rounds=3)
        except Exception as exc:
            LOGGER.warning(
                "Nearest-road maxspeed batch %d..%d failed (%s); skipping chunk.",
                start,
                start + len(chunk),
                exc,
            )
            continue

        ways = [el for el in payload.get("elements", []) if el.get("type") == "way" and el.get("geometry")]
        for cam in chunk:
            speed, road_name = select_best_road_maxspeed(
                cam.lat,
                cam.lon,
                ways,
                radius_m=match_tolerance_m,
            )
            if speed is not None:
                cam.maxspeed_kmh = speed
                if not cam.road_name and road_name:
                    cam.road_name = road_name
                enriched += 1

        if start + chunk_size < len(targets):
            time.sleep(1.5)

    LOGGER.info("Nearest-road maxspeed enrichment filled %d / %d missing limits.", enriched, len(targets))
    return enriched


def _classify_fotocivicas_type(row: dict[str, str]) -> str:
    """Map CDMX Fotocívicas/Fotomultas metadata to normalized camera_type."""
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
    #   ubi 47..99  -> speed radars on expressways & primary corridors
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

    road_text = f"{row.get('via_princi', '')} {row.get('ubicacion', '')}".lower()
    if any(tok in road_text for tok in ("perif", "viaducto", "autopista urbana", "aun", "circuito bicentenario")):
        return "fixed_speed"
    return "redlight"


def _extract_coords_from_cdmx_row(row: dict[str, str]) -> tuple[float | None, float | None]:
    """Extract (lat, lon) from CDMX open-data CSV row or embedded geo_shape JSON."""
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


def parse_fotocivicas_csv(text: str, source_label: str = "cdmx_fotocivicas") -> list[CameraPoint]:
    """Parse CDMX Fotocívicas or legacy Fotomultas CSV content into CameraPoint records."""
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
    priority = SOURCE_PRIORITY.get(source_label, 1)

    for raw_row in reader:
        row = {
            (k or "").strip().lower(): (v or "").strip()
            for k, v in raw_row.items()
            if k is not None
        }
        lat, lon = _extract_coords_from_cdmx_row(row)
        if not is_valid_mexico_coord(lat, lon):
            continue

        # Handle known shifted-column anomaly in official CDMX file (ubi=46)
        via = row.get("via_princi") or row.get("vialidad") or row.get("calle") or ""
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
            or row.get("limite")
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
                source=source_label,
                osm_id="",
                priority=priority,
            )
        )
    return cameras


def parse_fotocivicas_geojson(
    payload: dict[str, Any],
    source_label: str = "cdmx_fotocivicas",
) -> list[CameraPoint]:
    """Parse CDMX Fotocívicas or legacy Fotomultas GeoJSON into CameraPoint records."""
    cameras: list[CameraPoint] = []
    priority = SOURCE_PRIORITY.get(source_label, 1)

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
        road_name = (props.get("ubicacion") or props.get("via_princi") or props.get("vialidad") or "").strip()
        speed = parse_maxspeed(
            props.get("maxspeed_kmh")
            or props.get("velocidad")
            or props.get("limite_velocidad")
            or props.get("limite")
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
                source=source_label,
                osm_id="",
                priority=priority,
            )
        )
    return cameras


def _discover_ckan_resources(session: requests.Session, api_urls: list[str]) -> list[tuple[str, str]]:
    """Discover CSV/GeoJSON resource URLs from CDMX CKAN package_show / package_search endpoints."""
    candidate_urls: list[tuple[str, str]] = []
    for api_url in api_urls:
        try:
            resp = session.get(api_url, timeout=(8, 15))
            if resp.status_code != 200:
                continue
            result = resp.json().get("result") or {}
            packages = result.get("results") if isinstance(result.get("results"), list) else [result]
            for pkg in packages:
                for res in pkg.get("resources", []):
                    fmt = (res.get("format") or "").strip().lower()
                    url = (res.get("url") or "").strip()
                    name = (res.get("name") or "").lower()
                    if url and fmt in {"csv", "geojson", "json"}:
                        entry = (url, fmt)
                        if entry not in candidate_urls:
                            if "ubicaci" in name or "punto" in name or "fotocivic" in name or "fotomulta" in name:
                                candidate_urls.insert(0, entry)
                            else:
                                candidate_urls.append(entry)
        except Exception as exc:
            LOGGER.warning("CDMX CKAN metadata lookup failed (%s): %s", api_url, exc)
    return candidate_urls


def _download_cdmx_resources(
    session: requests.Session,
    candidate_urls: list[tuple[str, str]],
    source_label: str,
) -> list[CameraPoint]:
    """Download and parse the first valid CSV/GeoJSON resource from CDMX open data."""
    for url, fmt in candidate_urls:
        try:
            LOGGER.info("Fetching %s resource: %s", source_label, url)
            resp = session.get(url, timeout=(8, 25))
            resp.raise_for_status()
            content_type = (resp.headers.get("Content-Type") or "").lower()
            if fmt in {"geojson", "json"} or "json" in content_type:
                cameras = parse_fotocivicas_geojson(resp.json(), source_label=source_label)
            else:
                try:
                    text = resp.content.decode("utf-8-sig")
                except UnicodeDecodeError:
                    text = resp.content.decode("latin-1")
                cameras = parse_fotocivicas_csv(text, source_label=source_label)
            if cameras:
                LOGGER.info("Loaded %d %s points live from %s", len(cameras), source_label, url)
                return cameras
            LOGGER.warning("CDMX resource %s returned 0 valid camera points", url)
        except Exception as exc:
            LOGGER.warning("Failed to download %s resource %s: %s", source_label, url, exc)
    return []


def fetch_cdmx_fotocivicas(
    cache_path: Path | None = None,
    skip_live: bool = False,
) -> tuple[list[CameraPoint], str]:
    """Fetch official CDMX Fotocívicas points with graceful local-cache fallback."""
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    if not skip_live:
        candidate_urls = _discover_ckan_resources(session, CDMX_CKAN_PACKAGE_URLS)
        for direct_url in CDMX_DIRECT_RESOURCE_URLS:
            if all(u != direct_url for u, _ in candidate_urls):
                candidate_urls.append((direct_url, "csv"))

        live_cams = _download_cdmx_resources(session, candidate_urls, source_label="cdmx_fotocivicas")
        if live_cams:
            return live_cams, "live"

        LOGGER.warning(
            "CDMX open data portal (datos.cdmx.gob.mx) is unreachable or returned no Fotocívicas data; "
            "falling back to local cache if present."
        )

    if cache_path and cache_path.exists():
        text = cache_path.read_text(encoding="utf-8-sig")
        cameras = parse_fotocivicas_csv(text, source_label="cdmx_fotocivicas")
        LOGGER.warning(
            "Using local cached CDMX Fotocívicas dataset (%s): %d points.",
            cache_path,
            len(cameras),
        )
        return cameras, "cache"

    LOGGER.warning("No local CDMX Fotocívicas cache found; continuing with OSM-only data.")
    return [], "unavailable"


def fetch_cdmx_fotomultas(skip_live: bool = False) -> tuple[list[CameraPoint], str]:
    """
    Optionally fetch legacy CDMX Fotomultas (Ubicaciones) strictly for secondary metadata
    enrichment. Never fails the build if the portal is unreachable.
    """
    if skip_live:
        return [], "skipped"

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    candidate_urls = _discover_ckan_resources(session, CDMX_FOTOMULTAS_CKAN_URLS)
    if not candidate_urls:
        LOGGER.warning("CDMX legacy Fotomultas portal unavailable; skipping secondary metadata enrichment.")
        return [], "unavailable"

    records = _download_cdmx_resources(session, candidate_urls, source_label="cdmx_fotomultas")
    if records:
        return records, "live"

    LOGGER.warning("CDMX legacy Fotomultas dataset unavailable; skipping secondary metadata enrichment.")
    return [], "unavailable"


def enrich_with_fotomultas(
    cameras: list[CameraPoint],
    fotomultas_records: list[CameraPoint],
    radius_m: float = 25.0,
) -> int:
    """
    Secondary metadata-only enrichment using legacy CDMX Fotomultas records.

    - NEVER inserts standalone Fotomultas points into `cameras`.
    - Only backfills missing `maxspeed_kmh`, direction, road_name, or upgrades combined
      type on existing non-manual OSM/Fotocívicas points within `<= radius_m` meters.
    """
    if not fotomultas_records:
        return 0

    enriched = 0
    for cam in cameras:
        # Never overwrite curated manual entries
        if cam.priority >= SOURCE_PRIORITY["manual"]:
            continue

        nearest: CameraPoint | None = None
        nearest_dist = radius_m + 1.0
        for legacy in fotomultas_records:
            dist = haversine_m(cam.lat, cam.lon, legacy.lat, legacy.lon)
            if dist <= radius_m and dist < nearest_dist:
                nearest = legacy
                nearest_dist = dist

        if nearest is None:
            continue

        updated = False
        if cam.maxspeed_kmh is None and nearest.maxspeed_kmh is not None:
            cam.maxspeed_kmh = nearest.maxspeed_kmh
            updated = True
        if cam.direction_deg is None and nearest.direction_deg is not None:
            cam.direction_deg = nearest.direction_deg
            updated = True
        if not cam.road_name and nearest.road_name:
            cam.road_name = nearest.road_name
            updated = True
        # Upgrade pure redlight <-> fixed_speed to redlight_speed if legacy confirms dual enforcement
        if {cam.camera_type, nearest.camera_type} == {"fixed_speed", "redlight"} or (
            nearest.camera_type == "redlight_speed" and cam.camera_type in {"fixed_speed", "redlight"}
        ):
            cam.camera_type = "redlight_speed"
            updated = True

        if updated:
            enriched += 1

    LOGGER.info("Secondary Fotomultas metadata enrichment updated %d existing cameras.", enriched)
    return enriched


def load_manual_overrides(
    path: Path | None,
) -> tuple[list[CameraPoint], list[tuple[float, float, str]]]:
    """
    Load curated manual additions, overrides, and suppressions from `data/manual_mx.csv`.

    Expected schema:
      lat,lon,type,maxspeed,direction,active,source,notes

    Returns:
      (active_cameras, suppressions)
      where `suppressions` is a list of `(lat, lon, notes)` entries with `active=false`.
    """
    if path is None or not path.exists():
        return [], []

    active_cameras: list[CameraPoint] = []
    suppressions: list[tuple[float, float, str]] = []

    text = path.read_text(encoding="utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    for raw_row in reader:
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw_row.items() if k is not None}
        lat_s = row.get("lat", "")
        lon_s = row.get("lon", "")
        if not lat_s or not lon_s:
            continue
        if not is_valid_mexico_coord(lat_s, lon_s):
            LOGGER.warning("Skipping manual_mx.csv row with invalid Mexico coordinates: %s, %s", lat_s, lon_s)
            continue

        lat = round(float(lat_s), 6)
        lon = round(float(lon_s), 6)
        active_raw = (row.get("active") or "true").strip().lower()
        is_active = active_raw not in {"false", "0", "no", "off"}

        notes = row.get("notes", "")
        if not is_active:
            suppressions.append((lat, lon, notes))
            continue

        cam_type = normalize_manual_type(row.get("type"))
        speed = parse_maxspeed(row.get("maxspeed"))
        direction = parse_direction(row.get("direction"))
        src_note = row.get("source") or "manual"
        source_label = "manual" if src_note.lower() == "manual" else f"manual:{src_note}"

        active_cameras.append(
            CameraPoint(
                lat=lat,
                lon=lon,
                camera_type=cam_type,
                maxspeed_kmh=speed,
                direction_deg=direction,
                road_name=notes,
                source=source_label,
                osm_id="",
                priority=SOURCE_PRIORITY["manual"],
            )
        )

    if active_cameras or suppressions:
        LOGGER.info(
            "Loaded manual_mx.csv: %d active override(s), %d suppression rule(s).",
            len(active_cameras),
            len(suppressions),
        )
    return active_cameras, suppressions


def _metadata_richness(cam: CameraPoint) -> tuple[int, int, int, int]:
    """Secondary richness key used within the same source priority tier."""
    return (
        1 if cam.maxspeed_kmh is not None else 0,
        1 if cam.camera_type in {"redlight_speed", "average_speed"} else 0,
        1 if cam.direction_deg is not None else 0,
        1 if bool(cam.road_name) else 0,
    )


def deduplicate_cameras(
    cameras: Iterable[CameraPoint],
    radius_m: float = 25.0,
    suppressions: Iterable[tuple[float, float, str]] = (),
) -> list[CameraPoint]:
    """
    Apply suppression rules and deduplicate camera points within `radius_m` meters.

    Precedence order:
      1. `data/manual_mx.csv` (priority=3)
      2. CDMX Fotocívicas (priority=2)
      3. OpenStreetMap (priority=1)

    - Any non-manual point within `<= radius_m` of an `active=false` suppression rule is dropped.
    - When merging duplicates within `<= radius_m`, the higher-priority record wins on coordinates,
      `camera_type`, and explicit `maxspeed_kmh`; missing optional fields are backfilled from the
      lower-priority record.
    - Guarantees every pair of returned points is strictly > `radius_m` meters apart.
    """
    suppression_list = list(suppressions)

    # 1. Validate Mexico bounds and apply `active=false` suppression rules to non-manual sources
    valid: list[CameraPoint] = []
    suppressed_count = 0
    for cam in cameras:
        if not is_valid_mexico_coord(cam.lat, cam.lon):
            continue
        if cam.priority < SOURCE_PRIORITY["manual"] and suppression_list:
            if any(haversine_m(cam.lat, cam.lon, slat, slon) <= radius_m for slat, slon, _ in suppression_list):
                suppressed_count += 1
                continue
        valid.append(cam)

    if suppressed_count:
        LOGGER.info("Suppressed %d camera(s) via manual_mx.csv active=false rules.", suppressed_count)

    # 2. Sort strictly by descending source priority, then metadata richness, then coordinates
    valid.sort(
        key=lambda c: (
            -c.priority,
            -_metadata_richness(c)[0],
            -_metadata_richness(c)[1],
            -_metadata_richness(c)[2],
            -_metadata_richness(c)[3],
            c.lat,
            c.lon,
            c.camera_type,
        )
    )

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
            # Higher-priority `nearest` anchors coordinates, camera_type, and explicit maxspeed.
            # Backfill missing fields from the merged duplicate.
            if nearest.maxspeed_kmh is None and cam.maxspeed_kmh is not None:
                nearest.maxspeed_kmh = cam.maxspeed_kmh
            # Only combine types when both records come from the SAME priority tier;
            # higher-priority records (manual > Fotocívicas > OSM) strictly override lower-priority types.
            if nearest.priority == cam.priority and nearest.camera_type != cam.camera_type:
                pair = {nearest.camera_type, cam.camera_type}
                if "average_speed" in pair:
                    nearest.camera_type = "average_speed"
                elif "redlight_speed" in pair or pair == {"fixed_speed", "redlight"}:
                    nearest.camera_type = "redlight_speed"
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
            priority=cam.priority,
        )
        kept.append(cloned)
        grid.setdefault((gx, gy), []).append(cloned)

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
        name = f"stop @{speed}" if speed is not None else "stop"
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
    manual_active_count: int,
    manual_suppression_count: int,
    fotomultas_enriched: int,
    fotomultas_status: str,
    with_speed_before_road: int,
    nearest_road_enriched: int,
    final_cameras: list[CameraPoint],
) -> dict[str, Any]:
    """Compute structured build metrics for logging and CI summaries."""
    total = len(final_cameras)
    by_type = Counter(c.camera_type for c in final_cameras)
    by_source = Counter(c.source for c in final_cameras)
    by_region = Counter(infer_region(c.lat, c.lon) for c in final_cameras)
    with_speed = sum(1 for c in final_cameras if c.maxspeed_kmh is not None)
    cdmx_bbox_count = sum(
        1
        for c in final_cameras
        if CDMX_LAT_MIN <= c.lat <= CDMX_LAT_MAX and CDMX_LON_MIN <= c.lon <= CDMX_LON_MAX
    )
    edomex_count = by_region.get("Estado de México", 0)
    return {
        "osm_extracted": osm_raw_count,
        "cdmx_fotocivicas_extracted": cdmx_raw_count,
        "cdmx_fotocivicas_status": cdmx_status,
        "manual_active_count": manual_active_count,
        "manual_suppression_count": manual_suppression_count,
        "fotomultas_enriched": fotomultas_enriched,
        "fotomultas_status": fotomultas_status,
        "with_known_maxspeed_before_nearest_road": with_speed_before_road,
        "known_maxspeed_pct_before_nearest_road": round(100.0 * with_speed_before_road / total, 1) if total else 0.0,
        "nearest_road_enriched": nearest_road_enriched,
        "final_deduplicated_count": total,
        "with_known_maxspeed": with_speed,
        "known_maxspeed_pct": round(100.0 * with_speed / total, 1) if total else 0.0,
        "cdmx_bbox_count": cdmx_bbox_count,
        "edomex_count": edomex_count,
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
    print(
        f"  Manual overrides (manual_mx)  : {summary['manual_active_count']} active, "
        f"{summary['manual_suppression_count']} suppression(s)"
    )
    print(
        f"  CDMX Fotomultas backfill      : {summary['fotomultas_enriched']} matched "
        f"(status: {summary['fotomultas_status']})"
    )
    print(f"  Final deduplicated count      : {summary['final_deduplicated_count']}")
    print(
        f"  Known maxspeed (before road)  : {summary['with_known_maxspeed_before_nearest_road']} "
        f"({summary['known_maxspeed_pct_before_nearest_road']:.1f}%)"
    )
    print(f"  Nearest-road maxspeed added   : +{summary['nearest_road_enriched']}")
    print(
        f"  Known maxspeed (after road)   : {summary['with_known_maxspeed']} "
        f"({summary['known_maxspeed_pct']:.1f}%)"
    )
    print(f"  CDMX bbox / Edomex count      : {summary['cdmx_bbox_count']} / {summary['edomex_count']}")
    print("  Breakdown by camera type:")
    for ctype, cnt in summary["by_type"].items():
        print(f"    - {ctype:<16}: {cnt}")
    print("  Breakdown by merged source:")
    for src, cnt in summary["by_source"].items():
        print(f"    - {src:<26}: {cnt}")
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
        "--manual-csv",
        type=Path,
        default=repo_root / "data" / "manual_mx.csv",
        help="Curated manual override & suppression CSV",
    )
    parser.add_argument(
        "--skip-live-cdmx",
        action="store_true",
        help="Skip live datos.cdmx.gob.mx requests and use the local cache directly",
    )
    parser.add_argument(
        "--skip-nearest-road",
        action="store_true",
        help="Skip batched Overpass nearest-road maxspeed inference",
    )
    parser.add_argument(
        "--dedupe-radius-m",
        type=float,
        default=25.0,
        help="Haversine deduplication & suppression radius in meters (default: 25.0)",
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

    # 1. Source A: OpenStreetMap nationwide Mexico
    osm_raw = fetch_overpass_json()
    osm_cameras = extract_osm_cameras(osm_raw)
    LOGGER.info("Extracted %d valid camera points from OpenStreetMap.", len(osm_cameras))

    # 2. Source B: Official CDMX Fotocívicas (with resilient cache fallback)
    cdmx_cameras, cdmx_status = fetch_cdmx_fotocivicas(
        cache_path=args.cdmx_cache,
        skip_live=args.skip_live_cdmx,
    )

    # 3. Curated Manual Override & Suppression Layer
    manual_active, manual_suppressions = load_manual_overrides(args.manual_csv)

    # 4. Merge & deduplicate with strict precedence: manual > CDMX Fotocívicas > OSM
    combined = manual_active + cdmx_cameras + osm_cameras
    deduped = deduplicate_cameras(
        combined,
        radius_m=args.dedupe_radius_m,
        suppressions=manual_suppressions,
    )

    # 5. Secondary metadata-only enrichment via CDMX legacy Fotomultas (never adds rows)
    fotomultas_records, fotomultas_status = fetch_cdmx_fotomultas(skip_live=args.skip_live_cdmx)
    fotomultas_enriched = enrich_with_fotomultas(deduped, fotomultas_records, radius_m=args.dedupe_radius_m)

    # 6. Batched nearest-road maxspeed inference via Overpass way(around:15)
    with_speed_before_road = sum(1 for c in deduped if c.maxspeed_kmh is not None)
    nearest_road_enriched = 0
    if not args.skip_nearest_road:
        nearest_road_enriched = enrich_nearest_road_maxspeed(deduped, radius_m=15.0, chunk_size=50)

    # Re-sort deterministically after enrichment (maxspeed is part of the tie-break sort key)
    deduped.sort(key=lambda c: (c.lat, c.lon, c.camera_type, c.maxspeed_kmh or 0))

    # 7. Emit primary GATSO_ALL.csv
    written = write_gatso_csv(deduped, args.output)
    LOGGER.info("Wrote %d rows to %s", written, args.output)

    # 8. Optionally emit release bundle (dist/csv_files.zip + mx_speed_cameras.csv + GATSO_ALL.csv)
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
        manual_active_count=len(manual_active),
        manual_suppression_count=len(manual_suppressions),
        fotomultas_enriched=fotomultas_enriched,
        fotomultas_status=fotomultas_status,
        with_speed_before_road=with_speed_before_road,
        nearest_road_enriched=nearest_road_enriched,
        final_cameras=deduped,
    )
    print_summary(summary)

    if args.summary_json is not None:
        args.summary_json.parent.mkdir(parents=True, exist_ok=True)
        args.summary_json.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

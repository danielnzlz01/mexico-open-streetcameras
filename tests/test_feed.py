"""Validation and unit test suite for Mexico Open Zeekr GATSO_ALL.csv feed."""

from __future__ import annotations

import csv
import io
import math
import re
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import build_feed as bf  # noqa: E402

GATSO_PATH = REPO_ROOT / "GATSO_ALL.csv"
CDMX_CACHE_PATH = REPO_ROOT / "data" / "cdmx_fotocivicas_cache.csv"
MANUAL_CSV_PATH = REPO_ROOT / "data" / "manual_mx.csv"

NAME_RE = re.compile(r"^(?:max|stop|average)(?: @\d{2,3})?$")
ZEEKR_SPEED_RE = re.compile(r"@\s*(\d{2,3})")
FORBIDDEN_TOKENS = ("nan", "none", "null", "undefined")
MANUAL_REQUIRED_HEADERS = ["lat", "lon", "type", "maxspeed", "direction", "active", "source", "notes"]


# ---------------------------------------------------------------------------
# Unit tests: speed & direction normalization
# ---------------------------------------------------------------------------


def test_parse_maxspeed_variants():
    assert bf.parse_maxspeed("50") == 50
    assert bf.parse_maxspeed("50 km/h") == 50
    assert bf.parse_maxspeed("80") == 80
    assert bf.parse_maxspeed("80 kmh") == 80
    assert bf.parse_maxspeed("MX:urban") == 50
    assert bf.parse_maxspeed("mx:rural") == 80
    assert bf.parse_maxspeed("MX:motorway") == 110
    assert bf.parse_maxspeed("MX:living_street") == 20
    assert bf.parse_maxspeed("50;80") == 50
    assert bf.parse_maxspeed("50 mph") == 80
    # Ambiguous / non-numeric / out-of-range tags must be ignored
    assert bf.parse_maxspeed(None) is None
    assert bf.parse_maxspeed("") is None
    assert bf.parse_maxspeed("signals") is None
    assert bf.parse_maxspeed("variable") is None
    assert bf.parse_maxspeed("50 @ (07:00-22:00)") is None
    assert bf.parse_maxspeed("walk") is None
    assert bf.parse_maxspeed("5") is None
    assert bf.parse_maxspeed("300") is None


def test_parse_direction_variants():
    assert bf.parse_direction("N") == 0
    assert bf.parse_direction("240") == 240
    assert bf.parse_direction("360") == 0
    assert bf.parse_direction("Norte a Sur") == 180
    assert bf.parse_direction("Sur a Norte") == 0
    assert bf.parse_direction("Oriente a Poniente") == 270
    assert bf.parse_direction("Poniente a Oriente") == 90
    assert bf.parse_direction("invalid") is None


# ---------------------------------------------------------------------------
# Unit tests: OSM extraction (average_speed + man_made=surveillance devices)
# ---------------------------------------------------------------------------


def test_extract_osm_cameras_covers_average_speed_and_surveillance_devices():
    raw = {
        "elements": [
            # Direct speed_camera node inheriting parent way maxspeed
            {"type": "node", "id": 1, "lat": 19.4056, "lon": -99.1197, "tags": {"highway": "speed_camera"}},
            # Direct average_speed enforcement node
            {"type": "node", "id": 7, "lat": 19.5000, "lon": -99.2000, "tags": {"enforcement": "average_speed", "maxspeed": "110"}},
            # Relation device node tagged man_made=surveillance in a maxspeed enforcement relation
            {"type": "node", "id": 2, "lat": 19.4200, "lon": -99.1500, "tags": {"man_made": "surveillance"}},
            {"type": "node", "id": 3, "lat": 19.4190, "lon": -99.1500},
            {"type": "node", "id": 4, "lat": 19.4210, "lon": -99.1500},
            # Untagged enforcement relation whose device node is man_made=surveillance
            {"type": "node", "id": 8, "lat": 19.4600, "lon": -99.1600, "tags": {"man_made": "surveillance", "maxspeed": "60"}},
            # Red-light relation device node
            {"type": "node", "id": 5, "lat": 19.4300, "lon": -99.1400},
            # Non-traffic C5 surveillance relation node that MUST be excluded
            {"type": "node", "id": 6, "lat": 19.1900, "lon": -100.1300, "tags": {"man_made": "surveillance"}},
            # Parent way for node 1
            {"type": "way", "id": 100, "nodes": [1], "tags": {"highway": "trunk", "maxspeed": "80", "name": "Viaducto"}},
            # Enforcement maxspeed relation (man_made=surveillance device)
            {
                "type": "relation",
                "id": 200,
                "tags": {"type": "enforcement", "enforcement": "maxspeed", "maxspeed": "50"},
                "members": [
                    {"type": "node", "ref": 2, "role": "device"},
                    {"type": "node", "ref": 3, "role": "from"},
                    {"type": "node", "ref": 4, "role": "to"},
                ],
            },
            # Enforcement traffic_signals relation
            {
                "type": "relation",
                "id": 201,
                "tags": {"type": "enforcement", "enforcement": "traffic_signals"},
                "members": [{"type": "node", "ref": 5, "role": "device"}],
            },
            # Non-traffic surveillance relation (C5 town CCTV)
            {
                "type": "relation",
                "id": 202,
                "tags": {"type": "enforcement", "enforcement": "surveillance", "name": "C5"},
                "members": [{"type": "node", "ref": 6, "role": "device"}],
            },
            # Untagged type=enforcement relation with man_made=surveillance device
            {
                "type": "relation",
                "id": 203,
                "tags": {"type": "enforcement"},
                "members": [{"type": "node", "ref": 8, "role": "device"}],
            },
        ]
    }
    cams = bf.extract_osm_cameras(raw)
    by_id = {c.osm_id: c for c in cams}
    assert set(by_id.keys()) == {"node/1", "node/2", "node/5", "node/7", "node/8"}
    assert by_id["node/1"].camera_type == "fixed_speed"
    assert by_id["node/1"].maxspeed_kmh == 80
    assert by_id["node/2"].camera_type == "fixed_speed"
    assert by_id["node/2"].maxspeed_kmh == 50
    assert by_id["node/2"].direction_deg == 0
    assert by_id["node/5"].camera_type == "redlight"
    assert by_id["node/7"].camera_type == "average_speed"
    assert by_id["node/7"].maxspeed_kmh == 110
    assert by_id["node/8"].camera_type == "fixed_speed"
    assert by_id["node/8"].maxspeed_kmh == 60


# ---------------------------------------------------------------------------
# Unit tests: nearest-road maxspeed tie-breaking & distance bounds
# ---------------------------------------------------------------------------


def test_nearest_road_maxspeed_prefers_higher_road_class():
    cam_lat, cam_lon = 19.405600, -99.119700
    # Lateral residential street is 2m away (30 km/h); trunk expressway is 10m away (80 km/h);
    # motorway is 25m away (outside 15m radius -> must be ignored).
    ways = [
        {
            "id": 1,
            "tags": {"highway": "residential", "maxspeed": "30", "name": "Lateral"},
            "geometry": [{"lat": 19.405618, "lon": -99.119800}, {"lat": 19.405618, "lon": -99.119600}],
        },
        {
            "id": 2,
            "tags": {"highway": "primary", "maxspeed": "50", "name": "Av Primaria"},
            "geometry": [{"lat": 19.405650, "lon": -99.119800}, {"lat": 19.405650, "lon": -99.119600}],
        },
        {
            "id": 3,
            "tags": {"highway": "trunk", "maxspeed": "80 km/h", "name": "Viaducto"},
            "geometry": [{"lat": 19.405690, "lon": -99.119800}, {"lat": 19.405690, "lon": -99.119600}],
        },
        {
            "id": 4,
            "tags": {"highway": "motorway", "maxspeed": "110", "name": "Too Far"},
            "geometry": [{"lat": 19.405850, "lon": -99.119800}, {"lat": 19.405850, "lon": -99.119600}],
        },
        {
            "id": 5,
            "tags": {"highway": "motorway", "maxspeed": "signals", "name": "Ambiguous"},
            "geometry": [{"lat": 19.405605, "lon": -99.119800}, {"lat": 19.405605, "lon": -99.119600}],
        },
    ]

    speed, road = bf.select_best_road_maxspeed(cam_lat, cam_lon, ways, radius_m=15.0)
    assert speed == 80
    assert road == "Viaducto"

    # Full road-class ordering check: motorway > trunk > primary > secondary > tertiary > residential
    ordered_classes = ["motorway", "trunk", "primary", "secondary", "tertiary", "residential"]
    priorities = [bf.ROAD_CLASS_PRIORITY[c] for c in ordered_classes]
    assert priorities == sorted(priorities, reverse=True) and len(set(priorities)) == len(ordered_classes)


# ---------------------------------------------------------------------------
# Unit tests: manual_mx.csv override precedence & active=false suppression
# ---------------------------------------------------------------------------


def test_manual_override_precedence_and_suppression(tmp_path: Path):
    manual_csv = tmp_path / "manual_mx.csv"
    manual_csv.write_text(
        "\n".join(
            [
                "lat,lon,type,maxspeed,direction,active,source,notes",
                # Override existing Fotocívicas + OSM point with average_speed @ 90
                "19.405605,-99.119721,average_speed,90,90,true,MonitorVial,Viaducto override",
                # Suppress false positive within 25m
                "19.423568,-99.131166,speed,,,false,field_check,Removed camera",
                # Add brand new manual camera in Puebla
                "19.041400,-98.206300,speed,70,180,true,PueblaMonitorVial,Periférico Ecológico",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    manual_active, suppressions = bf.load_manual_overrides(manual_csv)
    assert len(manual_active) == 2
    assert len(suppressions) == 1

    # Underlying CDMX Fotocívicas & OSM points at same locations
    cdmx_override_target = bf.CameraPoint(
        lat=19.405615,
        lon=-99.119721,
        camera_type="fixed_speed",
        maxspeed_kmh=80,
        source="cdmx_fotocivicas",
        priority=bf.SOURCE_PRIORITY["cdmx_fotocivicas"],
    )
    osm_override_target = bf.CameraPoint(
        lat=19.405625,
        lon=-99.119721,
        camera_type="redlight",
        maxspeed_kmh=50,
        source="osm",
        osm_id="node/111",
        priority=bf.SOURCE_PRIORITY["osm"],
    )
    # Point 12m from suppression rule -> MUST be dropped
    osm_false_positive = bf.CameraPoint(
        lat=19.423650,
        lon=-99.131166,
        camera_type="fixed_speed",
        maxspeed_kmh=50,
        source="osm",
        osm_id="node/222",
        priority=bf.SOURCE_PRIORITY["osm"],
    )
    # Point ~500m away from suppression rule -> MUST be kept
    osm_keep = bf.CameraPoint(
        lat=19.428500,
        lon=-99.131166,
        camera_type="fixed_speed",
        maxspeed_kmh=60,
        source="osm",
        osm_id="node/333",
        priority=bf.SOURCE_PRIORITY["osm"],
    )
    # Fotocívicas vs OSM precedence check (CDMX Fotocívicas > OSM on type, backfill OSM maxspeed)
    cdmx_wins = bf.CameraPoint(
        lat=19.350000,
        lon=-99.180000,
        camera_type="fixed_speed",
        maxspeed_kmh=None,
        source="cdmx_fotocivicas",
        priority=bf.SOURCE_PRIORITY["cdmx_fotocivicas"],
    )
    osm_loses = bf.CameraPoint(
        lat=19.350080,
        lon=-99.180000,
        camera_type="redlight",
        maxspeed_kmh=80,
        source="osm",
        osm_id="node/444",
        priority=bf.SOURCE_PRIORITY["osm"],
    )

    merged = bf.deduplicate_cameras(
        manual_active + [cdmx_override_target, osm_override_target, osm_false_positive, osm_keep, cdmx_wins, osm_loses],
        radius_m=25.0,
        suppressions=suppressions,
    )

    assert len(merged) == 4  # 2 manual + osm_keep + cdmx_wins (osm_false_positive suppressed!)

    # Manual override won over both Fotocívicas (80, fixed_speed) and OSM (50, redlight)
    ov = next(c for c in merged if abs(c.lat - 19.405605) < 1e-4)
    assert ov.camera_type == "average_speed"
    assert ov.maxspeed_kmh == 90
    assert ov.osm_id == "node/111"  # backfilled metadata

    # Suppressed point is absent
    assert not any(abs(c.lat - 19.423650) < 1e-3 for c in merged)

    # CDMX Fotocívicas won over OSM on camera_type while inheriting OSM's maxspeed=80
    cdmx_m = next(c for c in merged if abs(c.lat - 19.350000) < 1e-4)
    assert cdmx_m.camera_type == "fixed_speed"
    assert cdmx_m.maxspeed_kmh == 80


def test_fotomultas_enrichment_is_metadata_only():
    existing = [
        bf.CameraPoint(
            lat=19.405605,
            lon=-99.119721,
            camera_type="fixed_speed",
            maxspeed_kmh=None,
            source="cdmx_fotocivicas",
            priority=bf.SOURCE_PRIORITY["cdmx_fotocivicas"],
        ),
        bf.CameraPoint(
            lat=19.350000,
            lon=-99.180000,
            camera_type="fixed_speed",
            maxspeed_kmh=70,
            source="manual",
            priority=bf.SOURCE_PRIORITY["manual"],
        ),
    ]
    legacy = [
        # Within 10m of existing[0] -> should backfill maxspeed=80
        bf.CameraPoint(lat=19.405680, lon=-99.119721, camera_type="fixed_speed", maxspeed_kmh=80, source="cdmx_fotomultas"),
        # Within 10m of manual point -> must NOT overwrite manual maxspeed=70
        bf.CameraPoint(lat=19.350050, lon=-99.180000, camera_type="redlight", maxspeed_kmh=50, source="cdmx_fotomultas"),
        # Standalone legacy camera 5km away -> must NEVER be inserted
        bf.CameraPoint(lat=19.490000, lon=-99.100000, camera_type="fixed_speed", maxspeed_kmh=60, source="cdmx_fotomultas"),
    ]

    updated = bf.enrich_with_fotomultas(existing, legacy, radius_m=25.0)
    assert updated == 1
    assert len(existing) == 2  # no standalone rows added
    assert existing[0].maxspeed_kmh == 80
    assert existing[1].maxspeed_kmh == 70
    assert existing[1].camera_type == "fixed_speed"


def test_manual_mx_csv_file_has_exact_header():
    assert MANUAL_CSV_PATH.exists(), "data/manual_mx.csv must exist"
    lines = MANUAL_CSV_PATH.read_text(encoding="utf-8").splitlines()
    assert len(lines) >= 1
    header = next(csv.reader([lines[0]]))
    assert header == MANUAL_REQUIRED_HEADERS


def test_release_zip_contains_gatso_all_csv(tmp_path: Path):
    cams = [
        bf.CameraPoint(lat=19.405605, lon=-99.119721, camera_type="fixed_speed", maxspeed_kmh=80),
        bf.CameraPoint(lat=19.423568, lon=-99.131166, camera_type="redlight", maxspeed_kmh=50),
        bf.CameraPoint(lat=19.440000, lon=-99.150000, camera_type="redlight", maxspeed_kmh=None),
        bf.CameraPoint(lat=19.500000, lon=-99.200000, camera_type="average_speed", maxspeed_kmh=110),
    ]
    gatso = tmp_path / "GATSO_ALL.csv"
    enriched = tmp_path / "mx_speed_cameras.csv"
    bundle = tmp_path / "csv_files.zip"
    bf.write_gatso_csv(cams, gatso)
    bf.write_enriched_csv(cams, enriched)
    bf.write_release_zip(bundle, gatso, enriched, REPO_ROOT / "ATTRIBUTION.md")

    with zipfile.ZipFile(bundle, "r") as zf:
        names = set(zf.namelist())
        assert {"GATSO_ALL.csv", "mx_speed_cameras.csv", "ATTRIBUTION.md"}.issubset(names)
        lines = zf.read("GATSO_ALL.csv").decode("utf-8").splitlines()
        assert lines == [
            '-99.119721,19.405605,"max @80","MX Radar fixed_speed"',
            '-99.131166,19.423568,"stop @50","MX Radar redlight"',
            '-99.15,19.44,"stop","MX Radar redlight"',
            '-99.2,19.5,"average @110","MX Radar average_speed"',
        ]


def test_cdmx_cache_parses_cleanly():
    assert CDMX_CACHE_PATH.exists(), "Local CDMX Fotocívicas fallback cache must exist"
    cams = bf.parse_fotocivicas_csv(CDMX_CACHE_PATH.read_text(encoding="utf-8-sig"))
    assert len(cams) >= 100
    assert any(c.camera_type == "fixed_speed" for c in cams)
    assert any(c.camera_type == "redlight" for c in cams)


# ---------------------------------------------------------------------------
# Integration validation tests against the generated GATSO_ALL.csv
# ---------------------------------------------------------------------------


def _load_gatso_rows() -> tuple[bytes, list[str], list[list[str]]]:
    assert GATSO_PATH.exists(), f"{GATSO_PATH} does not exist. Run scripts/build_feed.py first."
    raw = GATSO_PATH.read_bytes()
    assert len(raw) > 0, "GATSO_ALL.csv is empty"
    text = raw.decode("utf-8")
    lines = text.splitlines()
    rows = list(csv.reader(io.StringIO(text)))
    return raw, lines, rows


def test_gatso_encoding_and_line_endings():
    raw, lines, _ = _load_gatso_rows()
    assert not raw.startswith(b"\xef\xbb\xbf"), "GATSO_ALL.csv must be UTF-8 without BOM"
    assert b"\r\n" not in raw, "GATSO_ALL.csv must use LF (\\n) line endings"
    assert raw.endswith(b"\n"), "GATSO_ALL.csv must end with a trailing newline"
    assert len(lines) > 100, f"Expected > 100 Mexico camera points, got {len(lines)}"


def test_gatso_schema_and_zeekr_compatibility():
    _, lines, rows = _load_gatso_rows()
    assert len(lines) == len(rows), "Embedded newlines or malformed CSV rows detected"

    cdmx_count = 0
    with_speed = 0
    prev_key: tuple[float, float] | None = None

    for idx, (raw_line, row) in enumerate(zip(lines, rows), start=1):
        assert raw_line.count('"') % 2 == 0, f"Line {idx} has unbalanced quotes: {raw_line}"
        assert len(row) == 4, f"Line {idx} expected 4 columns, got {len(row)}: {raw_line}"

        lon_s, lat_s, name, comment = row

        for col in row:
            assert col.strip().lower() not in FORBIDDEN_TOKENS, f"Line {idx} contains forbidden token: {raw_line}"
            assert not re.search(r"\b(?:nan|none|null|undefined)\b", col, re.IGNORECASE), (
                f"Line {idx} contains forbidden token in {col!r}: {raw_line}"
            )

        # Strict lon,lat column order: Mexico longitudes are negative [-118.5, -86.5] in col 1
        lon = float(lon_s)
        lat = float(lat_s)
        assert math.isfinite(lon) and math.isfinite(lat), f"Line {idx} non-finite coords"
        assert lon < 0.0, f"Line {idx} Mexico longitude must be negative in col 1: {lon}"
        assert bf.MX_LON_MIN <= lon <= bf.MX_LON_MAX, f"Line {idx} lon out of Mexico bounds: {lon}"
        assert bf.MX_LAT_MIN <= lat <= bf.MX_LAT_MAX, f"Line {idx} lat out of Mexico bounds: {lat}"

        if bf.CDMX_LAT_MIN <= lat <= bf.CDMX_LAT_MAX and bf.CDMX_LON_MIN <= lon <= bf.CDMX_LON_MAX:
            cdmx_count += 1

        # Deterministic ascending sort by (lat, lon)
        key = (lat, lon)
        if prev_key is not None:
            assert key >= prev_key, f"Line {idx} out of deterministic sort order: {key} < {prev_key}"
        prev_key = key

        # Strict Garmin-POI / br-open-speedcameras / Open Zeekr naming conventions
        assert NAME_RE.match(name), f"Line {idx} invalid col 3 name format: {name!r}"
        assert comment.startswith("MX Radar "), f"Line {idx} unexpected col 4 comment: {comment!r}"
        assert not ZEEKR_SPEED_RE.search(comment), f"Line {idx} comment must not contain @<digits>: {comment!r}"

        # Exact mirror of Open Zeekr CameraStore.java parsing
        c1 = raw_line.index(",")
        c2 = raw_line.index(",", c1 + 1)
        assert abs(float(raw_line[:c1].strip()) - lon) < 1e-6
        assert abs(float(raw_line[c1 + 1 : c2].strip()) - lat) < 1e-6
        m = ZEEKR_SPEED_RE.search(raw_line[c2 + 1 :])
        if "@" in name:
            with_speed += 1
            assert m is not None, f"Line {idx} CameraStore regex failed to extract speed from {raw_line}"
            speed = int(m.group(1))
            assert 10 <= speed <= 150, f"Line {idx} extracted speed out of range: {speed}"
        else:
            assert m is None, f"Line {idx} unexpected speed extracted when no limit present: {raw_line}"

    assert cdmx_count >= 50, f"Expected >= 50 camera points inside CDMX bounding box, got {cdmx_count}"
    assert with_speed >= 50, f"Expected >= 50 cameras with known speed limits, got {with_speed}"


def test_gatso_no_duplicates_within_20_meters():
    _, _, rows = _load_gatso_rows()
    coords = [(float(r[1]), float(r[0])) for r in rows]  # (lat, lon)

    cell_deg = 0.0005  # ~55m
    grid: dict[tuple[int, int], list[tuple[int, float, float]]] = {}
    for idx, (lat, lon) in enumerate(coords):
        gx = int(math.floor(lat / cell_deg))
        gy = int(math.floor(lon / cell_deg))
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for other_idx, olat, olon in grid.get((gx + dx, gy + dy), ()):
                    dist = bf.haversine_m(lat, lon, olat, olon)
                    assert dist > 20.0, (
                        f"Duplicate points within 20m: row {other_idx + 1} ({olat}, {olon}) "
                        f"and row {idx + 1} ({lat}, {lon}) are {dist:.2f}m apart"
                    )
        grid.setdefault((gx, gy), []).append((idx, lat, lon))

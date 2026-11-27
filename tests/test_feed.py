"""Validation and unit test suite for Mexico Open Zeekr GATSO_ALL.csv feed."""

from __future__ import annotations

import csv
import io
import math
import re
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import build_feed as bf  # noqa: E402

GATSO_PATH = REPO_ROOT / "GATSO_ALL.csv"
CDMX_CACHE_PATH = REPO_ROOT / "data" / "cdmx_fotocivicas_cache.csv"

NAME_RE = re.compile(r"^(?:(?:max|average)(?: @\d{2,3})?|stop)$")
ZEEKR_SPEED_RE = re.compile(r"@\s*(\d{2,3})")
FORBIDDEN_TOKENS = ("nan", "none", "null", "undefined")


# ---------------------------------------------------------------------------
# Unit tests for parsing, cleaning, deduplication, and packaging
# ---------------------------------------------------------------------------


def test_parse_maxspeed_variants():
    assert bf.parse_maxspeed("50") == 50
    assert bf.parse_maxspeed("80 km/h") == 80
    assert bf.parse_maxspeed("MX:urban") == 50
    assert bf.parse_maxspeed("mx:rural") == 80
    assert bf.parse_maxspeed("MX:motorway") == 110
    assert bf.parse_maxspeed("MX:living_street") == 20
    assert bf.parse_maxspeed("50;80") == 50
    assert bf.parse_maxspeed("50 mph") == 80
    assert bf.parse_maxspeed(None) is None
    assert bf.parse_maxspeed("") is None
    assert bf.parse_maxspeed("signals") is None
    assert bf.parse_maxspeed("5") is None  # below sanity bound
    assert bf.parse_maxspeed("300") is None  # above sanity bound


def test_parse_direction_variants():
    assert bf.parse_direction("N") == 0
    assert bf.parse_direction("240") == 240
    assert bf.parse_direction("360") == 0
    assert bf.parse_direction("Norte a Sur") == 180
    assert bf.parse_direction("Sur a Norte") == 0
    assert bf.parse_direction("Oriente a Poniente") == 270
    assert bf.parse_direction("Poniente a Oriente") == 90
    assert bf.parse_direction("invalid") is None


def test_extract_osm_cameras_inherits_relation_and_excludes_surveillance():
    raw = {
        "elements": [
            # Direct speed_camera node inheriting parent way maxspeed
            {"type": "node", "id": 1, "lat": 19.4056, "lon": -99.1197, "tags": {"highway": "speed_camera"}},
            # Relation device node inheriting maxspeed & enforcement from relation
            {"type": "node", "id": 2, "lat": 19.4200, "lon": -99.1500},
            {"type": "node", "id": 3, "lat": 19.4190, "lon": -99.1500},
            {"type": "node", "id": 4, "lat": 19.4210, "lon": -99.1500},
            # Red-light relation device node
            {"type": "node", "id": 5, "lat": 19.4300, "lon": -99.1400},
            # Non-traffic C5 surveillance node that MUST be excluded
            {"type": "node", "id": 6, "lat": 19.1900, "lon": -100.1300},
            # Parent way for node 1
            {"type": "way", "id": 100, "nodes": [1], "tags": {"highway": "trunk", "maxspeed": "80", "name": "Viaducto"}},
            # Enforcement maxspeed relation
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
            # Non-traffic surveillance relation
            {
                "type": "relation",
                "id": 202,
                "tags": {"type": "enforcement", "enforcement": "surveillance", "name": "C5"},
                "members": [{"type": "node", "ref": 6, "role": "device"}],
            },
        ]
    }
    cams = bf.extract_osm_cameras(raw)
    by_id = {c.osm_id: c for c in cams}
    assert set(by_id.keys()) == {"node/1", "node/2", "node/5"}
    assert by_id["node/1"].camera_type == "fixed_speed"
    assert by_id["node/1"].maxspeed_kmh == 80
    assert by_id["node/2"].camera_type == "fixed_speed"
    assert by_id["node/2"].maxspeed_kmh == 50
    assert by_id["node/2"].direction_deg == 0  # south -> north
    assert by_id["node/5"].camera_type == "redlight"
    assert by_id["node/5"].maxspeed_kmh is None


def test_deduplicate_preserves_richest_metadata():
    p_cdmx = bf.CameraPoint(
        lat=19.405605,
        lon=-99.119721,
        camera_type="redlight",
        maxspeed_kmh=None,
        direction_deg=90,
        road_name="Viaducto",
        source="cdmx_fotocivicas",
    )
    # ~10 meters away, with known maxspeed and fixed_speed type
    p_osm = bf.CameraPoint(
        lat=19.405690,
        lon=-99.119721,
        camera_type="fixed_speed",
        maxspeed_kmh=80,
        direction_deg=None,
        road_name="",
        source="osm",
        osm_id="node/999",
    )
    # Invalid out-of-Mexico point should be dropped
    p_invalid = bf.CameraPoint(lat=0.0, lon=0.0, camera_type="fixed_speed", maxspeed_kmh=60)

    deduped = bf.deduplicate_cameras([p_cdmx, p_osm, p_invalid], radius_m=25.0)
    assert len(deduped) == 1
    merged = deduped[0]
    assert merged.maxspeed_kmh == 80
    assert merged.camera_type == "redlight_speed"
    assert merged.direction_deg == 90
    assert merged.road_name == "Viaducto"
    assert merged.source == "cdmx_fotocivicas+osm"


def test_release_zip_contains_gatso_all_csv(tmp_path: Path):
    cams = [
        bf.CameraPoint(lat=19.405605, lon=-99.119721, camera_type="fixed_speed", maxspeed_kmh=80),
        bf.CameraPoint(lat=19.423568, lon=-99.131166, camera_type="redlight", maxspeed_kmh=None),
    ]
    gatso = tmp_path / "GATSO_ALL.csv"
    enriched = tmp_path / "mx_speed_cameras.csv"
    bundle = tmp_path / "csv_files.zip"
    bf.write_gatso_csv(cams, gatso)
    bf.write_enriched_csv(cams, enriched)
    bf.write_release_zip(bundle, gatso, enriched, REPO_ROOT / "ATTRIBUTION.md")

    with zipfile.ZipFile(bundle, "r") as zf:
        names = set(zf.namelist())
        assert "GATSO_ALL.csv" in names
        assert "mx_speed_cameras.csv" in names
        assert "ATTRIBUTION.md" in names
        lines = zf.read("GATSO_ALL.csv").decode("utf-8").splitlines()
        assert lines == [
            '-99.119721,19.405605,"max @80","MX Radar fixed_speed"',
            '-99.131166,19.423568,"stop","MX Radar redlight"',
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
    prev_key: tuple[float, float] | None = None

    for idx, (raw_line, row) in enumerate(zip(lines, rows), start=1):
        # Verify balanced quotes on raw line and no header row
        assert raw_line.count('"') % 2 == 0, f"Line {idx} has unbalanced quotes: {raw_line}"
        assert len(row) == 4, f"Line {idx} expected 4 columns, got {len(row)}: {raw_line}"

        lon_s, lat_s, name, comment = row

        # No NaN / None / null strings anywhere
        for col in row:
            assert col.strip().lower() not in FORBIDDEN_TOKENS, f"Line {idx} contains forbidden token: {raw_line}"
            assert not re.search(r"\b(?:nan|none|null|undefined)\b", col, re.IGNORECASE), (
                f"Line {idx} contains forbidden token in {col!r}: {raw_line}"
            )

        # Strict float coordinate parsing in lon,lat order (mirrors Open Zeekr CameraStore.java)
        lon = float(lon_s)
        lat = float(lat_s)
        assert math.isfinite(lon) and math.isfinite(lat), f"Line {idx} non-finite coords"
        assert bf.MX_LON_MIN <= lon <= bf.MX_LON_MAX, f"Line {idx} lon out of Mexico bounds: {lon}"
        assert bf.MX_LAT_MIN <= lat <= bf.MX_LAT_MAX, f"Line {idx} lat out of Mexico bounds: {lat}"

        if bf.CDMX_LAT_MIN <= lat <= bf.CDMX_LAT_MAX and bf.CDMX_LON_MIN <= lon <= bf.CDMX_LON_MAX:
            cdmx_count += 1

        # Deterministic ascending sort by (lat, lon)
        key = (lat, lon)
        if prev_key is not None:
            assert key >= prev_key, f"Line {idx} out of deterministic sort order: {key} < {prev_key}"
        prev_key = key

        # Strict Garmin-POI / Open Zeekr naming & comment schema
        assert NAME_RE.match(name), f"Line {idx} invalid col 3 name format: {name!r}"
        assert comment.startswith("MX Radar "), f"Line {idx} unexpected col 4 comment: {comment!r}"
        assert not ZEEKR_SPEED_RE.search(comment), f"Line {idx} comment must not contain @<digits>: {comment!r}"

        # Mirror exact CameraStore.java parsing logic
        c1 = raw_line.index(",")
        c2 = raw_line.index(",", c1 + 1)
        assert abs(float(raw_line[:c1].strip()) - lon) < 1e-6
        assert abs(float(raw_line[c1 + 1 : c2].strip()) - lat) < 1e-6
        m = ZEEKR_SPEED_RE.search(raw_line[c2 + 1 :])
        if "@" in name:
            assert m is not None, f"Line {idx} CameraStore regex failed to extract speed from {raw_line}"
            speed = int(m.group(1))
            assert 10 <= speed <= 150, f"Line {idx} extracted speed out of range: {speed}"
        else:
            assert m is None, f"Line {idx} unexpected speed extracted when no limit present: {raw_line}"

    assert cdmx_count >= 50, f"Expected >= 50 camera points inside CDMX bounding box, got {cdmx_count}"


def test_gatso_no_duplicates_within_20_meters():
    _, _, rows = _load_gatso_rows()
    coords = [(float(r[1]), float(r[0])) for r in rows]  # (lat, lon)

    # Spatial grid check + exact Haversine distance verification for all candidate pairs
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

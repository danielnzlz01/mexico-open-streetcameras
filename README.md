# Mexico Open Street Cameras (`GATSO_ALL.csv`) for Open Zeekr

Automated open-data feed of fixed speed cameras, red-light cameras, and average-speed cameras across **Mexico**, formatted and packaged in the exact `csv_files.zip` → `GATSO_ALL.csv` Garmin-POI format expected by [**Open Zeekr**](https://github.com/borconi/openzeekr) (mirroring [`apgneto/br-open-speedcameras`](https://github.com/apgneto/br-open-speedcameras) and [`1e1/Open-GATSO-POI`](https://github.com/1e1/Open-GATSO-POI)).

## Adding This Feed in Open Zeekr

Open Zeekr pulls camera feeds from GitHub Releases (`releases/latest` → `csv_files.zip` → `GATSO_ALL.csv`) via [`GatsoUpdater.java`](https://github.com/borconi/openzeekr/blob/main/aa/src/main/java/io/aawireless/wirelessaamanipulator/hud/GatsoUpdater.java).

1. Open the **Open Zeekr** app on your head unit / phone.
2. Go to the **Android Auto** screen and scroll to **Speed camera sources**.
3. In the `owner/repo` field, enter:
   ```text
   danielnzlz01/mexico-open-streetcameras
   ```
4. Tap **Add**. On the next sync, Open Zeekr downloads the latest release's `csv_files.zip`, extracts `GATSO_ALL.csv`, and merges Mexico's cameras with your other enabled sources.

## Data Sources

1. **OpenStreetMap (Nationwide Mexico)** — queried via the Overpass API (`area["ISO3166-1"="MX"]["admin_level"="2"]`) across multiple fallback mirrors for:
   - `node["highway"="speed_camera"]`
   - `node["enforcement"="maxspeed" | "traffic_signals" | "average_speed"]`
   - `relation["type"="enforcement"]` (inheriting `maxspeed`, `enforcement`, and travel direction onto member `device` nodes, plus parent road `maxspeed` fallback; excluding non-traffic `surveillance` CCTV).
2. **CDMX Open Data — *Fotocívicas (Ubicación puntos)*** — fetched from the Secretaría de Movilidad ([`datos.cdmx.gob.mx`](https://datos.cdmx.gob.mx/dataset/informacion-fotocivicas)) with automatic fallback to a committed local snapshot ([`data/cdmx_fotocivicas_cache.csv`](data/cdmx_fotocivicas_cache.csv)) if the government portal is unreachable during a scheduled run.

Records are validated against Mexico's bounding box (`lat ∈ [14.5, 32.8]`, `lon ∈ [-118.5, -86.5]`), deduplicated within **25 meters** (preserving the richest speed-limit and camera-type metadata), and sorted deterministically by latitude/longitude.

## CSV Schema & Release Format

Each GitHub Release publishes:
- `csv_files.zip` containing:
  - `GATSO_ALL.csv` — Open Zeekr / Garmin-POI feed
  - `mx_speed_cameras.csv` — enriched dataset with `osm_id`, `lat`, `lon`, `maxspeed_kmh`, `direction_deg`, `type`, `road_name`, `source`
  - `ATTRIBUTION.md` — data licenses & attribution
- `GATSO_ALL.csv` (standalone asset, also tracked in this repository root)

`GATSO_ALL.csv` specification:
- **Columns (no header row):** `longitude,latitude,"name","comment"` (longitude first)
- **Encoding & line endings:** UTF-8 (no BOM), LF (`\n`), comma-delimited, RFC 4180 double-quoted text fields
- **Camera labels (`name`):** `max @<km/h>`, `max`, `stop` (red-light), or `average @<km/h>`
- **Comments (`comment`):** `MX Radar fixed_speed`, `MX Radar redlight`, `MX Radar redlight_speed`, or `MX Radar average_speed`

Example rows:
```csv
-99.119721,19.405605,"max @80","MX Radar fixed_speed"
-99.131166,19.423568,"stop","MX Radar redlight"
```

## Update Schedule

The GitHub Actions workflow ([`.github/workflows/update-feed.yml`](.github/workflows/update-feed.yml)) runs automatically:
- **Weekly** every **Sunday at 06:00 UTC**
- On-demand via **Actions → Update Mexico Speed & Red-Light Camera Feed → Run workflow** (`workflow_dispatch`)

Each run rebuilds `GATSO_ALL.csv`, executes the `pytest` validation suite, commits changes to `main` if the dataset changed, and publishes a new GitHub Release with `csv_files.zip` so Open Zeekr picks up updates automatically.

## Local Development & Verification

```bash
pip install -r requirements.txt
python scripts/build_feed.py --output GATSO_ALL.csv --dist-dir dist
pytest -v
```

## License & Attribution

- OpenStreetMap data © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors, licensed under **ODbL 1.0**.
- CDMX *Fotocívicas* data provided by **SEMOVI / Gobierno de la Ciudad de México** under **Libre Uso MX**.
- See [`ATTRIBUTION.md`](ATTRIBUTION.md) for full details.

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

Direct download URLs (updated on every scheduled and manual run):
- `https://github.com/danielnzlz01/mexico-open-streetcameras/releases/latest/download/csv_files.zip`
- `https://github.com/danielnzlz01/mexico-open-streetcameras/releases/latest/download/GATSO_ALL.csv`
- `https://raw.githubusercontent.com/danielnzlz01/mexico-open-streetcameras/main/GATSO_ALL.csv`

## Data Sources & Enrichment Pipeline

Sources are merged with strict precedence **`data/manual_mx.csv` > CDMX *Fotocívicas* > OpenStreetMap** and deduplicated within **25 meters**:

1. **Curated Manual Overrides (`data/manual_mx.csv`)** — highest priority for adding unmapped cameras, correcting speed limits/types, or suppressing confirmed false positives.
2. **CDMX Open Data — *Fotocívicas (Ubicación puntos)*** — fetched from the Secretaría de Movilidad ([`datos.cdmx.gob.mx`](https://datos.cdmx.gob.mx/dataset/informacion-fotocivicas)) with automatic fallback to [`data/cdmx_fotocivicas_cache.csv`](data/cdmx_fotocivicas_cache.csv) when the portal is unreachable.
3. **OpenStreetMap (Nationwide Mexico)** — queried via Overpass (`area["ISO3166-1"="MX"]["admin_level"="2"]`) across multiple fallback mirrors for:
   - `node["highway"="speed_camera"]`
   - `node["enforcement"="maxspeed" | "traffic_signals" | "average_speed"]`
   - `relation["type"="enforcement"]` (including `device` members tagged `highway=speed_camera` or `man_made=surveillance`, while excluding non-traffic `enforcement=surveillance` CCTV).
4. **Secondary Metadata-Only Enrichment:**
   - **CDMX Legacy *Fotomultas (Ubicaciones)*** — used strictly to backfill missing `maxspeed`/type on existing OSM or *Fotocívicas* points within 25 m (never inserts standalone legacy locations).
   - **Batched Nearest-Road `maxspeed` Inference** — queries Overpass `way(around:15)["highway"]["maxspeed"]` in batched chunks for cameras missing a numeric speed limit, preferring the highest road class (`motorway > trunk > primary > secondary > tertiary > residential`).

## Adding or Suppressing Cameras (`data/manual_mx.csv`)

You can patch missing or mislabeled cameras (e.g., Edomex, Puebla's *Monitor Vial*, Jalisco, or federal highways) directly in [`data/manual_mx.csv`](data/manual_mx.csv) without waiting for OpenStreetMap edits.

Schema:
```csv
lat,lon,type,maxspeed,direction,active,source,notes
```

- **`lat`, `lon`**: WGS84 decimal coordinates within Mexico (`lat` first in `manual_mx.csv`; `build_feed.py` outputs `lon,lat` in `GATSO_ALL.csv`).
- **`type`**: `speed`, `redlight`, or `average_speed`.
- **`maxspeed`**: Numeric limit in km/h (e.g., `80`), or blank to allow nearest-road inference.
- **`direction`**: Optional compass heading in degrees (`0`–`359`) or cardinal (`N`, `SE`, etc.).
- **`active`**:
  - `true` — adds the camera (or overrides any OSM / *Fotocívicas* point within **25 m**).
  - `false` — **suppression rule** that removes any OSM / *Fotocívicas* false positive within **25 m** during the merge.
- **`source`, `notes`**: Attribution and brief location context (e.g., `MonitorVial`, `Periférico Ecológico km 12`).

Example rows (do not commit placeholder coordinates):
```csv
19.041400,-98.206300,speed,70,180,true,MonitorVial,Puebla Periférico Ecológico
19.423568,-99.131166,speed,,,false,field_check,Confirmed decommissioned camera
```

## CSV Schema & Release Format

Each GitHub Release publishes:
- `GATSO_ALL.csv` (standalone asset, also tracked on `main`)
- `csv_files.zip` containing `GATSO_ALL.csv`, `mx_speed_cameras.csv`, and `ATTRIBUTION.md`
- `mx_speed_cameras.csv` (enriched metadata CSV)

`GATSO_ALL.csv` specification:
- **Columns (no header row):** `longitude,latitude,"name","comment"` (**longitude first**)
- **Encoding & line endings:** UTF-8 (no BOM), LF (`\n`), comma-delimited, RFC 4180 double-quoted text fields
- **Camera labels (`name`):** `max @<km/h>`, `max`, `stop @<km/h>`, `stop`, `average @<km/h>`, or `average`
- **Comments (`comment`):** `MX Radar fixed_speed`, `MX Radar redlight`, `MX Radar redlight_speed`, or `MX Radar average_speed`

## Update Schedule

The GitHub Actions workflow ([`.github/workflows/update-feed.yml`](.github/workflows/update-feed.yml)) runs:
- **Weekly** every **Sunday at 06:00 UTC**
- On-demand via **Actions → Update Mexico Speed & Red-Light Camera Feed → Run workflow** (`workflow_dispatch`)

Every run rebuilds `GATSO_ALL.csv`, executes `pytest -v`, commits updates to `main` when changed, and updates the `latest` GitHub Release (creating a new release tag when changed, or refreshing assets in place with `--clobber` so `releases/latest/download/GATSO_ALL.csv` and `csv_files.zip` always return HTTP 200).

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

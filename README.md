# Traffic Monitoring Pipeline
## Distributed CCTV Static Image Ingestion & Processing

A data engineering pipeline that ingests static images from 2,200+ traffic cameras operated by New York State's 511 system, stores raw assets reliably, audits data quality, and exposes structured data to downstream analysis systems via a REST API.

---

## Documentation

| Document | Description |
|----------|-------------|
| [Requirements Specification](docs/requirements_specification_en.md) | Downstream use cases, data quality requirements, API spec |
| [Pipeline Design](docs/pipeline_design_en.md) | DAG design, trigger properties, technology justification, MongoDB schema, setup instructions |

---

## Quick Start

```bash
# Step 1 — Start infrastructure on node0; wait for airflow-init to complete
docker compose -f docker-compose.node0.yml up -d

# Step 2 — Start workers on node1 and node2
docker compose -f docker-compose.node1.yml up -d
docker compose -f docker-compose.node2.yml up -d
```

Full deployment instructions (Fernet key generation, verification) are in [Pipeline Design § Setup](docs/pipeline_design_en.md).

| Service | URL |
|---------|-----|
| Airflow UI | `http://10.10.8.10:8080` |
| MinIO Console | `http://10.10.8.10:9001` |
| REST API | `http://10.10.8.12:8000` |
| Web Dashboard | Open `web/index.html`; set API base URL to `http://10.10.8.12:8000` |

---

## System Architecture

```
511ny.org (data source)
    ↓
[Ingestion]  ingest_web / ingest_web_priority
    ↓                  ↓
[Storage]  MinIO       MongoDB
         (images)    (metadata)
    ↓
[Quality]    quality_check → quality_audits
    ↓
[Curation]   curate → camera_hourly_summary
    ↓
[Features]   feature_extract → derived_features
    ↓
[Service]    FastAPI → Web Dashboard
```

---

## Pipeline Steps

### Step 1 — Camera Catalog Sync

**Schedule:** Daily  
**DAG:** `traffic_cameras_sync_web_pipeline`  
**File:** `app/sync_web_cameras.py`

Fetches all camera metadata from 511ny.org (paginated), writes to MongoDB `cameras` collection. Enriches missing state/county from a FIPS lookup table; reverse-geocodes valid coordinates via Nominatim. Cameras previously marked `no_feed` have their status preserved.

### Step 2 — Image Ingestion

**Normal pipeline — Schedule:** Every 10 minutes | **DAG:** `traffic_snapshot_web_pipeline` | **File:** `app/ingest_web.py`  
**Priority pipeline — Schedule:** Every 2 minutes | **DAG:** `traffic_snapshot_web_priority_pipeline` | **File:** `app/ingest_web_priority.py`

All `status=active` cameras are split across 3 parallel batches (100 cameras each). Priority cameras (I-87, I-95, I-495) are also ingested every 2 minutes. Capture timestamps are aligned to the scheduling interval boundary. Each image is MD5-checked for the "no live feed" placeholder and SHA-256 checksummed before upload to MinIO.

**MinIO path format:** `camera_id={id}/date={YYYY-MM-DD}/hour={HH}/{id}_{timestamp}.jpg`

### Step 3 — Data Quality Check

**Schedule:** Immediately after each ingest  
**File:** `app/quality_check.py`

Four quality flags are written to `quality_audits` for every expected (camera, time slot) pair:

| Check | Logic | Flag |
|-------|-------|------|
| Missing | No record or `success=False` | `is_missing_expected` |
| Duplicate | SHA-256 matches previous capture | `is_duplicate` |
| Corrupted | PIL cannot decode image from MinIO | `is_corrupted` |
| Delayed | `ingest_ts − capture_ts > 120s` | `is_delayed` |

### Step 4 — Hourly Curation

**Schedule:** Immediately after quality check  
**File:** `app/curate.py`

Aggregates the past 2 hours of `quality_audits` by (camera, date, hour) into `camera_hourly_summary`. Records completeness rate, duplicate/corrupted/delayed counts, and average delay. `no_feed` cameras receive a separate summary entry with `images_expected=0`.

### Step 5 — Feature Extraction

**Schedule:** Every 2 minutes  
**DAG:** `traffic_feature_extract_pipeline`  
**File:** `app/feature_extract_mock.py`

Scans for successful captures from the past 2 hours not yet in `derived_features`. Processes priority cameras first. Features computed: `vehicle_count`, `traffic_density`, `congestion_level`, `scene_change_score`, `confidence`. Current implementation is a mock extractor; the data model is production-ready for a real CV model.

### Step 6 — REST API & Dashboard

**File:** `app/api.py` + `web/index.html`

| Endpoint | Description |
|----------|-------------|
| `GET /cameras` | List cameras; filter by `region`, `roadway`, `county`, `state`, `priority`, `status` |
| `GET /cameras/{id}` | Single camera metadata |
| `GET /cameras/{id}/captures` | Historical captures with time-range filtering |
| `GET /cameras/{id}/summary` | Hourly completeness and quality metrics |
| `GET /cameras/{id}/features` | Derived CV features |
| `GET /images/{object_key}` | Proxy raw image from MinIO |

---

## Data Flow Timeline

```
T+0:00   Priority ingest      → download priority cameras (node1)
T+0:01   Quality check 2-min  → audit priority camera slots (node2)
T+0:01   Curate               → update hourly summary (node2)
T+0:02   Feature extract      → process backlog, priority first (node2)
         … (repeats every 2 minutes)

T+0:10   Normal ingest        → 3 parallel batches, all cameras (node1)
T+0:11   Quality check 10-min → audit all camera slots (node2)
T+0:11   Curate               → update hourly summary (node2)
         … (repeats every 10 minutes)

T+24:00  Catalog sync         → refresh camera list from 511ny.org (node2)
```

---

## Distributed Deployment

Three physical nodes connected over LAN, using Airflow CeleryExecutor with queue-based task routing.

| Node | IP | Services | Queue |
|------|----|----------|-------|
| node0 | 10.10.8.10 | PostgreSQL, Redis, MongoDB, MinIO, Airflow Scheduler, Webserver | — |
| node1 | 10.10.8.11 | Airflow Worker | `ingest` |
| node2 | 10.10.8.12 | Airflow Worker, FastAPI | `processing` |

---

## Team

| Member | Responsibility | Key Files |
|--------|---------------|-----------|
| Boyi Zhao | Data ingestion & camera catalog | `sync_web_cameras.py`, `ingest_web.py`, `ingest_web_priority.py` |
| Wenhao Wu | Storage, infrastructure & testing | `docker-compose.node0/1/2.yml`, `Dockerfile`, `utils.py` |
| Ziheng Pan | Data quality & curation | `quality_check.py`, `curate.py` |
| Russo | Feature extraction, API & dashboard | `feature_extract_mock.py`, `api.py`, `web/index.html` |

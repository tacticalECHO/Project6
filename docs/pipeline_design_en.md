# Pipeline Design Document
## Distributed Traffic Monitoring Pipeline — CCTV Static Image Ingestion & Processing

---

## 1. System Architecture Overview

The pipeline ingests static JPEG frames from the New York 511 traffic camera network (1,000+ cameras), stores raw assets in object storage, audits data quality, curates hourly summaries, and exposes structured data through a REST API.

```
511ny.org (data source)
        ↓
┌─────────────────────────────────────────┐
│         Ingestion Layer (node1)          │
│  ingest_web / ingest_web_priority        │
│  3 parallel batches × 100 cameras        │
└───────────────┬─────────────────────────┘
                ↓
      ┌─────────┴──────────┐
      ▼                    ▼
MinIO (raw images)    MongoDB (metadata)
      │                    │
      └─────────┬──────────┘
                ▼
┌─────────────────────────────────────────┐
│        Processing Layer (node2)          │
│  quality_check → curate → feature_extract│
└───────────────┬─────────────────────────┘
                ↓
┌─────────────────────────────────────────┐
│         Service Layer (node2)            │
│       FastAPI REST API + Web Dashboard   │
└─────────────────────────────────────────┘
```

---

## 2. DAG Design

The system runs 4 Airflow DAGs covering camera catalog sync, standard ingestion, priority ingestion, and feature extraction.

---

### 2.1 DAG: `traffic_camera_catalog_sync_web`

**File:** `dags/traffic_cameras_sync_web_pipeline.py`  
**Schedule:** `@daily` (once per day at 00:00 UTC)  
**Trigger properties:** `catchup=False`, `max_active_runs=1`  
**Execution node:** node2 (`processing` queue)

#### Task graph

```
sync_web_cameras
```

#### Description

Fetches all camera metadata from 511ny.org (paginated), parses WKT geometry for coordinates, enriches missing state/county from a built-in FIPS lookup table, and reverse-geocodes valid coordinates via Nominatim. Results are upserted into the MongoDB `cameras` collection. Cameras previously marked `no_feed` have their status preserved — not overwritten — to prevent false resets during source API outages.

---

### 2.2 DAG: `traffic_snapshot_web_pipeline` (Standard Full Ingestion)

**File:** `dags/traffic_snapshot_web_pipeline.py`  
**Schedule:** `*/10 * * * *` (every 10 minutes)  
**Trigger properties:** `catchup=False`, `max_active_runs=1`  
**Execution nodes:** `ingest_batch_*` → node1 (`ingest` queue); `quality_check` / `curate` → node2 (`processing` queue)

#### Task graph

```
ingest_batch_0 ──┐
ingest_batch_1 ──┼──→ quality_check ──→ curate_hourly_summary
ingest_batch_2 ──┘
```

#### Description

Reads all `status=active` cameras and distributes them across 3 parallel batches (up to 100 cameras each). Each batch runs independently on node1. The capture timestamp is floored to the nearest 10-minute boundary (e.g., a download at 14:07 is recorded as 14:00) to ensure consistent cross-cycle comparisons.

**Per-image processing:**
1. HTTP GET with a Unix timestamp cache-buster parameter
2. Validate HTTP 200 + `Content-Type: image/*`
3. Compute MD5; if it matches the known "no live feed" placeholder, mark `success=False` and set camera `status=no_feed`
4. Compute SHA-256 checksum
5. Upload to MinIO: `camera_id={id}/date={YYYY-MM-DD}/hour={HH}/{id}_{ts}.jpg`
6. Upsert metadata into MongoDB `raw_captures`

After all batches complete, `quality_check` audits the cycle and `curate` aggregates the hourly summary.

---

### 2.3 DAG: `traffic_snapshot_web_priority_pipeline` (Priority High-Frequency Ingestion)

**File:** `dags/traffic_snapshot_web_priority_pipeline.py`  
**Schedule:** `*/2 * * * *` (every 2 minutes)  
**Trigger properties:** `catchup=False`, `max_active_runs=1`  
**Execution nodes:** `ingest_priority_images` → node1; `quality_check` / `curate` → node2

#### Task graph

```
ingest_priority_images ──→ quality_check ──→ curate_hourly_summary
```

#### Description

Processes only cameras where `priority=True` (major highways: I-87 NYS Thruway, I-95 New England Thruway, I-495 Long Island Expressway). Capture timestamps are aligned to the 2-minute boundary. Quality check is called with `cycle_minutes=2` and audits only priority cameras. This pipeline coexists with the standard pipeline; records are distinguished by `pipeline: "priority"` and the different time alignment.

---

### 2.4 DAG: `traffic_feature_extract_pipeline`

**File:** `dags/traffic_feature_extract_pipeline.py`  
**Schedule:** `*/2 * * * *` (every 2 minutes)  
**Trigger properties:** `catchup=False`, `max_active_runs=1`  
**Execution node:** node2 (`processing` queue)

#### Task graph

```
extract_mock_features
```

#### Description

Runs independently of the ingestion DAGs. Scans `raw_captures` for successful captures from the past 2 hours that have no corresponding entry in `derived_features`. Processes up to 200 records per run, prioritising priority cameras first, then oldest-first (FIFO). Results are written to `derived_features` with a unique index on `(camera_id, capture_ts, model_id)` guaranteeing idempotency.

The current implementation is a mock extractor seeded deterministically from the image checksum. The data model and processing interface are production-ready; replacing the mock with a real CV model requires only reimplementing the feature computation function.

---

## 3. Trigger Properties Summary

| DAG | Schedule | `catchup` | `max_active_runs` | Queue(s) |
|-----|----------|-----------|-------------------|----------|
| `traffic_camera_catalog_sync_web` | `@daily` | False | 1 | processing |
| `traffic_snapshot_web_pipeline` | `*/10 * * * *` | False | 1 | ingest, processing |
| `traffic_snapshot_web_priority_pipeline` | `*/2 * * * *` | False | 1 | ingest, processing |
| `traffic_feature_extract_pipeline` | `*/2 * * * *` | False | 1 | processing |

**Why `catchup=False`:** The 511ny image API provides no historical replay. Backfilling historical slots would download the current image and record it under a past timestamp — producing incorrect time alignment. Enabling catchup after a restart would trigger a flood of meaningless runs.

**Why `max_active_runs=1`:** Prevents a slow run from spawning a concurrent run for the same pipeline. For a 10-minute cycle, two concurrent runs would attempt to write `raw_captures` records with the same `(camera_id, capture_ts)` unique key, causing duplicate-key conflicts.

---

## 4. Data Flow Timeline

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

T+24:00  Catalog sync         → refresh camera catalog from 511ny.org (node2)
```

---

## 5. Distributed Deployment

The pipeline runs on three physical machines using Airflow CeleryExecutor with explicit queue-based task routing.

### Node roles

| Node | IP | Services | Queue | Role |
|------|----|----------|-------|------|
| node0 | 10.10.8.10 | PostgreSQL, Redis, MongoDB, MinIO, Airflow Scheduler, Airflow Webserver | — | Infrastructure & scheduling control plane |
| node1 | 10.10.8.11 | Airflow Worker | `ingest` | I/O-intensive: HTTP downloads, MinIO uploads |
| node2 | 10.10.8.12 | Airflow Worker, FastAPI | `processing` | Compute-intensive: image decoding, aggregation, API |

**Why node0 runs no business tasks:** The Airflow scheduler is latency-sensitive. Co-locating it with hundreds of concurrent HTTP downloads (node1) or PIL decode operations (node2) would introduce scheduling jitter, degrading the timing precision of the 2-minute priority pipeline.

### Queue routing

```
Scheduler publishes task (with queue label)
        ↓
  Redis broker
  ┌────────────┬─────────────┐
  │  ingest    │  processing  │
  └─────┬──────┴──────┬───── ┘
        ↓              ↓
      node1           node2
```

### Task-to-node mapping

| DAG task | Queue | Node |
|----------|-------|------|
| `ingest_batch_0/1/2` | `ingest` | node1 |
| `ingest_priority_images` | `ingest` | node1 |
| `quality_check` | `processing` | node2 |
| `curate_hourly_summary` | `processing` | node2 |
| `extract_mock_features` | `processing` | node2 |
| `sync_web_cameras` | `processing` | node2 |

---

## 6. MongoDB Schema

Five collections store all pipeline metadata.

| Collection | Written by | Purpose |
|------------|-----------|---------|
| `cameras` | Daily sync | Camera catalog and status management |
| `raw_captures` | Each ingest | Per-image metadata and capture result |
| `quality_audits` | Each quality check | Per-slot quality flags |
| `camera_hourly_summary` | Each curate | Hourly completeness & quality report |
| `derived_features` | Each feature extraction | CV analysis results |

### Key indexes

| Collection | Index | Purpose |
|------------|-------|---------|
| `cameras` | `camera_id` (unique) | Primary lookup |
| `raw_captures` | `(camera_id, capture_ts)` (unique) | Deduplication, time-range queries |
| `quality_audits` | `(camera_id, capture_ts)` (unique) | Per-slot audit lookup |
| `camera_hourly_summary` | `(camera_id, date, hour)` (unique) | Per-hour summary lookup |
| `derived_features` | `(camera_id, capture_ts, model_id)` (unique) | Idempotent upsert, multi-model support |

---

### 6.1 `cameras` Collection

Synced daily from 511ny.org. Stores static metadata and status for every camera.

| Field | Type | Description |
|-------|------|-------------|
| `camera_id` | String | Unique identifier, e.g. `NY511_50` |
| `source` | String | Data source, fixed as `511NY` |
| `source_camera_id` | String | Original ID from the source system |
| `roadway` | String | Road name, e.g. `I-87 - NYS Thruway` |
| `direction` | String | Travel direction, e.g. `Northbound` |
| `location` | String | Location description |
| `latitude` | Float | Latitude |
| `longitude` | Float | Longitude |
| `state` | String | US state |
| `county` | String | County |
| `region` | String | Region, e.g. `NYC Metropolitan` |
| `status` | String | `active` / `inactive` / `no_feed` |
| `priority` | Boolean | Whether on a priority highway (I-87, I-95, I-495) |
| `image_url` | String | URL used to fetch the camera image |
| `geo_quality` | String | Source of geo data: `source` / `fips` / `geocoded` / `invalid_coordinates` |
| `last_catalog_refresh_ts` | Date | Last catalog sync timestamp |
| `last_ingested_at` | Date | Most recent successful capture timestamp |

---

### 6.2 `raw_captures` Collection

Written on every ingest cycle. Records the download result and storage location for each image.

| Field | Type | Description |
|-------|------|-------------|
| `camera_id` | String | Camera identifier |
| `capture_ts` | Date | Aligned capture slot (UTC), e.g. 14:07 → 14:00 |
| `ingest_ts` | Date | Actual download completion time (UTC) |
| `object_key` | String | MinIO path, e.g. `camera_id=NY511_50/date=2026-04-19/hour=14/...jpg` |
| `checksum` | String | SHA-256 hex digest |
| `file_size` | Int | Image size in bytes |
| `success` | Boolean | Whether capture succeeded |
| `http_status` | Int | HTTP response code |
| `error_message` | String | Failure reason; `null` on success |
| `content_type` | String | HTTP Content-Type, e.g. `image/jpeg` |
| `source_url` | String | Requested image URL |
| `pipeline` | String | `web` (standard) or `priority` |
| `roadway` | String | Denormalised from `cameras` for fast filtering |
| `direction` | String | Denormalised from `cameras` |
| `location` | String | Denormalised from `cameras` |

---

### 6.3 `quality_audits` Collection

Written after each quality check. One document per expected (camera, capture slot) pair.

| Field | Type | Description |
|-------|------|-------------|
| `camera_id` | String | Camera identifier |
| `capture_ts` | Date | Audited time slot |
| `audit_ts` | Date | Time the audit ran |
| `is_missing_expected` | Boolean | Expected capture absent or `success=False` |
| `is_duplicate` | Boolean | SHA-256 matches the previous capture (no scene change) |
| `is_corrupted` | Boolean | PIL failed to decode the image |
| `is_delayed` | Boolean | `ingest_ts − capture_ts > 120s` |
| `delay_seconds` | Int | Actual delay in seconds |
| `raw_capture_found` | Boolean | Whether a `raw_captures` record exists |
| `raw_capture_success` | Boolean | Whether the corresponding capture succeeded |
| `object_key` | String | MinIO path of the audited image |
| `corruption_reason` | String | Reason for corruption; `null` if clean |
| `notes` | Array | List of issue descriptions |

---

### 6.4 `camera_hourly_summary` Collection

Written by the curate task. Aggregates quality metrics per (camera, date, hour).

| Field | Type | Description |
|-------|------|-------------|
| `camera_id` | String | Camera identifier |
| `date` | String | Date string, e.g. `2026-04-19` |
| `hour` | Int | Hour of day (0–23) |
| `camera_status` | String | `active` or `no_feed` |
| `images_expected` | Int | Expected captures this hour (normal: 6, priority: 30) |
| `images_received` | Int | Successful captures received |
| `completeness_rate` | Float | `images_received / images_expected` |
| `duplicate_count` | Int | Number of duplicate frames |
| `corrupted_count` | Int | Number of corrupted images |
| `delayed_count` | Int | Number of delayed captures |
| `avg_delay_sec` | Float | Average delay in seconds |
| `last_updated_ts` | Date | Timestamp of last update |

---

### 6.5 `derived_features` Collection

Written by the feature extraction task. Stores CV model results for each successfully ingested image.

| Field | Type | Description |
|-------|------|-------------|
| `camera_id` | String | Camera identifier |
| `capture_ts` | Date | Capture slot of the source image |
| `object_key` | String | MinIO path — direct link to the source image |
| `model_id` | String | Model identifier, e.g. `mock_extractor` |
| `model_version` | String | Model version, e.g. `1.0` |
| `processed_at` | Date | Feature extraction timestamp |
| `processing_ms` | Int | Processing time in milliseconds |
| `success` | Boolean | Whether extraction succeeded |
| `error_message` | String | Failure reason; `null` on success |
| `vehicle_count` | Int | Estimated vehicle count |
| `traffic_density` | Float | Traffic density (0.0–1.0) |
| `congestion_level` | String | `free` / `moderate` / `heavy` / `standstill` |
| `scene_change_score` | Float | Difference from previous frame (0.0 = duplicate, up to 1.0) |
| `confidence` | Float | Model confidence score (0.75–0.95) |

---

## 7. Technology Justification

### 7.1 Apache Airflow (Workflow Orchestration)

**Selected because:**
- DAG-based task orchestration expresses task dependencies (`ingest → quality_check → curate`) as version-controlled code
- CeleryExecutor natively supports multi-node distributed execution; queue labels provide precise task-to-node routing without external coordination
- Built-in retry, timeout, and alerting reduce operational development effort
- Web UI provides real-time task status and full run history
- Deployed via Docker Compose (`docker-compose.node0/1/2.yml`) to guarantee identical runtime environments across all three physical nodes

**Alternatives:**

| Alternative | Why not chosen |
|-------------|----------------|
| **Cron + shell scripts** | No dependency management, no distributed support, no visibility into task state or history. Suitable only for single-machine, independent jobs. |

### 7.2 MongoDB (Metadata Store)

**Selected because:**
- Document model accommodates heterogeneous camera metadata (different cameras have different missing geographic fields) without requiring schema migrations
- Aggregation pipeline natively supports the group-by statistics needed for `camera_hourly_summary`
- Write throughput (hundreds of records every 2 minutes) is well within MongoDB's capabilities
- References between collections via `camera_id + capture_ts` match document store patterns naturally

**Alternatives:**

| Alternative | Why not chosen |
|-------------|----------------|
| **PostgreSQL** | Strong schema constraints would require many nullable columns or JSONB for heterogeneous geo fields — effectively degenerating into semi-structured storage. |

### 7.3 MinIO (Object Storage)

**Selected because:**
- S3-compatible API; the `camera_id/date/hour/` partitioning scheme maps directly to S3 prefix queries, enabling a seamless future migration to cloud storage
- JPEG images (50–200 KB each) are binary blobs — they belong in object storage, not in a row-store or document database
- Self-hosted, keeping all data within the private cluster (cluster2) and meeting any data residency requirements

**Alternatives:**

| Alternative | Why not chosen |
|-------------|----------------|
| **Local filesystem** | Cannot be shared across nodes. With 3-node distributed deployment, images uploaded on node1 would not be accessible for quality checks on node2 without additional network mounts. |

### 7.4 Redis (Message Broker)

**Selected because:**
- Official recommended Celery broker for Airflow CeleryExecutor; well-documented integration path
- In-memory data store: task queue push/pop latency is sub-millisecond, suitable for the 2-minute priority pipeline's tight timing
- Simple operational model — a single Redis instance is sufficient for this queue volume

**Alternatives:**

| Alternative | Why not chosen |
|-------------|----------------|
| **RabbitMQ** | Richer routing rules and dead-letter queues are not needed here. Two simple queues (`ingest`, `processing`) do not justify the added operational complexity. |
| **Kafka** | Designed for high-throughput, persistent message streams with consumer group replay. Using Kafka as a Celery broker is complex to configure and introduces unnecessary guarantees (message retention, partition management) for what is a simple task queue. |

### 7.5 FastAPI (API Service)

**Selected because:**
- Native `async/await` support handles concurrent I/O-bound requests (MinIO image proxy + MongoDB queries) without thread blocking
- Automatic OpenAPI documentation generation simplifies downstream system integration
- Lightweight footprint; co-located with the Airflow processing worker on node2 without resource contention

**Alternatives:**
- *Django*: Full-stack framework with ORM, templating engine, and admin panel. This project requires only a pure API layer; Django's overhead is unjustified.

### 7.6 Batch Processing vs. Stream Processing (Course Week 12)

**Selected because:**
- The 511ny.org API is a passive REST endpoint — the pipeline must actively make an HTTP request to retrieve each image; the source does not push events. Stream processing frameworks assume a continuous, producer-driven event flow and do not fit a pull-based ingestion model
- The core requirement is "capture an image from each camera at a specific point in time" — a scheduling problem, not a streaming problem. Airflow DAGs with cron expressions map directly to this semantics
- Each 10-minute cycle downloads ~300 images; each 2-minute cycle downloads tens. Batch processing time is well under the scheduling interval; Kafka cluster overhead would exceed the actual computation

**Alternatives:**

| Alternative | Why not chosen |
|-------------|----------------|
| **Kafka** | Suited for high-throughput, persistent event streams (logs, clickstreams, sensor pushes). Using it here would require a separate producer writing timed trigger messages to a topic — effectively reimplementing Airflow's scheduler inside Kafka with an extra, purposeless hop. |


---

## 8. Setup & Deployment Instructions

### 8.1 Prerequisites

- Three machines networked over LAN (10.10.8.10 / 10.10.8.11 / 10.10.8.12)
- Docker Engine and Docker Compose v2 installed on all nodes
- Repository cloned to the same path on each node

### 8.2 Generate Fernet Key

All three nodes must share the same Fernet key:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Set the output as `AIRFLOW__CORE__FERNET_KEY` in `docker-compose.node0.yml`, `docker-compose.node1.yml`, and `docker-compose.node2.yml`.

### 8.3 Startup Sequence

**Step 1 — Start infrastructure on node0:**

```bash
# On node0 (10.10.8.10)
docker compose -f docker-compose.node0.yml up -d
# Wait for airflow-init to exit cleanly
docker compose -f docker-compose.node0.yml logs airflow-init -f
```

**Step 2 — Start the ingest worker on node1:**

```bash
# On node1 (10.10.8.11)
docker compose -f docker-compose.node1.yml up -d
```

**Step 3 — Start the processing worker and API on node2:**

```bash
# On node2 (10.10.8.12)
docker compose -f docker-compose.node2.yml up -d
```

### 8.4 Verification

```bash
# Check worker registration (Airflow UI → Admin → Workers)
# Should show node1 listening on 'ingest' and node2 on 'processing'

# Check API
curl http://10.10.8.12:8000/cameras

# Check MinIO bucket
# Open http://10.10.8.10:9001 — bucket 'traffic-camera-images' should exist
```

### 8.5 Service Endpoints

| Service | URL |
|---------|-----|
| Airflow UI | `http://10.10.8.10:8080` |
| MinIO Console | `http://10.10.8.10:9001` |
| FastAPI / REST API | `http://10.10.8.12:8000` |
| Web Dashboard | Open `web/index.html` in browser; set API base URL to `http://10.10.8.12:8000` |


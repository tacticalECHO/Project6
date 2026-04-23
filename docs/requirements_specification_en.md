# Pipeline Requirements Specification
## Distributed Traffic Monitoring Pipeline — CCTV Static Image Ingestion & Processing

---

## 1. Overview

This document defines the downstream data needs that the traffic monitoring pipeline is designed to support. The pipeline ingests static images from 2,200+ traffic cameras operated by New York State's 511 system, reliably stores raw assets, and prepares curated datasets for downstream analysis.

**The pipeline does not perform downstream analysis.** It provides the data infrastructure that makes such analysis possible. Each team member is responsible for one pipeline component; downstream traffic analysis (vehicle counting, congestion classification, etc.) is out of scope and belongs to consuming systems.

---

## 2. Data Sources

| Source | Type | Description |
|--------|------|-------------|
| 511ny.org Image API | REST/HTTP | Returns JPEG frames per camera ID; no historical replay |
| 511ny.org Catalog API | REST/HTTP | Paginated list of all registered cameras with metadata |

The source APIs provide no historical playback capability. Images must be captured at the scheduled time; missed captures cannot be retroactively retrieved. This is a core design constraint that affects every scheduling decision in the pipeline.

---

## 3. Downstream Use Cases & Pipeline Support Requirements

### 3.1 Traffic Condition Analysis

**Downstream need:** Analysts need to assess road conditions per camera over time — identifying congestion patterns, lane occupancy, and visible incidents.

**Pipeline must provide:**
- Reliable per-slot image records stored in object storage with a consistent, queryable path scheme
- Metadata linking each image to its camera, timestamp, and geographic context (roadway, direction, region)
- Pre-computed metrics in `derived_features`: estimated vehicle count, traffic density (0.0–1.0), congestion level (`free` / `moderate` / `heavy` / `standstill`), and scene change score
- Stable join keys (`camera_id + capture_ts + object_key`) so any downstream task can retrieve the source image for any feature record

### 3.2 Time-Based Monitoring

**Downstream need:** Support comparisons of the same camera across time-of-day or date; retrieve ordered image sequences within a specified window.

**Pipeline must provide:**
- Images stored under time-aligned capture timestamps (rounded to the scheduling interval boundary, not the raw download time), ensuring consistent cross-cycle comparisons
- Both `capture_ts` (aligned slot) and `ingest_ts` (actual download time) per record, so downstream can distinguish planned availability from actual availability
- High-frequency capture (every 2 minutes) for priority cameras alongside standard capture (every 10 minutes), allowing time-series resolution to be matched to downstream needs at the per-camera level
- `camera_hourly_summary` aggregated by (camera, date, hour) with completeness rate, duplicate count, and delay metrics, so downstream can assess data reliability before running analysis on a given window

### 3.3 Camera-Centric Queries

**Downstream need:** Efficient access to all images and metadata for a given camera; filtering by roadway, region, direction, or time interval.

**Pipeline must provide:**
- `cameras` collection with structured, queryable metadata: `camera_id`, `roadway`, `direction`, `location`, `latitude`, `longitude`, `state`, `county`, `region`, `status`, `priority`
- Geographic enrichment beyond what the source API provides: state/county backfilled from FIPS lookup when absent; Nominatim reverse geocoding for cameras with valid coordinates
- Object storage paths partitioned by `camera_id / date / hour`, enabling efficient retrieval of all images for a camera or time range without full collection scans
- REST API endpoints supporting filters on `region`, `roadway`, `county`, `state`, `priority`, `status`, and time range (see Section 5)

### 3.4 Derived Feature Extraction

**Downstream need:** Enable image processing tasks to compute vehicle counts, traffic density, and scene change features; maintain traceability from feature back to source image.

**Pipeline must provide:**
- `derived_features` collection with fields: `camera_id`, `capture_ts`, `object_key`, `model_id`, `model_version`, `vehicle_count`, `traffic_density`, `congestion_level`, `scene_change_score`, `confidence`, `processed_at`
- `object_key` in `derived_features` pointing directly to the raw image in MinIO, ensuring every feature record traces to its source
- `model_id` and `model_version` fields to support multiple model iterations in the same collection without overwriting historical results
- Idempotent processing: re-running feature extraction on already-processed records does not create duplicates (enforced by unique index on `camera_id + capture_ts + model_id`)
- Priority camera images processed within the same 2-minute cycle as ingestion; standard cameras processed in the next available 2-minute feature extraction window

> **Note:** The current pipeline uses a mock feature extractor (`feature_extract_mock.py`). The data model and processing interface are fully defined; replacing the mock with a real CV model requires only re-implementing the feature computation function — no schema or pipeline structure changes are needed.

### 3.5 Operational Reporting

**Downstream need:** Dashboards or reports showing traffic conditions by corridor, region, or time slot; ability to view both summary metrics and source images.

**Pipeline must provide:**
- `camera_hourly_summary` collection with per-camera per-hour metrics: `images_expected`, `images_received`, `completeness_rate`, `duplicate_count`, `corrupted_count`, `delayed_count`, `avg_delay_sec`
- Distinction between `no_feed` cameras (source hardware/signal issue) and failed captures, so reports can accurately reflect operational health versus pipeline health
- REST API (`/cameras`, `/cameras/{id}/summary`, `/cameras/{id}/features`, `/cameras/{id}/captures`, `/images/{object_key}`) so dashboards can query metadata, metrics, features, and retrieve raw images without direct database access

---

## 4. Data Quality Requirements

The pipeline must produce data of sufficient quality that downstream consumers can assess reliability before use.

| Requirement | Implementation |
|-------------|----------------|
| **Completeness tracking** | Every expected capture slot is audited; missing or failed captures are explicitly recorded in `quality_audits` with `is_missing_expected=True` |
| **Duplicate detection** | SHA-256 hashes compared across consecutive captures per camera; MD5-based detection of the static "no live feed" placeholder image |
| **Corruption detection** | Images downloaded from MinIO and decoded with PIL post-ingestion; failures flagged as `is_corrupted=True` |
| **Delay tracking** | `ingest_ts − capture_ts` recorded per image; captures exceeding 120 seconds flagged as `is_delayed=True` |
| **Checksum integrity** | SHA-256 checksum stored in `raw_captures` per image; enables downstream verification of file integrity at any time |

---

## 5. Downstream Access API

| Endpoint | Purpose |
|----------|---------|
| `GET /cameras` | List cameras; filter by `region`, `roadway`, `county`, `state`, `priority`, `status` |
| `GET /cameras/{id}` | Full metadata for a single camera |
| `GET /cameras/{id}/captures` | Historical capture records with time-range filtering |
| `GET /cameras/{id}/summary` | Hourly completeness and quality metrics |
| `GET /cameras/{id}/features` | Derived CV features with optional model filtering |
| `GET /images/{object_key}` | Proxy raw image from MinIO |

---

## 6. Scalability Requirements

The pipeline must accommodate growth without requiring architectural redesign:

- **More cameras:** Ingestion is batch-partitioned; adding cameras increases batch load, not structure
- **Higher frequency:** Schedule interval is a DAG configuration parameter; storage and data model are interval-agnostic
- **Additional metadata:** The `cameras` collection uses a document model (MongoDB); new fields require no migration
- **New downstream systems:** The REST API decouples consumers from direct database access

---

## 7. Project Scope Boundary

This project delivers the **data pipeline** that supports downstream traffic analysis. It explicitly excludes:

- Real-time traffic incident detection
- Congestion prediction or forecasting
- Vehicle tracking across cameras
- Any alerting or notification system

These are downstream responsibilities. The pipeline's obligation is to provide reliable, quality-audited, time-stamped image data and metadata through a stable API that downstream systems can build on.

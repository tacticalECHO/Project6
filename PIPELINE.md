# Traffic Monitoring Pipeline — 系统流程文档

## 系统架构总览

```
511ny.org (数据源)
    ↓
[采集层] ingest_web / ingest_web_priority
    ↓              ↓
[存储层] MinIO    MongoDB
(原始图片)      (元数据)
    ↓
[质量层] quality_check → quality_audits
    ↓
[汇总层] curate → camera_hourly_summary
    ↓
[特征层] feature_extract → derived_features
    ↓
[服务层] FastAPI → Web Dashboard
```

---

## 第一步：摄像头目录同步

**调度频率：** 每天一次  
**DAG：** `traffic_cameras_sync_web_pipeline`  
**文件：** `app/sync_web_cameras.py`

511ny.org 提供纽约州约 1000+ 个交通摄像头的元数据。系统每天分页拉取所有摄像头信息，解析后写入 MongoDB `cameras` 集合。

### cameras 集合字段

| 字段 | 说明 |
|------|------|
| `camera_id` | 唯一标识，如 `NY511_50` |
| `roadway` | 所在路段，如 `I-87 - NYS Thruway` |
| `direction` | 方向，如 `Northbound` |
| `location` | 位置描述 |
| `latitude / longitude` | 坐标 |
| `state / county / region` | 地理信息 |
| `status` | `active` / `inactive` / `no_feed` |
| `priority` | 是否为重点路段（I-87、I-95、I-495） |
| `image_url` | 图片抓取地址 |
| `geo_quality` | 地理信息来源（`source` / `fips` / `geocoded`） |

### 特殊处理逻辑

- 若 511ny 未提供 state/county，用内置 FIPS 编码表补全
- 若坐标有效，调用 Nominatim API 反向地理编码
- 若摄像头曾被标记为 `no_feed`，同步时**不覆盖**该状态（防止状态被错误重置）

---

## 第二步：图像采集

### Normal Pipeline（全量采集）

**调度频率：** 每 10 分钟  
**DAG：** `traffic_snapshot_web_pipeline`  
**文件：** `app/ingest_web.py`

- 读取所有 `status=active` 的摄像头
- 分 3 个批次并行处理（batch_0 / batch_1 / batch_2，每批 100 台）
- 时间槽对齐到 10 分钟边界（如 14:07 对齐到 14:00）

### Priority Pipeline（重点路段高频采集）

**调度频率：** 每 2 分钟  
**DAG：** `traffic_snapshot_web_priority_pipeline`  
**文件：** `app/ingest_web_priority.py`

- 只处理 `priority=True` 的摄像头
- 时间槽对齐到 2 分钟边界
- 保证主干道数据的时效性

### 每张图片的处理流程

```
1. 向 image_url 发 HTTP GET（带 Unix 时间戳参数防缓存）
2. 检查 HTTP 状态码 → 非 200 记录失败
3. 检查 Content-Type → 非 image/* 记录失败
4. 计算 MD5 → 匹配"No live camera feed"占位图则：
       标记 success=False，将摄像头 status 改为 no_feed
5. 计算 SHA-256 校验和
6. 上传图片到 MinIO，路径格式：
       camera_id={id}/date={YYYY-MM-DD}/hour={HH}/{id}_{timestamp}.jpg
7. 将元数据写入 MongoDB raw_captures 集合
```

### raw_captures 集合字段

| 字段 | 说明 |
|------|------|
| `camera_id` | 摄像头标识 |
| `capture_ts` | 对齐后的采集时间槽（UTC） |
| `ingest_ts` | 实际完成下载时间（UTC） |
| `object_key` | MinIO 中的存储路径 |
| `checksum` | SHA-256 校验和 |
| `file_size` | 图片字节数 |
| `success` | 是否成功采集 |
| `http_status` | HTTP 响应码 |
| `error_message` | 失败原因（成功时为 null） |
| `pipeline` | `priority` 或 `web`（normal） |

---

## 第三步：数据质量检查

**调度频率：** 紧跟采集任务之后  
**文件：** `app/quality_check.py`

采集完成后立即对当前时间槽做四项质量审计，结果写入 `quality_audits` 集合。

### 四项质量检查

| 检查项 | 判断逻辑 | 标记字段 |
|--------|---------|---------|
| **缺失** | raw_captures 中无记录，或 `success=False` | `is_missing_expected` |
| **重复** | 与上一张图片 SHA-256 相同（画面无变化） | `is_duplicate` |
| **损坏** | 从 MinIO 读取图片后 PIL 无法解码 | `is_corrupted` |
| **延迟** | `ingest_ts - capture_ts > 120 秒` | `is_delayed` |

### 双周期支持

- Normal pipeline 调用：`quality_check_main(cycle_minutes=10)` → 审计所有 active 摄像头
- Priority pipeline 调用：`quality_check_main(cycle_minutes=2)` → 只审计 priority 摄像头

### quality_audits 集合字段

| 字段 | 说明 |
|------|------|
| `camera_id` | 摄像头标识 |
| `capture_ts` | 被审计的时间槽 |
| `audit_ts` | 审计执行时间 |
| `is_missing_expected` | 是否缺失 |
| `is_duplicate` | 是否重复帧 |
| `is_corrupted` | 是否损坏 |
| `is_delayed` | 是否延迟 |
| `delay_seconds` | 实际延迟秒数 |
| `raw_capture_success` | 对应采集记录是否成功 |
| `notes` | 问题描述列表 |

---

## 第四步：小时汇总

**调度频率：** 紧跟质量检查之后  
**文件：** `app/curate.py`

将过去 2 小时的 `quality_audits` 按（摄像头, 日期, 小时）聚合，写入 `camera_hourly_summary` 集合，供下游报表和 API 查询使用。

### 期望采集数计算

| 摄像头类型 | 周期 | 每小时期望数 |
|-----------|------|------------|
| Normal | 10 分钟 | 6 |
| Priority | 2 分钟 | 30 |

### camera_hourly_summary 集合字段

| 字段 | 说明 |
|------|------|
| `camera_id` | 摄像头标识 |
| `date` | 日期，如 `2026-04-19` |
| `hour` | 小时（0-23） |
| `camera_status` | `active` 或 `no_feed` |
| `images_expected` | 该小时期望采集数 |
| `images_received` | 实际成功采集数 |
| `completeness_rate` | 完整率（received / expected） |
| `duplicate_count` | 重复帧数量 |
| `corrupted_count` | 损坏图片数量 |
| `delayed_count` | 延迟采集数量 |
| `avg_delay_sec` | 平均延迟秒数 |

### no_feed 摄像头处理

`no_feed` 摄像头同样会写入一条 summary 记录，`images_expected=0`，`camera_status="no_feed"`，让下游能区分"采集失败"和"摄像头本身无信号"。

同时监控特征提取积压：若过去 2 小时内未处理的 raw_captures 超过 150 条，输出 WARN 告警。

---

## 第五步：特征提取

**调度频率：** 每 2 分钟  
**DAG：** `traffic_feature_extract_pipeline`  
**文件：** `app/feature_extract_mock.py`

独立运行，扫描过去 2 小时内尚未被处理的成功采集记录，priority 摄像头优先处理，结果写入 `derived_features` 集合。

### 计算的特征

| 特征 | 说明 |
|------|------|
| `vehicle_count` | 估计车辆数 |
| `traffic_density` | 交通密度（0.0 - 1.0） |
| `congestion_level` | `free` / `moderate` / `heavy` / `standstill` |
| `scene_change_score` | 与上一帧的差异程度（0.0 - 1.0） |
| `confidence` | 模型置信度 |

### derived_features 关联关系

```
derived_features.camera_id + capture_ts + object_key
        ↓
raw_captures.camera_id + capture_ts
        ↓
MinIO: object_key → 原始图片文件
```

> 当前为 mock 实现，数据模型已为真实 CV 模型预留接口，替换时只需重写特征计算函数。

---

## 第六步：API 服务与 Dashboard

**文件：** `app/api.py` + `web/index.html`

### API 接口列表

| 接口 | 说明 |
|------|------|
| `GET /cameras` | 摄像头列表，支持 roadway / county / state / priority / status 过滤 |
| `GET /cameras/{id}` | 单个摄像头详情 |
| `GET /cameras/{id}/captures` | 历史采集记录，支持时间范围过滤 |
| `GET /cameras/{id}/summary` | 小时完整率汇总 |
| `GET /cameras/{id}/features` | CV 特征数据 |
| `GET /images/{object_key}` | 从 MinIO 代理返回原始图片 |

### Web Dashboard 功能

- 左侧摄像头列表：按 status / priority 筛选，按 roadway / county 搜索
- 右侧详情面板：摄像头元数据、历史采集图片（支持下载）、CV 指标展示

---

## 数据流时序总结

```
T+0:00   Priority ingest → 采集 priority 摄像头
T+0:01   Quality check (2min) → 审计 priority 摄像头
T+0:01   Curate → 更新小时汇总（含 no_feed 状态）
T+0:02   Feature extract → 处理新图片，priority 优先
         ...（每 2 分钟循环）

T+0:10   Normal ingest → 3 批并行采集所有摄像头
T+0:11   Quality check (10min) → 审计所有摄像头
T+0:11   Curate → 更新小时汇总
         ...（每 10 分钟循环）

T+24:00  Catalog sync → 从 511ny.org 更新摄像头目录
```

---

## MongoDB 集合总览

| 集合 | 写入时机 | 主要用途 |
|------|---------|---------|
| `cameras` | 每日同步 | 摄像头目录、状态管理 |
| `raw_captures` | 每次采集 | 原始图片元数据、采集结果 |
| `quality_audits` | 每次质量检查 | 逐条质量标记 |
| `camera_hourly_summary` | 每次汇总 | 小时级完整率报表 |
| `derived_features` | 每次特征提取 | CV 分析结果 |

---

## 分布式部署：三节点物理分工

系统部署在三台物理机上，通过 Airflow CeleryExecutor + Queue 路由机制控制任务分发。

### 什么是 Queue

Queue 是 Celery 的任务分发标签。每个 Airflow 任务在 DAG 中声明自己属于哪个 queue，每个 Worker 节点启动时指定监听哪个 queue，从而实现**指定任务只在指定机器上运行**。

```
Scheduler 发任务（带 queue 标签）
        ↓
  ┌─────────────────┐
  │  Redis (broker) │
  ├────────┬────────┤
  │ ingest │processing│
  └────┬───┴────┬───┘
       ↓        ↓
    node1     node2
  (--queues  (--queues
   ingest)   processing)
```

### 节点分配

| 节点 | IP | 运行服务 | 监听 Queue |
|------|-----|---------|-----------|
| **node0** | `10.10.8.10` | postgres、redis、mongo、minio、airflow-webserver、airflow-scheduler | — |
| **node1** | `10.10.8.11` | airflow-worker | `ingest` |
| **node2** | `10.10.8.12` | airflow-worker、FastAPI | `processing` |

### 任务 → Queue → 节点 对应关系

| DAG 任务 | Queue | 执行节点 |
|---------|-------|---------|
| `ingest_batch_0` | `ingest` | node1 |
| `ingest_batch_1` | `ingest` | node1 |
| `ingest_batch_2` | `ingest` | node1 |
| `ingest_priority_images` | `ingest` | node1 |
| `quality_check` | `processing` | node2 |
| `curate_hourly_summary` | `processing` | node2 |
| `extract_mock_features` | `processing` | node2 |
| `sync_web_cameras` | `processing` | node2 |

### 节点职责说明

- **node0**：基础设施 + 调度控制面，不跑业务任务，调度延迟敏感与业务隔离
- **node1**：I/O 密集型——大量 HTTP 请求下载图片并上传 MinIO
- **node2**：计算密集型——PIL 图片解码、特征计算、MongoDB 聚合，同时对外提供 API

### 启动顺序

```bash
# 1. node0 先启动，等待 airflow-init 完成
docker compose -f docker-compose.node0.yml up -d

# 2. node1、node2 再启动
docker compose -f docker-compose.node1.yml up -d   # 10.10.8.11
docker compose -f docker-compose.node2.yml up -d   # 10.10.8.12
```

### 对外访问地址

| 服务 | 地址 |
|------|------|
| Airflow UI | `http://10.10.8.10:8080` |
| MinIO Console | `http://10.10.8.10:9001` |
| API | `http://10.10.8.12:8000` |
| Web Dashboard | 浏览器打开 `web/index.html`，API 填 `http://10.10.8.12:8000` |

---

## 四人分工

| 成员 | 负责模块 | 核心文件 |
|------|---------|---------|
| A | 数据采集 & 摄像头目录 | `sync_web_cameras.py`, `ingest_web.py`, `ingest_web_priority.py` |
| B | 存储 & 基础设施 | `docker-compose.node0/1/2.yml`, `Dockerfile`, `utils.py` |
| C | 数据质量 & 汇总 | `quality_check.py`, `curate.py` |
| D | 特征提取 & API & Dashboard | `feature_extract_mock.py`, `api.py`, `web/index.html` |

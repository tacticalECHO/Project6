# 流水线设计文档
## 基于 CCTV 静态图像的分布式交通监控流水线

---

## 1. 系统架构总览

```
511ny.org（数据源）
        ↓
  ┌─────────────────────────────────────────┐
  │            采集层（node1）               │
  │  ingest_web / ingest_web_priority        │
  └───────────────┬─────────────────────────┘
                  ↓
        ┌─────────┴──────────┐
        ▼                    ▼
  MinIO（原始图像）     MongoDB（元数据）
        │                    │
        └─────────┬──────────┘
                  ▼
  ┌─────────────────────────────────────────┐
  │          处理层（node2）                 │
  │  quality_check → curate → feature_extract│
  └───────────────┬─────────────────────────┘
                  ↓
  ┌─────────────────────────────────────────┐
  │          服务层（node2）                 │
  │        FastAPI + Web Dashboard           │
  └─────────────────────────────────────────┘
```

---

## 2. DAG 设计

系统共有 4 个 Airflow DAG，覆盖摄像头目录同步、标准采集、优先采集和特征提取四条流水线。

---

### 2.1 DAG：`traffic_camera_catalog_sync_web`

**文件：** `dags/traffic_cameras_sync_web_pipeline.py`  
**调度：** `@daily`（每天一次）  
**触发属性：** `catchup=False`，`max_active_runs=1`  
**执行节点：** node2（`processing` queue）

#### 任务结构

```
sync_web_cameras
```

#### 流程说明

每天从 511ny.org 分页拉取所有摄像头元数据，解析后写入 MongoDB `cameras` 集合。对源数据缺少 state/county 的摄像头从内置 FIPS 编码表补全；对坐标有效的摄像头调用 Nominatim API 反向地理编码。曾被标记为 `no_feed` 的摄像头状态不被覆盖，防止状态错误重置。

---

### 2.2 DAG：`traffic_snapshot_web_pipeline`（标准全量采集）

**文件：** `dags/traffic_snapshot_web_pipeline.py`  
**调度：** `*/10 * * * *`（每 10 分钟）  
**触发属性：** `catchup=False`，`max_active_runs=1`  
**执行节点：** ingest_batch_* → node1；quality_check / curate → node2

#### 任务结构

```
ingest_batch_0 ──┐
ingest_batch_1 ──┼──→ quality_check ──→ curate_hourly_summary
ingest_batch_2 ──┘
```

#### 流程说明

读取所有 `status=active` 摄像头，分 3 个批次并行下载（每批最多 100 台），时间槽对齐到 10 分钟边界。每张图像：
1. HTTP GET 下载（带时间戳参数防缓存）
2. 检查 HTTP 状态码与 Content-Type
3. 计算 MD5 检测"无信号"占位图
4. 计算 SHA-256 校验和
5. 上传至 MinIO（路径：`camera_id={id}/date={YYYY-MM-DD}/hour={HH}/{id}_{ts}.jpg`）
6. 元数据写入 MongoDB `raw_captures`

采集完成后依次执行质量检查（`quality_check`）和小时汇总（`curate`）。

---

### 2.3 DAG：`traffic_snapshot_web_priority_pipeline`（优先路段高频采集）

**文件：** `dags/traffic_snapshot_web_priority_pipeline.py`  
**调度：** `*/2 * * * *`（每 2 分钟）  
**触发属性：** `catchup=False`，`max_active_runs=1`  
**执行节点：** ingest_priority_images → node1；quality_check / curate → node2

#### 任务结构

```
ingest_priority_images ──→ quality_check ──→ curate_hourly_summary
```

#### 流程说明

只处理 `priority=True` 的摄像头（I-87、I-95、I-495 等主干道），时间槽对齐到 2 分钟边界。处理逻辑与标准流水线相同，但质量检查以 `cycle_minutes=2` 调用，只审计优先摄像头。

---

### 2.4 DAG：`traffic_feature_extract_pipeline`

**文件：** `dags/traffic_feature_extract_pipeline.py`  
**调度：** `*/2 * * * *`（每 2 分钟）  
**触发属性：** `catchup=False`，`max_active_runs=1`  
**执行节点：** node2（`processing` queue）

#### 任务结构

```
extract_mock_features
```

#### 流程说明

独立运行，扫描过去 2 小时内尚未被处理（`derived_features` 中无对应记录）的成功采集记录。优先处理 `priority` 摄像头，结果写入 `derived_features` 集合。每条结果通过 `object_key` 直接关联 MinIO 中的原始图像。当前为 mock 实现，数据模型已为真实 CV 模型预留接口。

---

## 3. 触发属性汇总

| DAG | 调度 | catchup | max_active_runs | Queue |
|-----|------|---------|-----------------|-------|
| `traffic_camera_catalog_sync_web` | `@daily` | False | 1 | processing |
| `traffic_snapshot_web_pipeline` | `*/10 * * * *` | False | 1 | ingest / processing |
| `traffic_snapshot_web_priority_pipeline` | `*/2 * * * *` | False | 1 | ingest / processing |
| `traffic_feature_extract_pipeline` | `*/2 * * * *` | False | 1 | processing |

**`catchup=False` 的理由：** 图像 API 无历史回放能力，补跑历史时间槽只会请求当前图像，产生错误的时间戳对齐。

**`max_active_runs=1` 的理由：** 防止并发运行同一 DAG 造成同一时间槽被重复采集或重复审计。

---

## 4. 分布式部署

系统部署在三台物理机上，通过 Airflow CeleryExecutor + Celery Queue 路由机制控制任务分发。

### 节点分工

| 节点 | IP | 服务 | 监听 Queue | 职责定位 |
|------|-----|------|-----------|---------|
| node0 | 10.10.8.10 | Postgres、Redis、MongoDB、MinIO、Airflow Webserver、Scheduler | — | 基础设施 + 调度控制面 |
| node1 | 10.10.8.11 | Airflow Worker | `ingest` | I/O 密集型：HTTP 下载、MinIO 上传 |
| node2 | 10.10.8.12 | Airflow Worker、FastAPI | `processing` | 计算密集型：图像解码、特征计算、聚合查询、API 服务 |

**node0 不跑业务任务的理由：** 调度器对延迟敏感，与大量 I/O 或计算任务共置会导致调度抖动，影响全局时间精度。

### Queue 路由机制

```
Scheduler 发布任务（带 queue 标签）
        ↓
  Redis Broker
  ┌────────────┬─────────────┐
  │  ingest    │  processing  │
  └─────┬──────┴──────┬───── ┘
        ↓              ↓
      node1           node2
```

---

## 5. 数据流时序

```
T+0:00   Priority ingest    → 采集优先摄像头（node1）
T+0:01   Quality check 2min → 审计优先摄像头（node2）
T+0:01   Curate             → 更新小时汇总（node2）
T+0:02   Feature extract    → 处理新图像，priority 优先（node2）
         …（每 2 分钟循环）

T+0:10   Normal ingest      → 3 批并行采集所有摄像头（node1）
T+0:11   Quality check 10min→ 审计所有摄像头（node2）
T+0:11   Curate             → 更新小时汇总（node2）
         …（每 10 分钟循环）

T+24:00  Catalog sync       → 从 511ny.org 更新摄像头目录（node2）
```

---

## 6. 技术选型与理由

### 6.1 Apache Airflow（工作流调度）

**选择理由：**
- 基于 DAG 的任务编排，任务依赖关系（ingest → quality_check → curate）以代码形式表达，可版本控制
- CeleryExecutor 原生支持多节点分布式执行，Queue 机制实现任务到节点的精确路由
- 内置重试、超时、告警机制，减少运维开发量
- Web UI 提供实时任务状态监控和历史运行记录
- 通过 Docker Compose 分节点部署（`docker-compose.node0/1/2.yml`），保证三台物理机运行环境一致

**其他方案：**
- *Cron + shell scripts*：无依赖管理、无分布式支持、无可视化，适合单机简单任务

### 6.2 MongoDB（元数据存储）

**选择理由：**
- 文档模型适合摄像头元数据的异构字段（不同摄像头缺失不同地理字段），无需 schema migration 即可扩展
- 聚合管道（Aggregate Pipeline）原生支持 `camera_hourly_summary` 所需的 group-by 统计
- `cameras`、`raw_captures`、`quality_audits`、`derived_features` 四个集合之间通过 `camera_id + capture_ts` 关联，适合 document store 的引用模式
- 写入吞吐量满足每 2 分钟数百条记录的写入需求

**其他方案：**
- *PostgreSQL*：强 schema 约束，摄像头地理字段的可选性处理需要大量 nullable 列或 EAV 设计，不如文档模型直观

### 6.3 MinIO（对象存储）

**选择理由：**
- 兼容 S3 API，路径分区方案（`camera_id/date/hour/`）直接对应 S3 prefix 查询模式，未来可无缝迁移至云存储
- 图像文件（JPEG, ~50–200KB）天然适合对象存储，不适合存入关系型或文档型数据库
- 自托管，数据不离开私有集群，符合合规要求

**其他方案：**
- *本地文件系统*：无法跨节点共享访问，不支持分布式部署

### 6.4 Redis（消息代理）

**选择理由：**
- Airflow CeleryExecutor 的官方推荐 broker，配置简单，延迟低（亚毫秒级任务分发）

**其他方案：**
- *RabbitMQ*：功能更丰富（路由规则、死信队列），但本项目队列逻辑简单（两个 queue），引入 RabbitMQ 是过度设计
- *Kafka*：适合高吞吐持久化消息流，作为 Celery broker 配置复杂，且本项目不需要消息持久化回放

### 6.5 FastAPI（API 服务）

**选择理由：**
- 异步支持（async/await），适合同时处理 MinIO 图像代理和 MongoDB 查询的 I/O 密集型请求
- 自动生成 OpenAPI 文档，便于下游系统接入
- 轻量，与 Airflow Worker 共置于 node2 不产生资源冲突

**其他方案：**
- *Django*：全栈框架，本项目只需纯 API 层，Django 引入过多不需要的组件

### 6.6 批处理 vs 流处理（Streams）

**选择理由：**
- 511ny.org 是被动 REST API，系统必须主动发起 HTTP 请求才能获取图像，数据源本身不推送事件；流处理框架的前提是数据源持续产生事件流，不适用于主动拉取场景
- 本项目核心需求是"在特定时间点采集特定摄像头的图像"，这是调度问题，Airflow DAG + Cron 直接映射这一语义
- 每 10 分钟约 300 张图、每 2 分钟约数十张，单次批次处理时间远低于调度间隔，流处理集群的维护开销不合算

**其他方案：**
- *Kafka*：适合高吞吐、持久化的事件流（日志、点击流、传感器推送）。用于本项目需额外引入生产者定期向 topic 写触发消息，再由消费者拉取——等于把 Airflow 的调度职责转移到 Kafka，增加了无意义的中间层


---

## 7. MongoDB Schema

| 集合 | 写入时机 | 主要用途 |
|------|---------|---------|
| `cameras` | 每日同步 | 摄像头目录、状态管理 |
| `raw_captures` | 每次采集 | 原始图像元数据、采集结果 |
| `quality_audits` | 每次质量检查 | 逐条质量标记 |
| `camera_hourly_summary` | 每次汇总 | 小时级完整率报表 |
| `derived_features` | 每次特征提取 | CV 分析结果 |

### 关键索引

| 集合 | 索引 | 用途 |
|------|------|------|
| `cameras` | `camera_id`（唯一） | 主键查询 |
| `raw_captures` | `(camera_id, capture_ts)`（唯一） | 去重、时间范围查询 |
| `quality_audits` | `(camera_id, capture_ts)`（唯一） | 按时间槽查询审计记录 |
| `camera_hourly_summary` | `(camera_id, date, hour)`（唯一） | 按小时查询汇总 |
| `derived_features` | `(camera_id, capture_ts, model_id)`（唯一） | 幂等写入、多模型支持 |

---

### 7.1 `cameras` 集合

每日从 511ny.org 同步，存储所有摄像头的静态元数据与状态。

| 字段 | 类型 | 说明 |
|------|------|------|
| `camera_id` | String | 唯一标识，如 `NY511_50` |
| `source` | String | 数据来源，固定为 `511NY` |
| `source_camera_id` | String | 源系统原始 ID |
| `roadway` | String | 所在路段，如 `I-87 - NYS Thruway` |
| `direction` | String | 方向，如 `Northbound` |
| `location` | String | 位置描述 |
| `latitude` | Float | 纬度 |
| `longitude` | Float | 经度 |
| `state` | String | 所在州 |
| `county` | String | 所在县 |
| `region` | String | 区域，如 `NYC Metropolitan` |
| `status` | String | `active` / `inactive` / `no_feed` |
| `priority` | Boolean | 是否为重点路段（I-87、I-95、I-495） |
| `image_url` | String | 图像抓取地址 |
| `geo_quality` | String | 地理信息来源：`source` / `fips` / `geocoded` / `invalid_coordinates` |
| `last_catalog_refresh_ts` | Date | 最近一次目录同步时间 |
| `last_ingested_at` | Date | 最近一次成功采集时间 |

---

### 7.2 `raw_captures` 集合

每次采集写入，记录每张图像的下载结果与存储位置。

| 字段 | 类型 | 说明 |
|------|------|------|
| `camera_id` | String | 摄像头标识 |
| `capture_ts` | Date | 对齐后的采集时间槽（UTC），如 `14:07` 对齐为 `14:00` |
| `ingest_ts` | Date | 实际完成下载时间（UTC） |
| `object_key` | String | MinIO 存储路径，如 `camera_id=NY511_50/date=2026-04-19/hour=14/...jpg` |
| `checksum` | String | SHA-256 校验和（十六进制） |
| `file_size` | Int | 图像字节数 |
| `success` | Boolean | 是否成功采集 |
| `http_status` | Int | HTTP 响应码 |
| `error_message` | String | 失败原因，成功时为 `null` |
| `content_type` | String | HTTP Content-Type，如 `image/jpeg` |
| `source_url` | String | 请求的图像 URL |
| `pipeline` | String | `web`（标准）或 `priority`（优先） |
| `roadway` | String | 冗余字段，便于快速过滤 |
| `direction` | String | 冗余字段 |
| `location` | String | 冗余字段 |

---

### 7.3 `quality_audits` 集合

每次质量检查写入，对每个预期时间槽逐条记录四项质量标志。

| 字段 | 类型 | 说明 |
|------|------|------|
| `camera_id` | String | 摄像头标识 |
| `capture_ts` | Date | 被审计的时间槽 |
| `audit_ts` | Date | 审计执行时间 |
| `is_missing_expected` | Boolean | 预期采集但无记录或 `success=False` |
| `is_duplicate` | Boolean | SHA-256 与上一次采集相同（画面无变化） |
| `is_corrupted` | Boolean | PIL 无法解码图像 |
| `is_delayed` | Boolean | `ingest_ts − capture_ts > 120s` |
| `delay_seconds` | Int | 实际延迟秒数 |
| `raw_capture_found` | Boolean | `raw_captures` 中是否存在对应记录 |
| `raw_capture_success` | Boolean | 对应采集记录是否成功 |
| `object_key` | String | 对应图像的 MinIO 路径 |
| `corruption_reason` | String | 损坏原因，正常时为 `null` |
| `notes` | Array | 问题描述列表 |

---

### 7.4 `camera_hourly_summary` 集合

每次 curate 任务写入，按（摄像头、日期、小时）聚合质量指标。

| 字段 | 类型 | 说明 |
|------|------|------|
| `camera_id` | String | 摄像头标识 |
| `date` | String | 日期，如 `2026-04-19` |
| `hour` | Int | 小时（0–23） |
| `camera_status` | String | `active` 或 `no_feed` |
| `images_expected` | Int | 该小时预期采集数（normal: 6，priority: 30） |
| `images_received` | Int | 实际成功采集数 |
| `completeness_rate` | Float | 完整率（`received / expected`） |
| `duplicate_count` | Int | 重复帧数量 |
| `corrupted_count` | Int | 损坏图像数量 |
| `delayed_count` | Int | 延迟采集数量 |
| `avg_delay_sec` | Float | 平均延迟秒数 |
| `last_updated_ts` | Date | 最近一次更新时间 |

---

### 7.5 `derived_features` 集合

每次特征提取写入，存储 CV 模型对每张图像的分析结果。

| 字段 | 类型 | 说明 |
|------|------|------|
| `camera_id` | String | 摄像头标识 |
| `capture_ts` | Date | 对应图像的采集时间槽 |
| `object_key` | String | MinIO 图像路径，直接关联原始图像 |
| `model_id` | String | 模型标识，如 `mock_extractor` |
| `model_version` | String | 模型版本，如 `1.0` |
| `processed_at` | Date | 特征提取执行时间 |
| `processing_ms` | Int | 处理耗时（毫秒） |
| `success` | Boolean | 是否成功提取 |
| `error_message` | String | 失败原因，成功时为 `null` |
| `vehicle_count` | Int | 估计车辆数 |
| `traffic_density` | Float | 交通密度（0.0–1.0） |
| `congestion_level` | String | `free` / `moderate` / `heavy` / `standstill` |
| `scene_change_score` | Float | 与上一帧的差异程度（0.0–1.0，重复帧为 0.0） |
| `confidence` | Float | 模型置信度（0.75–0.95） |

---

## 8. 对外访问地址

| 服务 | 地址 |
|------|------|
| Airflow UI | `http://10.10.8.10:8080` |
| MinIO Console | `http://10.10.8.10:9001` |
| FastAPI | `http://10.10.8.12:8000` |
| Web Dashboard | 浏览器打开 `web/index.html`，API 地址填 `http://10.10.8.12:8000` |

---

## 9. 部署与启动说明

### 9.1 前置条件

- 三台物理机已通过内网互通（10.10.8.10 / 11 / 12）
- 所有节点已安装 Docker 和 Docker Compose v2
- 代码已克隆到各节点的相同路径

### 9.2 生成 Fernet Key

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

将生成的 key 填入三台节点的 `docker-compose.node*.yml` 的 `AIRFLOW__CORE__FERNET_KEY` 环境变量（三台必须一致）。

### 9.3 启动顺序

**第一步：在 node0 启动基础设施**

```bash
# node0（10.10.8.10）
docker compose -f docker-compose.node0.yml up -d
# 等待 airflow-init 容器退出（exit 0）后再继续
docker compose -f docker-compose.node0.yml logs airflow-init -f
```

**第二步：在 node1 启动采集 Worker**

```bash
# node1（10.10.8.11）
docker compose -f docker-compose.node1.yml up -d
```

**第三步：在 node2 启动处理 Worker 与 API**

```bash
# node2（10.10.8.12）
docker compose -f docker-compose.node2.yml up -d
```

### 9.4 验证

```bash
# 检查 Worker 是否注册
# 在 Airflow UI → Admin → Workers 可见 node1 和 node2

# 检查 MinIO bucket 创建
curl http://10.10.8.10:9000/traffic-camera-images

# 检查 API
curl http://10.10.8.12:8000/cameras
```


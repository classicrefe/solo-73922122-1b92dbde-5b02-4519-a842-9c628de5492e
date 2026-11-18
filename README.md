# 电表日结 SQL 核对与转线归属 API

服务包含两部分能力：

1. **SQL 核对**：数仓工程师改写电表日结的关联 SQL 后，用本服务确认读数
   **没有漏计或重复计入**。提交两条只读 `SELECT`，服务在同一已提交快照上并行
   读取两侧结果，按**行内容 + 出现次数**做多重集核对（忽略行序，`NULL` 与空串
   视为不同），返回一致结论或最多 20 种差异行及两侧次数。
2. **转线登记与历史归属**：表计转线后 `meters.line_name` 只保留现值，直接关联会
   把旧读数错归新线。登记每次转线的表号、生效时刻与目标产线后，可按 UTC 日期
   查询单表每个读数**采集时刻所属的产线**。

## 1. 快速启动

```bash
docker compose up --build
# API:   http://localhost:8000
# 文档:  http://localhost:8000/docs   （Swagger UI，可直接发请求演示）
```

启动后 PostgreSQL 自动执行 `sql/init.sql`，建好 `meters` / `readings`
源表、`meter_line_transfers` 转线表并写入示例数据；文件末尾附有可直接粘贴到
`/docs` 的两条等价示例核对查询。示例含表 1001 在 `2026-11-15 09:00 UTC`
从 A线转到 B线，打开 `/docs` 直接调
`GET /meters/1001/readings?date=2026-11-15` 即可看到 08:00 归 A线、
09:00 起归 B线。

停止并清理：

```bash
docker compose down -v
```

## 2. 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET`  | `/health` | 存活探针，不触碰数据库 |
| `POST` | `/jobs` | 提交核对任务（同时只允许一组运行，占用中返回 `409`） |
| `GET`  | `/jobs` | 列出近期任务（含终态），状态查询始终可用 |
| `GET`  | `/jobs/{job_id}` | 查询单个任务状态与结论 |
| `POST` | `/jobs/{job_id}/cancel` | 请求取消，及时受理 |
| `POST` | `/meters/{meter_id}/transfers` | 登记一次转线（表号、生效时刻、目标产线） |
| `GET`  | `/meters/{meter_id}/readings?date=YYYY-MM-DD` | 单表按 UTC 日期查询读数及当时产线 |

任务状态：`pending → running →（cancelling）→ succeeded | failed | cancelled | timeout`。
失败、取消、超时一律 **不发布比较结论**（`result` 为 `null`），仅给出原因。

### 请求示例

```bash
curl -X POST http://localhost:8000/jobs -H 'Content-Type: application/json' -d '{
  "left_sql":  "SELECT meter_id::bigint, wh::bigint FROM readings",
  "right_sql": "SELECT meter_id::bigint, wh::bigint FROM readings",
  "name": "daily-settlement-v2"
}'
```

### 结论结构（成功时）

```json
{
  "status": "succeeded",
  "result": {
    "identical": false,
    "columns": ["meter_id", "wh"],
    "column_types": ["bigint", "bigint"],
    "left_row_total": 504013,
    "right_row_total": 504012,
    "matched_group_count": 504011,
    "diff_group_total": 1,
    "diffs_shown": 1,
    "diffs": [
      {"values": [1001, 1150], "left_count": 2, "right_count": 1}
    ]
  }
}
```

## 3. 对提交 SQL 的约束

- 必须是**单条只读 SELECT**（允许 `WITH` / `UNION` / `INTERSECT` / `EXCEPT`），
  静态解析拒绝多语句与任何写操作 / DDL / `SELECT INTO` / `LOCK` / `CALL` 等；
- 结果列只允许 `bigint` 与 `text`，两侧**列数相同、对应位置类型相同**，
  否则本轮失败且不给出比较结论；
- 数据库连接再叠加一层纵深防御：`READ ONLY` 事务、`statement_timeout`、
  `lock_timeout`，即使静态校验被绕过也无法写入或长时间持锁。

## 4. 转线登记与历史归属

### 数据模型与归属规则

- `meters.line_name` 仍是表计的**当前**产线（只存现值），保持原表不动；
- 新表 `meter_line_transfers(meter_id, effective_at, target_line, created_at)`，
  主键 `(meter_id, effective_at)`，同一表号同一时刻至多一条；
- **首次登记**时额外写入一条 `effective_at = 公元 1 年 UTC` 的基线行，
  `target_line` 取登记时刻的 `meters.line_name`（原产线），供更早读数使用
  （不用 PG 的 `-infinity`：psycopg3 无法解码为 Python `datetime`）；
- 归属规则：读数 `r` 归到满足 `effective_at <= r.read_time` 的最近一条登记的
  `target_line`，因此**生效时刻的读数归新线**，更早的读数归上一条（首段为原产线）。

### `POST /meters/{meter_id}/transfers`

请求体：

```json
{ "meter_id": 1001, "effective_at": "2026-11-15T09:00:00Z", "target_line": "B线" }
```

语义与状态码：

- `effective_at` 必须带时区偏移（服务统一归一到 UTC 存储与回显），
  `target_line` 非空非纯空白，路径表号与请求体表号不一致返回 `400`；
- 表号不存在于 `meters` 返回 `404`；
- 同表号同时刻**同值**重复提交：幂等成功 `201`，响应 `idempotent=true`，可安全重试；
- 同表号同时刻**异值**：拒绝 `409`，库内不新增任何行；
- **允许补录更早的转线**：当补录时刻早于该表所有既有业务登记时，校验历史归属的
  最新一段（最新登记的目标产线）必须与 `meters.line_name` 的最新归属一致，
  否则拒绝 `409`，避免补录与现值互相矛盾；
- 登记全程在**单事务**内完成：先 `SELECT ... FOR UPDATE` 锁定该表号行串行化并发
  补录，再查重 / 写基线 / 插入，任一检查失败整体回滚，**不留部分变更**；
  主键冲突是并发的最后防线，冲突后再读一次区分同值（幂等）与异值（409）。

响应示例（首次登记）：

```json
{
  "meter_id": 1001,
  "effective_at": "2026-11-15T09:00:00Z",
  "target_line": "B线",
  "idempotent": false,
  "baseline_line": "A线"
}
```

### `GET /meters/{meter_id}/readings?date=2026-11-15`

- 按 UTC 日期过滤：`[当日 00:00, 次日 00:00) UTC`，返回读数时刻（UTC）、`wh`
  和**当时产线**，按时刻升序；
- 整个查询在一个 `REPEATABLE READ READ ONLY` 事务内完成，固定调用时的已提交快照。
  `readings` 经独立 SQL 更正并提交后，再次调用会从**新的已提交快照重新归属**；
- 表号不存在返回 `404`；表号存在但从未登记转线返回 `409`（无法确定历史归属）；
  当日无读数返回空 `rows`；
- 响应含 `count` / `truncated` / `limit`，单次行数上限由
  `ATTRIBUTION_ROW_LIMIT`（默认 10000）控制，超限截断并置 `truncated=true`。

```json
{
  "meter_id": 1001, "date": "2026-11-15", "timezone": "UTC",
  "count": 3, "truncated": false, "limit": 10000,
  "rows": [
    {"read_time": "2026-11-15T08:00:00Z", "wh": 1000, "line_name": "A线"},
    {"read_time": "2026-11-15T09:00:00Z", "wh": 1150, "line_name": "B线"},
    {"read_time": "2026-11-15T10:00:00Z", "wh": 1320, "line_name": "B线"}
  ]
}
```

### 数据卷兼容

- 首次启动：PostgreSQL 入口依次执行 `sql/init.sql`（其中 `\ir lineage.sql` 建表）
  并写入示例数据，含表 1001 的一次示例转线；
- 沿用已有数据卷：入口初始化脚本不会重跑，应用启动时（FastAPI lifespan）会
  **幂等执行同一份建表 DDL** 自动补建 `meter_line_transfers`，
  `/docs` 与新接口立即可用；重复执行无副作用。

## 5. 关键设计（SQL 核对）

### 同一已提交快照，源表仍可更正

每条任务开三条独立连接：快照连接执行
`BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY` + `pg_export_snapshot()`，
两条读取连接分别 `SET TRANSACTION SNAPSHOT` 绑定**同一个快照**。
核对期间其他会话对 `readings` / `meters` 的更正照常提交，但对本轮不可见。

### 内存有界的外部归并（不全量载入、不靠哈希判等）

1. 命名服务端游标按 `itersize` 分批拉取，客户端从不缓存整表；
2. 每 `SORT_CHUNK_ROWS`（默认 2 万）行在进程内排序，逐行流式 pickle 落盘为一个 run；
3. 归并阶段对两侧 run 做单轮 k 路堆归并（堆中每流只驻留一行），
   键随行走，按全序键聚合等值组后比较两侧次数；
4. 差异明细最多保留 20 种，但总行数、差异组总数等计数覆盖全量数据；
5. 全程不使用整表哈希作为判等依据。

### 单名额、30 秒时限与终止语义

- 进程内单名额信号量：全局同时只运行一组，冲突返回 `409`；
- 看门狗按 `JOB_TIMEOUT_SECONDS`（默认 30）判定超时；
- 取消/超时/任一侧失败后：`conn.cancel()` 请求中止本轮全部 SQL，
  必要时 `pg_terminate_backend` 兜底；
- 严格按顺序收尾：**中止 SQL → 关闭连接 → 清空暂存目录 → 释放名额 → 才落终态**，
  因此状态查询一旦看到终态，名额必然已经释放、暂存必然已经清空。

## 6. 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PG_DSN` | `postgresql://recon:recon@postgres:5432/recon` | 数据库连接串 |
| `JOB_TIMEOUT_SECONDS` | `30` | 单轮硬时限 |
| `PG_STATEMENT_TIMEOUT_MS` | `28000` | 服务端单语句超时（留 2s 收尾余量） |
| `SORT_CHUNK_ROWS` | `20000` | 单个排序块行数 |
| `MAX_DIFF_TYPES` | `20` | 差异展示上限 |
| `ATTRIBUTION_ROW_LIMIT` | `10000` | 单表日归属查询单次返回行数上限 |
| `WORK_DIR` | `/tmp/recon-work` | 外部排序暂存目录（独立卷） |

## 7. 本地自测

```bash
# 单元 + 集成测试（集成测试需要一个 PostgreSQL；可用 PG_DSN 指定）
python -m pytest tests/ -q

# 50 万行级端到端压测（内存峰值、暂存占用、结论正确性、清理与名额释放）
python scripts/load_test.py
```

实测（受限沙箱环境，容器内通常更快）：两侧各 504,012 行、
含一次重复计入差异，端到端约 16 秒，进程峰值 RSS 约 68 MiB，
暂存峰值约 44 MiB，结束后暂存目录清空、名额释放。

## 8. 目录结构

```
app/
  config.py          运行期配置
  sqlvalidation.py   只读 SELECT 静态校验
  db.py              快照会话、只读连接、命名游标流式拉取、列类型闸门
  external_merge.py  流式 run、k 路归并、多重集核对
  manager.py         单名额编排、看门狗、取消/超时/失败的统一收尾
  lineage.py         转线登记（单事务/幂等/并发串行化）与 UTC 日归属查询
  models.py          API 模型
  main.py            FastAPI 路由（启动时幂等补建转线表）
sql/init.sql         源表、示例数据、示例核对查询（\ir 引入 lineage.sql）
sql/lineage.sql      转线登记表 DDL（应用启动时也幂等执行，兼容旧数据卷）
scripts/             压测数据生成与负载脚本
tests/               单元与集成测试（含 test_lineage.py 转线归属专项）
docker-compose.yml   应用（128MiB 上限）+ PostgreSQL
Dockerfile
```

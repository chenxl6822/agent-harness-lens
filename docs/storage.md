# T05b 存储层设计：SQLite 持久化 + Repository + 幂等重放

> 实现位于 `engine/hs_core/store.py`（Repository）与 `engine/hs_core/cli.py`（`hs ingest` / `hs db verify`）。
> 复测脚本：`probe/t05b/work/measure.py`（probe/ 被 gitignore，长期引用的实测数字归档在 `docs/evidence/T05b/`）。

## 1. DDL（user_version = 1）

两张表、5 个索引。`PRAGMA user_version` 记 DDL 版本，`store.MIGRATIONS` 列表留升级路径
（v1 之上追加 v2 即可，`Store.__init__` 自动按序应用；user_version 高于引擎支持值时显式报错，防静默降级）。

### runs（run 元数据）

| 列 | 类型 | 说明 |
|---|---|---|
| run_id | TEXT PK | |
| created_at | TEXT | 首次入库时间（ISO-8601 UTC ms，ON CONFLICT 不覆盖） |
| platform | TEXT | 本批事件 source.platform 多数派（平局取字典序最小，确定性） |
| channel_policy | TEXT | **实际生效的摄入策略**（primary_only / merge），每次 ingest 刷新 |
| source_digest | TEXT | 摄入源内容 sha256（排序后文件名字节 + 文件字节串联；判定「是否同一份源」） |
| event_count | INTEGER | 每次写入后以 `COUNT(*)` 刷新，不依赖批次大小 |

### events（复合主键，硬约束 G1）

主键 **`(run_id, event_id)`**。原因：`event_id` 摘要白名单不含 `run_id`（T05a-R1 §4.1），
同一份源在两个 run 下摄入得到完全相同的 `event_id`；单列主键会让第二个 run 覆盖第一个。
实测：同源双 run 各 603 行互不覆盖、库内共 1206 行（`evidence/T05b/e3-two-runs.txt`）。

| 列 | 类型 | 说明 |
|---|---|---|
| run_id / event_id | TEXT | 复合主键；FK → runs(run_id) |
| seq | INTEGER | 方案 A：整 run 重算后的 0..n-1 全序 |
| ts | TEXT | 原始 ISO 字符串原样存储（不做 datetime 往返，保证逐字节可比） |
| ts_ms | INTEGER | 由 ts 派生的 epoch 毫秒，仅用于排序/索引 |
| session_id / type / schema_version | TEXT | |
| source_platform / source_channel | TEXT | source 拆两列（schema 中 Source 的 version/degraded 本卡采集器不产出） |
| agent_id / agent_role / parent_agent_id / raw_ref | TEXT NULL | 可选字段 |
| labels / payload | TEXT | 原始 JSON 文本（键序保留，round-trip 无字节漂移） |

### 索引及用途

| 索引 | 用途 |
|---|---|
| `(run_id, seq)` | dump_run / iter_events 默认序（方案 A 的时序全序），T06 时序归并主路径 |
| `(run_id, ts)` | 按时间窗查询 |
| `(run_id, type)` | 按事件类型过滤（iter_events(types=...)） |
| `(session_id)` | 跨 run 会话聚合 |
| `(agent_id)` | 跨 run 智能体聚合 |

### PRAGMA

- `journal_mode=WAL`：读写不互斥，并发读不阻塞写。
- `foreign_keys=ON`：events → runs 引用完整性。
- `synchronous=NORMAL`：WAL 下的标准取舍——应用崩溃不丢已提交事务，仅断电/系统崩溃可能丢最后一个事务。
  评测数据是可重放的（同源重摄即恢复），不值得为断电级持久化付出 FULL 的写入吞吐代价。

## 2. seq 决策：方案 A（整 run 重算）——唯一决策

**决策：方案 A。** 每次 `ingest` 对「库内既有事件 ∪ 本批事件」按 `(ts_ms, event_id)`
全量重排，覆盖写 `seq 0..n-1`（与 `collector.assign_seq` 完全相同的排序键）。既有行只允许 `seq`
列变化（`INSERT OR IGNORE` + 仅 UPDATE seq 列）。

**理由（相对方案 B「只追加分配」）：**

1. **T06 语义最简**：`(run_id, seq)` 保持「按时间排序的 run 内全序 0..n-1」，T06 的 turn/step
   时序归并可继续用 `ORDER BY seq`，无需另建 `(run_id, ts, event_id)` 排序规则，也无需在两套
   排序之间解释差异。
2. **幂等承诺最强**：增量摄入后的库状态与一次性摄入全量**逐字节一致**（`dump_run` sha256 相同，
   含 seq）。方案 B 下新事件永远排尾，`dump_run` 与全量摄入必然出现 seq 序差异，E3.2 只能降级为
   「可解释差异」。
3. **与上游天然一致**：`collect()` 本来就对整批重算 seq（T05a 已验收语义）；方案 A 等于把
   collector 的 seq 语义原样延伸到库里，零新概念。
4. **代价实测可忽略**（`evidence/T05b/e2-incremental.txt`、`e5-throughput-verify.txt`）：
   - 压力场景：182 行既有 run 增量摄入 603 行批次，**seq 重算 180 行**，含重算的总耗时 6.5ms
     （对照一次性全量 7.2ms——重算开销淹没在解析/事务噪声里）。
   - 8908 事件 run 全量摄入 176.9ms（约 5.0 万 events/s）。即使 run 放大到 10 万事件，O(n) 重算
     也在秒级内，且重放型摄入（本项目主路径）本就携带全批事件。

方案 B 唯一的优势（避免重写既有行）在本项目规模下没有可测收益，却要在 T06 引入第二排序键，
故不采用。

**方案 A 下 T06 用什么**：不变，继续 `(run_id, seq)`（全序 = `(ts_ms, event_id)` 序）。

## 3. 幂等语义（E3）

`ingest(run_id, events, *, channel_policy="primary_only", allow_merge=False, source_digest=None)`
单事务（`BEGIN IMMEDIATE`）内完成：批内去重（同 event_id 取首条）→ 读既有 `(event_id, seq)` →
全量重排 → `INSERT OR IGNORE` 新行 → `UPDATE` 仅 seq 漂移的既有行 → 刷新 runs。返回
`IngestResult(inserted, updated, skipped, total)`。

四个场景实测（dsh 1646 行源 → 603 事件，详见 evidence）：

| 场景 | 结果 |
|---|---|
| 1 同输入两次摄入 | 第二次 inserted=0 skipped=603，行数不变，dump sha256 相同（`e1`） |
| 2a 增量（自然序追加） | 增量库与一次性全量 dump sha256 逐字节一致（`e2` 场景 2） |
| 2b 增量（乱序追加，压力） | 既有行仅 seq 重算 180 行；库 613 = 603 + 10 条 stale 变体（见 §5 边界 1） |
| 3 双 run 同源 | 各 603 行互不覆盖；dump 除 run_id 取值外逐字节一致（`e3`） |
| 4 并发写（2 进程） | 两进程均 rc=0；worker0 inserted=603，worker1 inserted=0 skipped=603；库 603 行零重复、无锁死（`e4`） |

**并发行为的诚实边界**：SQLite 单写者模型。并发 ingest 靠 `BEGIN IMMEDIATE` 抢写锁 +
`busy_timeout=30s` 等待实现**串行化**；不承诺「同时提交成功」，而是「一先一后都成功、结果与
串行一致」。等待超时会抛 `sqlite3.OperationalError`（30s 内未实测出现，603/8908 行事务均为
毫秒级）。T06 如需多进程并发摄入同一 run，应依赖此语义而非假设并行写。

## 4. 策略门禁（G4）

- `channel_policy="merge"` 且未显式 `allow_merge=True` → `PolicyError`，拒绝任何写入（拒绝即零写入）。
- 非法 policy 值同样拒绝。实际策略写入 `runs.channel_policy`。
- CLI：`hs ingest --channel-policy merge` 默认拒绝（exit 2）；`--allow-merge` 显式放行。
  T06 指标层应断言 `runs.channel_policy == "primary_only"`。

## 5. 已知边界（如实声明）

1. **源侧 stale 变体**：dsh 的 `message.recv` 按 message.id 归并多源观测，代表观测的 ts 参与
   event_id 摘要（T05a-R1 语义）。若增量摄入的追加行包含对**既有 message 的更早观测**（乱序追加
   才会发生；自然序追加不会），部分观测快照下先生成的变体 event_id 与全量归并结果不同，Store 以
   `(run_id, event_id)` 保留两者（实测 613 = 603 + 10，全部为 message.recv，`e2` 场景 1）。
   这是 collect() 源侧现象，存储层无法在不违反「既有行不可改写」的前提下消除；T06 按载荷
   `message_id` 二次归并即可。
2. **raw_ref 锚定源文件名**：同内容经不同文件名摄入（如 `aaa.ndjson` / `zzz.ndjson`）event_id
   相同（raw_ref 不进摘要），但 raw_ref 列随**首次摄入**的文件名定格，其后不再改写。
3. **dump_run 键序为规范形**：重建键序固定为 collector `_mk_event` 的插入序。dsh/file/hook 三通道
   产物与 collect() NDJSON 逐字节一致（`test_e5_verify_and_dump_roundtrip` 实测 dsh 603 行全等）；
   若未来某通道产物键序不同，以 dump_run 规范形为准（哈希比较均基于库内导出，自洽）。
4. **Source 可选字段**：schema 的 `source.version/degraded` 本卡采集器不产出，DB 拆列不保存；
   若上游开始产出，需 v2 迁移。

## 6. E5 自检（`hs db verify`）

`hs db verify --run-id <id> --db <path> [--ndjson <file>]`：行数 vs NDJSON、每行 pydantic
`TraceEvent` 反序列化 + `trace-event.schema.json`（Draft 2020-12）校验、seq 连续无洞（方案 A）、
`(run_id, event_id)` 无重复。违规 exit 2，通过 exit 0。真实数据实测：603 行全绿（`e6-cli-smoke.txt`）。

## 7. 吞吐实测

| 数据源 | 事件数 | ingest 耗时 | 吞吐 | 库大小（含 WAL） |
|---|---|---|---|---|
| dsh（1646 行源） | 603 | 8.2ms | ~73,000 events/s | 460 KB |
| 真实 codebuddy 快照 | 8,908 | 176.9ms | ~50,000 events/s | 10.7 MB |

（首次插入口径，含整 run 重算 seq + 单事务提交；幂等热路径更快，603 行 1.8ms。）

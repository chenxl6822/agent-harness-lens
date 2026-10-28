# metrics-engine.md — T06 指标引擎口径登记与复核指南

> 实现：`engine/hs_core/metrics.py`（注册器 + 装配，唯一 IO 边界）、`engine/hs_core/metrics_core.py`
> （13 个指标纯函数，零 IO）、`engine/hs_core/cli.py`（`hs metrics run|list`）。
> 实测证据：`docs/evidence/T06/`（`selfcheck.json.txt` 可脚本复跑、`pytest.txt`、`cli.txt`）。
> 本文件**不替代** T07 的 `docs/metric-definitions.md`（那是协作指标的地盘）。

## 0. 一句话结论

给定**任意一个 `channel_policy='primary_only'` 的 run**，本引擎产出双文件
（`metricset.json` 过 schema + `metricset.ext.json` 旁路补充），并用
`canonical_sha256` 保证「换个人重算得到一模一样的结果」。

真实数据（`~/.codebuddy` → 9809 事件，`primary_only`）上**能算的只有 4 项**：
`process.step_count 2567`、`process.tool_correctness 0.986971`、`process.retry_rate 0.002172`、
`process.redundant_loop 1`。其余 9 项**如实降级**（`needs_input` × 1、`no_source` × 8），
降级原因与「归谁修」逐条写在 `ext.degraded_reasons`。

**算不出来就写 `null`。** 本引擎里 `0` 与 `null` 是两个不同的世界。
**有值就必有档位。** `value != null` 而 `confidence == null` 是缺陷（装配层直接抛错，见 §4.3）。

---

## 1. 三层与扩展点

| 层 | 文件 | 职责 | 是否允许 IO |
|---|---|---|---|
| 注册器 + 装配 | `hs_core/metrics.py` | `@register_metric` / `REGISTRY` / `compute_run` / `compute_group` / canonical 摘要 / 落盘 | **仅此层** |
| 指标纯函数 | `hs_core/metrics_core.py` | 13 个 `(events_sorted, params, ctx) -> MetricOutcome` | 禁止 |
| 契约生成物 | `hs_core/models.py` | 由 `packages/schema/*.schema.json` 生成，只消费不改 | — |

**T07 接入方式（零改动本模块）**：新建 `hs_core/metrics_collab.py`，用同一个
`@register_metric(group="collaboration")` 注册即可。`BUILTIN_METRIC_MODULES =
("hs_core.metrics_core", "hs_core.metrics_collab")`，后者缺失时静默跳过。
第三方模块还可走 `hs metrics run --extra-module <name>`（`PYTHONPATH` 指向模块所在目录）。

注册器硬规则：

- 重复注册同 key → `MetricRegistryError`（**不静默覆盖**）。同一模块重复加载是幂等的
  （加载前先摘掉该模块自己注册的 key，跨模块抢注仍然报错）。
- key 前缀必须与 group 匹配：`result.`→`result`、`process.`→`process`、`collab.`→`collaboration`
  （`group="collab"` 作为别名接受）、`cost.`→`cost`。
- 2 元函数 `(events, params)` 自动包装成 3 元（PRD 对外签名兼容）。
- 阈值全部走 `MetricParams`（frozen dataclass）+ `engine/config/metrics.yaml` 覆盖；
  **改配置即生效，代码里没有任何魔法数字**。未知配置键直接报错（防「以为改了其实没改」）。
- `confidence` 与 `value` 同生共死：非降级指标必须给 `high`/`low`，降级指标必须为 `null`
  （装配层硬断言，见 §4.3）。第三方模块同样适用（`docs/evidence/T06/ext_probe.py` 已按此写）。

---

## 2. 输出双文件（schema 冲突的物理映射）

`packages/schema/metric-set.schema.json` 是 T01 唯一事实源、本卡禁改，且顶层
`additionalProperties:false`：没有 `channel_policy`、没有每 key 的 `degraded_reason`、
`degraded_metrics` 只是 `list[string]`，`EvidenceRef.event_ids` 还有 `minItems:1`。

| 文件 | 内容 | 消费者 |
|---|---|---|
| `metricset.json` | **纯 schema 合规**的 `MetricSet`（`hs schema check --kind metric-set` exit 0）；`degraded_metrics` 是 `list[str]` | T11 / T14 / 跨语言消费者 |
| `metricset.ext.json` | `channel_policy` / `blocked` / `blocked_reason` / **`degraded_reasons{key:{reason,detail}}`** / `key_count_check` / `notes` / `data_quality_flags` / `data_quality_details` / `tool_call_identity` / `evidence_index` / `params_digest` / `canonical_sha256` / `elapsed_ms` | 验收脚本 / 排查 / T14 下钻 |

**键名互斥（R1-3，已修）**：ext 里那份「key → {reason, detail}」叫 **`degraded_reasons`**（对象）；
主文件的 `degraded_metrics` 是 **`list[str]`**（schema 约束，不可动）。ext 里**不得再出现**
`degraded_metrics`，主文件里**不得出现** `degraded_reasons`——同名不同构会让下游取键时
静默拿到错误类型（不抛异常，最难排查）。单测逐字断言这四条。

两条实现细节（都是被 schema 逼出来的，已写进单测）：

1. **`unit` / `higher_is_better` / `confidence` 取 None 时整体省略，不写 `null`**。
   schema 对这三个字段只允许枚举字符串或布尔，`null` 会直接判违规（`value: null` 是唯一被允许的 null）。
2. **降级指标的 `reason` 无法落 `EvidenceRef.note`**：`event_id` 有 `minItems:1`，
   降级时没有 event_id，硬造一个就是假证据。所以主文件里降级项固定是
   `value=null + evidence=[]`，原因在 `ext.degraded_reasons`（+ `notes`）。
   「reason 同时写 note」这条设计在 schema 不变的前提下不可实现，已记为待决事项（见 §8-U3）。

---

## 3. 规范序列化与 `canonical_sha256`（复核用完整定义）

`computed_at` 每次不同，直接比对文件字节永远不等。故判据是 `ext.canonical_sha256`：

**输入**：`metricset.json` 的**全部内容，去掉 `computed_at` 一个字段**（其余字段一个不少、
一个不改）。字段取值就是文件里的值（因此同样遵守「None 的可选字段整体省略」）。

**规范化步骤**（顺序无关，最终都由 `canonical_json` 收敛）：

1. **删除** `computed_at`。
2. **列表排序**：`groups.result / process / collaboration / cost` 四个数组各按 `key` 升序；
   `degraded_metrics` 按字符串升序。`evidence[]` 内部**不排序**（它是逻辑顺序：
   先错误调用后全量调用等），`event_ids` 由生成侧保证按 `seq` 升序。
3. **数值**：所有 `float` 统一 `round(x, 6)`（6 位小数）。布尔与整数原样（`true`/`false`/整数）。
   出现 `NaN` / `Infinity` 直接抛 `MetricAssemblyError`（schema 的 `number` 也不允许）。
4. **键序**：所有对象键按字典序升序（`json.dumps(sort_keys=True)`）。
5. **序列化**：`json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))`
   —— 紧凑、无空格、不转义中文。
6. **摘要**：`"sha256:" + hashlib.sha256(<上述 UTF-8 字节>).hexdigest()`。

**复核脚本**（`scripts/t06_selfcheck.py::_sha_from_file` 就这么做，实测与引擎输出逐字节一致）：

```python
payload = json.loads(Path("metricset.json").read_text(encoding="utf-8"))
payload.pop("computed_at")
payload["groups"] = {k: sorted(v, key=lambda m: m["key"]) for k, v in payload["groups"].items()}
payload["degraded_metrics"] = sorted(payload["degraded_metrics"])
blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
sha = "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()
```

`params_digest` 同法作用于 `MetricParams.as_dict()`（含 `pricing`），用于判断「两次计算用的阈值是否同一套」。

CLI `--at <ISO>` 可冻结 `computed_at`，使**整文件**逐字节可比（golden 测试用）。

---

## 4. 降级语义

### 4.1 reason 枚举（集中定义在 `metrics.DEGRADE_REASONS`）

| reason | 含义 |
|---|---|
| `no_source` | 源里根本没有这份数据（不是 0，是读不到） |
| `insufficient_data` | 有源但分母为 0 / 样本不足 |
| `needs_input` | 等上游供数（T09 verdict、T10 归属元数据） |
| `blocked` | `channel_policy != primary_only`，整个 run 被策略门禁拒绝 |
| `computation_error` | 单指标抛异常被隔离（只记异常类型名，不记 traceback，避免路径泄漏） |
| `other:<简短说明>` | 兜底（必须出现在 `ext.degraded_reasons` 里，不允许静默丢弃） |

### 4.2 三合一不变量

`value is None` ⟺ `key ∈ degraded_metrics` ⟺ `evidence == []`。装配层 `_assert_invariants` 硬断言，
违反即抛 `MetricAssemblyError`（这能抓住指标函数的 bug，不是装饰）。

### 4.3 `confidence` 规则（R1-5 按 Q7 落地；R2-1 改为按**来源等级**动态判定）

R2-1 裁定：样本量回答「够不够」，来源等级回答「准不准」，这是两个问题——签名是代理键时
样本量再大也改变不了「签名是近似的」。因此签名类指标（retry_rate / redundant_loop）的档位
由 `_signature_confidence` 按**来源等级**动态判定（禁止写死到 key 上）：

| 来源等级 | 判据 | confidence |
|---|---|---|
| 原生 | 本 run 内 `tool.call.content_sha256` 去重后取值数 > 1 且 proxy == 0 | 再叠加样本量阈值（n < `low_confidence_n` 取 low） |
| 代理 | 去重后取值数 == 1（全为常量，签名实为 `span_id`） | `low` |
| 键缺失 | 去重后取值数 == 0 | **不得据此判为代理**，走「未知」分支：`low`，note 写明「签名来源未采集」 |
| 上界 / 派生 | 口径存在已知不可观测项（如 deny 信号无源） | `low` |
| 样本不足 | n < `low_confidence_n` | `low`（在上述基础上**再降**，取最低者） |
| 降级 | `value == null` | **`null`** |

实现陷阱（单测 `test_native_signature_count_ignores_missing_key` 拦截）：`_usable_hash()` 对
「键缺失」与「值为空常量」都返回 None，而 `empty_own` 只统计后者，**两者不是补集**；
**禁止**用 `native = total - empty_own` 反推，原生的正向条件是 **`proxy == 0`**。

其余指标仍按 Q7 的样本量规则：

| 情形 | `confidence` |
|---|---|
| 有值，**直接观测**且样本量 `n ≥ low_confidence_n`（默认 20） | `"high"` |
| 有值，直接观测但 `n < 20` | `"low"` |
| 有值，口径为**派生 / 上界**（`tool_correctness` 的 deny 上界、`cost.cny` 的换算式、`trajectory_efficiency` 的比值、trace 级汇总的 token、`default` 单价兜底、单样本墙钟） | `"low"`（与 n 无关，`note` 写明「口径为派生」） |
| **降级（`value = null`）** | **`null`** |

**两条红线（装配层硬断言，违反即 `MetricAssemblyError`）**：

- `value != null` ⟹ `confidence != null`（有值必有档位；「有数但没档位」同样是误导）
- `value == null` ⟹ `confidence == null`（**禁止**给 `no_source` 填 `"low"`——那会把
  「没数」伪装成「有数但不可信」）

本条指标的实际档位（9809 事件基线 run）：

| key | 档位 | 依据 |
|---|---|---|
| `process.step_count` | `high` | 直接观测计数，n = 2567 |
| `process.retry_rate` | `low` | **R2-1：签名来源=代理**（content_sha256 去重取值数 == 1，全为 `sha256("")` 常量，签名实为 span_id；样本量 921 不改变这一点） |
| `process.redundant_loop` | `low` | **R2-1：同上，代理来源**（T05 v2 补到真实 content 后动态升 high，不写死） |
| `process.tool_correctness` | `low` | deny 信号无源、值为**上界** → 派生 |
| 其余 9 项 | `null` | 全部降级 |

### 4.4 `data_quality_flags`（不进 `degraded_metrics`）

| flag | 触发 | 语义 |
|---|---|---|
| `tool_call_identity_violated` | `count(tool.call) != count(tool.result) + count(tool.error)` | 调用/结果/错误的 span 配对结构被破坏。**指标照常出值**，T11 渲染成数据质量提示 |
| `tool_call_signature_degenerate` | 某一侧 `content_sha256` 在整批数据里**全部相同**（`n ≥ 2` 且 `distinct ≤ 1`） | 该字段没有区分度，**不得当签名用**。指标走 span_id 从 span_end 补齐；flag 只说事实，不改口径 |

`degraded_metrics` 回答「这个指标有没有数」，`data_quality_flags` 回答「这些数脚下的数据干不干净」，
两个问题不共用一个通道。真实数据本次**两条 flag 都触发**：`921 ≠ 908 + 12 = 920`（有 1 次调用
既无 result 也无 error），且 `tool.call` 自身哈希 921 条全同（`distinct = 1`）。详见 §7-2。

`ext.tool_call_identity` 除计数外还带 **`event_count`** 与 **`snapshot_digest`**（排序后 event_id
列表的 sha256 前 16 位）：恒等式是快照相关的结构性质，两份快照比较前先对指纹，再看差多少。
两处用的都是本次运行的事件流，不是历史快照的数字。

---

## 5. 指标登记（13 项）

> 通用：事件输入恒为 `Store.iter_events(run_id, order_by="seq")`（`seq` 是 `(ts_ms, event_id)` 全序，
> 禁止第二排序键）；证据按 `seq` 升序去重、截断到 `max_evidence_ids`（默认 50）并在 note 写明总数。

### 5.1 `result.success` — 任务成功率

| 项 | 内容 |
|---|---|
| 公式 | `passed 数 / 本 run 的 verdict 数`（verdict 来自 T09 最小接口 `{run_id, task_id, passed}`） |
| 单位 / 方向 | `ratio` / ↑ |
| 依赖 | **不依赖事件流语义**，依赖 `ctx.verdicts`；证据锚定 `payload.task_id` 命中的 `task.create/update/complete` |
| 降级值 | `null + needs_input`（`verdicts is None`，或传入的 verdict 无一条 `run_id` 匹配本 run） |
| 局限 | 事件流里没有「任务通过与否」的语义，**不得由事件推测**；verdict 未锚到 task 事件时锚点退化为 run 末事件，note 如实写 |
| 谁修 | T09 判定器 |

### 5.2 `result.pass_k` — k 次全通过比例

| 项 | 内容 |
|---|---|
| 公式 | 按 `(task_id, variant_id)` 分组，`k` 次重复 run **全部 passed** 的组数 ÷ 跑满 k 次的组数 |
| 单位 / 方向 | `ratio` / ↑ / `scope=group` |
| 依赖 | `RunMembership{task_id, variant_id, k}`（T10 供给）+ 各成员 run 的 `result.success` |
| 降级值 | run 级恒 `null + no_source`；聚合时无分组/无跑满组 → `no_source` / `insufficient_data` |
| 局限 | 不完整组（不足 k 次）**排除**而不是当失败；缺归属元数据的成员排除并计数 |
| 谁修 | T10 编排层（runs 表加列属 v2 迁移） |

### 5.3 `process.tool_correctness` — 工具调用正确率（**上界**）

| 项 | 内容 |
|---|---|
| 公式 | `(tool.call 数 − 错误调用数) / tool.call 数`，clamp 到 `[0,1]` |
| 分子定义 | **无配对 `tool.error` 的调用数**。配对优先级：① `payload.span_id` 相等 → ② `payload.call_id` 相等 → ③ 同签名 `(tool_name, content_sha256)` 的**时序最近邻**（`seq` 在其后、距离最小且 ≤ `retry_window_events`）。一次调用被多个 error 命中只计一次 |
| 分母 | `tool.call` 总数（PM v1.2 裁定：`845 + 12 = 857` 说明 result 与 error 互补、call 是全量） |
| 单位 / 方向 | `ratio` / ↑ |
| 依赖 | `tool.call` / `tool.result` / `tool.error`（含 `is_error=True` 的 result，按事件取并集不双计） |
| 降级值 | `null + insufficient_data`（无 `tool.call`，分母 0） |
| 必带声明 | note 固定含「deny/permission 信号当前无数据源（本 run 扫描 N 条事件，deny/permission 标记命中 M），本值为正确率上界，口径为派生」；N/M 每次**现场统计**，不复制历史快照数字（R1-6） |
| confidence | 恒 `low`（上界/派生口径，与样本量无关） |
| 局限 | ① `unmatched_errors`（找不到任何调用的 error）**不会**让任何调用变「错误」，正确率因此偏高，note 如实报数；② `unmatchable_calls`（span_id/call_id/签名三键全无）只能算「无配对」，属上界口径；③ deny/permission 无源 |
| 谁修 | deny 信号需 T05 v2 采集侧补；error 归属缺失属通道产物问题 |

### 5.4 `process.step_count` — 步数（观测）

| 项 | 内容 |
|---|---|
| 公式 | `count(type == "step.start")`，**唯一的计数来源**（R1-1 口径锁定），不读 payload |
| 硬规则 | **禁止**改用 `agent/step`（每会话只发一次 → 步数恒为 1，这是本次裁定担心的失效模式）、`session.start`、`step.end` 或任何 payload 字段计数 |
| 交叉校验 | dsh 的 `agent/pre-step` 在采集侧映射为 `step.start`，payload 带 `turn`/`step`（单会话内序号）。实现读 `step.start` 的 payload 取 `step` 最大值与事件计数对照，note 写成「事件计数 N；`pre-step` 的 `step` 最大值 M，差异 D」；**两口径不一致时以事件计数为准**（`step` 不是 run 级步数，多会话会重复）。CodeBuddy 形态无该字段 → note 明写「交叉校验仅记录不参与判定」 |
| 单位 / 方向 | `count` / 无方向（观测类，`higher_is_better` 省略） |
| 降级值 | `null + no_source`（本 run 无 `step.start`） |
| confidence | n ≥ 20 → `high`（基线 run n = 2567） |
| 局限 | `agent_id` 全空（本次实测 0/2567），**不可按成员拆分**；`step.end` 不参与计数 |
| 谁修 | 成员级步数需 T07 另找数据源 |

### 5.5 `process.trajectory_efficiency` — 轨迹效率

| 项 | 内容 |
|---|---|
| 公式 | 组内每个 run 的 `step_count / min(组内 step_count)` 再取均值（参考步数 = 同 `(task_id, variant_id)` 的最短步数） |
| 单位 / 方向 | `ratio` / ↓ / `scope=group` |
| 依赖 | `RunMembership{task_id, variant_id}` + `process.step_count` |
| 降级值 | run 级恒 `null + no_source`；聚合无样本 → `insufficient_data` |
| 局限 | 「参考步数」用组内历史最短，不是人工标注的最优解；参考步数为 0 的组跳过 |
| 谁修 | T10 归属元数据 + 跨 run 历史 |

### 5.6 `process.redundant_loop` — 冗余循环次数

| 项 | 内容 |
|---|---|
| 公式 | 在按 `seq` 排列的调用签名序列上，贪心找**最小周期** `p ≤ loop_max_period`（默认 5）且连续重复轮数 `≥ loop_min_repeat`（默认 2）；向右扩展覆盖长度 `L`，贡献 `(L // p) − 1` 次冗余；`i` 跳到已覆盖段末尾，避免重叠计数 |
| 参数实效 | `loop_min_repeat` **真正参与判定**（R1-7 已修：旧实现只比较 1 个周期，把 2 改成 3 结果不变 = 半死参数）。判定阈值写进 note：「判定=连续重复 ≥ N 轮」 |
| 签名 | `(tool_name, content_sha256)`。**通道 A 的 `tool.call` 自身哈希不可用**（全为 `sha256("")`），按 `span_id` 从同 span 的 `tool.result/tool.error` **补齐**，见 §7-1。note 必须声明「取自 span_end **结果内容哈希**，入参哈希未采集」 |
| 单位 / 方向 | `count` / ↓ |
| 降级值 | `null + insufficient_data`（无 `tool.call`，或**全部调用都拿不到有效签名**） |
| 有值语义 | 参与判定 ≥1 次且未检出循环 → **`0` 是真实值**（不是降级）；note 写 `ineligible` 次数 |
| confidence | **R2-1 来源等级动态判定**：当前签名来源=代理（content_sha256 全为常量）→ `low`；T05 v2 补到真实 content 后自动升 high（禁止写死） |
| 局限 | 只看连续重复，跨段/乱序重放不算；签名是**结果内容哈希**而非入参哈希：**相同入参不同输出会漏检**、**不同入参相同输出会误判**；签名含 token 级差异时不会被判为同一动作（宁可漏判不误判） |
| 谁修 | 指标算法本卡负责；真入参哈希需 T05 v2 采集侧；性能 P2（TT06-12） |

### 5.7 `process.retry_rate` — 重试率

| 项 | 内容 |
|---|---|
| 公式 | `matched error 数 / tool.call 数`（clamp [0,1]） |
| matched 判定 | 对每个 error（`tool.error` ∪ `is_error=True`），在窗口内向后找**同签名**的 `tool.call`：优先级 `content_sha256` > `call_id` > `span_id`；窗口 `0 ≤ call.ts − err.ts ≤ retry_window_ms`（默认 60s）；任一侧时戳不可解析时退化为 `0 ≤ Δseq ≤ retry_window_events`（默认 10） |
| 签名来源 | 与 §5.6 相同：`tool.call` 自身哈希不可用，按 `span_id` 取 span_end 的**结果内容哈希**；note 必须声明这一点 |
| 单位 / 方向 | `ratio` / ↓ |
| 降级值 | `null + insufficient_data`（无 `tool.call`，或调用侧全部无签名——此时报 0 等于把「无法判断」说成「没有重试」） |
| confidence | **R2-1 来源等级动态判定**：当前签名来源=代理 → `low`；T05 v2 补到真实 content 后自动升 high（禁止写死） |
| 局限 | 分子按 **error** 计数：每个 error 最多记 1 次，多个 error 指向同一调用会分别计数（同一动作重试两次确实是两次重试）；`unmatched` 如实报数；超出窗口的真实重试会漏判（窗口是配置项） |
| 谁修 | 指标算法本卡负责；跨会话重试需 T07/T10 |

### 5.8 `cost.tokens_total` — Token 总量

| 项 | 内容 |
|---|---|
| 公式 | 路径①：`sum(llm.response 的 usage.total)`（total 缺失时用 `in + out`）。路径②（默认关）：`sum(metric.custom/metric=trace_summary 的 total_tokens > 0)` |
| 互斥防双计 | run 内只要有一条**非零**请求级 usage → 只用路径①，路径②完全不参与（不相加、不取大） |
| 单位 / 方向 | `tokens` / ↓ |
| 依赖 | `llm.response`（主源 A）；`metric.custom`（条件二路径） |
| 降级值 | `null + no_source`（请求级读不到 **且** 请求级合计为 0；路径②关闭或无正值） |
| 局限 | 见 §6 实测（**9809 事件快照归档**，非固定值）：`llm.response` 765/765 无 usage；`metric.custom` 33/33 的 `total_tokens` 全 0（占位未实现）。运行时 `detail` 里的这几个数字由**本次运行现场统计**（R1-6），不复制本表的快照值 |
| 前向兼容 | T05 v2 补采通道 B 的 `providerData.usage` 后**零改动出值**（`_usage_of` 已兼容 4 组同义键 + `providerData` 嵌套） |
| 谁修 | **T05 v2 采集侧**（超出本卡权限） |

### 5.9 `cost.tokens_in` / `cost.tokens_out`

| 项 | 内容 |
|---|---|
| 公式 | `sum(llm.response 的 usage.in / usage.out)` |
| 单位 / 方向 | `tokens` / ↓ |
| 降级值 | `null + no_source` |
| 硬规则 | **不受条件二路径影响**：trace_summary 只有 total，没有 in/out 拆分，**禁止用 total 反推** |
| 谁修 | T05 v2 |

### 5.10 `cost.cny` — 成本

| 项 | 内容 |
|---|---|
| 公式 | `Σ(tokens_in × price_in + tokens_out × price_out)`，单价取 `engine/config/pricing.yaml`（`model` → 单价；未命中用 `default`） |
| model 来源 | `payload.model` / `labels.model` / `payload.providerData.model`，缺失记 `<model 缺失>` |
| 单位 / 方向 | `cny` / ↓ |
| 降级值 | tokens 降级 → 连带 `no_source`；tokens 有值但该 model 无单价且无 default → `no_source`，detail 写「单价表未配置」 |
| 部分命中 | **只要有任意一条 `llm.response` 的 model 拿不到单价，整体降级**（不输出部分和——部分和会系统性低估成本）；命中 `default` 兜底时 `confidence=low` |
| 硬规则 | **单价留空占位，禁止编造**（编造的单价会让成本变成假数据）；本轮 `default.in/out` 均为 `null` |
| 谁修 | 主理人提供单价表 + T05 v2 补 model 字段 |

### 5.11 `cost.wall_clock_p50` / `cost.wall_clock_p95` — 墙钟分位数

| 项 | 内容 |
|---|---|
| 公式 | 单 run：`ctx.membership.wall_clock_ms`（T10 供给），否则 `run.end.ts − run.start.ts`；跨 run：对成员 run 的墙钟取线性插值分位数（`wall_clock_quantiles` 可配） |
| 单位 / 方向 | `ms` / ↓ / `scope=group` |
| 降级值 | `null + no_source` |
| 证据约束 | 即使 `membership` 给了值，**事件流里没有 `run.start/run.end` 就无法下钻** → 如实降级（G5 优先于指标成色） |
| 局限 | 26 类事件表里 `run.start/run.end` 是「编排层合成」，被测平台不产出；span 级 `duration_ms` 实测 701/701 非空，但那是 span 级不是 run 级 |
| 谁修 | T10 编排层（合成 run.start/run.end + 归属元数据） |

---

## 6. 真实数据实测（`~/.codebuddy`，`primary_only`，9809 事件）

> 原始输出：`docs/evidence/T06/selfcheck.json.txt`、`cli.txt`、`R1-result.json.txt`（R1 复测）。
> 基线 run 由 `hs ingest --run-id t06-codebuddy-primary --codebuddy-home ~/.codebuddy` 摄入。
> 数字随本机活跃数据增长会变，**语义不变**（主理人验收基线为 9098 事件快照：`step.start 2376`、
> `tool.call 857`、`llm.response 701`；本次为 9809 事件：2567 / 921 / 765）。
>
> **本表是 9809 事件快照的归档值，不是运行时文案的来源**：`note` / `degraded_reasons[].detail`
> 里的「本 run N 条」全部现场计算（R1-6）。历史快照的数字若必须出现，一律写成
> 「引用（**非本 run 观测**）… 见 docs/metrics-engine.md」。

| 指标 | 值 | confidence | 说明 |
|---|---|---|---|
| `process.step_count` | **2567** | `high` | `step.start` 计数；`agent_id` 非空 0/2567；本 run 无 `pre-step` 形态 → 交叉校验仅记录 |
| `process.tool_correctness` | **0.986971** | `low` | 909/921；错误调用 12，全部按 `span_id` 配对，`unmatched_errors 0`、`unmatchable_calls 0`；上界口径 |
| `process.retry_rate` | **0.002172** | `low` | matched 2 / 921；`unmatched 10`（12 个 error 里只有 2 个在窗口内找到同签名重发）；R2-1：签名来源=代理 |
| `process.redundant_loop` | **1** | `low` | 参与判定 920/921 次调用，869 个不同签名，检出 1 个周期循环（周期 1，连续重复 2 轮）；R2-1：签名来源=代理 |
| `result.success` | `null` | `null` | `needs_input`（等 T09） |
| `result.pass_k` / `process.trajectory_efficiency` / `cost.wall_clock_p50` / `p95` | `null` | `null` | `no_source`（等 T10 归属元数据） |
| `cost.tokens_*` / `cost.cny` | `null` | `null` | `no_source`（用量在通道 B 被主源策略挡住） |
| 总耗时 | **95–110 ms / 9809 事件**（R1 复测 108.469 ms） | — | 远低于 2s（T06-12 目标） |
| `canonical_sha256` | 见 `probe/t06/R1-result.json` | — | 两次计算（`--at` 不同）与「从文件字节复算」三者一致 |
| `data_quality_flags` | `tool_call_identity_violated` + `tool_call_signature_degenerate` | — | 见 §7-1/§7-2；两条都**不影响指标值** |

### E5-P 探针：token 源实测

| 项 | 实测 |
|---|---|
| `llm.response` 总数 / 含 usage | **765 / 0** |
| `metric.custom`（trace_summary）总数 | **33** |
| 其中 `total_tokens` 非 null | 33（**但全部为 0**） |
| 其中 `total_tokens > 0` | **0** |
| 合计 | **0** |

**判断**：`metric.custom.total_tokens` 是**未实现的占位字段**（33/33 全 0，且对应
`span_count=1` 的极小 trace），**不可作为成本数据源**；与 `llm.response` 是否重复计入
无从谈起（两路都是 0）。故 `cost.tokens_total` 维持 `no_source`，
`tokens_trace_summary_fallback` 保持默认 `False`（开关与互斥逻辑已实现并有 4 个单测，
将来通道 A 产出非零值时可零改动启用，但**本轮不启用**）。

**通道 B 的用量（引用，非本次运行观测）**：主理人在 9098 事件快照上实测 transcript 的
`providerData.usage` 共 **723 行**；`primary_only` 主源取 A，故本 run 的产物里读不到它。
运行时 `degraded_reasons` 的 detail 以「引用（**非本 run 观测**）…723 行，见本文件 §6」的
形式引用这个数字（R1-6），不会把它写成「本次实测」。

---

## 7. 已知边界（如实声明）

1. **通道 A 的 `tool.call.content_sha256` 全是空输入常量哈希**：本次实测 `921/921` 条
   `content_sha256 == sha256("")`、`content_bytes == 0`（`HS_CONTENT_CAPTURE` 默认 `off`，
   collector 的 span_start 不带 `toolInput`）；QA 在另一份快照上复现为 `1124/1124`——即
   「全部相同」是**结构性质**，不是本快照的偶然。字段**存在**但值不可用作签名——若照单取用，
   `(tool_name, hash)` 会退化成 `tool_name`，QA 反事实实测 `redundant_loop` 从 1 变成 **602**
   （数百次 Bash 并成一个循环），那是假数字。

   **R2-3 统一措辞（与 `probe/t06/R2-result.json` 的 `signature_source` 段、
   `metrics_core._call_signatures()` 附近代码注释逐字一致，三处同步改）**：

   > 签名口径：优先取 `tool.call` 的 `content_sha256`；检测到该字段在本 run 内去重后取值数 ≤ 1 时
   > （当前实测 921/921 全为 `sha256("")` 常量），回退到 `span_id` → `span_end` 补齐的内容哈希。
   > **不得以「键缺失」作为回退依据**——键缺失与「键存在但值恒为常量」是两种状态，后者才是当前实况。

   **R1-6 裁定 (a)（正式口径）**：按 `span_id` 从同 span 的 `span_end`（`tool.result`/`tool.error`，
   它们的 `content_sha256` 是真实哈希）补齐，本次补齐 **920/921**，得到 869 个不同签名。
   **语义必须说清**：补的是**「结果内容哈希」，不是「入参哈希」**——两次相同入参产生不同输出会
   **漏检**冗余；两次不同入参产生相同输出（如都失败返回同一错误）会**误判**冗余。它是当前数据下
   唯一可用的近似，不是 `args_hash` 的真实语义。指标 `note` 已逐条声明来源。
   **防御**：整批 `content_sha256` 全同时登记 `data_quality_flags=[tool_call_signature_degenerate]`，
   不允许静默把它当签名用；反事实单测保证不会回退到字面空哈希。
2. **`tool.call` 与 `tool.result+tool.error` 差 1**：`921 ≠ 908 + 12 = 920`（主理人快照是
   `857 = 845 + 12` 恰好闭合）。差的 1 次调用既无 result 也无 error（span 只有 start），
   已登记 `data_quality_flags=[tool_call_identity_violated]`，**不影响指标值**。
   **这是快照相关的结构性质，差 1 不是 bug**：后来者看到 `identity_violated` 应当去核查快照，
   **禁止**为消除该 flag 而修改计数逻辑或放宽配对规则——那是把一个诚实的上报改成掩盖。
3. **`run.start` / `run.end` 事件不存在**（编排层合成，属 T10），故 `cost.wall_clock_*` 降级。
4. **`agent_id` 在 `step.*` 上全空**（0/2567），成员级拆分不可得。
5. **降级原因无法落 `EvidenceRef.note`**（`event_ids` 有 `minItems:1`），只能落 ext（§2-2）。
6. **`metric.custom` 的 `total_tokens` 是占位 0**（§6 E5-P），不作数据源。
7. **`pricing.yaml` 单价全为占位 `null`**：即便 tokens 有值，`cost.cny` 也会因「单价表未配置」降级。
8. **`hs metrics run` 的 `out_dir` 会打印相对路径**（如 `runs\t06\metrics`），产物内不含用户名/绝对路径/`raw_ref`。

---

## 8. 复跑方法（验收用）

```bash
# 0) 准备基线库（一次性）
.venv/Scripts/python.exe -m hs_core.cli ingest --run-id t06-codebuddy-primary \
    --db runs/t06/t06.db --codebuddy-home "$HOME/.codebuddy" --out-dir runs/t06/out

# 1) 单测（G4/G7/G8/G9/G12/G14/G15）
.venv/Scripts/python.exe -m pytest engine/tests/test_metrics.py engine/tests/test_metrics_core.py \
    -q --basetemp=.tmp-pytest

# 2) 指标清单（G1）与单 run 计算（G2/G5/G6/G10/G11/G13）
.venv/Scripts/python.exe -m hs_core.cli metrics list
.venv/Scripts/python.exe -m hs_core.cli metrics run --run-id t06-codebuddy-primary \
    --db runs/t06/t06.db --out-dir runs/t06/metrics --pretty
.venv/Scripts/python.exe -m hs_core.cli schema check runs/t06/metrics/metricset.json --kind metric-set

# 3) 全门禁一次跑完（G1–G15，输出 JSON 可直接比对 docs/evidence/T06/selfcheck.json.txt）
.venv/Scripts/python.exe scripts/t06_selfcheck.py --db runs/t06/t06.db \
    --run-id t06-codebuddy-primary --out .tmp-t06

# 4) R1 返工复测（R1-1…R1-7 一次跑完，产出 probe/t06/R1-result.json）
.venv/Scripts/python.exe scripts/t06_r1_check.py --metrics runs/t06/metrics \
    --db runs/t06/t06.db --run-id t06-codebuddy-primary --out probe/t06/R1-result.json

# 5) G1 第三方模块探针
PYTHONPATH=docs/evidence/T06 .venv/Scripts/python.exe -m hs_core.cli metrics list   # 无 --extra-module 时不含探针
PYTHONPATH=docs/evidence/T06 .venv/Scripts/python.exe -m hs_core.cli metrics run \
    --run-id t06-codebuddy-primary --db runs/t06/t06.db --extra-module ext_probe --out-dir .tmp-probe
```

## 9. 待决事项

| # | 事项 | 需要谁 |
|---|---|---|
| U3 | 是否走变更卡把 `degraded_reason` / `channel_policy` / `EvidenceRef.run_id` 收进 schema，使「原因落 note」「降级指标可下钻」成为可能，并把输出收敛为单文件 | 主理人 / 用户 |
| ~~U4~~ | ~~`tool.call.content_sha256` 取值不可用：修 collector 还是长期依赖 span_end 补齐~~ → **R1-6 已裁定 (a)**：认 span_end 补齐为正式口径（语义已在 §7-1 写明）；T05 v2 补到真入参哈希后自动失效 | 已闭环 / T05 v2 |
| OQ1 | 通道 B 的 `providerData.usage` 在 `primary_only` 下无法复算，`cost.*` 是否需要专用通道 | 主理人 / T05 v2 |
| — | 单价表（§7-7） | 主理人 |

## 10. R1 返工验收对照（实测证据可复跑）

原始输出：`docs/evidence/T06/R1-result.json.txt`（脚本 stdout 归档）、`probe/t06/R1-result.json`
（机器产物，`probe/` 被 gitignore，长期结论以本表 + 归档 txt 为准）。
复跑：见 §8-4。基线 run：`t06-codebuddy-primary`，9809 事件，`primary_only`。

| 项 | 复核结论 | 本次实测（可复跑） | 落点 |
|---|---|---|---|
| R1-1 | 原「分子分母倒置」**未复现**（当前口径本来就对）→ 转**防御性加固** | `step_count 2567` == 库内 `count(step.start) 2567`；本 run 无 `pre-step` 形态 → 交叉校验仅记录 | `metrics_core.step_count` + `test_step_count_uses_step_start_events` / `test_step_count_crosscheck_with_pre_step` |
| R1-2 | 13 = 4 可算 + 9 降级，**已自洽**→ 转回归锁定 | `key_count_check = {total 13, computed 4, degraded 9, consistent true}` | `metrics.EXPECTED_*` + `test_key_count_is_self_consistent` |
| R1-3 | **确认存在**（同名不同构）→ 已改 | ext 键列表含 `degraded_reasons`、**不含** `degraded_metrics`；主文件反之 | `metrics.compute_run` + `test_degraded_keys_naming_is_disjoint_between_files` |
| R1-4 | 已通过，仅复测 | `event_ids_total 117 / resolved 117 / unresolved []`；`identity_holds=false` 但退出码 0 | `scripts/t06_r1_check.py::drilldown` |
| R1-5 | **确认存在**（4 项全 None）→ 已改 | step_count/retry_rate/redundant_loop = `high`，tool_correctness = `low`，其余 9 项 `null`；`valued_without_confidence = []`（**R2-1 注**：retry_rate / redundant_loop 的 `high` 已被 R2 推翻改 `low`，见 §11——本行是 R1 当时的实测归档） | `metrics_core._confidence` + `metrics._assert_invariants` |
| R1-6 | ad-hoc 数字（别人快照）→ 已改 | `cost.*` detail 含「本 run `llm.response` 765 条…」，723 行只以「引用（**非本 run 观测**）」出现 | `metrics_core._cost_no_source_fact` / `_usage_fact` / `_trace_fact` |
| R1-7 | `loop_min_repeat` 半死 → 已修 | 2 轮序列：min_repeat=2 → 1，=3 → 0；3 轮序列：=3 → 2 | `metrics_core._scan_loops` + `test_params_change_loop_min_repeat_takes_effect` |

签名口径：**裁定 (a)** —— 认 `span_id` 从 `span_end` 补齐的内容哈希为正式签名（语义与漏检/误判
边界见 §7-1）；并新增 `tool_call_signature_degenerate` 防御 flag（整批哈希全同 → 登记事实，
不得静默当签名用）。

## 11. R2 返工验收对照（实测证据可复跑）

原始输出：`docs/evidence/T06/R2-result.json.txt`（脚本 stdout 归档）、`probe/t06/R2-result.json`
（机器产物）。复跑：`.venv/Scripts/python.exe scripts/t06_r2_check.py --metrics runs/t06/metrics \
--db runs/t06/t06.db --run-id t06-codebuddy-primary --out probe/t06/R2-result.json`。

| 项 | 复核结论 | 本次实测（可复跑） | 落点 |
|---|---|---|---|
| R2-1 | **R1 裁定被推翻**：retry_rate / redundant_loop 记 `high` 是错的（样本量回答「够不够」，来源等级回答「准不准」）→ 已改 | confidence_audit：`step_count high`、`retry_rate low`、`redundant_loop low`（代理）、`tool_correctness low`（上界）、9 降级 `null`；档位由 `_signature_confidence` 按**来源等级**动态判定，禁止写死 | `metrics_core._signature_confidence` / `_call_signatures` + `test_native_signature_count_ignores_missing_key` / `test_signature_confidence_rises_to_high_with_real_hashes` |
| R2-2 | 三份独立快照（主理人 9098 / 架构师 9809 / QA 12411）实测 `redundant_loop == 1` → 锁定 + 反向断言 | `redundant_loop == 1` 且 `≤ 50`（反事实 602 = 签名口径失效特征值，出现即缺陷） | `test_redundant_loop_real_shape_value_locked_and_bounded` |
| R2-3 | 签名口径与 confidence 规则的措辞三处不一致 → 已统一 | 统一措辞逐字落在：本文 §7-1、`R2-result.json.signature_source`、`metrics_core._call_signatures()` 注释 | 三处同步改 |

## 12. 修订记录

- v1.0（2026-10-26）T06 交付：13 指标口径、canonical 定义、双文件映射、真实数据实测与已知边界。
- v1.1（2026-10-26）T06-R1 返工：
  - **R1-1** `step_count` 口径锁定（唯一来源 `step.start` 事件）+ dsh `pre-step` 的 `turn`/`step`
    交叉校验（不一致以事件计数为准，差异写 note）；补两条防回归单测。
  - **R1-2** 以代码常量 `EXPECTED_COMPUTED_KEYS` / `EXPECTED_DEGRADED_KEYS` 锁 key 集合，
    注册与装配双重校验；ext 新增 `key_count_check`（4 可算 + 9 降级自洽）。
  - **R1-3** ext 的映射键改名 `degraded_reasons`，删除重复的 `degraded_metrics`（同名不同构已消除）。
  - **R1-4** 恒等式维持「只上报不失败」（§7-2 写明**禁止**为消 flag 改口径）；可下钻复测 117/117。
  - **R1-5** `confidence` 按 Q7 落地：high / low / null 三态，反向断言（有值必有档位）进装配层。
  - **R1-6** note/detail 里的数字改为**本 run 现场计算**，无法复算的历史快照改成引用式并注明出处；
    签名口径定案 (a) + `tool_call_signature_degenerate` 防御 flag。
  - **R1-7** `loop_min_repeat` 真正参与判定（旧实现只比 1 个周期，改成 3 结果不变）。
- v1.2（2026-10-26）T06-R2 返工：
  - **R2-1** confidence 改为按**来源等级**动态判定（不是按样本量）：代理/键缺失/混合 → 恒 `low`，
    原生 → 再叠加样本量阈值取最低者；retry_rate / redundant_loop 由 `high` 改判 `low`（推翻 R1-5
    的样本量裁定）；实现正向计数 `native`（`proxy == 0` 才算原生），反例单测
    `test_native_signature_count_ignores_missing_key` 拦截「键缺失被误算原生」。
  - **R2-2** `redundant_loop` 期望值锁定 1 + 反向断言 `≤ 50`（反事实 602 = 签名失效特征值）。
  - **R2-3** 签名口径统一措辞三处逐字一致（本文 §7-1 / R2-result.json `signature_source` /
    `metrics_core._call_signatures()` 注释）；§4.3 补来源等级规则表。

## 13. T05c usage 补采后的 run_id 与 event_id 约定（2026-10-28）

T05c 把通道 B transcript 的 `providerData.usage`/`model` 按时间窗顺序配对注入通道 A
`llm.response` 的 payload（`alignment: "time_window"`）。payload 进 `event_id` 摘要
（`_DIGEST_KEYS` 含 payload），由此产生两条硬约定：

1. **T05c 之后的 run 一律使用 `-p2` 类后缀的 `run_id`**（首例 `t06-codebuddy-primary-p2`）。
   旧 run（如 `t06-codebuddy-primary`）保持不动；**禁止在同一 `run_id` 下混两套
   `event_id` 口径**（同 run 聚合审计会对不上账）。
2. **跨卡对比只用指标值（`metricset.json`），不用 `event_id`**。payload 未变化的事件
   （如未配对注入的 `llm.response`、全部 `tool.call`）在旧/新 run 中确定性 id 相同，
   这是「id=内容摘要」的设计行为，不构成混用；注入了 usage 的 `llm.response` id 必然改变。

指标侧唯一改动（P0-4，主理人批准）：`_tokens_slot()` / `tokens_total()` 两处——
参与计算的 usage 事件存在 `payload.usage.alignment == "time_window"` 时 `confidence`
强制 `low`，note 追加「时间窗顺序配对，非键级对齐」；`alignment == "key"`（未来键级对齐）
时仍按样本量正常判档。配对三件套（单调顺序配对 / 数量比守卫 [0.8,1.25] / 1s 熔断）
实现于 `collector._pair_usage_into_responses()`，逐会话报告见 `collect()` 返回的
`usage_pairing` 与 `probe/t05c/pairing-table.json`。

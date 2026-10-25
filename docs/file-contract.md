# file-contract.md — CodeBuddy 文件通道契约（T03）

> 数据来源：`~/.codebuddy/`（生产读 `CODEBUDDY_HOME`，缺省回退 `~/.codebuddy`）。
> 实测基准：2026-10-25，本机 20 个 trace 文件 / 695 行 transcript / 12 个任务文件。
> 本卡硬约束：`packages/schema/*` 零改动；所有无法映射的字段登记在 §5。

## 1. 三通道目录布局与字段全量表

### 1.1 通道 A：`traces/<workerPid>/trace_<traceId>.jsonl`（结构化执行轨迹，主结构源）

一行一个 JSON。顶层 `type` 四种：

| 顶层 type | 结构 | 关键字段（实测全量） |
| --- | --- | --- |
| `trace_start` | `trace` | traceId, name, workerPid, workerHostname, startedAt, endedAt, duration, status, spanCount, totalTokens, metadata |
| `span_start` / `span_end` | `span` | traceId, spanId, parentId, name, type, startedAt, endedAt, duration, status, error；span_end 可能额外带 attributes、toolInput；type=function 时带 toolName、toolInput；**type=agent 时带 agentName**（§6） |
| `trace_end` | `trace` + `summary` | summary: traceId, name, category, agentName, prompt, startedAt, endedAt, duration, status, spanCount, totalTokens, errorCount, firstErrorMessage, firstErrorSpanId, firstErrorWhere |

- `parentId=null` → 根 span；否则指向父 span 的 spanId（调用树现成）
- `duration` 单位毫秒（整数）
- span `type` 实测三种：`custom`（内部阶段）、`generation`（LLM）、`function`（工具）；**另有 `agent`（智能体 span，T03 §6 实测新增，卡片原文只列了前两种）**
- span `name` 实测：`Read`/`Write`/`Edit`/`Bash`/`mcp_tools`/`generation`/`cli`/`Agent`/`Explore`（后三者为 §6 实验新增）
- span `status`：`running` / `ok` / `error`
- trace `summary.category` 实测：`main`（15）/ `auxiliary`（4，如 terminalTitleGenerator）
- 时间戳：ISO-8601 UTC 字符串

### 1.2 通道 B：`projects/<cwd-slug>/<sessionId>.jsonl`（会话全文，内容源）

`<cwd-slug>` = 工作目录路径中 `/`、`:`、`\` 替换为 `-`。实测 695 行，类型分布：

| 行 type | 占比（实测） | 关键字段 | 处理 |
| --- | --- | --- | --- |
| `function_call` | 284 | id, parentId, timestamp, callId, name, arguments, providerData{traceId, agent, model…} | tool.call |
| `function_call_result` | 283 | callId, name, output, status, parentId, timestamp | tool.result / tool.error(status=error) |
| `file-history-snapshot` | 120 | messageId, snapshot, cwd, _meta | **噪声，直接跳过** |
| `message` | 92 | id, timestamp, role, content | message.recv(user) / message.send |
| `reasoning` | 115 | content, rawContent, id, parentId, timestamp | message.send(kind=reasoning) |
| `session-meta` | 1 | id, sessionId, timestamp, meta | **未映射**（见 §5），计入 skippedLines |

- **每行都带 `_meta`**：`traceparent`（W3C）+ `baggage`（含 `codebuddy.session_id`、`codebuddy.conversation_request_id`）
- `timestamp` 实测为**毫秒 epoch 整数**（见 §6 偏差 D1），实现中统一转 ISO-8601
- 顶层 `<slug>/` 下另有 `<sessionId>/tool-results/` 子目录（本卡不解析，§8 范围外）

### 1.3 通道 C：`tasks/<sessionId>/<taskId>.json`（任务板快照）

一任务一文件，文件名即任务 id。字段实测全量：

```
subject, description, activeForm, status, id, createdAt, updatedAt
```

- `createdAt`/`updatedAt`：毫秒 epoch 整数（如 1792894999185）→ 实现统一转 ISO-8601
- `status`：观测到 `pending` / `in_progress` / `completed`；未知值按 update 处理并出 warning
- 同目录隐藏文件（`.acp-v2-plan-revision`）**跳过不解析**
- **无 `owner` / `assignee` 字段**（卡片已知缺口，实测确认；见 §5 偏差 D3）

## 2. 关联键图

```
sessionId（baggage: codebuddy.session_id）
  ├── tasks/<sessionId>/            （通道 C 目录名）
  ├── projects/<slug>/<sessionId>.jsonl  （通道 B 文件名 + baggage）
  └── ⚠ 通道 A 的 trace 不携带 sessionId —— TraceEvent.session_id 用 traceId 占位（偏差 D6）

traceId
  ├── 通道 A：trace 文件名 / trace 字段 = trace_<32hex>
  └── 通道 B：_meta.traceparent 第 2 段 = 32hex（无 trace_ 前缀）
      ⚠ 两者不相等，不能直接交叉校验（偏差 D2）；traceparent trace-id 只出现在
        transcript 行的 providerData.traceId 与 trace 文件的 toolInput 回显文本中

callId（仅通道 B 内部）
  └── function_call.callId ↔ function_call_result.callId 配对；
      跨批次未配对的调用持久化在游标 openCalls（上限 512）等待，不误报；
      result 找不到任何 call 才发 error 事件（error_type=callid-unpaired）
```

## 3. 游标策略对比

| | 通道 A / B（jsonl 追加型） | 通道 C（tasks 整体重写型） |
| --- | --- | --- |
| 游标键 | 文件绝对路径 | 文件绝对路径 |
| 游标字段 | path, ino, dev, size, offset, lastLineHash, agentNames(A) / openCalls(B) | hash(sha256 全文), mtimeMs, status, owner |
| 判新增 | offset < size | hash 变化 |
| 半行保护 | offset 停在最后一个 `\n` 之后，未完行不消费 | N/A（整体 JSON.parse） |
| 截断检测 | size < offset 或 inode 变化 → 归零重读 + error 事件 | 旧 hash 失配自然覆盖 |
| 幂等 | offset==size → 空数组，游标不写 | hash 相同 → 空数组，游标不写 |
| 存放位置 | `$HS_HOME/state/file-cursors.json`（原子写：tmp + rename；缺省回退 `~/.harness_scope`） | 同左（同一文件） |

大文件契约：jsonl 读取为 64KB 分块 `readSync` 逐块提取完整行，**不使用 readFileSync 整体载入**（12MB 文件实测通过，见 §7）。

## 4. EventType 映射表（通道 A/B/C → 26 类契约）

| 数据源 | EventType | payload 关键字段 |
| --- | --- | --- |
| A `trace_start` | `session.start` | trace_name, worker_pid, worker_hostname |
| A `span` type=function start/end | `tool.call` / `tool.result` / `tool.error`(status=error) | tool_name, args_hash, args_bytes（capture=on 才有 args，且过 redact） |
| A `span` type=generation | `llm.request` / `llm.response` | span_name, duration_ms, status |
| A `span` type=agent start/end | `agent.spawn` / `agent.stop` | agent_name（§6） |
| A `span` 其他（custom） | `step.start` / `step.end` | span_name, duration_ms, status |
| A `trace_end` | `session.end` + `metric.custom` | metric=trace_summary: span_count, total_tokens, error_count, first_error_* |
| A `trace_end` category!=main 且 span 流未发过同名 | `agent.spawn` + `agent.stop` | agent_name, category |
| B `message` role=user/其他 | `message.recv` / `message.send` | role, content_hash, content_bytes |
| B `reasoning` | `message.send` | kind=reasoning, content_hash, content_bytes |
| B `function_call` | `tool.call` | tool_name, call_id, args_hash, args_bytes |
| B `function_call_result` | `tool.result` / `tool.error` | tool_name, call_id, output_hash, output_bytes |
| B `function_call` name=TaskCreate/TaskUpdate | `task.create` / `task.claim`(仅当 args 有 owner) / `task.update` / `task.complete`(status=completed) | task_id, status（**不透传 arguments 原文**） |
| B 配对异常 | `error` | error_type=callid-unpaired |
| 游标截断检测 | `error` | error_type=file-truncated, reset_reason |
| C 首次见到任务 | `task.create` | task_id, subject, description, created_at, updated_at, status |
| C status→completed / 其他变化 | `task.complete` / `task.update` | task_id, updated_at |
| C owner 出现/变化 | `task.claim` | task_id, owner |
| 顶层面包屑 | 所有事件 `source={platform:'codebuddy', channel:'file'}`，`raw_ref=<scrubbed path>@<byte offset>` | |

内容策略（与 T02 一致）：`toolInput` / `arguments` / `message.content` / `output` 默认只存 sha256 hash + 字节数；仅 `HS_CONTENT_CAPTURE=on` 保留原文且必须过 `redact.ts`；home 目录前缀一律替换为 `~`。

## 5. 字段级未映射清单与偏差表

### 5.1 未映射（写入下一轮契约变更候选）

| 字段/记录 | 通道 | 未映射理由 |
| --- | --- | --- |
| `session-meta` 行类型 | B | 1/695 行，meta.mainAgent 信息暂无指标口径；计入 skippedLines |
| `span.attributes`（capture=off 时） | A | 内部遥测属性，无契约落点；capture=on 时随 payload 透传（脱敏后） |
| `trace_end.trace.metadata`（如 tracePayloadTruncated） | A | 无指标口径 |
| `providerData`（model、rawUsage、stepSeq、argumentsDisplayText…） | B | token 用量已在 trace summary 聚合（totalTokens）；逐行 usage 待 T07 指标口径定夺 |
| `<slug>/<sessionId>/tool-results/` 子目录 | B | 大体积工具结果缓存，与指标无关（§8 范围外） |
| `summary.prompt`（如 "Read, Write, Write (+20)"） | A | 摘要文本，无指标口径 |
| `file-history-snapshot` | B | 噪声（卡片明示跳过） |

### 5.2 偏差表（卡片 §2 与实测的冲突，以实测为准）

| # | 卡片说法 | 实测 | T03 处理 |
| --- | --- | --- | --- |
| D1 | §2.4：transcript 时间戳为 ISO-8601 UTC 字符串 | **毫秒 epoch 整数**（如 1792894894220） | 统一转 ISO-8601 后写入 `ts` |
| D2 | §2.4：traceparent trace-id 可与通道 A traceId 交叉校验 | 通道 A traceId 为 `trace_<32hex>`，**≠** traceparent 的 32hex trace-id；后者只在 toolInput 回显文本中出现 | 关联锚点实际只有 `baggage.session_id`（B↔C）；A↔B 目前靠时间线+workerPid 间接对齐，已在 §2 标注 |
| D3 | §2.3：从通道 B 的 function_call 提取任务归属交叉验证 | TaskCreate/TaskUpdate 的 arguments **均无 owner/assignee** 字段（24+16 次调用实测） | `task.claim` 路径保留但当前无数据来源；「认领冲突率」「派单均衡度」成员归属缺口如实上抛，待 T07 或下一轮契约处理 |
| D4 | §2.1：span type 观察到 custom/generation 两种 | **第三种 `agent`**（T03 §6 多智能体实验发现） | 已映射 agent.spawn/agent.stop（见 §4） |
| D5 | TraceEvent.session_id=被测平台会话 ID | 通道 A trace 不含 sessionId | session_id 用 traceId 占位（trace_<hex>），workerPid 落 payload；A↔B 会话归并留给 T05/T07 |
| D6 | §3.1：`teams-watcher.ts` 更名为 `tasks-watcher.ts` | 按卡片执行 | 已命名 tasks-watcher.ts，无别名导出 |
| D7 | span type=function 的 toolInput 大量为空字符串 | 属实（toolInput:""） | 空串按 hash("")+bytes=0 落盘，不特殊处理 |

## 6. 多智能体验证记录（T03 §6）

**验证方法**：在本会话内用 CodeBuddy 自身的多智能体能力（Agent 工具）真实派发了一个 Explore 子智能体执行只读任务，随后复查 `~/.codebuddy/traces/`。

**实测结论（有真实证据）**：

1. **子智能体不产生独立的 `traces/<pid>/` 目录**：trace 目录 diff 前后无变化。子智能体以**同一 trace 内的新 span** 呈现（同一 workerPid）。
2. **span 出现新类别 `type:"agent"`**，携带 `agentName` 字段：
   - 根智能体：`{name:"cli", type:"agent", agentName:"cli", parentId:null}`（全部 20 个 trace 均有此根 span）
   - 子智能体：`{name:"Explore", type:"agent", agentName:"Explore", parentId:"span_87825d…"}` —— `agentName` 即子智能体名 ✓
   - 层级：`Agent`（type=function 的派发工具调用）→ 子智能体 agent span 与子智能体的工具调用均为其子 span（parentId 指向 Agent function span）
3. **`summary.agentName` 是否为子智能体名**：子智能体不产生独立 trace_end，故此问题对子智能体不适用；根 trace 的 summary.agentName 恒为 `cli`（main）。

**证据边界（只有单智能体/辅助样本，未验证项）**：

- `summary.category` 是否出现 `main`/`auxiliary` 之外的新取值：**未验证**（本轮子智能体复用主 trace，无独立 trace_end 可产生新 category）
- teammate 类智能体（平等协作而非派发）的呈现方式：**未验证**
- `tasks/*.json` 归属字段：**未验证**（继续无 owner 字段，与偏差 D3 一致）

已按上述结论实现：span type=agent → `agent.spawn`/`agent.stop`（agent_id=agentName，role：cli→lead、其他→subagent）；trace_end 的 summary 补发仅在 span 流未发过同名时生效，避免双计。

## 7. 性能实测（2026-10-25，快照于本机真实数据）

环境：Node 22.22.2 / Windows 11；数据快照：20 个 trace jsonl（约 9.9MB）+ 5 个 transcript（695 行）+ 12 个任务文件 → 3455 事件。

| 指标 | 阈值 | 实测（5 次原始值） | 结论 |
| --- | --- | --- | --- |
| 冷启动全量解析 | < 800ms p95 | 177.5 / 114.6 / 108.2 / 108.4 / 104.3 ms → **p95=177.5ms** | PASS |
| 增量解析（游标命中） | < 20ms p95 | 7.19 / 6.17 / 5.96 / 5.65 / 5.74 ms → **p95=7.19ms** | PASS |
| 内存峰值 | < 120MB | peakHeap=39.2MB；peakRss=107.8MB（含 Node 运行时基线约 50MB）；解析期 heap 增量约 24.8MB | PASS |
| 单文件 > 10MB 流式 | 不整载 | 12.0MB 合成 trace：5707 个 tool.call，解析 214.1ms，RSS 增量 54.6MB；实现为 64KB 分块 readSync | PASS |

## 8. 实现文件索引

```
packages/adapters-ts/src/codebuddy/
  paths.ts           # CODEBUDDY_HOME / HS_HOME 路径解析
  jsonl-tail.ts      # 增量尾部读取器（半行/截断/坏行/幂等）+ CursorStore
  trace-reader.ts    # 通道 A → TraceEvent[]（含 makeEvent/contentFields 共享件）
  transcript.ts      # 通道 B → TraceEvent[]（traceparent/baggage/callId 配对）
  tasks-watcher.ts   # 通道 C → TraceEvent[]（mtime+hash 游标）
  index.ts           # collectFromFiles() 编排 + 统一导出
packages/adapters-ts/test/file-channel.test.mjs   # §5 六场景 + G4-G7 用例（54 项全绿）
```

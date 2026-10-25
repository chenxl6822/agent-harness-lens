# channel-priority.md — 通道优先级表与 merged 流语义声明（T05a-R1-3）

## 背景：为什么必须有优先级

T05a 验收（`acceptance/G-T05a-验收报告.md` §3.2）实测：通道 A（`~/.codebuddy/traces/`）与
通道 B（`~/.codebuddy/projects/` transcript）承载**同一批工具调用的两份独立观测**——
merged 流中 `tool.call` 1243 = A 636 + B 607，以「同 tool_name + ≤200ms 时间近邻」配对
84.4%（537/636）重叠；而 `traceId` 与 `sessionId` **无一同名（0/17）**，T03 已证实
`traceparent ≠ traceId`，故 A↔B **不存在键级归并可能**，只能时间对齐，且对齐精度实测为
**≤200ms 而非精确相等**（0ms 精确相等仅 0.2%，两侧时间戳粒度/取值不同）。

结论：

1. **键级去重不可行**——同一工具调用在 A/B 产生两个必然不同的 `event_id`（raw_ref/稳定标识不同），
   `dedup_dropped: 0` 是必然结果，不是缺陷。
2. 因此 `collect()` 引入 `channel_policy` 参数（`primary_only` | `merge`），**默认 `primary_only`**：
   9 类 A/B 双源事件只取主源通道，B 侧 `function_call`/`function_call_result` 记录抑制
   （drop 计数 `channel_b_secondary_suppressed`）。
3. **merge 模式仅用于映射覆盖测绘**，其产物不得直接作为 T06 指标口径——否则工具类指标
   重复近一倍（实测 tool.call 1416 vs 727）。

## A/B 双源 9 类事件的主源判定

| 事件类型 | 主源 | 次源 | 依据 |
| --- | --- | --- | --- |
| `tool.call` | **A** | B（D） | A span 有 `spanId` + `startedAt/endedAt` 高精度时间 + `toolInput` 哈希；B 是 UI transcript 镜像，缺 rootCall 语义；D 为描述子 preview（skipped-heavy 有损） |
| `tool.result` | **A** | B（D） | 同上；A span_end 带 status/duration |
| `tool.error` | **A** | B（D） | 实测本次快照 A=7、B=0：B 的 transcript 丢失 error 标记，A 是唯一可靠 error 源 |
| `step.start` | **A** | D | custom span 成对出现且含 duration；B 无此源（实测 B=0） |
| `step.end` | **A** | — | 同上；D 仅有 pre-step 隐式 start，无 end |
| `llm.request` | **A** | D | generation span 含 status/duration；D `llm/stream` 仅请求参数（maxTokens 等） |
| `llm.response` | **A** | — | B、D 均无此源（D 无 usage/token 回报，见矩阵 no_source 注记） |
| `artifact.write` | **A** | B（D） | 跟随 `tool.call` 主源派生（主源判定一致性：派生事件必须与父事件同源，否则派生层双计） |
| `artifact.ref` | **A** | B（D） | 同上 |

## 26 类事件 × 主源通道全表

优先级序（`>` 表示取前者；括号为该通道无此源时顺延）。channel 代号：H=hook spool、
A=traces、B=transcript、C=tasks 快照、D=dsh。

| # | 事件类型 | 主源 | 优先级序 | 依据 |
| --- | --- | --- | --- | --- |
| 1 | run.start | 无 | （编排层合成） | run 是评测执行单元，被测平台无此概念 |
| 2 | run.end | 无 | （编排层合成） | 同上 |
| 3 | session.start | D | D > A > H | D `session/created` 有真实 sessionId + createdAt；A 的 session_id 是 traceId 占位（T03 D5） |
| 4 | session.end | A | A > H | A `trace_end` 有 status/duration；D 无会话结束事件 |
| 5 | agent.spawn | D | D > A | D `agent/created` 有 provider/model/parentSession |
| 6 | agent.stop | D | D > A | D 有 stop_reason + last_message 哈希；直挂/镜像双计已按 min-ts 确定性归并 |
| 7 | turn.start | D | D > H | 账本 `turn/start`（多 turn 由 pre-step 派生，T06 补） |
| 8 | turn.end | H | H | 唯一 source |
| 9 | step.start | A | A > D | 见上表 |
| 10 | step.end | A | A | 见上表 |
| 11 | llm.request | A | A > D | 见上表 |
| 12 | llm.response | A | A | 见上表 |
| 13 | tool.call | A | A > B > D | 见上表 |
| 14 | tool.result | A | A > B > D | 见上表 |
| 15 | tool.error | A | A > B > D | 见上表 |
| 16 | task.create | C | C > B | C 快照是持久真源（subject/description_hash）；B 只有调用瞬间 arguments |
| 17 | task.claim | H | H | 唯一潜在源（headless -p 不触发 hooks，见 result.json open_questions） |
| 18 | task.update | C | C > B | C 有 updatedAt 最终态；B 的 TaskUpdate arguments 无中间历史 |
| 19 | task.complete | C | C > B | 同上 |
| 20 | message.send | B | B > D | B message/reasoning 内容完整；D 仅由 send_message 工具调用派生 |
| 21 | message.recv | D | D > B | D inbox 事件有 via 语义（first_via/via_set 固定优先级归并）；B user message 是会话输入镜像 |
| 22 | artifact.write | A | A > B > D | 跟随 tool.call 主源派生 |
| 23 | artifact.ref | A | A > B > D | 同上 |
| 24 | context.compact | H | H | 唯一 source（A/B/C/D 实测无压缩标记） |
| 25 | error | 全通道并列 | 不去重 | error 是补充信号，各通道独立保留 |
| 26 | metric.custom | A | A | 唯一 source（trace_summary） |

注：优先级序是**同类型多源同时可用时**的取舍规则；`primary_only` 当前只在 A/B 之间生效
（A/D、A/H 等跨平台双源归并留待 T05b/T06，其归并同样受「无公共标识锚点」约束）。

## 本次实测重叠数字（2026-10-26 快照，merge 模式）

`~/.codebuddy`（活跃数据，较验收时已增长：原始行 9043）：

| 类型 | A | B | ≤200ms 时间近邻重叠 |
| --- | --- | --- | --- |
| tool.call | 727 | 689 | 619 |
| tool.result | 719 | 752 | 660 |
| tool.error | 7 | 0 | 0 |
| step.start / step.end | 1994 / 1994 | 0 | 0 |
| llm.request / llm.response | 584 / 584 | 0 | 0 |
| artifact.write | 206 | 212 | 197 |
| artifact.ref | 93 | 95 | 83 |

`primary_only` 下 codebuddy-files：tool.call **727**（= 通道 A 单独计数），merge 下 1416
（727+689，零键级去重）。数字与 `probe/t05a/result.json` 的 `cross_channel_redundancy` 段一致。

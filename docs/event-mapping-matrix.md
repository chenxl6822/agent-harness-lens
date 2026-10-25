# event-mapping-matrix.md — 三通道统一摄入映射完备性测绘（T05a）

> 机读真源：`probe/t05a/mapping-coverage.json`（T05b 直接消费）。本文为人读摘要。
> 数据基准（全部实测）：
> - **D**：`probe/rc3c/raw/e1c-events.ndjson`（T04c run3 全量落盘，1646 行 / 71,157,639 字节）
> - **A/B/C**：`~/.codebuddy/traces|projects|tasks`（本机真实数据，2026-10-25，20 trace 文件 / 21 transcript / 16 任务文件）
> - **H**：`probe/t05a/work/spool2/t05a-hook-replay.jsonl`（真实 CLI stdin 形状经真实 `hook-cli.js` 重放；live-fire 实录见 `docs/hook-contract.md` 附录A）
>
> 格式：`source` = 有直接数据源（附字段路径与实测证据）；`derived` = 可由其他事件推导（附规则）；`no_source` = 确实没有（附原因）。

## 0. 两个派单前事实的实测结论（任务卡 §2）

### 2.1 `turn.end` / `step.end` 在 dsh 侧不存在 —— 已在 71.2MB 全量样本上复核

`internal/dispatch` 派发的 15 种事件、`session/event` 账本 8 种 item 类型，均**无** `turn/end`、`step/end`。
且 `agent/assistant-stream` 的 320 采样帧只有 `start`(2)/`chunk`(318)，**未见 `end` 帧**（158904 帧总量的采样，end 帧是否存在待更大样本确认）。

**结论（已写入矩阵）**：
- `turn.end`(D) = `derived`：同 agent 下一个 `turn/start` 的 ts；末 turn 用最后一条 `agent/status='idle'` 的 ts。
- `step.end`(D) = `derived`：同 agent 下一条 `agent/pre-step` 的 ts；末步同上。
- 推导规则**不依赖**未证实的 stream end 帧。T06 算 turn/step 时长时取 `derived` 列即可，不会得到 null。

### 2.2 `session/event` 重复计数 —— 已定量证实并去重

实测：767 个 `session/event` 快照共携带 **6070** 条账本 item，但按 `(session.id, item.seq)` 去重后仅 **24** 条唯一（3 个 agent × 各 8 条）。
**每个快照携带的是全量账本**（非增量），重复率 **99.6%**。去重后 `turn/start` 从 758 条回落到 3 条（每 agent 1 条），与 3 个 agent 的真实结构吻合。

| 指标 | 值 |
|---|---|
| session/event 快照数 | 767 |
| 账本 item 总出现次数 | 6070 |
| 唯一 (session.id, item.seq) | **24** |
| 重复率 | 99.60% |
| 去重键 | `session.id + item.seq`（实测每 session 内 seq 0..7 连续唯一） |

摄入器实测（`hs_core/collector.py`，dsh 通道统计）：`ledger_dup_session_seq=6046`，`turn.start=3`，`step.start=98`（pre-step 主源）。

## 1. 映射矩阵总览（26 类 × 5 通道 = 130 格）

| EventType | H(hook) | A(traces) | B(transcript) | C(tasks) | D(dsh) |
|---|---|---|---|---|---|
| run.start / run.end | no | no | no | no | no |
| session.start | **src** SessionStart | **src** trace_start | der 首行 sessionId | no | **src** session/created |
| session.end | **src** SessionEnd | **src** trace_end | no | no | der 最后 status=idle |
| agent.spawn | **src** SubagentStart | **src** span(type=agent) | no | no | **src** agent/created |
| agent.stop | **src** SubagentStop | **src** span_end(type=agent) | no | no | **src** subagent/end |
| turn.start | **src** UserPromptSubmit | no | der message(role=user) | no | **src** 账本 turn/start |
| turn.end | **src** Stop | no | no | no | der 下一个 turn/start |
| step.start | no | **src** span(custom) | der stepSeq 变化 | no | **src** agent/pre-step |
| step.end | no | **src** span_end(custom) | der 下一 stepSeq | no | der 下一条 pre-step |
| llm.request | no | **src** span(generation) | no | no | **src** llm/stream |
| llm.response | no | **src** span_end(generation) | der rawUsage 行 | no | der stream chunk 尾帧 |
| tool.call | **src** PreToolUse | **src** span(function) | **src** function_call | no | **src** tools/execute |
| tool.result | **src** PostToolUse | **src** span_end(ok) | **src** function_call_result | no | **src** tools/result |
| tool.error | **src** PostToolUseFailure | **src** span_end(error) | **src** result(status=error) | no | **src** result(isError=true) |
| task.create | **src** TaskCreated | no | **src** TaskCreate 调用 | **src** 首见快照 | no |
| task.claim | **src** TaskCreated.owner | no | no | no | no |
| task.update | no | no | **src** TaskUpdate 调用 | **src** 快照变化 | no |
| task.complete | **src** TaskCompleted | no | **src** TaskUpdate(completed) | **src** status=completed | no |
| message.send | no | no | **src** message/reasoning 行 | no | **src** send_message 调用 |
| message.recv | no | no | **src** message(role=user) | no | **src** agent/inbox/* |
| artifact.write | der Write/Edit 类 | der 同左 | der 同左 | no | der write/edit 调用 |
| artifact.ref | der Read/Glob/Grep | der 同左 | der 同左 | no | der read/glob/grep/read_image |
| context.compact | **src** PreCompact | no | no | no | no |
| error | no | der summary.errorCount | no | no | der isError / agent/error |
| metric.custom | no | **src** trace summary | der rawUsage | no | no |

**合计：source 47 / derived 20 / no_source 63**（每通道：H 13/2/11，A 12/3/11，B 8/8/10，C 3/0/23，D 11/7/8）。

## 2. 任务卡点名的四组语义裁决

### 2.1 `agent.spawn` / `agent.stop`：dsh 侧如何映射，父 agent 算不算 spawn？

- 子 agent：`agent/created`(args[0].agent.id, session.header.parentSession=父id) → `agent.spawn`(role=subagent)；`subagent/end`(runId, id, stopReason, lastAssistantMessage) → `agent.stop`。`subagent/start` 与 `agent/created` 冗余（唯一 runId 逐一对应），摄入时丢弃（`subagent_start_redundant_with_agent_created=2`）；`subagent/start|end` 的直挂+镜像双计按 `(event, runId)` 去重（实测 4/4 → 2）。
- **父 agent 算 `agent.spawn`：是。** 判据：`agent/created` 对父（id=`session-` 前缀、无 parentSession）与子各发一次（实测 3 次）；父的诞生是本 session 智能体生命周期的起点，T06 按成员算「存在时长/负载」必须有它（role=lead）。实测：父 `agent.spawn` 1 + 子 2 = 3。
- 父的 `agent.stop` 无直接事件：由最后一条 `agent/status='idle'` derived（矩阵已记）。子 `agent.stop`=2，父=0（derived 路径覆盖）。

### 2.2 `message.send` / `message.recv`：CodeBuddy 与 dsh 语义是否对齐？

- **对齐**。CodeBuddy：transcript 的 `message(role=user/assistant)` 行 + `send_message` 工具调用；dsh：`tools/execute(name='send_message')` → `message.send`，`agent/inbox/inserted|claimed` → `message.recv`（按 `message.id` 去重：inserted×24 + claimed×24 → 12 条唯一消息；账本 `agent/inbox/spliced` 与 inbox 同 id，6 条丢弃避免三源重复）。
- 差异记录：CodeBuddy 的 inbox 是 CLI 层邮箱（SubagentStart/TaskCreated 等 hook 事件另走通道）；dsh 的 inbox 是 agent 间消息总线，`claimed` 携带 `turn` 号（承接时刻），正好支持 T06 的「消息轮次效率」。

### 2.3 `task.create/claim/update/complete`：通道 C 快照能否还原四类？

**如实回答：只能还原 3 类，且是有损还原。**

- `task.create` ✅：首见快照，`createdAt` 为创建时刻。
- `task.complete` ✅：`status='completed'` + `updatedAt`。
- `task.update` ⚠️ 有损：快照**无变更历史**，只有最终态 + `createdAt`/`updatedAt`。同任务多次 update 只能还原最后一次；`in_progress→pending` 等中间迁移**不可还原**。本机实测 16 文件（completed×15 + pending×1）。
- `task.claim` ❌ `no_source`：快照字段全量（subject/description/activeForm/status/id/createdAt/updatedAt）实测**无 owner/assignee**；通道 B 的 TaskCreate/TaskUpdate arguments 亦无 owner（T03 偏差 D3，24+16 次调用实测）。认领冲突率、派单均衡度在 CodeBuddy 侧的成员归属缺口如实上抛。
- 通道 B 的 TaskCreate/TaskUpdate 调用行（tool.call 语义）可作 `task.create/update` 的第二来源（有调用时刻，无中间历史）。

### 2.4 其余 `no_source` 的代表性原因（全量见 mapping-coverage.json）

- `run.start/run.end`（5×2 格）：run 是 harness 评测执行单元，被测平台无此概念，由编排层合成——**这不是数据缺口，是职责边界**。
- `context.compact`：H 有 PreCompact ✅；A/B/C/D 全无（e1c 71.2MB 全量事件名清单无 compact 类；CodeBuddy trace/transcript 亦无标记行）。上下文压缩指标目前**只有 hook 通道可观测**。
- `metric.custom`(D)：llm/stream 只含请求参数（maxTokens），**无 usage/token 回报**；dsh 成本类指标无数据源。

## 3. 关联键与脱敏豁免

- 豁免脱敏（原文保留）：`callId` / `rootCallId` / `agentId` / `attemptId` / `parentId` / `runId`(subagent) / `session.id` / `message.id` / `traceId`。
- 内容默认只存 `content_sha256 + content_bytes`（HS_CONTENT_CAPTURE=off 安全默认）。
- 路径脱敏：本机用户 home（`C:/Users/<USER>`）→ `<WINHOME>`；G6 复核：三份 `events.normalized.ndjson` 与本卡全部产物 `grep <本机用户名>` 命中 0。

## 4. 对 T05b 的输入结论

1. 事件流已可统一、无重、可归属：三通道实测产出 8432 条事件，全部通过 `trace-event.schema.json` 校验（invalid=0）。
2. SQLite schema 可按 `session_id + type + seq` 建索引；`event_id` 是确定性 ULID（时间戳 + 内容摘要），天然幂等键。
3. `turn.end/step.end/session.end` 在 D 通道是 derived——物化层需要一次「边界闭包」步骤（T05b 重放时执行）。
4. `task.claim`（CodeBuddy 全通道）、`metric.custom`（D 通道）、`context.compact`（A/B/C/D）是诚实缺口，T06/T07 需为相关指标设计降级口径。

# dsh 桥接契约草案：事件/参数 → TraceEvent 映射（T04b 产出）

> 状态：草案，供 T05 实现参考。全部映射基于 **rc.2 运行时实测**（T04b，2026-10-25）。事件名以实测为准，映射到 harness_scope 的 TraceEvent.type 26 枚举时由 T05 落定。

## 0. 桥接架构（实测结论）

- 插件经 `--patch`（挂在 profile 名之后：`dsh headless --patch x.yml "任务"`）以 `.mjs` 加载，`apply(ctx)` 收到的 ctx 与 root/events 共享同一条 cordis 总线。
- **推荐订阅方式：`ctx.on('<事件名>', handler)` 直挂**（agent/* 已运行时证明全量可达，11670 帧无一丢失）。
- `internal/dispatch` 是总线「派发遥测镜像」：`(method, eventName, argsArray, callback|null)`。任何事件无论走 emit/waterfall/serial/bail/parallel 都会镜像一条。**它是兜底全量面**；若直挂监听遗漏某事件，镜像必然有记录。

## 1. 事件清单（rc.2 运行时实测，一次读+写 headless 任务）

| 总线事件 | 派发方法 | 次数 | 载荷要点 |
| --- | --- | --- | --- |
| `agent/assistant-stream` | waterfall 为主 | 11670 | frame={type:start\|chunk\|end, attemptId, revision, index, turn, step, outcome}；start 36 / chunk 11601 / end 33 |
| `tools/pre-execute` / `tools/execute` / `tools/post-execute` | waterfall | 各 33 | [toolCall, callback?]；toolCall={token, callId, rootCallId, name, arguments, agent, concludeTurn,…} |
| `tools/result` | emit | 33 | [toolCall, result]；result={isError, content[{type,text}], meta, value} |
| `agent/pre-step` | waterfall | 36 | {messages[全量消息], turn, step, signal, agent} |
| `agent/request` | waterfall | 36 | {turn, step, signal, agent}（LLM 请求前） |
| `llm/stream` | waterfall | 3 | {maxTokens, reasoningEffort, provider, model, messages, system, sessionId, purpose}（purpose 区分主对话/会话标题等） |
| `agent/created` | serial | 3 | {agent{id, options{provider,model}, session, phase}, source} |
| `agent/inbox/inserted` / `claimed` | emit | 各 3 | {message{content, source, role, id}, turn?, agent} |
| `agent/status` | emit | 3 | {status:'running'…, agent} |
| `session/created` / `session/event` / `session/flush` | emit | 3/30/6 | session 对象含 header{id,createdAt,cwd} 与 log[]（seq/time/type/data 事件账本） |
| `tools/change` / `commands/change` / `skills/change` / `llm/adapters-updated` | — | 75/15/6/15 | 工具/命令/技能/适配器清单变化 |
| `system-prompt/change` / `fs/observed` / `fs/write-intent` / `settings/document-updated` / `domain/changed` / `subagent/provider-added` / `loader/patch-context` | — | 78/9/6/33/30/6/270 | 框架面 |
| `internal/status` / `internal/plugin` | — | 651/360 | 框架面 |

## 2. 轨迹要素 → 事件映射

| TraceEvent 语义 | 来源事件与字段 | 置信 |
| --- | --- | --- |
| 工具调用开始 | `tools/execute`（或 pre-execute）→ name, callId, arguments | 运行时，high |
| 工具调用结果 | `tools/result` → 按 callId 关联，result.content[].text / isError | 运行时，high |
| LLM 消息（输入） | `agent/pre-step` → messages[]（role/content/source） | 运行时，high |
| LLM 消息（流式输出） | `agent/assistant-stream` chunk 帧（type/chunk 增量） | 运行时，medium（chunk 内层结构未逐帧展开，见 §4） |
| turn 边界 | frame.turn、agent/inbox/claimed.turn、agent.phase.turn | 运行时，high |
| step 边界 | frame.step、agent/pre-step.step、agent/request.step | 运行时，high |
| 会话/代理生命周期 | agent/created、agent/status、session/created、session/flush | 运行时，high |
| 会话账本（可整体物化） | session/event → session.log[]（自带 seq/time） | 运行时，high |

## 3. 实现注意（全部来自实测踩坑）

1. **不要只包 `ctx.emit`**：语义事件大多走 waterfall/serial/bail，emit 包裹会漏掉 99% 的轨迹（T04a 教训）。用 `ctx.on` 直挂 + `internal/dispatch` 镜像兜底。
2. 同一总线在 ctx/ctx.root/ctx.events/registry[ctx] 多个别名上可达，**只挂一处监听**，否则事件重复计数。
3. `ctx.on` 对 RegExp 模式可用（`/^(agent|subagent)\//` 实测挂载成功）。
4. 参数对象含大循环引用（session.log 14 万+字符、agent 全量引用），**落盘前必须做深度截断与脱敏**（参照 probe/rc3b/probe2.mjs 的 redact/capVal）。
5. 插件必须是 `.mjs`（rc.2 无 jiti/tsx）；`--patch` 必须在 profile 名之后。
6. 凭证：headless 须环境变量注入 `DEEPSEEK_API_KEY`（`~/.dsh/.credentials.yaml`，sk- 前缀），不得落盘。
7. `[object Object]` 事件（emit 首参为对象，keys 为空）实测为空对象派发，可忽略。

## 4. 未决（T05 开工时验证）

- `agent/assistant-stream` chunk 帧的内层增量结构（本次探针捕获深度≤3 未展开；事件本体可达无疑问）。
- `tools/*`、`llm/*`、`session/*` 的 `ctx.on` 直挂验证（本次只直挂了 agent/*；镜像已证明其派发，同总线理论可达）。
- rc.3 行为（静态存在 ≠ 运行时可达，门禁文档已锁 rc.2）。

---

## 5. 验收复核补正（WorkBuddy，G-T04b）

> 本节由验收方追加，保留上文字可追溯。全部结论来自对 `probe/rc3b/raw/events.ndjson`（26059 行 / 148MB）的独立流式重算，不采信自检数字。详见 `Agent应用项目立项/acceptance/G-T04b-验收报告.md`。

### 5.1 `llm/stream` 不是主 LLM 通道（修正 §1、§2）

上文把 `llm/stream` 的作用描述为「purpose 区分主对话/会话标题等」。实测：**`llm/stream` 仅 3 条，`purpose` 全部为 `session-title`**，主对话根本不走它。

| 事件 | 次数 | 有无 messages |
| --- | --- | --- |
| `agent/pre-step` | 36 | **有，全量消息列表**（主对话 LLM 输入的唯一正确来源） |
| `agent/request` | 36 | 无（仅 `{turn, step, signal, agent}`） |
| `llm/stream` | 3 | 有，但全部是会话标题辅助请求 |

**T05 实现约束**：主对话消息取 `agent/pre-step.messages`，不要取 `llm/stream`。

### 5.2 只有 `start` 帧带 turn/step（修正 §1 表格 `frame` 字段说明）

| 帧类型 | 帧数 | 字段 | turn/step |
| --- | --- | --- | --- |
| `start` | 36 | `{type, attemptId, revision, turn, step}` | **有** |
| `chunk` | 11601 | `{type, attemptId, revision, index, time, chunk}` | 无 |
| `end` | 33 | `{type, attemptId, revision, index, outcome}` | 无 |

含 turn+step 的帧占 36/11670 = **0.3%**。**T05 实现约束**：chunk 帧按 `attemptId` 回连同 attempt 的 `start` 帧取 turn/step（`attemptId` 形如 `session-…:3`，末段为 turn 号）。

### 5.3 `callId` 脱敏使配对不可复核（新增硬约束）

`≥24 位 base64 → <B64>` 规则把 `call_00_xxxx…` 全部替换成 `call_00_<B64>`，实测后果：

| 指标 | 值 |
| --- | --- |
| `tools/execute` | 33 |
| execute 唯一 callId | **2** |
| `tools/result` 唯一 callId | **2** |

33 次调用塌缩为 2 个唯一 callId，因此「按 callId 一一对应」在留存产物中**无法验证**。

**T05 实现约束（硬）**：正式桥接必须对 `callId` / `rootCallId` 豁免脱敏，并增加门禁「`tool.call`↔`tool.result` 配对率 = 100%」。否则工具调用与结果在离线阶段即断裂。

### 5.4 本次运行无子智能体（限定结论适用范围）

`agent/created` 只出现 **1 个** session id（`session-15d2684a-…`，12 次 attempt）；`subagent/*` **仅 `provider-added` ×6**（启动期 provider 注册，非派生）。子智能体实例数 = **0**。

因此「轨迹粒度 = full」应读作「**单智能体 full**」。`agent/*` 的可达性结论成立，`subagent/*` **仍未验证**——而协作类指标全部依赖它。此项不在本卡范围内，已升级为项目级风险，由后续多智能体探针任务解决。

### 5.5 附带确认

- **sessionId 在 dsh 侧完全可得**（`agent.id`、`session/created`、`session/flush`、`session/event` 均携带）。T03 遗留的「通道 A 无 sessionId」问题**在 dsh 通道不存在**，跨通道归并难点只在 CodeBuddy `traces` 一侧。
- `session/event` 的 `session.log[]` 自带 `seq/time/type/data`，是一份比 `internal/dispatch` 镜像更干净的物化账本，T05 存储层应优先评估。

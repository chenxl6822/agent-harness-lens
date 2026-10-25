# T04a 证据归档

> 为什么放在 `docs/evidence/` 而不是 `probe/`：`probe/` 在 `.gitignore` 里（一次性产物不入库），但**这一份的结论要长期引用**，所以单独归档。
> 原始 `raw/` 目录（含 465KB 的 `events-rc2.ndjson`）仍留在 `probe/rc3/raw/`，不入库；本目录只保留可复核的结论与清单。

## 来源

| 项 | 值 |
|---|---|
| 任务卡 | `taskcards/T04a-dsh-rc3复验与事件测绘.md` |
| 派单台账 | `runs/T04a-2026-10-25T13-51-24-934Z/` |
| CodeBuddy 会话 | `01a17354-a3b8-7aec-8e49-5100b8908430` |
| 退出码 / 耗时 | 0 / 1699s |
| 验收报告 | `Agent应用项目立项/acceptance/G-T04a-验收报告.md` |
| 执行时间 | 2026-10-25 21:51 – 22:19 (+08:00) |

## 文件

| 文件 | 内容 |
|---|---|
| `result.json` | 结构化结论：版本、`--patch` seam、`ctx` API、事件名、阻塞项、对 T04b 的建议 |
| `notes.md` | 执行过程记录（命令与原文输出） |
| `events-runtime-rc2.names.json` | 从 3404 条落盘事件里聚合出的事件名清单 |

## 事件名清单的准确读法

`events-runtime-rc2.names.json` 里 `distinct = 13`，但这 13 个 key **不能直接当成 13 个事件类型**：

- `[object Object]`（24 条）：`emit` 首参传的是对象而非字符串，名字无法字符串化
- `(no-name)`（2 条）：记录里没有 `event` 也没有 `name` 字段
- `rc3-probe/ping`（2 条）：**探针自己发射的**，用于验证 `emit` 包裹链路通不通

所以真正的 dsh 框架事件是剩下的 **10 个**：`internal/dispatch` 1784、`internal/status` 942、`loader/partial-dispose` 290、`internal/plugin` 227、`system-prompt/change` 64、`tools/change` 50、`settings/document-updated` 11、`domain/changed` 4、`subagent/provider-added` 2、`fs/observed` 2。

## 三条必须记住的结论

1. **`--patch` 必须放在 profile 名之后**：`dsh headless --patch x.yml "任务"`。放最前报 `--profile <name> is required`，挂在 `--profile web` 后报 `too many arguments`。
2. **根 ctx 上没有任何 agent/turn/step/llm 语义事件**。轨迹只可能藏在 `internal/dispatch` 的参数值里（1784 条，量级支持这个猜测），但当前只落了参数类型，未验证。
3. **rc.3 不可运行**：只存在于 `D:\DSH\resources\app.asar`（Electron 打包，182,434,783 字节），解包需引入新依赖。所有运行时结论锁死在 rc.2，rc.3 只有静态证据。

## 独立复核记录

以下数字由 WorkBuddy 实测，非执行方自报：

| 项 | 实测值 |
|---|---|
| `events-rc2.ndjson` 行数 | 3404 |
| `internal/dispatch` 条数 | 1784 |
| 可运行 dsh 版本 | `0.2.1-rc.2` |
| `app.asar` 字节数 | 182,434,783 |
| asar 内 `0.2.1-rc.3` 出现次数 | 4623 |
| asar 内 `agent/assistant-stream` / `agent/inbox/claimed` / `subagent/start` | 11 / 16 / 11 次 |
| 凭证泄漏（`sk-*` 模式） | 0 |

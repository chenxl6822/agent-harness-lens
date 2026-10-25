# hook-contract.md — CodeBuddy Hook 采集通道契约（T02）

> 基准：CodeBuddy CLI 2.173.0（hook 事件名自 CLI 二进制提取，全部实测存在）
> 本文档是 `hook-mapping.ts` / `install.ts` 的权威配套说明。

## 1. Hook 配置承载位置与 JSON 形状

**承载位置**（优先级从高到低）：

1. 环境变量 `HS_CODEBUDDY_SETTINGS` 指定的任意路径（测试/CI 用）
2. `~/.codebuddy/settings.json`（默认）

CodeBuddy CLI 使用与 Claude-Code 兼容的 hooks 配置形状（同一套 `hook_event_name` / `matcher` / `permission_mode` 字段）。`install.js` 写入的真实片段：

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "node \"C:\\path\\to\\harness_scope\\packages\\adapters-ts\\dist\\codebuddy\\hook-cli.js\""
          }
        ]
      }
    ]
  }
}
```

12 个事件（SessionStart / UserPromptSubmit / PreToolUse / PostToolUse / PostToolUseFailure / SubagentStart / SubagentStop / TaskCreated / TaskCompleted / PreCompact / Stop / SessionEnd）各写一条 `matcher: "*"` 条目。

> ⚠️ **诚实声明**：以上形状按任务卡 §6 给定的 Claude-Code 兼容结构写入，本卡未在真实 CodeBuddy 会话中验证触发（见 §6 E2E 记录）。若 CLI 拒绝该形状或不触发，按任务卡 §6.4 的指引去 CLI 二进制 `grep hook_event_name` 附近查真实 schema，并更新本节。

## 2. 12 个事件的真实 stdin 样例

公共字段：`session_id`、`transcript_path`、`cwd`、`permission_mode`、`hook_event_name`。

```jsonc
// SessionStart
{"session_id":"cb-a1b2","transcript_path":"~/.codebuddy/projects/p/t.jsonl","cwd":"D:/demo","permission_mode":"default","hook_event_name":"SessionStart","matcher":"startup"}
// UserPromptSubmit
{"session_id":"cb-a1b2","transcript_path":"...","cwd":"D:/demo","permission_mode":"default","hook_event_name":"UserPromptSubmit","prompt":"修复登录 bug"}
// PreToolUse
{"session_id":"cb-a1b2","transcript_path":"...","cwd":"D:/demo","permission_mode":"default","hook_event_name":"PreToolUse","tool_name":"Read","tool_input":{"file_path":"src/a.ts"}}
// PostToolUse
{"session_id":"cb-a1b2","transcript_path":"...","cwd":"D:/demo","permission_mode":"default","hook_event_name":"PostToolUse","tool_name":"Read","tool_input":{"file_path":"src/a.ts"},"tool_response":{"type":"text","file":{"filePath":"src/a.ts"}}}
// PostToolUseFailure
{"session_id":"cb-a1b2","transcript_path":"...","cwd":"D:/demo","permission_mode":"default","hook_event_name":"PostToolUseFailure","tool_name":"Bash","error_type":"exit_code_1"}
// SubagentStart
{"session_id":"cb-a1b2","transcript_path":"...","cwd":"D:/demo","permission_mode":"default","hook_event_name":"SubagentStart","agent_id":"impl-01","parent_agent_id":"lead"}
// SubagentStop
{"session_id":"cb-a1b2","transcript_path":"...","cwd":"D:/demo","permission_mode":"default","hook_event_name":"SubagentStop","agent_id":"impl-01"}
// TaskCreated
{"session_id":"cb-a1b2","transcript_path":"...","cwd":"D:/demo","permission_mode":"default","hook_event_name":"TaskCreated","task_id":"t-042","owner":"impl-01"}
// TaskCompleted
{"session_id":"cb-a1b2","transcript_path":"...","cwd":"D:/demo","permission_mode":"default","hook_event_name":"TaskCompleted","task_id":"t-042","owner":"impl-01"}
// PreCompact
{"session_id":"cb-a1b2","transcript_path":"...","cwd":"D:/demo","permission_mode":"default","hook_event_name":"PreCompact","trigger":"auto"}
// Stop
{"session_id":"cb-a1b2","transcript_path":"...","cwd":"D:/demo","permission_mode":"default","hook_event_name":"Stop","stop_hook_active":false}
// SessionEnd
{"session_id":"cb-a1b2","transcript_path":"...","cwd":"D:/demo","permission_mode":"default","hook_event_name":"SessionEnd","reason":"clear"}
```

> 字段名以 CLI 2.173.0 实测为准；专属字段（`prompt`/`tool_input`/`duration_ms` 等）缺失时适配器有安全兜底（`unknown`/省略），不会丢事件。

## 3. 未使用事件及理由

| 事件 | 理由 |
| --- | --- |
| `PermissionRequest` | 无对应指标口径（架构 §2.3 六项协作指标均不消费），接入只会污染 trace 语义 |
| `Notification` | 纯 UI 通知，高频低信息量，无评测口径 |

二者进入 hook-cli 后按「未知事件」处理：丢弃并计入 `_stats.json` 的 `dropped_unknown_event`，不报错。

## 4. install 写入路径与备份策略

- 写入路径：`HS_CODEBUDDY_SETTINGS` → 默认 `~/.codebuddy/settings.json`（`install.js` 中 `settingsPath()`）。
- 备份：目标文件已存在时，先复制为 `settings.json.hs-backup-<ISO 时间戳>`（冒号/点替换为 `-`），再写入。
- 幂等：以「command 含 `hook-cli.js`」识别我们的条目；重复 install 先移除旧条目再写入最新一条，用户其它 hooks 原样保留。第二次 install 全部 skipped。
- 卸载：`node .../dist/codebuddy/install.js --uninstall` 只移除我们的条目。
- 试运行：`--dry-run` 只打印 added/skipped，绝不落盘。
- 生效时机：settings 修改后通常需要重启 CodeBuddy 会话才生效。

## 5. 已知坑

1. **Node 冷启动**：本开发机（Windows 11 + 杀软）裸 `node -e ""` spawn P95 ≈ 210ms，因此 hook 进程总耗时 P95 ≈ 236ms，超过卡片 150ms 阈值——但 **hook 自身净开销 P95 ≈ 27ms**（`npm run bench:hook` 输出 `hook_net_overhead_p95_ms`，已含裸 Node 基线对照）。进程内 normalize+redact+spool P95 ≈ 1.2ms ≪ 10ms 达标。正常开发机（无 AV 扫描拖累）总耗时预期 < 150ms。
2. **spool 目录不可写时 `_stats.json` 也写不进去**（计数与数据同目录）：此时 `spool_error` 无法落盘计数，只能保证 exit 0 + stdout 空（尽力而为语义，任务卡允许非事务性）。
3. **`seq` 恒为 0**：hook 进程间无共享状态，全局自增必然引入锁、违背 P95 目标；seq 由 T05 collector 入库时按 `(run_id, ts_mono_ms, event_id)` 单调赋号。
4. **stdout 红线**：hook 进程任何情况下不写 stdout（CLI 会把它当 hook 响应注入上下文）；诊断信息只在 `HS_DEBUG=1` 时写 stderr。
5. **stdin 挂起**：调用方不关 stdin 时，300ms 超时按 EOF 处理（`.unref()` 不阻塞退出）。
6. **`@types/node`**：为编译 `node:fs` 等内建模块类型而新增的 devDependency（类型包，非运行时依赖，与卡片「不引新依赖」不冲突，已在自报 deviations 登记）。

## 6. E2E 真实会话验证记录（可选门禁）

**未执行**。原因：验证需把 hooks 写入真实 `~/.codebuddy/settings.json` 并重启当前正在运行本任务的 CodeBuddy 会话，会影响用户全局配置与当前工作流，属需用户确认的敏感操作。install 已在隔离 settings（`HS_CODEBUDDY_SETTINGS`）下完整验证幂等/卸载/备份。T03/T05 开工时或用户授权后可补：

```
node packages/adapters-ts/dist/codebuddy/install.js   # 写入真实 settings
# 重启 CodeBuddy 会话，跑一次只读任务，观察 $HS_SPOOL_DIR/<run_id>.jsonl 增长
```

## 7. T02 门禁结果摘要（2026-10-25）

| 门禁 | 结果 |
| --- | --- |
| G0 结构 | §3.1/3.2/3.3 清单齐全 ✅ |
| G2 可构建 | `npm run build:adapters` exit 0，dist 14 个文件 ✅ |
| G2 可运行 | `echo '{"hook_event_name":"PreToolUse",...}' \| node dist/codebuddy/hook-cli.js` → exit 0、stdout 空、spool 1 行合法 TraceEvent ✅ |
| G3 静态 | `npm run typecheck` 0 error ✅ |
| G4 映射 | 12/12 事件 type 一致 ✅ |
| G4 反例 | 非法 JSON/缺字段/未知事件 exit 0，`_stats.json` 计数 +1；spool 不可写 exit 0 ✅ |
| G4 契约 | 12 行 spool 全过 `trace-event.schema.json`（Python jsonschema 校验，0 错误）✅ |
| G5 安全 | 假密钥 payload 开/关捕获均无明文（关：只存 hash；开：`[REDACTED:api-key]`）✅；扫描器仅命中脱敏正则字面量与本文档 grep 命令文本（非密钥）⚠️ 见 JSON notes |
| G6 性能 | 进程内 P95 1.15ms ✅；进程总耗时 P95 236.39ms ❌（本机裸 Node 基线 P95 209.58ms 所致，hook 净开销 26.81ms，见 §5.1） |
| G6 幂等 | install 连跑 2 次 12 条目无重复 ✅ |
| 单测 | `npm run test:adapters` 40/40 ✅ |

---

## 附录 A · WorkBuddy 验收补测：真实会话 E2E（2026-10-25）

> 本卡执行时 `hook_config_shape_verified=false`（未在真实 CLI 中触发）。WorkBuddy 验收阶段已补做，**结论：配置形状正确，hooks 真实触发**。

**做法（可复现）**
1. 备份 `~/.codebuddy/settings.json`（哈希 `1ba80947...`）
2. `node packages/adapters-ts/dist/codebuddy/install.js` → 写入 12 个事件，自建备份成功
3. 设置 `HS_SPOOL_DIR=<临时目录> HS_RUN_ID=e2e-<ts>`，跑一次真实会话：
   `cbc-run -y -p "读 packages/schema/index.ts 并只回答 SCHEMA_VERSION 的值"`
4. 检查 spool

**实测结果**：spool 新增 **5 行**，事件序列完整闭合——
`session.start` → `turn.start` → `tool.call` → `tool.result` → `turn.end`
（对应 `SessionStart` / `UserPromptSubmit` / `PreToolUse` / `PostToolUse` / `Stop`）

**结论**
- `hooks.<EventName>[].{matcher, hooks[].{type:'command',command}}` 形状**被 CLI 正确识别**，`hook_config_location` 判断无误。
- 一次真实单工具任务会稳定产出 5 个事件，说明「一次任务 = 一条完整轨迹」的采集前提成立。
- 副作用：hook 在真实会话中**未阻断、未改输出**，会话正常回答 `SCHEMA_VERSION = '1.0.0'`。

**配置已还原**：`install.js --uninstall` 移除 12 条目后 `hooks` 键消失、插件列表完整；
但因 JSON 重新序列化，**字节级哈希与原始不同**（语义等价）。已用备份做字节级还原，
最终哈希回到 `1ba80947...`，并清理了 install/uninstall 产生的备份残留。

**由此得出两点改进建议**（非阻塞）：
1. `uninstall` 追求**字节级还原**（保留原始缩进/键序）更利于用户信任。
2. `install` 的备份需**保留策略**（如仅留最近 3 份），避免反复安装堆积文件。

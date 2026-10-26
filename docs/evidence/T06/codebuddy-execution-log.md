# CodeBuddy 执行日志与派单记录（自动生成）

生成时间：2026-10-26 15:34:33　|　台账条目：**11** 条（筛选 `--prefix *`）

> 由 `scripts/gen-exec-log.py` 从 `runs/dispatch-ledger.jsonl` 与会话库汇总，路径已脱敏。
> 原始台账与原始会话不入库（`runs/` 被 gitignore、会话在用户目录），本文件用于长期留档。
> 每行的「会话」列来自 CodeBuddy 自己写的 `~/.codebuddy/projects/<slug>/<sessionId>.jsonl`，
> 「命中」= 该文件里能找到本次派单提示词的原文指纹——这是「任务确实交给了 CodeBuddy」的硬证据。


## 1. 派单总表

| 任务 | 派单时间 | 耗时 | 退出码 | CodeBuddy 会话 | 提示词命中 | 变更文件 | 状态 |
|---|---|---|---|---|---|---|---|
| **LEDGER-SMOKE** | 2026-10-25 13:49 | 80s | 0 | `01a17352…`<br>3 行 / 2 KB | 是 | 1 个 | ✅ |
| **T04a** | 2026-10-25 13:51 | 1699s | 0 | `01a17354…`<br>149 行 / 436 KB | 是 | 1 个 | ✅ |
| **T04b** | 2026-10-25 14:29 | 770s | 0 | `01a17377…`<br>91 行 / 417 KB | 是 | 3 个 | ✅ |
| **T04c** | 2026-10-25 14:48 | 1798s | 0 | `01a17388…`<br>301 行 / 896 KB | 是 | 2 个 | ✅ |
| **T05a** | 2026-10-25 15:26 | 2094s | 0 | `01a173ac…`<br>392 行 / 1253 KB | 是 | 6 个 | ✅ |
| **T05a-R1** | 2026-10-25 16:13 | 1146s | 0 | `01a173d6…`<br>315 行 / 988 KB | 是 | 7 个 | ✅ |
| **T05b** | 2026-10-26 02:43 | 2120s | 0 | `01a17617…`<br>287 行 / 999 KB | 是 | 5 个 | ✅ |
| **T06** | 2026-10-26 05:28 | 1244s | 0 | `01a176ae…`<br>529 行 / 1755 KB | 是 | 10 个 | ✅ |
| **T06-R1** | 2026-10-26 06:41 | 968s | 0 | `01a176f1…`<br>491 行 / 1506 KB | 是 | 12 个 | ✅ |
| **T06-R2** | 2026-10-26 07:12 | 837s | 0 | `01a1770d…`<br>165 行 / 585 KB | 是 | 23 个 | ✅ |
| **T06-R2** | 2026-10-26 07:00 | — | — | `01a17702…`<br>93 行 / 443 KB | 是 | 24 个 | 🔁 补录 |

## 2. 逐任务明细

### LEDGER-SMOKE

- **runTag**：`LEDGER-SMOKE-2026-10-25T13-49-01-370Z`
- **时间**：2026-10-25T13:49:01.370Z → 2026-10-25T13:50:21.142Z（80s）
- **退出码**：0　**中断信号**：无　**执行器**：`cbc-run.js`（headless，无需人工点确认）
- **提示词**：8 字符，sha256 `4bab2fdd390e202ea8030d5ab3a60f64…`
  - 文件：`<HOME>\WorkBuddy\项目1012\harness_scope\runs\LEDGER-SMOKE-2026-10-25T13-49-01-370Z\prompt.txt`
- **CodeBuddy 会话**：`01a17352-726e-76da-820f-01051d8c3312`　原文命中：**是**
  - 会话记录：`<HOME>\.codebuddy\projects\c-Users-<user>-WorkBuddy-项目1012-harness_scope\01a17352-726e-76da-820f-01051d8c3312.jsonl`（2 KB）
  - 记录行数：**3**　时间跨度：0.9 分钟（21:49:05 → 21:49:59）
  - 类型分布：{'message': 2, 'file-history-snapshot': 1}
  - 工具调用：0 次（）
- **trace 文件（span 树）**：1 个
  - `<HOME>\.codebuddy\traces\41308\trace_fbee74a8db4748ddac995e50e8f45c84.jsonl`（24 KB，mtime 2026-10-25T13:50:07.419Z）
- **仓库**：HEAD `8bb07c58` → `8bb07c58`（无变动）
- **变更文件（1）**：
  - `?? scripts/dispatch.mjs`
- **判决**：`dispatchedToCodeBuddy=True`、`producedChanges=True`、`succeeded=True`

### T04a

- **runTag**：`T04a-2026-10-25T13-51-24-934Z`
- **时间**：2026-10-25T13:51:24.934Z → 2026-10-25T14:19:43.967Z（1699s）
- **退出码**：0　**中断信号**：无　**执行器**：`cbc-run.js`（headless，无需人工点确认）
- **提示词**：475 字符，sha256 `c4d09b455592f5d2d1c974bd0f5a0054…`
  - 文件：`<HOME>\WorkBuddy\项目1012\harness_scope\runs\T04a-2026-10-25T13-51-24-934Z\prompt.txt`
- **CodeBuddy 会话**：`01a17354-a3b8-7aec-8e49-5100b8908430`　原文命中：**是**
  - 会话记录：`<HOME>\.codebuddy\projects\c-Users-<user>-WorkBuddy-项目1012-harness_scope\01a17354-a3b8-7aec-8e49-5100b8908430.jsonl`（436 KB）
  - 记录行数：**149**　时间跨度：28.2 分钟（21:51:29 → 22:19:40）
  - 类型分布：{'message': 12, 'file-history-snapshot': 7, 'reasoning': 32, 'function_call': 49, 'function_call_result': 49}
  - 工具调用：49 次（Bash×35, Write×5, Read×4, Glob×2, Grep×1, PowerShell×1, Edit×1）
- **trace 文件（span 树）**：1 个
  - `<HOME>\.codebuddy\traces\9360\trace_534244effcc147828c2571082d50e22a.jsonl`（525 KB，mtime 2026-10-25T14:10:17.315Z）
- **仓库**：HEAD `0d549941` → `d2c8b27d`（有变动）
- **变更文件（1）**：
  - `?? docs/dsh-compat.md`
- **判决**：`dispatchedToCodeBuddy=True`、`producedChanges=True`、`succeeded=True`

### T04b

- **runTag**：`T04b-2026-10-25T14-29-50-860Z`
- **时间**：2026-10-25T14:29:50.860Z → 2026-10-25T14:42:41.206Z（770s）
- **退出码**：0　**中断信号**：无　**执行器**：`cbc-run.js`（headless，无需人工点确认）
- **提示词**：571 字符，sha256 `e13291bad2e4e987feec7b8b6ea1c4ba…`
  - 文件：`<HOME>\WorkBuddy\项目1012\harness_scope\runs\T04b-2026-10-25T14-29-50-860Z\prompt.txt`
- **CodeBuddy 会话**：`01a17377-d40e-7b7d-a11e-0fdebba55bf5`　原文命中：**是**
  - 会话记录：`<HOME>\.codebuddy\projects\c-Users-<user>-WorkBuddy-项目1012-harness_scope\01a17377-d40e-7b7d-a11e-0fdebba55bf5.jsonl`（417 KB）
  - 记录行数：**91**　时间跨度：12.7 分钟（22:29:55 → 22:42:38）
  - 类型分布：{'message': 11, 'file-history-snapshot': 11, 'reasoning': 15, 'function_call': 27, 'function_call_result': 27}
  - 工具调用：27 次（Bash×10, Write×9, Read×7, Edit×1）
- **trace 文件（span 树）**：1 个
  - `<HOME>\.codebuddy\traces\16276\trace_62ad360077124a92a6998e7201287df5.jsonl`（933 KB，mtime 2026-10-25T14:42:38.272Z）
- **仓库**：HEAD `053215cb` → `3d462d43`（有变动）
- **变更文件（3）**：
  - `M docs/dsh-compat.md`
  - `?? docs/dsh-bridge-contract.md`
  - `?? docs/dsh-version-gate.md`
- **判决**：`dispatchedToCodeBuddy=True`、`producedChanges=True`、`succeeded=True`

### T04c

- **runTag**：`T04c-2026-10-25T14-48-26-884Z`
- **时间**：2026-10-25T14:48:26.884Z → 2026-10-25T15:18:25.156Z（1798s）
- **退出码**：0　**中断信号**：无　**执行器**：`cbc-run.js`（headless，无需人工点确认）
- **提示词**：744 字符，sha256 `cc70bf543a791c4ff85ff516d96a97d2…`
  - 文件：`<HOME>\WorkBuddy\项目1012\harness_scope\runs\T04c-2026-10-25T14-48-26-884Z\prompt.txt`
- **CodeBuddy 会话**：`01a17388-dc08-767a-b0e0-59a07a6c0204`　原文命中：**是**
  - 会话记录：`<HOME>\.codebuddy\projects\c-Users-<user>-WorkBuddy-项目1012-harness_scope\01a17388-dc08-767a-b0e0-59a07a6c0204.jsonl`（896 KB）
  - 记录行数：**301**　时间跨度：29.8 分钟（22:48:31 → 23:18:22）
  - 类型分布：{'message': 28, 'file-history-snapshot': 20, 'reasoning': 49, 'function_call': 102, 'function_call_result': 102}
  - 工具调用：102 次（Bash×69, Write×15, Read×7, Edit×4, TaskCreate×3, TaskUpdate×3, MultiEdit×1）
- **trace 文件（span 树）**：1 个
  - `<HOME>\.codebuddy\traces\12352\trace_c749a0513107490eb4d3c9babd04d05a.jsonl`（2579 KB，mtime 2026-10-25T15:18:22.296Z）
- **仓库**：HEAD `3d462d43` → `07fd37ad`（有变动）
- **变更文件（2）**：
  - `M docs/dsh-bridge-contract.md`
  - ` M docs/dsh-compat.md`
- **判决**：`dispatchedToCodeBuddy=True`、`producedChanges=True`、`succeeded=True`

### T05a

- **runTag**：`T05a-2026-10-25T15-26-56-527Z`
- **时间**：2026-10-25T15:26:56.527Z → 2026-10-25T16:01:50.946Z（2094s）
- **退出码**：0　**中断信号**：无　**执行器**：`cbc-run.js`（headless，无需人工点确认）
- **提示词**：396 字符，sha256 `a558676b6b9e747851ff039e49df24ee…`
  - 文件：`<HOME>\WorkBuddy\项目1012\harness_scope\runs\T05a-2026-10-25T15-26-56-527Z\prompt.txt`
- **CodeBuddy 会话**：`01a173ac-18ba-7d54-86b9-2632482fde7a`　原文命中：**是**
  - 会话记录：`<HOME>\.codebuddy\projects\c-Users-<user>-WorkBuddy-项目1012-harness_scope\01a173ac-18ba-7d54-86b9-2632482fde7a.jsonl`（1253 KB）
  - 记录行数：**392**　时间跨度：34.8 分钟（23:27:00 → 00:01:47）
  - 类型分布：{'message': 16, 'file-history-snapshot': 23, 'function_call': 141, 'function_call_result': 141, 'reasoning': 71}
  - 工具调用：141 次（Bash×93, Edit×13, Read×10, TaskUpdate×10, Write×7, TaskCreate×5, MultiEdit×3）
- **trace 文件（span 树）**：3 个
  - `<HOME>\.codebuddy\traces\34432\trace_7224546854f64de8b9302a9152728823.jsonl`（3911 KB，mtime 2026-10-25T16:01:47.919Z）
  - `<HOME>\.codebuddy\traces\40116\trace_7be731e45d134a62bbbfa08ac99ff6ca.jsonl`（49 KB，mtime 2026-10-25T15:40:23.455Z）
  - `<HOME>\.codebuddy\traces\37172\trace_7d9cc0b2ed0940e3a415fef08b662fd2.jsonl`（49 KB，mtime 2026-10-25T15:34:42.777Z）
- **仓库**：HEAD `53d772ec` → `53d772ec`（无变动）
- **变更文件（6）**：
  - `M packages/adapters-ts/src/normalize.ts`
  - ` M packages/schema/trace-event.schema.json`
  - `?? docs/event-mapping-matrix.md`
  - `?? docs/seq-assignment.md`
  - `?? engine/hs_core/collector.py`
  - `?? engine/tests/test_collector.py`
- **判决**：`dispatchedToCodeBuddy=True`、`producedChanges=True`、`succeeded=True`

### T05a-R1

- **runTag**：`T05a-R1-2026-10-25T16-13-02-636Z`
- **时间**：2026-10-25T16:13:02.636Z → 2026-10-25T16:32:08.997Z（1146s）
- **退出码**：0　**中断信号**：无　**执行器**：`cbc-run.js`（headless，无需人工点确认）
- **提示词**：1116 字符，sha256 `fc4bb1d146d9a5fed3ca9a9406250a0a…`
  - 文件：`<HOME>\WorkBuddy\项目1012\harness_scope\runs\T05a-R1-2026-10-25T16-13-02-636Z\prompt.txt`
- **CodeBuddy 会话**：`01a173d6-5002-70f6-b06c-f461381884ac`　原文命中：**是**
  - 会话记录：`<HOME>\.codebuddy\projects\c-Users-<user>-WorkBuddy-项目1012-harness_scope\01a173d6-5002-70f6-b06c-f461381884ac.jsonl`（988 KB）
  - 记录行数：**315**　时间跨度：18.9 分钟（00:13:08 → 00:32:03）
  - 类型分布：{'message': 26, 'file-history-snapshot': 39, 'reasoning': 46, 'function_call': 102, 'function_call_result': 102}
  - 工具调用：102 次（Bash×32, Edit×24, Read×15, MultiEdit×15, TaskUpdate×7, TaskCreate×4, Write×2, Grep×2）
- **trace 文件（span 树）**：1 个
  - `<HOME>\.codebuddy\traces\13740\trace_ccc39fbd2f94414cb3067d883df93d2b.jsonl`（2712 KB，mtime 2026-10-25T16:32:03.348Z）
- **仓库**：HEAD `53d772ec` → `53d772ec`（无变动）
- **变更文件（7）**：
  - `M packages/adapters-ts/src/normalize.ts`
  - ` M packages/schema/trace-event.schema.json`
  - `?? docs/channel-priority.md`
  - `?? docs/event-mapping-matrix.md`
  - `?? docs/seq-assignment.md`
  - `?? engine/hs_core/collector.py`
  - `?? engine/tests/test_collector.py`
- **判决**：`dispatchedToCodeBuddy=True`、`producedChanges=True`、`succeeded=True`

### T05b

- **runTag**：`T05b-2026-10-26T02-43-41-064Z`
- **时间**：2026-10-26T02:43:41.064Z → 2026-10-26T03:19:01.166Z（2120s）
- **退出码**：0　**中断信号**：无　**执行器**：`cbc-run.js`（headless，无需人工点确认）
- **提示词**：1341 字符，sha256 `db1dfb1e7ecb2c26193e5ea029cc0c1e…`
  - 文件：`<HOME>\WorkBuddy\项目1012\harness_scope\runs\T05b-2026-10-26T02-43-41-064Z\prompt.txt`
- **CodeBuddy 会话**：`01a17617-a831-77b0-a0f1-5bc02f3e8fb4`　原文命中：**是**
  - 会话记录：`<HOME>\.codebuddy\projects\c-Users-<user>-WorkBuddy-项目1012-harness_scope\01a17617-a831-77b0-a0f1-5bc02f3e8fb4.jsonl`（999 KB）
  - 记录行数：**287**　时间跨度：35.3 分钟（10:43:44 → 11:19:00）
  - 类型分布：{'message': 26, 'file-history-snapshot': 24, 'reasoning': 49, 'function_call': 94, 'function_call_result': 94}
  - 工具调用：94 次（Bash×44, Read×12, Write×9, MultiEdit×9, TaskUpdate×8, Edit×8, TaskCreate×4）
- **trace 文件（span 树）**：1 个
  - `<HOME>\.codebuddy\traces\12880\trace_c5431a10d9424346943f6022b9f20393.jsonl`（2733 KB，mtime 2026-10-26T03:19:00.287Z）
- **仓库**：HEAD `f25944cb` → `f25944cb`（无变动）
- **变更文件（5）**：
  - `M engine/hs_core/cli.py`
  - `?? docs/evidence/T05b/`
  - `?? docs/storage.md`
  - `?? engine/hs_core/store.py`
  - `?? engine/tests/test_store.py`
- **判决**：`dispatchedToCodeBuddy=True`、`producedChanges=True`、`succeeded=True`

### T06

- **runTag**：`T06-2026-10-26T05-28-30-106Z`
- **时间**：2026-10-26T05:28:30.106Z → 2026-10-26T05:49:14.137Z（1244s）
- **退出码**：0　**中断信号**：无　**执行器**：`cbc-run.js`（headless，无需人工点确认）
- **提示词**：4409 字符，sha256 `4d66db2fa6e94b833df266a180cd4ea9…`
  - 文件：`<HOME>\WorkBuddy\项目1012\harness_scope\runs\T06-2026-10-26T05-28-30-106Z\prompt.txt`
- **CodeBuddy 会话**：`01a176ae-9002-7fc8-9356-3755b280623c`　原文命中：**是**
  - 会话记录：`<HOME>\.codebuddy\projects\c-Users-<user>-WorkBuddy-项目1012-harness_scope\01a176ae-9002-7fc8-9356-3755b280623c.jsonl`（1755 KB）
  - 记录行数：**529**　时间跨度：20.6 分钟（13:28:35 → 13:49:13）
  - 类型分布：{'message': 21, 'file-history-snapshot': 62, 'function_call': 182, 'function_call_result': 182, 'reasoning': 82}
  - 工具调用：182 次（Bash×86, Edit×53, Read×14, TaskUpdate×14, Write×8, TaskCreate×7）
- **trace 文件（span 树）**：1 个
  - `<HOME>\.codebuddy\traces\29052\trace_603d226b105a4ac4b240b15da0e3b6f3.jsonl`（5661 KB，mtime 2026-10-26T05:49:13.432Z）
- **仓库**：HEAD `bfe43ad5` → `bfe43ad5`（无变动）
- **变更文件（10）**：
  - `M .gitignore`
  - ` M engine/hs_core/cli.py`
  - `?? docs/evidence/T06/`
  - `?? docs/metrics-engine.md`
  - `?? engine/config/`
  - `?? engine/hs_core/metrics.py`
  - `?? engine/hs_core/metrics_core.py`
  - `?? engine/tests/test_metrics.py`
  - `?? engine/tests/test_metrics_core.py`
  - `?? scripts/t06_selfcheck.py`
- **判决**：`dispatchedToCodeBuddy=True`、`producedChanges=True`、`succeeded=True`

### T06-R1

- **runTag**：`T06-R1-2026-10-26T06-41-48-288Z`
- **时间**：2026-10-26T06:41:48.288Z → 2026-10-26T06:57:56.558Z（968s）
- **退出码**：0　**中断信号**：无　**执行器**：`cbc-run.js`（headless，无需人工点确认）
- **提示词**：3658 字符，sha256 `9c90a2bf3b09f0528816201b2b04caa6…`
  - 文件：`<HOME>\WorkBuddy\项目1012\harness_scope\runs\T06-R1-2026-10-26T06-41-48-288Z\prompt.txt`
- **CodeBuddy 会话**：`01a176f1-aa32-7bcb-a0d6-3c0815010167`　原文命中：**是**
  - 会话记录：`<HOME>\.codebuddy\projects\c-Users-<user>-WorkBuddy-项目1012-harness_scope\01a176f1-aa32-7bcb-a0d6-3c0815010167.jsonl`（1506 KB）
  - 记录行数：**491**　时间跨度：16.1 分钟（14:41:52 → 14:57:55）
  - 类型分布：{'message': 8, 'file-history-snapshot': 74, 'function_call': 156, 'function_call_result': 156, 'reasoning': 97}
  - 工具调用：156 次（Edit×75, Bash×54, Read×26, Write×1）
- **trace 文件（span 树）**：1 个
  - `<HOME>\.codebuddy\traces\31992\trace_da733ca0126e4b53b25e690f74fd0e98.jsonl`（4735 KB，mtime 2026-10-26T06:57:55.866Z）
- **仓库**：HEAD `bfe43ad5` → `bfe43ad5`（无变动）
- **变更文件（12）**：
  - `M .gitignore`
  - ` M engine/hs_core/cli.py`
  - `?? .tmp-qa/`
  - `?? docs/evidence/T06/`
  - `?? docs/metrics-engine.md`
  - `?? engine/config/`
  - `?? engine/hs_core/metrics.py`
  - `?? engine/hs_core/metrics_core.py`
  - `?? engine/tests/test_metrics.py`
  - `?? engine/tests/test_metrics_core.py`
  - `?? scripts/t06_r1_check.py`
  - `?? scripts/t06_selfcheck.py`
- **判决**：`dispatchedToCodeBuddy=True`、`producedChanges=True`、`succeeded=True`

### T06-R2

- **runTag**：`T06-R2-2026-10-26T07-12-15-098Z`
- **时间**：2026-10-26T07:12:15.098Z → 2026-10-26T07:26:11.684Z（837s）
- **退出码**：0　**中断信号**：无　**执行器**：`cbc-run.js`（headless，无需人工点确认）
- **提示词**：4304 字符，sha256 `8df0500f70da59900eb6827ecf029ce8…`
  - 文件：`<HOME>\WorkBuddy\项目1012\harness_scope\runs\T06-R2-2026-10-26T07-12-15-098Z\prompt.txt`
- **CodeBuddy 会话**：`01a1770d-8a10-794f-b7ee-3a6e9ef6e18e`　原文命中：**是**
  - 会话记录：`<HOME>\.codebuddy\projects\c-Users-<user>-WorkBuddy-项目1012-harness_scope\01a1770d-8a10-794f-b7ee-3a6e9ef6e18e.jsonl`（585 KB）
  - 记录行数：**165**　时间跨度：13.9 分钟（15:12:19 → 15:26:11）
  - 类型分布：{'message': 15, 'file-history-snapshot': 13, 'reasoning': 29, 'function_call': 54, 'function_call_result': 54}
  - 工具调用：54 次（Bash×23, Read×8, Edit×7, TaskUpdate×6, MultiEdit×4, TaskCreate×3, Grep×2, Write×1）
- **trace 文件（span 树）**：1 个
  - `<HOME>\.codebuddy\traces\16120\trace_6993d004a03f4a3ea9d675752c1c462d.jsonl`（1405 KB，mtime 2026-10-26T07:26:11.109Z）
- **仓库**：HEAD `bfe43ad5` → `bfe43ad5`（无变动）
- **变更文件（23）**：
  - `M .gitignore`
  - ` M engine/hs_core/cli.py`
  - `?? .gen-log.py`
  - `?? .tmp-pytest-r1/`
  - `?? .tmp-pytest-r2-a/`
  - `?? .tmp-qa/`
  - `?? .tmp-r2-final-1792999466/`
  - `?? .tmp-r2-pytest-0cc6c8df/`
  - `?? .tmp-r2-pytest-a35a4c66/`
  - `?? .tmp-r2-pytest-dd9a4129/`
  - `?? .tmp-r2-pytest-e5543039/`
  - `?? .tmp-r2-pytest-f42798fc/`
  - `?? .tmp-r2-run1/`
  - `?? docs/evidence/T06/`
  - `?? docs/metrics-engine.md`
  - `?? engine/config/`
  - `?? engine/hs_core/metrics.py`
  - `?? engine/hs_core/metrics_core.py`
  - `?? engine/tests/test_metrics.py`
  - `?? engine/tests/test_metrics_core.py`
  - `?? scripts/t06_r1_check.py`
  - `?? scripts/t06_r2_check.py`
  - `?? scripts/t06_selfcheck.py`
- **判决**：`dispatchedToCodeBuddy=True`、`producedChanges=True`、`succeeded=True`

### T06-R2

- **runTag**：`T06-R2-2026-10-26T07-00-34-632Z`
- **⚠️ 补录记录**：包装进程被前台超时 SIGTERM 掐断，post-flight 未执行；由 --backfill 重建，会话侧证据齐全
  - 补录时间：2026-10-26T07:26:48.932Z
  - `exitCode` / `durationSec` 为 `null`：随包装进程一起丢失，**不推测**
- **提示词**：4304 字符，sha256 `8df0500f70da59900eb6827ecf029ce8…`
  - 文件：`<HOME>\WorkBuddy\项目1012\harness_scope\runs\T06-R2-2026-10-26T07-00-34-632Z\prompt.txt`
- **CodeBuddy 会话**：`01a17702-dac6-791b-b095-19e8d6623822`　原文命中：**是**
  - 会话记录：`<HOME>\.codebuddy\projects\c-Users-<user>-WorkBuddy-项目1012-harness_scope\01a17702-dac6-791b-b095-19e8d6623822.jsonl`（443 KB）
  - 记录行数：**93**　时间跨度：9.5 分钟（15:00:38 → 15:10:11）
  - 类型分布：{'message': 8, 'file-history-snapshot': 11, 'reasoning': 14, 'function_call': 30, 'function_call_result': 30}
  - 工具调用：30 次（Edit×10, Read×7, TaskUpdate×5, TaskCreate×4, Bash×3, Grep×1）
- **trace 文件（span 树）**：2 个
  - `<HOME>\.codebuddy\traces\16120\trace_6993d004a03f4a3ea9d675752c1c462d.jsonl`（1405 KB，mtime 2026-10-26T07:26:11.109Z）
  - `<HOME>\.codebuddy\traces\30236\trace_50cc8463f25e4bc59f531591f230226c.jsonl`（924 KB，mtime 2026-10-26T07:10:11.404Z）
- **仓库（补录时为事后快照）**：HEAD `bfe43ad5`
- **变更文件（24）**：
  - `M .gitignore`
  - ` M engine/hs_core/cli.py`
  - ` M scripts/dispatch.mjs`
  - `?? .gen-log.py`
  - `?? .tmp-pytest-r1/`
  - `?? .tmp-pytest-r2-a/`
  - `?? .tmp-qa/`
  - `?? .tmp-r2-final-1792999466/`
  - `?? .tmp-r2-pytest-0cc6c8df/`
  - `?? .tmp-r2-pytest-a35a4c66/`
  - `?? .tmp-r2-pytest-dd9a4129/`
  - `?? .tmp-r2-pytest-e5543039/`
  - `?? .tmp-r2-pytest-f42798fc/`
  - `?? .tmp-r2-run1/`
  - `?? docs/evidence/T06/`
  - `?? docs/metrics-engine.md`
  - `?? engine/config/`
  - `?? engine/hs_core/metrics.py`
  - `?? engine/hs_core/metrics_core.py`
  - `?? engine/tests/test_metrics.py`
  - `?? engine/tests/test_metrics_core.py`
  - `?? scripts/t06_r1_check.py`
  - `?? scripts/t06_r2_check.py`
  - `?? scripts/t06_selfcheck.py`
- **判决**：`dispatchedToCodeBuddy=True`、`producedChanges=None`、`succeeded=None`


## 3. 台账完整性自检

- 条目总数：**11**
- 补录条目：**1**（T06-R2 @ 0-34-632Z）
- 带中断标记：**1**（T06-R2）
- 提示词原文未命中的条目：**0**

> 补录条目意味着「包装进程被信号掐断、post-flight 没跑」。现已修：`dispatch.mjs` 收到 SIGTERM/SIGINT/SIGHUP 会先杀子进程再走完取证落盘（记录带 `interrupted`）。


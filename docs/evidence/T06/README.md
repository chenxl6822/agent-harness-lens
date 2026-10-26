# docs/evidence/T06 — 指标引擎门禁证据（可脚本复跑）

> 口径与公式登记见 `docs/metrics-engine.md`；结构化结论见 `probe/t06/result.json`、
> `probe/t06/R1-result.json` 与 `probe/t06/R2-result.json`（`probe/` 被 gitignore，需要长期引用的结论已同时落到本目录与
> `docs/metrics-engine.md`）。
> 所有命令均在**仓库根**执行，pytest 一律带 `--basetemp=<全新目录>`（本机默认 tmp 在 D: 盘会被拒；
> 复用目录会误报 PermissionError）。

## 文件说明

| 文件 | 内容 | 复跑命令 |
|---|---|---|
| `selfcheck.json.txt` | G1/G2/G3/G5/G6/G10/G11/G13/G15 + E5-P 探针的**一次跑完**结构化报告（每条带 `ok` 与实测数字） | `.venv/Scripts/python.exe scripts/t06_selfcheck.py --db runs/t06/t06.db --run-id t06-codebuddy-primary --out .tmp-t06` |
| `R1-result.json.txt` | **T06-R1 返工验收**：`naming_check` / `key_count_check` / `drilldown` / `confidence_audit` / `signature_audit` + schema/隐私/schema-diff/单测四门禁 + 裁定与 open_questions | `.venv/Scripts/python.exe scripts/t06_r1_check.py --metrics runs/t06/metrics --db runs/t06/t06.db --run-id t06-codebuddy-primary --out probe/t06/R1-result.json` |
| `R2-result.json.txt` | **T06-R2 返工验收**：来源等级版 `confidence_audit`（retry_rate/redundant_loop → low，签名来源=代理由库内事件正向复算）/ `redundant_loop_check`（==1 且 ≤50）/ `signature_source`（统一措辞三处逐字自动比对）+ 四门禁 | `.venv/Scripts/python.exe scripts/t06_r2_check.py --metrics runs/t06/metrics --db runs/t06/t06.db --run-id t06-codebuddy-primary --out probe/t06/R2-result.json` |
| `R2-pytest.txt` | R2 收口后的单测原始输出（87 passed，含 R2 新增 3 条） | `.venv/Scripts/python.exe -m pytest engine/tests/test_metrics.py engine/tests/test_metrics_core.py -q --basetemp=<全新目录>` |
| `pytest.txt` | 指标两文件单测 + 全套单测原始输出（84 passed / 118 passed） | 见文件内每段 `$` 命令行 |
| `cli.txt` | `hs metrics list` / `hs metrics run --pretty` / `hs schema check` / `git status packages/schema` 原样输出 | 见文件内每段 `$` 命令行 |
| `ext_probe.py` | G1 正向探针：第三方模块注册 `process.probe_metric` | `PYTHONPATH=docs/evidence/T06 .venv/Scripts/python.exe -m hs_core.cli metrics run --run-id <run> --db <db> --extra-module ext_probe --out-dir .tmp-probe` |
| `ext_probe_dup.py` | G1 反向探针：抢注内置 `process.step_count` → `MetricRegistryError`（不静默覆盖） | 同上，`--extra-module ext_probe_dup`（预期非零退出 + 异常） |
| `selfcheck.stderr.txt` | 自检脚本 stderr（应为空） | — |
| `qa/` | QA 独立复核脚本与其当时的原始输出（**历史快照**，含修复前的 FAIL 记录，只作留痕不改写） | 见各 `.py` |
| `lead_verify.py` | **验收方（主理人）自写的独立复核**：四形态置信度反事实 + `redundant_loop` 签名塌缩反事实（119 > 50）+ 真实 run 硬不变量 + 三处措辞。与执行方的 `scripts/t06_r2_check.py` **无共用代码** | `env -u NODE_OPTIONS PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe docs/evidence/T06/lead_verify.py` |
| `lead-independent-verify.json` | 上一脚本的结构化输出（`verdict.all_ok=true`） | 同上（脚本自行落盘，不依赖 stdout 重定向） |
| `codebuddy-execution-log.md` | **执行日志与派单记录**：11 条台账汇总成人类可读表 + 每轮的提示词 sha256 / 会话行数 / 工具调用 / 变更文件 + 台账完整性自检。路径已脱敏 | `.venv/Scripts/python.exe scripts/gen-exec-log.py --out docs/evidence/T06/codebuddy-execution-log.md` |

## 门禁 → 证据索引

| 门禁 | 证据位置 |
|---|---|
| G1 注册器可插拔 | `selfcheck.json.txt` → `G1_registry`（`extra_key_visible=true`、`duplicate_key_error=true`、`cli_extra_module_exit=0`）+ 两个探针文件 |
| G2 schema 合规 | `selfcheck.json.txt` → `G2_schema`；`cli.txt` 末段 `OK metric-set` / `git status` 为空 |
| G3 策略门禁 | `selfcheck.json.txt` → `G3_policy_gate`（merge run 全 null、`blocked`、`cli_exit=2`） |
| G4 口径正确 | `pytest.txt`；用例内逐条写出人工期望值（`test_metrics_core.py`） |
| G5 可下钻 | `selfcheck.json.txt` → `G5_drilldown`（117/117 event_id 命库） |
| G6 可复现 | `selfcheck.json.txt` → `G6_reproducibility`（两次相同 + **从文件字节复算一致** + `nan_or_infinity_hits=0`） |
| G7 成本前向兼容 | `pytest.txt`（`test_cost_metrics_present_when_usage_exists` / `..._degrade_on_current_real_shape`） |
| G8 失败隔离 | `pytest.txt`（`test_failing_metric_is_isolated`） |
| G9 阈值参数化 | `pytest.txt`（`test_yaml_overrides_change_results_without_touching_code`） |
| G10 隐私 | `selfcheck.json.txt` → `G10_privacy`（用户名 / `C:\Users\` / `/home/` / `raw_ref` 命中均 0） |
| G11 真实数据 E2E | `selfcheck.json.txt` → `G11_cli` + `G11_real_data`；`cli.txt` |
| G12 单测全绿 | `pytest.txt`（两处 `exit=0`） |
| G13 降级诚实 | `selfcheck.json.txt` → `G13_degraded`（`mismatch={}`、`reasons_outside_enum=[]`） |
| G14 条件二路径 | `pytest.txt`（4 个 `test_tokens_trace_fallback_*` / `test_tokens_in_out_never_use_trace_fallback`） |
| G15 分母恒等式 | `selfcheck.json.txt` → `G15_tool_call_identity`（真实数据只上报：`identity_holds=false` + flag）；golden 硬断言在 `test_metrics_core.py::test_tool_correctness_golden` |
| **R1-1** step_count 口径锁定 | `R1-result.json.txt` → `key_count_check`（step_count 在 computed 里）+ `pytest.txt`（`test_step_count_uses_step_start_events` / `test_step_count_crosscheck_with_pre_step`） |
| **R1-2** key 计数自洽 | `R1-result.json.txt` → `key_count_check`（`total 13 / computed 4 / degraded 9 / consistent true`） |
| **R1-3** 双文件键名 | `R1-result.json.txt` → `naming_check`（ext 无 `degraded_metrics`、主文件无 `degraded_reasons`） |
| **R1-4** 可下钻 | `R1-result.json.txt` → `drilldown`（117/117、`unresolved=[]`） |
| **R1-5** confidence | `R1-result.json.txt` → `confidence_audit`（`valued_without_confidence=[]`、`degraded_with_confidence=[]`、`mismatched_vs_q7=[]`） |
| **R1-6** 数字来自本次运行 | `R1-result.json.txt` + `pytest.txt`（`test_degraded_detail_is_this_run_not_hardcoded_snapshot`）；签名事实在 `signature_audit` |
| **R1-7** loop_min_repeat 生效 | `pytest.txt`（`test_params_change_loop_min_repeat_takes_effect`：2→1 / 3→0 / 3 轮→2） |
| **R2-1** confidence 按来源等级 | `R2-result.json.txt` → `confidence_audit`（step_count high / retry_rate low / redundant_loop low / tool_correctness low / 9 降级 null；`signature_source.source_level=proxy` 由库内事件正向复算）+ `R2-pytest.txt`（`test_native_signature_count_ignores_missing_key` / `test_signature_confidence_rises_to_high_with_real_hashes`） |
| **R2-2** redundant_loop 锁值 | `R2-result.json.txt` → `redundant_loop_check`（value==1、≤50）+ `R2-pytest.txt`（`test_redundant_loop_real_shape_value_locked_and_bounded`） |
| **R2-3** 措辞三处一致 | `R2-result.json.txt` → `signature_source.wording_consistency`（docs §7-1 / metrics_core 代码注释 / result JSON 三处逐字，脚本自动比对） |

## 台账完整性（T06-R2 事故留档）

同一份 R2 提示词被派了两次：`07-00-34-632Z`（会话 `01a17702`，572s，改了 `metrics_core.py` 与
`test_metrics_core.py`）与 `07-12-15-098Z`（会话 `01a1770d`，837s，exit 0）。第一跑的台账**没写成**——
`dispatch.mjs` 当时没有信号处理，前台超时 SIGTERM 掉包装进程后 post-flight 不执行。

已做两处修复（`scripts/dispatch.mjs`）：

1. 收到 `SIGTERM/SIGINT/SIGHUP` → 先杀子进程，**再照常走完取证与落盘**，记录带 `interrupted`；
2. 新增 `--backfill <runTag>`：从 `runs/<tag>/prompt.txt` + 会话库 + git 重建记录，强制
   `backfilled:true` / `exitCode:null` / `verdict.succeeded:null`（退出码确实丢了，不推测），
   并拒绝重复补录；同一提示词命中多个会话时按「首行时间戳最接近派单起始时刻」认跑次。

补录行在 `codebuddy-execution-log.md` 里标为 **🔁 补录**。

## 基线库`runs/t06/t06.db`（`runs/` 被 gitignore）：`hs ingest --run-id t06-codebuddy-primary
--codebuddy-home "$HOME/.codebuddy"` 摄入，`channel_policy=primary_only`，本次 9809 事件。
数字随本机活跃数据增长会变，语义不变；主理人 9098 事件快照的对应值为
`step.start 2376 / tool.call 857 / tool.result 845 / tool.error 12 / llm.response 701 / metric.custom 33`。

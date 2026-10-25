# T01 自检记录（harness_scope）

> 执行日期：2026-10-25 ｜ 任务卡：T01-契约与骨架 + T01-派单补充说明
> 环境：Node v22.22.2 / npm 10.9.7 / uv 0.13.0 / Python 3.13.14（WorkBuddy 托管版）
> 本机无 `make`，以下所有 make 命令以 `mingw32-make`（GNU make for Windows，PATH 已有）执行，语法完全兼容。

## G0 结构

```
$ ls packages/schema engine/hs_core
packages/schema:
  trace-event.schema.json  metric-set.schema.json  taskset.schema.json  capability.schema.json
  index.ts  examples/{trace-event.sample.json, metric-set.sample.json, taskset.sample.yaml,
                     capability.sample.json}  generated/ts/{4 个 .d.ts}  .gen-hash.json
engine/hs_core:
  __init__.py  config.py  schema.py  cli.py  models.py(AUTO-GENERATED)
```

根：`package.json`（npm workspaces）/ `tsconfig.base.json` / `.npmrc` / `Makefile` / `.gitignore` / `.env.example` / `AGENTS.md` ✅
额外基础设施（任务卡未列但为闭环所必需）：`tsconfig.json`（root，extends base）、`scripts/{gen_ts.mjs, gen_python_models.py, gen_hash.py}`、`engine/tests/test_examples.py`、`engine/pyproject.toml`、`docs/`。
`probe/` 未做任何改动。

## G2 可运行

```
$ .venv/Scripts/hs.exe version
hs (harness-scope-engine) 0.1.0
schema_version 1.0.0          → exit 0 ✅

$ .venv/Scripts/hs.exe schema list
capability  https://harness-scope.dev/schema/capability.json
metric-set  https://harness-scope.dev/schema/metric-set.json
taskset     https://harness-scope.dev/schema/taskset.json
trace-event https://harness-scope.dev/schema/trace-event.json   → exit 0 ✅

$ hs schema check packages/schema/examples/trace-event.sample.json --kind trace-event
OK trace-event: packages\schema\examples\trace-event.sample.json   → exit 0 ✅
```

## G3 静态

```
$ mingw32-make lint
ruff check engine → All checks passed!   ✅ 0 告警

$ mingw32-make typecheck
npx --no-install tsc -p tsconfig.json    → exit 0，strict 全过 ✅
```

## G4 数据正确

```
$ mingw32-make smoke   # 4 份 example 全部 OK
OK trace-event ... OK metric-set ... OK taskset ... OK capability   ✅

# 反例：删掉 event_id 后必须失败
$ hs schema check .gen-tmp-broken.json --kind trace-event
FAIL trace-event: .gen-tmp-broken.json
  - <root>: 'event_id' is a required property   → exit 1 ✅

$ mingw32-make test
10 passed ✅（含反例断言 + 契约一致性断言 + config 安全默认断言）
```

## G5 安全

```
$ grep -rniE "sk-[a-z0-9]{10,}|api[_-]?key\s*[:=]\s*['\"][^'\"]{8,}|BEGIN .*PRIVATE KEY" . \
    --exclude-dir=node_modules
（无输出，exit 1）✅ 无命中
```

## G6 可复现

```
$ mingw32-make gen && mingw32-make check-gen   # 连跑两轮
第一轮 gen: GEN 4 个 .d.ts + models.py + .gen-hash.json(5 files)
第二轮 gen: 产物完全一致（datamodel-code-generator --disable-timestamp、json2ts 固定 banner）
check-gen ×2: 全部 CHECK OK（TS 重新生成比对 + models.py 重新生成比对 + 哈希比对）✅
```

## 契约一致性

```
$ python -c "…assert len(schema['$defs']['EventType']['enum'])==26"
event enum count = 26 PASS ✅
且 pytest test_event_type_enum_exact 断言 26 个枚举与任务卡 §4.1 列表逐字相同（含顺序）。
```

## 生成器版本（锁定）

| 生成器 | 版本 |
| --- | --- |
| json-schema-to-typescript | 16.0.0（package.json `^16.0.0`） |
| typescript | 5.6.3（`~5.6.3`） |
| datamodel-code-generator | 0.86.0（pyproject `>=0.25.8`，`--disable-timestamp` 保证幂等） |
| pydantic | ≥2.7（实际 2.x） |

## 返工 R1 自验（构建入口不依赖 make，2026-10-25）

变更：`package.json` scripts 直连脚本（不含 make）；新增 `scripts/{lib.mjs, py.mjs, gen_all.mjs, check_gen.mjs, smoke.mjs}`；`Makefile` 改为 `npm run *` 薄壳；`tsconfig.base.json` 增加 `exclude: [node_modules, probe, .venv]`（否则 tsc 默认全量包含会把 `probe/probe-plugin.ts` 编进来）；`AGENTS.md` 补「npm run 为准，make 是别名」。未动 schema / examples / generated / models.py / probe。

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 无 make 也能生成 | `npm run gen` | exit 0 ✅ |
| 幂等 | 连跑两次 `npm run gen` | `.gen-hash.json` sha256 `7b1c1ee0…` 前后不变 ✅ |
| 漂移可检出 | 改 trace-event.schema.json 描述 → `npm run check-gen` | `CHECK FAIL: trace-event.d.ts 与 schema 不一致`，exit 1 ✅ |
| 还原后恢复 | 还原 schema → `npm run check-gen` | 全部 CHECK OK + `check-gen PASS`，exit 0 ✅ |
| 静态 | `npm run typecheck` && `npm run lint` | tsc 0 error；ruff All checks passed ✅ |
| make 仍可用 | `mingw32-make gen`（薄壳 → npm run gen） | exit 0 ✅ |
| 冒烟 | `npm run smoke` | version/schema list/4 example 全 OK ✅ |

## 偏差记录（与架构文档）

1. pnpm workspaces → **npm workspaces**（本机无 pnpm，任务卡指定）。
2. 架构 §2.2 说 `hs_core/schema.py` 为生成的 pydantic 模型；按任务卡 §3.3 实际拆为：`schema.py` = 手写校验器（加载 schema + validate），`models.py` = 生成的 pydantic 模型。
3. `CapabilityReport.channels` 由架构的 `Channel[]` 细化为对象数组（channel/status/degraded/note），以承载任务卡要求的 example 中 `degraded: L1/L2/L3` 各出现一次。
4. 生成器版本漂移：json-schema-to-typescript 13.x（架构 §2.9.1）→ **16.x**；datamodel-code-generator ~=0.26 → **0.86.0**（均按任务卡 §4.3 指定）。
5. 额外基础设施文件：root `tsconfig.json`、`scripts/` 三个生成脚本、`engine/tests/`（门禁自动化所必需）。
6. 本机无 `make`：R1 返工后构建入口以 `npm run *` 为准，`Makefile`（mingw32-make 执行）仅是别名薄壳；`hs` 需通过 `.venv/Scripts/hs.exe`、`python -m hs_core.cli` 或 `npm run smoke` 调用。

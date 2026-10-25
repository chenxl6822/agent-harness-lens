# AGENTS.md — harness_scope 项目铁律（给后续 AI 协作者）

> 违反任何一条 = 返工。本文件由 T01 立卡，后续任务卡只能追加、不能放宽。

## 目录职责

| 目录 | 职责 | 运行时 |
| --- | --- | --- |
| `packages/schema/` | 跨语言契约唯一真源（4 份 `*.schema.json`）+ 生成产物 `generated/ts/` + 黄金样例 `examples/` | — |
| `packages/adapters-ts/` | 采集适配器（T02–T04 才建；本卡禁止创建） | Node 22+ / TS |
| `engine/` | 核心评测引擎；源码只放 `engine/hs_core/`，测试只放 `engine/tests/` | Python 3.11+ |
| `scripts/` | 生成/校验等构建辅助脚本（由 Makefile 调用） | Node + Python |
| `docs/` | 指标定义、兼容矩阵、自检记录等文档 | — |
| `probe/` | T00 探针产物，**只读**，任何任务不许改动、不许引用进 schema | — |
| `tasksets/`、`delivery/`、`ci/`、`packages/web/` | 属于后续任务卡，T01 阶段禁止创建 | — |

## 命名规范

- TS：文件 `kebab-case.ts`，类型 `PascalCase`，常量 `SCREAMING_SNAKE`
- Python：文件/函数 `snake_case`，类 `PascalCase`，常量 `UPPER_CASE`
- 指标 key：`<group>.<snake_case>`，group ∈ {result, process, collab, cost}
- 环境变量：`HS_` 前缀；中文展示字段：`_zh` 后缀

## 契约铁律

1. **schema 是唯一真源**：所有跨语言类型由 `packages/schema/*.schema.json` 生成，**禁止手写、禁止手改 `packages/schema/generated/` 与 `engine/hs_core/models.py`**。
2. 单向生成流：`schema.json → TS（json-schema-to-typescript）→ 生成 .d.ts`；`schema.json → datamodel-code-generator → pydantic models.py`。**禁止反向生成。**
3. 改契约 = 改 schema → `make gen` → 生成产物与 `.gen-hash.json` 一并提交。**`make check-gen` 必须绿**，CI/验收不过不许合并。
4. `TraceEvent.type` 枚举共 **26 个**，与任务卡 T01 §4.1 逐字一致；增删改任何一个都必须先改本卡。
5. 顶层与固定结构对象一律 `additionalProperties: false`；**唯独 `payload` 与 `labels` 开放**（`additionalProperties: true`）。
6. 所有对 `packages/schema` 的引用必须**相对仓库根解析**（`Path(__file__).resolve().parents[2]` 等），**禁止硬编码绝对路径**。

## 安全红线

- 禁止提交真实密钥、真实用户轨迹、真实 PII 进仓库、example 或 `.env.example`。
- `HS_CONTENT_CAPTURE` 与 `HS_CLOUD_JUDGE` 默认必须 `off`（安全默认，不许改默认值）。
- 报告/产物导出前必须过脱敏自检（后续任务卡实现）。

## 工程约定

- 包管理：**npm workspaces**（本机无 pnpm）；npm 安装一律 `npm install --userconfig=.npmrc`（绕开用户全局失效代理）。
- Python：**uv venv + uv pip**（清华镜像）；`.venv` 在仓库根；CLI 入口 `.venv/Scripts/hs.exe`。
- `TraceEvent` 是唯一写入原语；一切派生实体由事件流物化（后续任务卡遵守）。
- 禁止交叉 import：TS 侧不 import Python，反之亦然；两侧只通过 schema 契约对齐。
- 统一入口：`make gen / check-gen / lint / typecheck / test / smoke`（本机用 `mingw32-make` 执行，语法标准 POSIX）。**构建入口以 `npm run *` 为准，`make` 只是便利别名薄壳**（真实现位于 `scripts/*.mjs` 与 venv 内工具）。

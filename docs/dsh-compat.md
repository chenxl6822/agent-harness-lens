# dsh 兼容矩阵与事件名清单（T00 + T04a 合并维护）

> 状态：T04a（2026-10-25）更新。**核心结论：本机可运行的 CLI 是 0.2.1-rc.2；桌面端 0.2.1-rc.3 未经运行时验证。**

## 1. 版本矩阵

| 项 | 版本 | 位置 | 状态 |
| --- | --- | --- | --- |
| CLI（npm 包，实际执行） | **0.2.1-rc.2** | `~/.workbuddy/binaries/node/workspace/node_modules/@deepseek-ai/dsh/lib/bin.js` | T00 与 T04a 均实测运行 |
| 桌面运行时 | 0.2.1-rc.3 | `~/.dsh/dsh-runtimes/dsh-primary-runtime/runtime.json` → desktopVersion | 仅元数据 |
| 桌面应用 | 0.2.1-rc.3 | `D:\DSH`（`resources/app.asar` 182MB） | 打包态，CLI 不可直接运行 |
| npm registry 最新 | 0.2.1-rc.2 | `npm view @deepseek-ai/dsh version` | 2026-10-25 查询 |

**推论：T00 的全部运行时结论对 rc.2 依然成立；对 rc.3 一律视为"未验证"，T04b 必须做版本门禁。**

## 2. seam 兼容性

| 能力 | rc.2（运行时实测） | rc.3（静态/未验证） |
| --- | --- | --- |
| `--patch` 挂 `.mjs` 插件 | ✅ 2026-10-25 复验通过（加载→订阅→落盘 3404 条） | ❓ 未验证（CLI 不可运行） |
| ctx surface 7 键 `on/emit/once/logger/events/root/registry` | ✅ 与 T00 逐项一致，无增减 | ❓ 未验证 |
| `.ts` 插件直接加载 | ❌（无 jiti/tsx，T00） | ❓ 推定不变 |
| headless + 凭据 | ✅ 须环境变量注入 DEEPSEEK_API_KEY | ❓ 未验证 |
| `--patch` 语法位置 | ✅ 必须在 profile 名之后：`dsh headless --patch x.yml "任务"` | ❓ 同上 |

## 3. 事件名清单

### 3a. rc.2 运行时实测（根 ctx emit 包裹，一次 headless 任务，3404 条）

`internal/dispatch`(1784, 4参数 string,string,array,object|null)、`internal/status`(942, object,number)、`loader/partial-dispose`(290)、`internal/plugin`(227)、`system-prompt/change`(64)、`tools/change`(50)、`settings/document-updated`(11)、`[object Object]`(24)、`domain/changed`、`subagent/provider-added`、`fs/observed`。

### 3b. rc.3 静态 grep（asar 中 `emit("<ns>/<name>")`，非运行时观测）

agent/assistant-stream · agent/error · agent/status · agent/inbox/{claimed,discarded,inserted} · app/ready · connection/reset · cordis/{dynamic-package,dynamic-retract,inspect-query,inspect-query-resolved,request-run,request-run-resolved} · domain/changed · fs/observed · goal/{changed,activation-changed} · hmr/{change,reload} · internal/{dispatch,plugin,status} · loader/{config-update,entry-init,partial-dispose} · locale/change · schedule/changed · settings/document-updated · slots/changed · subagent/{start,end,provider-added} · theme/change · tools/change · webserver/index-inject

### 3c. 语义覆盖缺口（对 TraceEvent 契约的影响）

- agent/turn/step/llm 语义：**turn、step、llm 无任何事件名**（运行时与静态皆无）；agent/* 仅 rc.3 静态存在且在 rc.2 根 ctx 上零触发。
- 逐次工具调用：最可能藏在 `internal/dispatch` 的 4 个参数里（T04a 只记录了参数类型，值未记录）——**T04b 第一个实验就是把参数值落盘**。
- 降级链（与 T00 一致）：L3 CLI 黑盒（低）→ L2 session log（中，仅会话头）→ L1 插件桥接轨迹（需 T04b）。

## 4. 对 T04b 的门禁要求

1. 启动时校验 `dsh --version`；非 rc.2 时阻断或显式降级（rc.3 seam/事件面未验证）。
2. 开工三实验：internal/dispatch 参数值落盘；枚举 ctx.events/root/registry 找 agent/* 挂载点；拿到可运行 rc.3 后重跑探针。
3. 事件名 → TraceEvent.type（26 枚举）映射表待上述实验后补充本文件。

> **T04b（2026-10-25）已完成上述三实验**：internal/dispatch 实证为总线派发遥测镜像并承载工具/LLM/turn-step 全量轨迹；agent/* 经 `ctx.on` 直挂全量可达（11670 帧）；版本门禁规则落 `docs/dsh-version-gate.md`，映射草案落 `docs/dsh-bridge-contract.md`，实测数据在 `probe/rc3b/`。§3c 的缺口结论已被推翻——轨迹粒度可达 **full**（仅 rc.2）。
>
> ⚠️ **上句的 `full` 须读作 `full (single-agent)`**（WorkBuddy 验收补正）：该次运行 `agent/created` 仅 1 个 session、`subagent/*` 仅启动期 `provider-added`，子智能体实例为 0，多智能体轨迹**尚未验证**。另有三处结论需按 `docs/dsh-bridge-contract.md` §5 修正：`llm/stream` 仅 3 条且全为 session-title（主对话消息取 `agent/pre-step.messages`）；仅 start 帧带 turn/step；`callId` 脱敏导致配对不可复核。详见 `acceptance/G-T04b-验收报告.md`。

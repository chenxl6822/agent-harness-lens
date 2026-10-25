# agent-harness-lens

> 给 AI Agent 装的「行车记录仪 + 体检报告」——不问"它答对了没有"，而问"它是怎么答的、花了多少、哪里在浪费"。

`agent-harness-lens`（内部代号 `harness_scope`，CLI 名 `hs`）是一个**智能体过程评测与优化工作台**：
采集真实 Agent 运行轨迹，量化**多智能体协作质量**与**Harness 配置收益**，输出可回归、可对比、可下钻的诊断报告。

## 为什么做这个

现在评测 AI Agent，绝大多数人只看一个数字：**任务完成了没有**。但同样是"完成"，过程可能天差地别：

- 有的 3 步搞定，有的绕了 20 步
- 有的一个 agent 干完，有的 3 个 agent 互相重复劳动
- 固定同一个模型、只换 harness 配置，得分与成本都可能差出一个数量级

换句话说：**大家都在给模型打分，很少有人给"怎么用模型"打分。**

本项目把"怎么用"变成可测量、可对比、可改进的工程指标。

## 核心概念

### 1. 一个任务 = 一条完整轨迹

Agent 每做一个动作，就在运行时记一行结构化事件（`TraceEvent`）。目前定义了 **26 类事件**，覆盖：

运行/会话边界 · agent 生命周期 · 轮次与步骤 · 模型与工具调用 · 任务协作 · 消息传递 · 产物与上下文

实测示例（让 CodeBuddy 读一个文件，自动记下 5 个事件，共 1.8 KB）：

```
session.start → turn.start → tool.call → tool.result → turn.end
```

### 2. 四组指标，其中协作组是差异化的关键

| 组 | 回答什么 |
| --- | --- |
| 结果层 | 完成了吗？连跑 k 次都成功吗（pass^k）？ |
| 过程层 | 工具用对了吗？有没有冗余循环、反复重试？ |
| **协作层** | **多个 agent 是"团队"还是"互相拖累"？** |
| 成本层 | 花了多少 token / 多少钱 / 多少墙钟时间？ |

协作层 6 项指标（普通测试通过率一个都测不出来）：

| 指标 | 它在回答什么问题 |
| --- | --- |
| 认领冲突率 | 是不是两个 agent 抢了同一个任务 |
| 成员空转率 | 是不是有 agent 从头到尾没干活 |
| 重复劳动率 | 不同 agent 是不是重复读同一批文件 |
| 上下文污染度 | 交接时把无关信息一起带过去了没有 |
| 消息轮次效率 | 沟通是不是在绕圈 |
| 派单均衡度 | 活是不是全压在一个人身上 |

**每个指标都能下钻到具体事件**——报告里的数字不是孤立的分数，而是可以点开看到"是哪几个动作造成的"。

### 3. 一个任务集 = 一次可复现实验

强制记录 harness 变体指纹、任务集版本、模型版本、重复次数、随机种子、SDK 版本。
没有这些元数据的报告只是好看的图表，不是能写进论文、能被别人复现的实验。

## 当前状态

**早期开发中。** 已通过验收的部分：

| 模块 | 状态 | 说明 |
| --- | --- | --- |
| 跨语言契约 | ✅ | 4 份 JSON Schema 为唯一真源，TS / Python 类型全部生成，永不手写 |
| CodeBuddy Hook 采集 | ✅ | 12 类 Hook 全映射；真实会话实测触发 5 事件；hook 净开销 ~45ms，不影响被测 Agent |
| dsh 采集通道探针 | ✅ | 实测结论：dsh 0.2.1-rc.2 **无原生 OTLP**、会话日志**只有会话头**、`--patch` 插件桥接**可用** |
| 文件采集通道 | ✅ | trace / transcript / tasks 三通道；1452 个 span 解析零误差、调用树零环、增量 p95 8ms |
| dsh 插件桥接 | ⏳ | 待开发 |
| 存储 / 指标引擎 / 报告 | ⏳ | 待开发 |

## 快速开始

```bash
# 1. 安装依赖（Node 22+ / Python 3.11+ / uv）
npm install
uv venv && uv pip install -e engine

# 2. 跑契约与骨架的自检
npm run gen          # 由 schema 生成 TS + Python 类型
npm run check-gen    # 校验生成产物是否与 schema 同步
npm test             # Python 侧单测

# 3. 构建采集适配器
npm run build:adapters
npm run test:adapters

# 4. 装 hook 到 CodeBuddy（会先备份 settings.json）
node packages/adapters-ts/dist/codebuddy/install.js --dry-run   # 先看要改什么
node packages/adapters-ts/dist/codebuddy/install.js

# 5. 跑任意一次 CodeBuddy 会话，轨迹落到 spool
export HS_SPOOL_DIR=/tmp/hs-spool HS_RUN_ID=demo
codebuddy -p "读一下 README.md"

# 6. 卸载
node packages/adapters-ts/dist/codebuddy/install.js --uninstall
```

## 设计要点

- **被测对象无感**：hook 只做「读 stdin → 归一化 → 追加一行 → exit 0」，stdout 永远为空，绝不阻断、绝不注入上下文；任何异常都 exit 0。
- **失败不阻断**：解析失败、目录不可写、未知事件，全部降级计数，不影响 Agent 运行。
- **默认不存内容**：`HS_CONTENT_CAPTURE=off` 时只存 hash 与元数据；开启时强制脱敏（正则特征库 + 高熵检测），并在事件上打 `redacted` 标记。
- **跨语言单一真源**：`packages/schema/*.schema.json` 是唯一真相。CI 里 `npm run check-gen` 会拦住"改了 schema 忘了重新生成"。
- **对齐 OpenTelemetry GenAI 语义约定**：轨迹字段与 OTel 的 `gen_ai.*` 对齐，便于和现成可观测性后端交叉验证。

## 不做什么

- ❌ 不训练模型、不优化 prompt 本身
- ❌ 不做生产环境 APM / 实时监控（只评"实验"，不盯线上）
- ❌ 不追求"单一总分排行榜"（单一分数没有诊断价值）

## 许可证

[Apache-2.0](LICENSE)

## 致谢

本项目的被测对象使用 [CodeBuddy](https://www.codebuddy.cn/) 的多智能体能力，实验台参考 [DeepSeek Harness](https://www.npmjs.com/package/@deepseek-ai/dsh) 的插件体系。
二者均为各自权利人的商标/产品，本项目与其无隶属关系，且**不随仓库分发任何第三方专有代码**。

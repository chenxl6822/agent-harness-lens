# dsh 版本门禁与降级链规则（T04b-E3）

> 状态：T04b（2026-10-25）。每条规则均可由不写代码的人照步骤验证。证据来源标注在括号内。

## 1. 怎么探测当前 dsh 版本

按优先级依次执行，**前一步成功即停**：

1. **可运行 CLI（权威）**：执行
   `node "<dsh安装目录>/lib/bin.js" --version`
   本机实证输出 `0.2.1-rc.2`（运行时，2026-10-25，probe/rc3b/raw/version.txt）。
   - dsh 安装目录定位：`~/.workbuddy/binaries/node/workspace/node_modules/@deepseek-ai/dsh`；`which dsh` 为空、无全局命令（静态，T04a 复核）。
2. **npm 包元数据（交叉核对）**：读同目录 `package.json` 的 `version` 字段，须与 `--version` 输出一致；不一致时**以可运行的 `--version` 为准并告警**（静态规则，两来源均为真实文件读取）。
3. **桌面端在装检测（仅用于门禁告警，不作版本判定）**：读
   `~/.dsh/dsh-runtimes/*/runtime.json` 的 `desktopVersion` 字段（本机为 `0.2.1-rc.3`，运行时读取，T04a）。
   该字段只证明「桌面运行时版本」，不代表 CLI 可桥接版本——两者可以并存且不同（本机即 rc.3 桌面 + rc.2 CLI 并存）。
4. **探测失败怎么办**：步骤 1 报错/无输出/非语义化版本 → 视为「dsh 不可用」，直接落 L3 黑盒，禁止猜测版本继续走桥接。

## 2. 版本 × 能力矩阵

| 版本 | 可用通道 | 轨迹粒度 | 数据完整性声明 | 门禁动作 |
| --- | --- | --- | --- | --- |
| `0.2.1-rc.2`（CLI 实测） | `--patch` 插件 + `ctx.on`/`internal/dispatch` 镜像 | **full**：工具调用（tools/execute+result，含 name/arguments/callId）、LLM 消息（agent/pre-step.messages、llm/stream）、turn/step 边界（frame 字段）、session 事件账本 | 事件面经运行时实测（T04b，26059 行 NDJSON）；但仅覆盖 headless 用法，桌面 GUI 会话未经运行时验证 | 允许桥接；首跑须自检探针（能收到 `agent/assistant-stream` 即通过） |
| `0.2.1-rc.3`（仅桌面端在装） | 无已验证通道 | 无 | CLI 在 asar 内不可运行，`--patch`/ctx 面未验证（静态 grep：37 个命名空间事件名，**静态存在 ≠ 运行时可达**） | **拒绝桥接**，降级 L3 黑盒；打告警「检测到 rc.3 桌面运行时在装」 |
| 探测失败 / 其他版本 | L3 黑盒（退出码/墙钟/stdout） | blackbox_only | 仅有任务级结果，无过程数据 | 降级；版本号记 `unknown` |

## 3. rc.3 检测规则（要能检测到「用户装了 rc.3」这件事本身）

满足任一即判定「rc.3 桌面端在装」，输出告警并强制降级：

1. `~/.dsh/dsh-runtimes/*/runtime.json` 中任一 `desktopVersion` 以 `-rc.3` 结尾或 > rc.2（运行时读取可验证，本机命中）。
2. 可运行 CLI 的 `--version` 输出非 `0.2.1-rc.2`（唯一权威桥接版本）。

验证方法（无需写代码）：

```text
$ cat ~/.dsh/dsh-runtimes/dsh-primary-runtime/runtime.json   # 期望看到 "desktopVersion": "0.2.1-rc.3"
$ node ~/.workbuddy/binaries/node/workspace/node_modules/@deepseek-ai/dsh/lib/bin.js --version   # 期望 0.2.1-rc.2
```

两者同时成立 = rc.3 在装但 CLI 仍是 rc.2 → 允许桥接，告警提示版本漂移风险。

## 4. 降级链（固定顺序，逐级降）

1. **L1 桥接轨迹**（rc.2 + 门禁通过）：`--patch` 插件订阅 `agent/*` 与 `internal/dispatch`，产出工具/LLM/turn-step 级轨迹。
2. **L2 session log**（L1 失败）：读 `~/.dsh` 下会话头（仅会话级元数据）。
3. **L3 黑盒**（兜底）：退出码、墙钟耗时、stdout 摘要；声明「无过程轨迹，数据完整性=任务级」。

**规则**：任何一级启用前必须先跑 §1 版本探测并落门禁结论；降级事件必须写入产物元数据（`trajectory_granularity` 字段），禁止静默降级。

## 5. 证据边界声明

- rc.2 的 `--patch`、ctx surface、事件面、`internal/dispatch` 载荷：**运行时实测**（T04a 3404 条 + T04b 26059 行，2026-10-25）。
- rc.3：**仅静态**（asar grep 事件名、runtime.json 元数据）。在拿到可运行的 rc.3 CLI 前，本矩阵对 rc.3 的「拒绝桥接」是保守策略，不是「rc.3 不可桥接」的证明。

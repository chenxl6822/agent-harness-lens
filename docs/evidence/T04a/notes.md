# T04a 探针过程记录（rc3-recheck）

- 日期：2026-10-25（本机时间 22:05–22:50 +08:00）
- 执行方：CodeBuddy；所有输出均为真实命令的真实输出，未概括、未编造。

## 关键结论先行

**本机实际能跑起来的 dsh CLI 是 0.2.1-rc.2，不是 rc.3。** rc.3 只存在于桌面端安装 `D:\DSH`（打包进 `resources/app.asar`，无法用 node 直接运行）。因此本卡全部运行时证据仅覆盖 rc.2；rc.3 只有 asar 二进制静态 grep 证据。

## 1. 版本定位（Q1）

```text
$ node C:/Users/chenxl/.workbuddy/binaries/node/workspace/node_modules/@deepseek-ai/dsh/lib/bin.js --version
0.2.1-rc.2
$ grep '"version"' <同目录>/package.json
"version": "0.2.1-rc.2",
```

桌面端（rc.3）证据：

```text
$ cat ~/.dsh/dsh-runtimes/dsh-primary-runtime/runtime.json   ->  "desktopVersion": "0.2.1-rc.3"
$ cat ~/AppData/Local/@deepseek-aidsh-desktop-updater/pending/update-info.json
{"fileName":"deepseek-harness-0.2.1-rc.3-win-x64.exe",...}
$ grep -c "0.2.1-rc.3" D:/DSH/resources/app.asar
4623
$ cat D:/DSH/version          ->  44.7.0   （Electron 版本，非 dsh 版本）
```

dsh CLI 的定位过程：`which dsh` 为空；Start Menu 快捷方式 `DeepSeek Harness.lnk` 解析出 `D:\DSH\DeepSeek Harness.exe`；CLI 本体在 `~/.workbuddy/binaries/node/workspace/node_modules/@deepseek-ai/dsh/lib/bin.js`（T00 任务卡已记录该路径）。

## 2. --patch seam（Q2，运行时证据，rc.2）

调用姿势勘误：`--patch` 必须挂在 **profile 名之后**：

```text
# 失败：node bin.js --patch rc3-overlay.yml headless "..."
error: --profile <name> is required

# 失败：node bin.js --profile web --patch rc3-overlay.yml headless "..."
error: too many arguments. Expected 0 arguments but got 2: headless, 读取...

# 成功：node bin.js headless --patch rc3-overlay.yml "任务"
```

成功运行（cwd=probe/rc3，凭据经环境变量注入，未打印未落盘）：

```text
$ HS_PROBE_OUT=<...>/raw/events-rc2.ndjson DEEPSEEK_API_KEY=... \
  node <dsh>/lib/bin.js headless --patch rc3-overlay.yml "读取当前目录下 input.txt 的内容并原样输出"
exit_code=0
--- stdout:
[rc3-probe] ctx keys = ["fiber"]
[rc3-probe] ctx api surface = {"on":"function","once":"function","emit":"function","logger":"function","events":"object","root":"object","registry":"object","fiber":"object"}
[rc3-probe] ctx.fiber api surface = {}
[rc3-probe] emit wrapped on ctx
[rc3-probe] ndjson out = .../probe/rc3/raw/events-rc2.ndjson
当前目录下未找到 `input.txt`，已读取 `l3-work/input.txt`，该文件内容如下：
（…任务正常完成…）
--- stderr: dsh: reasoning: …（思维流，正常）
```

插件包裹的 emit 落盘 **3404 条** NDJSON（raw/events-rc2.ndjson）——链路「加载→订阅→落盘」全通。

## 3. ctx API 面（Q3，运行时证据，rc.2）

`Object.keys(ctx) = ["fiber"]`（与 T00 一致）；`in` 探测的 surface 与 T00 的 7 键逐项一致：
`on / once / emit / logger / events / root / registry`（均为 function/object），**无增无减**。`ctx.fiber` 自身 surface 为 `{}`。

## 4. 事件名测绘（Q4）

### 4a. 运行时（rc.2，根 ctx emit 包裹，3404 条）

| 事件名 | 次数 |
| --- | --- |
| internal/dispatch | 1784（参数 4 个：string,string,array,object\|null） |
| internal/status | 942（object,number） |
| loader/partial-dispose | 290 |
| internal/plugin | 227 |
| system-prompt/change | 64 |
| tools/change | 50 |
| settings/document-updated | 11 |
| [object Object] | 24（emit 首参是对象，事件名无法字符串化） |
| domain/changed、rc3-probe/ping、subagent/provider-added、fs/observed | 各 1–4 |

**注意：根 ctx 上没有任何 agent/turn/step/tool(逐次)/llm 语义事件名。**

### 4b. 静态（rc.3，`grep -oaE "emit\(['\"]name"` app.asar）

37 个命名空间事件名（完整清单见 result.json），含语义相关的：
`agent/assistant-stream, agent/error, agent/status, agent/inbox/{claimed,discarded,inserted}, subagent/start, subagent/end, goal/changed, goal/activation-changed, app/ready, tools/change, internal/{dispatch,plugin,status}, loader/*, settings/document-updated, domain/changed, fs/observed, subagent/provider-added` 等。另见大量底层 EventEmitter 词（error/close/end/finalMessage/thinking/text/tokens 等，归属未验证）。

## 5. 局限与未决（如实申报）

1. **rc.3 未做运行时验证**：CLI 打包在 asar 内，解包需 asar 工具（本卡禁止引入新依赖）。上文 4b 是静态推导，不是运行时观测。
2. **internal/dispatch 参数值未记录**（bridge.mjs 只记了 argTypes）：tool/llm 语义是否内嵌其中未验证，是 T04b 第一个实验。
3. **agent/* 在根 ctx 零触发**：挂载点待查（子 context 或 ctx.events 枚举）。
4. `[object Object]`：24 次 emit 首参为对象，未深究。

## 6. 约束遵守声明

- 未修改 packages/schema；未改 package.json、未装任何依赖；未 git 提交；未改任何用户级配置。
- 只新建了 `probe/rc3/`（bridge.mjs、rc3-overlay.yml、l3-work/input.txt、raw/*、result.json、本文件）与 `docs/dsh-compat.md`。
- 凭据仅在运行时注入环境变量，未打印、未落盘。

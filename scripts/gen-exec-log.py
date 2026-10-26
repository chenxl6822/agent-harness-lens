#!/usr/bin/env python
"""gen-exec-log.py —— 把「派单台账 + CodeBuddy 会话库」汇总成人类可读的执行日志。

为什么需要它：`runs/dispatch-ledger.jsonl` 是机器台账，`runs/` 又被 gitignore，
外部读者看不到「任务到底交给了谁、跑了多久、留下什么」。本脚本把两边的证据缝在一起，
输出一份可长期留档的 Markdown，并把本机路径脱敏成 `<HOME>` / `<user>`。

用法：
    .venv/Scripts/python.exe scripts/gen-exec-log.py                      # 全部任务
    .venv/Scripts/python.exe scripts/gen-exec-log.py --prefix T06         # 只看 T06 系列
    .venv/Scripts/python.exe scripts/gen-exec-log.py --out docs/evidence/T06/codebuddy-execution-log.md
"""

from __future__ import annotations

import argparse
import datetime
import json
import pathlib
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parents[1]
HOME = str(pathlib.Path.home())
USER = pathlib.Path.home().name


def scrub(s: str) -> str:
    return str(s).replace(HOME, "<HOME>").replace(USER, "<user>")


def stat_session(p: pathlib.Path | None) -> dict | None:
    """读 CodeBuddy 会话 jsonl，统计行数/类型/工具调用/时间跨度。"""
    if p is None or not p.exists():
        return None
    types: Counter = Counter()
    tools: Counter = Counter()
    first = last = None
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.strip():
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        types[o.get("type", "?")] += 1
        if o.get("type") == "function_call" and o.get("name"):
            tools[o["name"]] += 1
        ts = o.get("timestamp")
        if isinstance(ts, (int, float)):
            first = ts if first is None else min(first, ts)
            last = ts if last is None else max(last, ts)
    span = None
    if first and last:
        span = (
            f"{(last - first) / 1000 / 60:.1f} 分钟"
            f"（{datetime.datetime.fromtimestamp(first / 1000):%H:%M:%S}"
            f" → {datetime.datetime.fromtimestamp(last / 1000):%H:%M:%S}）"
        )
    return {"lines": sum(types.values()), "types": types, "tools": tools, "span": span}


def cell(rec: dict) -> dict:
    ev = rec.get("evidence") or {}
    cs = ev.get("codebuddySession") or {}
    git = ev.get("git") or {}
    v = rec.get("verdict") or {}
    sp = pathlib.Path(cs["file"]) if cs.get("file") else None
    st = stat_session(sp)
    sess = f"`{cs['sessionId'][:8]}…`" if cs.get("sessionId") else "—"
    if st and sp:
        sess += f"<br>{st['lines']} 行 / {sp.stat().st_size // 1024} KB"
    ok = v.get("dispatchedToCodeBuddy") and v.get("producedChanges") and v.get("succeeded")
    return {
        "ev": ev, "cs": cs, "git": git, "v": v, "st": st,
        "sess": sess,
        "matched": "是" if cs.get("matched") else ("否" if cs else "—"),
        "nchg": len(git.get("changedFiles") or []),
        "mark": "✅" if ok else ("🔁 补录" if rec.get("backfilled") else "⚠️"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix", default="", help="只看任务编号以此开头的记录，如 T06")
    ap.add_argument("--out", default="docs/evidence/T06/codebuddy-execution-log.md")
    a = ap.parse_args()

    ledger = ROOT / "runs" / "dispatch-ledger.jsonl"
    rows = [json.loads(l) for l in ledger.read_text(encoding="utf-8").splitlines() if l.strip()]
    if a.prefix:
        rows = [r for r in rows if str(r.get("task", "")).startswith(a.prefix)]

    out: list[str] = []
    out.append("# CodeBuddy 执行日志与派单记录（自动生成）\n")
    out.append(
        f"生成时间：{datetime.datetime.now():%Y-%m-%d %H:%M:%S}　|　台账条目：**{len(rows)}** 条"
        f"（筛选 `--prefix {a.prefix or '*'}`）\n"
    )
    out.append(
        "> 由 `scripts/gen-exec-log.py` 从 `runs/dispatch-ledger.jsonl` 与会话库汇总，路径已脱敏。\n"
        "> 原始台账与原始会话不入库（`runs/` 被 gitignore、会话在用户目录），本文件用于长期留档。\n"
        "> 每行的「会话」列来自 CodeBuddy 自己写的 `~/.codebuddy/projects/<slug>/<sessionId>.jsonl`，\n"
        "> 「命中」= 该文件里能找到本次派单提示词的原文指纹——这是「任务确实交给了 CodeBuddy」的硬证据。\n"
    )

    out.append("\n## 1. 派单总表\n")
    out.append("| 任务 | 派单时间 | 耗时 | 退出码 | CodeBuddy 会话 | 提示词命中 | 变更文件 | 状态 |")
    out.append("|---|---|---|---|---|---|---|---|")
    for r in rows:
        c = cell(r)
        dur = "—" if r.get("durationSec") is None else f"{r['durationSec']}s"
        code = "—" if r.get("exitCode") is None else r["exitCode"]
        out.append(
            f"| **{r['task']}** | {r['startedAt'][:16].replace('T', ' ')} | {dur} | {code} "
            f"| {c['sess']} | {c['matched']} | {c['nchg']} 个 | {c['mark']} |"
        )

    out.append("\n## 2. 逐任务明细\n")
    for r in rows:
        c = cell(r)
        ev, cs, git, st, v = c["ev"], c["cs"], c["git"], c["st"], c["v"]
        p = r.get("prompt") or {}
        out.append(f"### {r['task']}\n")
        out.append(f"- **runTag**：`{r['runTag']}`")
        if r.get("backfilled"):
            out.append(
                f"- **⚠️ 补录记录**：{r.get('backfillReason', '')}"
                f"\n  - 补录时间：{r.get('backfilledAt', '')}"
                f"\n  - `exitCode` / `durationSec` 为 `null`：随包装进程一起丢失，**不推测**"
            )
        else:
            out.append(f"- **时间**：{r['startedAt']} → {r.get('endedAt')}（{r.get('durationSec')}s）")
            out.append(
                f"- **退出码**：{r.get('exitCode')}　**中断信号**：{r.get('interrupted') or '无'}"
                f"　**执行器**：`cbc-run.js`（headless，无需人工点确认）"
            )
        out.append(f"- **提示词**：{p.get('chars')} 字符，sha256 `{(p.get('sha256') or '')[:32]}…`")
        out.append(f"  - 文件：`{scrub(p.get('file', ''))}`")
        if cs:
            out.append(
                f"- **CodeBuddy 会话**：`{cs.get('sessionId')}`　原文命中："
                f"**{'是' if cs.get('matched') else '否'}**"
            )
            out.append(f"  - 会话记录：`{scrub(cs.get('file', ''))}`（{cs.get('bytes', 0) // 1024} KB）")
            if st:
                out.append(f"  - 记录行数：**{st['lines']}**　时间跨度：{st['span']}")
                out.append(f"  - 类型分布：{dict(st['types'])}")
                top = ", ".join(f"{k}×{n}" for k, n in st["tools"].most_common(8))
                out.append(f"  - 工具调用：{sum(st['tools'].values())} 次（{top}）")
        trs = ev.get("codebuddyTraces") or []
        if trs:
            out.append(f"- **trace 文件（span 树）**：{len(trs)} 个")
            for t in trs[:3]:
                out.append(f"  - `{scrub(t['path'])}`（{t.get('size', 0) // 1024} KB，mtime {t.get('mtime', '')}）")
        if git.get("postHoc"):
            out.append(f"- **仓库（补录时为事后快照）**：HEAD `{str(git.get('headAtBackfill'))[:8]}`")
        else:
            out.append(
                f"- **仓库**：HEAD `{str(git.get('headBefore'))[:8]}` → `{str(git.get('headAfter'))[:8]}`"
                f"（{'有变动' if git.get('headChanged') else '无变动'}）"
            )
        ch = git.get("changedFiles") or []
        if ch:
            out.append(f"- **变更文件（{len(ch)}）**：")
            for x in ch[:40]:
                out.append(f"  - `{scrub(x)}`")
        out.append(
            f"- **判决**：`dispatchedToCodeBuddy={v.get('dispatchedToCodeBuddy')}`、"
            f"`producedChanges={v.get('producedChanges')}`、`succeeded={v.get('succeeded')}`\n"
        )

    out.append("\n## 3. 台账完整性自检\n")
    backfilled = [r for r in rows if r.get("backfilled")]
    interrupted = [r for r in rows if r.get("interrupted")]
    out.append(f"- 条目总数：**{len(rows)}**")
    out.append(f"- 补录条目：**{len(backfilled)}**" + (f"（{', '.join(r['task'] + ' @ ' + r['runTag'][-9:] for r in backfilled)}）" if backfilled else ""))
    out.append(f"- 带中断标记：**{len(interrupted)}**" + (f"（{', '.join(r['task'] for r in interrupted)}）" if interrupted else ""))
    out.append("- 提示词原文未命中的条目：**%d**" % sum(1 for r in rows if not (r.get("evidence", {}).get("codebuddySession") or {}).get("matched")))
    out.append(
        "\n> 补录条目意味着「包装进程被信号掐断、post-flight 没跑」。"
        "现已修：`dispatch.mjs` 收到 SIGTERM/SIGINT/SIGHUP 会先杀子进程再走完取证落盘（记录带 `interrupted`）。\n"
    )

    text = "\n".join(out) + "\n"
    dest = ROOT / a.out
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8", newline="\n")
    print(f"written {dest}  {len(text)} chars")
    print(f"隐私自检：包含真实用户名？{USER in text}  |  包含绝对 HOME？{HOME in text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""T05a 最小摄入管线：三通道原始数据 → 统一 TraceEvent NDJSON。

范围（T05a 任务卡 §3 E4）：
- 输入：hook spool 目录 / ~/.codebuddy 根（通道 A/B/C）/ T04c dsh 事件 NDJSON（探针信封格式）
- 输出：统一 TraceEvent NDJSON（先落盘 runs/<runId>/events.normalized.ndjson，不写 SQLite）
- 内置：seq 确定性赋号（assign_seq）、event_id 确定性生成、去重幂等、脱敏路径

不写指标计算（T06）、不写报告（T10）、不写 SQLite（T05b）。
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = "1.0.0"

# Crockford Base32（ULID 字母表，与 trace-event.schema.json 的 event_id pattern 一致）
_ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_10CHARS_TS_BITS = 50  # ULID 时间戳 48 bit + 防碰撞扩展，取 50 bit 编 10 字符

EVENT_TYPES: frozenset[str] = frozenset(
    [
        "run.start", "run.end", "session.start", "session.end",
        "agent.spawn", "agent.stop", "turn.start", "turn.end",
        "step.start", "step.end", "llm.request", "llm.response",
        "tool.call", "tool.result", "tool.error",
        "task.create", "task.claim", "task.update", "task.complete",
        "message.send", "message.recv", "artifact.write", "artifact.ref",
        "context.compact", "error", "metric.custom",
    ]
)


# ---------------------------------------------------------------------------
# 确定性原语：event_id（确定性 ULID）与 seq 赋号
# ---------------------------------------------------------------------------

def _crockford_encode(value: int, length: int) -> str:
    chars = []
    for i in range(length):
        shift = 5 * (length - 1 - i)
        chars.append(_ULID_ALPHABET[(value >> shift) & 0x1F])
    return "".join(chars)


def _iso_to_ms(ts: str) -> int:
    """ISO-8601 UTC → epoch 毫秒。解析失败返回 0（排序时排最前，不丢事件）。"""
    try:
        from datetime import datetime, timezone

        s = ts.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError, OverflowError):
        return 0


def make_event_id(ts: str, content: str) -> str:
    """确定性 ULID：48bit 时间戳（ms）+ 80bit 内容摘要。

    同一批内容相同 → event_id 相同 → 去重即幂等；不同事件必不同（sha256 摘要）。
    """
    ts_ms = _iso_to_ms(ts)
    digest = hashlib.sha256(content.encode("utf-8")).digest()
    time_part = _crockford_encode(ts_ms & ((1 << 50) - 1), 10)
    rand_bits = int.from_bytes(digest[:10], "big")
    return time_part + _crockford_encode(rand_bits, 16)


# event_id 摘要白名单（R1-1）：逐键显式列举，禁止黑名单推导。
# 明确排除：raw_ref（行号/文件位置，文件追加后漂移）、run_id（run 重绑不应换 id）、
# agent_role / labels / schema_version（派生或装饰字段）。
_DIGEST_KEYS = ("ts", "session_id", "type", "source", "payload",
                "agent_id", "parent_agent_id")


def _compute_event_id(ev: dict) -> str:
    """按白名单键 + 通道稳定标识计算 event_id（不读 ev 全量，杜绝黑名单回归）。"""
    body: dict = {}
    for k in _DIGEST_KEYS:
        v = ev.get(k)
        if v is not None:
            body[k] = v
    sk = ev.get("_stable_key")
    if sk is not None:
        body["stable_key"] = sk
    return make_event_id(ev["ts"], json.dumps(body, sort_keys=True, ensure_ascii=False))


def assign_seq(events: list[dict]) -> list[dict]:
    """纯函数：run 内全局单调赋号，输入顺序无关、输出确定。

    排序键 = (ts 毫秒, event_id)；event_id 由 ts+内容摘要确定性生成，
    因此任意顺序输入得到同一输出序列。seq 覆盖写 0..n-1（占位值仅在摄入前有效）。
    """
    ordered = sorted(events, key=lambda e: (_iso_to_ms(e["ts"]), e["event_id"]))
    for i, ev in enumerate(ordered):
        ev["seq"] = i
    return ordered


# ---------------------------------------------------------------------------
# 通用小件
# ---------------------------------------------------------------------------

class Stats:
    """每类事件条数 + 丢弃原因计数。"""

    def __init__(self, channel: str) -> None:
        self.channel = channel
        self.type_counts: Counter[str] = Counter()
        self.drop_reasons: Counter[str] = Counter()
        self.lines_in = 0

    def ok(self, ev_type: str) -> None:
        self.type_counts[ev_type] += 1

    def drop(self, reason: str) -> None:
        self.drop_reasons[reason] += 1

    def as_dict(self) -> dict:
        mapped = sum(self.type_counts.values())
        dropped = sum(self.drop_reasons.values())
        return {
            "channel": self.channel,
            "lines_in": self.lines_in,
            "units_expanded": mapped + dropped,
            "events_out": mapped,
            "type_counts": dict(sorted(self.type_counts.items())),
            "dropped": dropped,
            "drop_reasons": dict(sorted(self.drop_reasons.items())),
        }


def _mk_event(
    *,
    run_id: str,
    ts: str,
    session_id: str,
    ev_type: str,
    platform: str,
    channel: str,
    payload: dict,
    agent_id: str | None = None,
    agent_role: str | None = None,
    parent_agent_id: str | None = None,
    labels: dict | None = None,
    raw_ref: str = "",
    stable_key: Any = None,
    scrub_home: str | None = None,
) -> dict:
    """组装 TraceEvent（schema 形状），seq 占位 0，event_id 白名单确定性生成。

    stable_key：通道记录级稳定标识（R1-1），进入 event_id 摘要但不写入事件本体：
    - 通道 A：(trace_id, spanId, span_start|span_end) / (trace_id, trace_start|trace_end)
    - 通道 B：(rec.id 或 callId, 记录 type)
    - 通道 C：(taskId, updatedAt, 事件形态)
    - 通道 D：(session.id, log.seq)（账本）/ (event 名, agent, ts)（其余）
    - hook：spool 内 event_id 原值
    """
    if ev_type not in EVENT_TYPES:
        raise ValueError(f"invalid event type: {ev_type!r}")
    ev = {
        "schema_version": SCHEMA_VERSION,
        "event_id": "",
        "run_id": run_id,
        "seq": 0,
        "ts": ts,
        "session_id": session_id,
        "type": ev_type,
        "source": {"platform": platform, "channel": channel},
        "labels": labels or {},
        "payload": payload,
    }
    if agent_id is not None:
        ev["agent_id"] = agent_id
    if agent_role is not None:
        ev["agent_role"] = agent_role
    if parent_agent_id is not None:
        ev["parent_agent_id"] = parent_agent_id
    if raw_ref:
        ev["raw_ref"] = raw_ref
    ev["_stable_key"] = stable_key
    if scrub_home:
        ev["payload"] = json.loads(
            json.dumps(ev["payload"], ensure_ascii=False).replace(scrub_home, "<WINHOME>")
        )
        ev["raw_ref"] = ev.get("raw_ref", "").replace(scrub_home, "<WINHOME>")
    ev["event_id"] = _compute_event_id(ev)
    return ev


def _content_hash(text: Any) -> dict:
    """内容默认只存 hash + 字节数（HS_CONTENT_CAPTURE=off 安全默认）。"""
    if text is None:
        return {}
    b = text.encode("utf-8") if isinstance(text, str) else json.dumps(text, ensure_ascii=False).encode("utf-8")
    return {"content_sha256": hashlib.sha256(b).hexdigest(), "content_bytes": len(b)}


# ---------------------------------------------------------------------------
# 通道 H：hook spool（T02 产出，已是 TraceEvent 形状）
# ---------------------------------------------------------------------------

def load_hook_spool(path: Path, run_id: str, stats: Stats, scrub_home: str | None = None) -> list[dict]:
    """读 spool 目录（*.jsonl）或单文件，校验并规整为统一 TraceEvent。"""
    files = sorted(path.glob("*.jsonl")) if path.is_dir() else [path]
    events: list[dict] = []
    for f in files:
        for lineno, line in enumerate(_iter_lines(f, stats), 1):
            stats.lines_in += 1
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                stats.drop("parse_error")
                continue
            if ev.get("schema_version") != SCHEMA_VERSION or ev.get("type") not in EVENT_TYPES:
                stats.drop("invalid_trace_event")
                continue
            # T02 的 seq 恒为 0（占位）；run_id 重绑为本卡 run；event_id 由本卡确定性重生成
            # 稳定标识（R1-1）：spool 内 event_id 原值（T02 已按 hook 语义生成，天然稳定）
            ev["run_id"] = run_id
            if scrub_home:
                ev = json.loads(json.dumps(ev, ensure_ascii=False).replace(scrub_home, "<WINHOME>"))
            ev["_stable_key"] = ev.get("event_id") or (ev["session_id"], ev["type"], ev["ts"])
            ev["event_id"] = _compute_event_id(ev)
            ev["seq"] = 0
            stats.ok(ev["type"])
            events.append(ev)
    return events


def _iter_lines(f: Path, stats: Stats) -> Iterable[str]:
    try:
        with f.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield line
    except OSError as exc:
        stats.drop(f"unreadable_file:{exc.errno}")


# ---------------------------------------------------------------------------
# 通道 A/B/C：~/.codebuddy 文件通道（T03 映射，本卡为最小实现）
# ---------------------------------------------------------------------------

def _ms_to_iso(ms: int | None) -> str:
    if not ms:
        return "1970-01-01T00:00:00.000Z"
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}Z"


_WRITER_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
_READER_TOOLS = {"Read", "Glob", "Grep"}


def load_codebuddy_files(home: Path, run_id: str, stats: Stats, scrub_home: str | None = None,
                         channel_policy: str = "primary_only") -> list[dict]:
    """通道 A（traces）+ B（transcript）+ C（tasks 快照）→ TraceEvent。

    channel_policy（R1-3）：
    - primary_only（默认）：9 类 A/B 双源事件只取主源通道 A，B 的 function_call/function_call_result 抑制
    - merge：A+B 全量并入（仅用于映射覆盖测绘，不得作为 T06 指标口径，见 docs/channel-priority.md）
    """
    events: list[dict] = []
    events += _load_channel_a(home / "traces", run_id, stats, scrub_home)
    events += _load_channel_b(home / "projects", run_id, stats, scrub_home,
                              channel_policy=channel_policy)
    events += _load_channel_c(home / "tasks", run_id, stats, scrub_home)
    return events


def _load_channel_a(root: Path, run_id: str, stats: Stats, scrub_home: str | None = None) -> list[dict]:
    events: list[dict] = []
    if not root.is_dir():
        stats.drop("channel_a_missing")
        return events
    for f in sorted(root.glob("*/*.jsonl")):
        trace_id = f.stem.removeprefix("trace_")
        sid = f"trace_{trace_id}"  # T03 偏差 D5：通道 A 无 sessionId，traceId 占位
        ref_base = f"traces/{f.parent.name}/{f.name}"  # 稳定锚点，不含行号（R1-1）
        for line in _iter_lines(f, stats):
            stats.lines_in += 1
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                stats.drop("parse_error")
                continue
            t = rec.get("type")
            if t == "trace_start":
                tr = rec.get("trace", {})
                events.append(_mk_event(
                    run_id=run_id, ts=tr.get("startedAt") or _ms_to_iso(None), session_id=sid,
                    ev_type="session.start", platform="codebuddy", channel="file",
                    payload={"trace_name": tr.get("name"), "worker_pid": tr.get("workerPid"),
                             "worker_hostname": tr.get("workerHostname")},
                    labels={"sub_channel": "A"},
                    raw_ref=f"{ref_base}#trace", stable_key=(sid, "trace_start"),
                    scrub_home=scrub_home))
                stats.ok("session.start")
            elif t in ("span_start", "span_end"):
                events += _map_span(rec, sid, run_id, ref_base, stats, scrub_home)
            elif t == "trace_end":
                tr = rec.get("trace", {})
                s = rec.get("summary", {})
                ts = tr.get("endedAt") or _ms_to_iso(None)
                events.append(_mk_event(
                    run_id=run_id, ts=ts, session_id=sid, ev_type="session.end",
                    platform="codebuddy", channel="file",
                    payload={"status": tr.get("status"), "duration_ms": tr.get("duration")},
                    labels={"sub_channel": "A"},
                    raw_ref=f"{ref_base}#trace", stable_key=(sid, "trace_end"),
                    scrub_home=scrub_home))
                stats.ok("session.end")
                events.append(_mk_event(
                    run_id=run_id, ts=ts, session_id=sid, ev_type="metric.custom",
                    platform="codebuddy", channel="file",
                    payload={"metric": "trace_summary", "span_count": s.get("spanCount"),
                             "total_tokens": s.get("totalTokens"), "error_count": s.get("errorCount")},
                    labels={"sub_channel": "A"},
                    raw_ref=f"{ref_base}#trace", stable_key=(sid, "trace_end", "metric"),
                    scrub_home=scrub_home))
                stats.ok("metric.custom")
            else:
                stats.drop("channel_a_unknown_record")
    return events


def _map_span(rec: dict, sid: str, run_id: str, ref_base: str, stats: Stats,
              scrub_home: str | None = None) -> list[dict]:
    out: list[dict] = []
    sp = rec.get("span", {})
    is_start = rec["type"] == "span_start"
    stype = sp.get("type")
    ts = (sp.get("startedAt") if is_start else sp.get("endedAt")) or _ms_to_iso(None)
    status = sp.get("status")
    span_id = sp.get("spanId", "")
    ref = f"{ref_base}#span={span_id or 'unknown'}"  # 稳定锚点，不含行号（R1-1）
    stable_key = (sid, span_id, rec["type"])
    base = dict(platform="codebuddy", channel="file", raw_ref=ref,
                stable_key=stable_key, labels={"sub_channel": "A"})
    if stype == "function":
        tool = sp.get("toolName") or sp.get("name") or "unknown"
        ev_type = "tool.call" if is_start else ("tool.error" if status == "error" else "tool.result")
        payload: dict = {"tool_name": tool, "span_id": span_id,
                         **_content_hash(sp.get("toolInput") or "")}
        out.append(_mk_event(run_id=run_id, ts=ts, session_id=sid, ev_type=ev_type,
                             payload=payload, scrub_home=scrub_home, **base))
        stats.ok(ev_type)
        if tool in _WRITER_TOOLS and is_start:
            out.append(_mk_event(run_id=run_id, ts=ts, session_id=sid, ev_type="artifact.write",
                                 payload={"tool_name": tool, "derived_from": "span:function"},
                                 scrub_home=scrub_home, **base))
            stats.ok("artifact.write")
        elif tool in _READER_TOOLS and is_start:
            out.append(_mk_event(run_id=run_id, ts=ts, session_id=sid, ev_type="artifact.ref",
                                 payload={"tool_name": tool, "derived_from": "span:function"},
                                 scrub_home=scrub_home, **base))
            stats.ok("artifact.ref")
    elif stype == "generation":
        ev_type = "llm.request" if is_start else "llm.response"
        out.append(_mk_event(run_id=run_id, ts=ts, session_id=sid, ev_type=ev_type,
                             payload={"span_name": sp.get("name"), "status": status,
                                      "duration_ms": sp.get("duration")}, scrub_home=scrub_home, **base))
        stats.ok(ev_type)
    elif stype == "custom":
        ev_type = "step.start" if is_start else "step.end"
        out.append(_mk_event(run_id=run_id, ts=ts, session_id=sid, ev_type=ev_type,
                             payload={"span_name": sp.get("name"), "status": status,
                                      "duration_ms": sp.get("duration")}, scrub_home=scrub_home, **base))
        stats.ok(ev_type)
    elif stype == "agent":
        name = sp.get("agentName") or sp.get("name") or "unknown"
        ev_type = "agent.spawn" if is_start else "agent.stop"
        out.append(_mk_event(run_id=run_id, ts=ts, session_id=sid, ev_type=ev_type,
                             agent_id=name, agent_role="lead" if name == "cli" else "subagent",
                             payload={"agent_name": name, "span_id": span_id, "parent_span_id": sp.get("parentId")},
                             scrub_home=scrub_home, **base))
        stats.ok(ev_type)
    else:
        stats.drop("span_unknown_type")
    return out


def _load_channel_b(root: Path, run_id: str, stats: Stats, scrub_home: str | None = None,
                    channel_policy: str = "primary_only") -> list[dict]:
    events: list[dict] = []
    if not root.is_dir():
        stats.drop("channel_b_missing")
        return events
    for f in sorted(root.glob("*/*.jsonl")):
        ref_base = f"projects/{f.parent.name}/{f.name}"  # 稳定锚点，不含行号（R1-1）
        for line in _iter_lines(f, stats):
            stats.lines_in += 1
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                stats.drop("parse_error")
                continue
            t = rec.get("type")
            sid = rec.get("sessionId") or ""
            ts = _ms_to_iso(rec.get("timestamp"))
            base = dict(platform="codebuddy", channel="file",
                        labels={"sub_channel": "B"},
                        stable_key=_b_stable_key(rec, t),
                        raw_ref=f"{ref_base}{_b_ref_anchor(rec, t)}")
            if t == "message":
                role = rec.get("role")
                ev_type = "message.recv" if role == "user" else "message.send"
                content = rec.get("content")
                text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
                events.append(_mk_event(run_id=run_id, ts=ts, session_id=sid, ev_type=ev_type,
                                        payload={"role": role, "message_id": rec.get("id"),
                                                 **_content_hash(text)}, scrub_home=scrub_home, **base))
                stats.ok(ev_type)
            elif t == "reasoning":
                content = rec.get("content")
                text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
                events.append(_mk_event(run_id=run_id, ts=ts, session_id=sid, ev_type="message.send",
                                        payload={"kind": "reasoning", "message_id": rec.get("id"),
                                                 **_content_hash(text)}, scrub_home=scrub_home, **base))
                stats.ok("message.send")
            elif t in ("file-history-snapshot", "session-meta"):
                stats.drop(f"noise_{t}")
            elif t in ("function_call", "function_call_result") and channel_policy == "primary_only":
                # R1-3：tool.call/result/error 主源为通道 A，B 侧同源观测在 primary_only 下抑制
                stats.drop("channel_b_secondary_suppressed")
            elif t == "function_call":
                events += _map_transcript_call(rec, sid, ts, run_id, stats, base, scrub_home)
            elif t == "function_call_result":
                name = rec.get("name") or "unknown"
                status = rec.get("status")
                ev_type = "tool.error" if status == "error" else "tool.result"
                out = rec.get("output")
                out_text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
                events.append(_mk_event(run_id=run_id, ts=ts, session_id=sid, ev_type=ev_type,
                                        payload={"tool_name": name, "call_id": rec.get("callId"),
                                                 "status": status, **_content_hash(out_text)},
                                        scrub_home=scrub_home, **base))
                stats.ok(ev_type)
            else:
                stats.drop("channel_b_unknown_record")
    return events


def _b_stable_key(rec: dict, t: str) -> tuple:
    """通道 B 记录级稳定标识（R1-1）：rec.id（无则 callId）+ 记录 type。"""
    rid = rec.get("id") or rec.get("callId") or ""
    return (str(rid), t)


def _b_ref_anchor(rec: dict, t: str) -> str:
    """通道 B 稳定锚点（R1-1）：不含行号，文件追加后不漂移。"""
    rid = rec.get("id") or rec.get("callId")
    if rid:
        return f"#id={rid}"
    return f"#type={t}"


def _map_transcript_call(rec: dict, sid: str, ts: str, run_id: str,
                         stats: Stats, base: dict, scrub_home: str | None = None) -> list[dict]:
    out: list[dict] = []
    name = rec.get("name") or "unknown"
    call_id = rec.get("callId")
    try:
        args = json.loads(rec.get("arguments") or "{}")
        if not isinstance(args, dict):
            args = {}
    except json.JSONDecodeError:
        args = {}
    if name in ("TaskCreate", "TaskUpdate"):
        task_id = str(args.get("taskId") or args.get("id") or args.get("subject") or "")
        if name == "TaskCreate":
            out.append(_mk_event(run_id=run_id, ts=ts, session_id=sid, ev_type="task.create",
                                 payload={"task_id": task_id, "subject": args.get("subject")},
                                 scrub_home=scrub_home, **base))
            stats.ok("task.create")
        else:
            status = args.get("status")
            if status == "completed":
                ev_type = "task.complete"
            else:
                ev_type = "task.update"
            out.append(_mk_event(run_id=run_id, ts=ts, session_id=sid, ev_type=ev_type,
                                 payload={"task_id": task_id, "status": status},
                                 scrub_home=scrub_home, **base))
            stats.ok(ev_type)
        # 不透传 arguments 原文（T03 §4 内容策略）
        return out
    payload = {"tool_name": name, "call_id": call_id, **_content_hash(rec.get("arguments") or "")}
    out.append(_mk_event(run_id=run_id, ts=ts, session_id=sid, ev_type="tool.call",
                         payload=payload, scrub_home=scrub_home, **base))
    stats.ok("tool.call")
    if name in _WRITER_TOOLS:
        out.append(_mk_event(run_id=run_id, ts=ts, session_id=sid, ev_type="artifact.write",
                             payload={"tool_name": name, "derived_from": "function_call"},
                             scrub_home=scrub_home, **base))
        stats.ok("artifact.write")
    elif name in _READER_TOOLS:
        out.append(_mk_event(run_id=run_id, ts=ts, session_id=sid, ev_type="artifact.ref",
                             payload={"tool_name": name, "derived_from": "function_call"},
                             scrub_home=scrub_home, **base))
        stats.ok("artifact.ref")
    return out


def _load_channel_c(root: Path, run_id: str, stats: Stats, scrub_home: str | None = None) -> list[dict]:
    """tasks 快照 → task.create / task.update / task.complete。

    快照无变更历史（仅最终态 + createdAt/updatedAt），只能还原首次创建与最终迁移，
    中间历史不可还原（矩阵 no_source 注记）。幂等：event_id 由 (taskId, updatedAt, 形态) 决定。
    """
    events: list[dict] = []
    if not root.is_dir():
        stats.drop("channel_c_missing")
        return events
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        sid = d.name
        for f in sorted(d.glob("*.json")):
            stats.lines_in += 1
            try:
                task = json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                stats.drop("parse_error")
                continue
            task_id = str(task.get("id") or f.stem)
            status = task.get("status")
            created = _ms_to_iso(task.get("createdAt"))
            updated = _ms_to_iso(task.get("updatedAt"))
            base = dict(platform="codebuddy", channel="file", labels={"sub_channel": "C"},
                        raw_ref=f"tasks/{d.name}/{f.name}")
            events.append(_mk_event(
                run_id=run_id, ts=created, session_id=sid, ev_type="task.create",
                payload={"task_id": task_id, "subject": task.get("subject"),
                         "description_hash": _content_hash(task.get("description") or "")},
                stable_key=(task_id, task.get("updatedAt"), "create"),
                scrub_home=scrub_home, **base))
            stats.ok("task.create")
            if status == "completed":
                events.append(_mk_event(run_id=run_id, ts=updated, session_id=sid,
                                        ev_type="task.complete",
                                        payload={"task_id": task_id, "status": status},
                                        stable_key=(task_id, task.get("updatedAt"), "complete"),
                                        scrub_home=scrub_home, **base))
                stats.ok("task.complete")
            elif status == "in_progress":
                events.append(_mk_event(run_id=run_id, ts=updated, session_id=sid,
                                        ev_type="task.update",
                                        payload={"task_id": task_id, "status": status},
                                        stable_key=(task_id, task.get("updatedAt"), "update"),
                                        scrub_home=scrub_home, **base))
                stats.ok("task.update")
            else:
                stats.drop("task_snapshot_terminal_or_unknown_status")
    return events


# ---------------------------------------------------------------------------
# 通道 D：dsh 事件 NDJSON（T04c 探针信封格式）
# ---------------------------------------------------------------------------

def _d(v: Any) -> Any:
    """从探针信封描述子提取叶子值：{'t','preview'} → Python 值。"""
    if not isinstance(v, dict):
        return v
    if v.get("t") == "skipped-heavy":
        return None
    if "preview" in v and v.get("t") in ("string", "number", "boolean"):
        p = v["preview"]
        if v["t"] == "number":
            try:
                return int(p) if "." not in str(p) else float(p)
            except (ValueError, TypeError):
                return p
        if v["t"] == "boolean":
            return p == "true"
        return p
    return None


def _dobj(v: Any, keys: Iterable[str]) -> dict:
    """从对象描述子浅提取指定键。"""
    if not isinstance(v, dict):
        return {}
    out = {}
    for k in keys:
        if k in v:
            val = _d(v[k])
            if val is not None:
                out[k] = val
    return out


def _ledger_item(it: Any) -> dict | None:
    """账本 item 两种形态：JSON 字符串 preview / 描述子对象。"""
    if not isinstance(it, dict):
        return None
    prev = it.get("preview")
    if isinstance(prev, str):
        try:
            return json.loads(prev)
        except json.JSONDecodeError:
            return None
    if it.get("t") == "object" and "type" in (it.get("keys") or []):
        item = _dobj(it, ("type", "time"))
        seq = _d(it.get("seq"))
        if seq is not None:
            item["seq"] = seq
        return item if "type" in item else None
    return None


_MSG_LEDGER_TYPES = {"turn/start": "turn.start"}
_VIA_PRIORITY = ("claimed", "inserted")  # 固定优先级（R1-2）：claimed > inserted，禁止依赖到达顺序
_UNMAPPED_LEDGER_TYPES = frozenset(
    ["permission/preset", "sandbox/mode", "approval/policy", "subagent/descriptor",
     "system/message", "agent/inbox/spliced"]
)


def _merge_msg_obs(entry: dict) -> None:
    """归并同一 message.id 的多条 inbox 观测（R1-2，顺序无关）。

    代表观测 = min(ts, via 优先级)；via_set/first_via 按固定优先级排序；
    最终重算 event_id（白名单摘要）。仅依赖观测集合，与到达顺序无关。
    """
    ev = entry["event"]
    vias = {o["via"] for o in entry["obs"]}
    via_set = [v for v in _VIA_PRIORITY if v in vias]
    rep = min(entry["obs"], key=lambda o: (_iso_to_ms(o["ts"]),
              _VIA_PRIORITY.index(o["via"]) if o["via"] in _VIA_PRIORITY else len(_VIA_PRIORITY)))
    ev["ts"] = rep["ts"]
    ev["session_id"] = str(rep["agent_id"])
    ev["agent_id"] = rep["agent_id"]
    ev["raw_ref"] = rep["ref"]  # 锚定代表观测（确定性），不随首见行漂移
    ev["payload"]["first_via"] = via_set[0] if via_set else rep["via"]
    ev["payload"]["via_set"] = via_set
    ev["event_id"] = _compute_event_id(ev)


def load_dsh_events(path: Path, run_id: str, stats: Stats, scrub_home: str | None = None) -> list[dict]:
    """dsh 通道（T04c 探针信封 NDJSON）→ TraceEvent。

    去重三件套（P0）：
    1. session/event 账本快照按 (session.id, item.seq) 去重（实测 6070 → 24）
    2. 直挂+internal/dispatch 镜像双计按 (event, runId) 去重
    3. inbox 消息按 message.id 归并：同内容多源观测折入单事件（via_set/first_via，
       固定优先级 claimed > inserted，顺序无关）；内容不一致记 error 事件 + 冲突计数（R1-2）
    """
    events: list[dict] = []
    seen_ledger: set[tuple[str, int]] = set()
    msg_obs: dict[str, dict] = {}
    seen_subagent: dict[tuple[str, str], dict | None] = {}
    if not path.is_file():
        stats.drop("dsh_file_missing")
        return events
    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            if len(line) > 8_000_000:  # 单行骨架保护（§1.7 禁切片截断）
                stats.drop("line_over_8mb_skeleton")
                continue
            stats.lines_in += 1
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                stats.drop("parse_error")
                continue
            if rec.get("kind") != "event":
                stats.drop("non_event_line")
                continue
            ev_name = rec.get("event", "")
            ts = rec.get("ts") or _ms_to_iso(None)
            ref = f"dsh/{path.name}#{ev_name}@{ts}"  # 稳定锚点，不含行号（R1-1）
            args = rec.get("args") or []
            a0 = args[0] if args else {}
            agent_id = _d(a0.get("agent", {}).get("id")) if isinstance(a0, dict) else None
            try:
                events += _map_dsh_event(
                    ev_name, args, a0, agent_id, ts, run_id, ref, stats,
                    seen_ledger, msg_obs, seen_subagent, scrub_home)
            except Exception as exc:  # 单事件解析失败不中断整批
                stats.drop(f"dsh_map_error:{type(exc).__name__}")
    return events


def _map_dsh_event(
    ev_name: str, args: list, a0: dict, agent_id: str | None, ts: str, run_id: str,
    ref: str, stats: Stats, seen_ledger: set, msg_obs: dict, seen_subagent: set,
    scrub_home: str | None = None,
) -> list[dict]:
    out: list[dict] = []
    base = dict(platform="dsh", channel="sdk", labels={"sub_channel": "D"})

    def mk(ev_type: str, session_id: str, payload: dict, **kw) -> None:
        out.append(_mk_event(run_id=run_id, ts=kw.get("ts_override") or ts,
                             session_id=session_id, ev_type=ev_type,
                             payload=payload, agent_id=kw.get("agent_id") or agent_id,
                             agent_role=kw.get("agent_role"), parent_agent_id=kw.get("parent_agent_id"),
                             stable_key=kw.get("stable_key") or (ev_name, str(agent_id), ts),
                             raw_ref=kw.get("raw_ref_override") or ref,
                             scrub_home=scrub_home, **base))
        stats.ok(ev_type)

    if ev_name == "session/event":
        header = a0.get("header", {})
        sid = _d(header.get("id"))
        for it in a0.get("log", {}).get("items", []):
            item = _ledger_item(it)
            if not item:
                stats.drop("ledger_item_unparseable")
                continue
            seq = item.get("seq")
            itype = item.get("type", "")
            key = (sid, seq)
            if key in seen_ledger:
                stats.drop("ledger_dup_session_seq")
                continue
            seen_ledger.add(key)
            it_ts = _ms_to_iso(item.get("time")) if item.get("time") else ts
            if itype in _MSG_LEDGER_TYPES:
                # raw_ref 锚到 (sid,seq)：同一账本 item 出现在多份快照行中，行级 ts 各异，
                # 若随首见行漂移会破坏整文件字节确定性（R1-1）
                mk(_MSG_LEDGER_TYPES[itype], sid, {"ledger_seq": seq, "data": item.get("data")},
                   agent_id=sid, ts_override=it_ts, stable_key=(str(sid), seq),
                   raw_ref_override=ref.split("@")[0] + f"@ledger={sid}:{seq}")
            elif itype == "step/start":
                # 账本 step/start 与 agent/pre-step 首步重叠，step.start 统一以 pre-step 为源
                stats.drop("ledger_step_start_superseded_by_pre_step")
            elif itype == "agent/inbox/spliced":
                stats.drop("ledger_spliced_superseded_by_inbox")
            else:
                stats.drop(f"ledger_unmapped_type")
        return out

    if ev_name == "session/created":
        header = a0.get("header", {})
        sid = _d(header.get("id"))
        mk("session.start", sid, {"created_at": _ms_to_iso(_d(header.get("createdAt"))),
                                  "cwd": _d(header.get("cwd"))}, agent_id=sid)
        return out

    if ev_name == "agent/created":
        agent = a0.get("agent", {})
        aid = _d(agent.get("id"))
        header = agent.get("session", {}).get("header", {})
        parent = _d(header.get("parentSession"))
        opts = _dobj(agent.get("options", {}), ("provider", "model"))
        role = "lead" if (parent is None and str(aid).startswith("session-")) else "subagent"
        mk("agent.spawn", str(parent or aid), {"provider": opts.get("provider"), "model": opts.get("model")},
           agent_id=str(aid), agent_role=role, parent_agent_id=parent)
        return out

    if ev_name in ("subagent/start", "subagent/end"):
        runid = _d(a0.get("runId"))
        key = (ev_name, str(runid))
        cid = _d(a0.get("id"))
        payload = {"stop_reason": _d(a0.get("stopReason")),
                   "run_id": runid,
                   "last_message": _content_hash(json.dumps(a0.get("lastAssistantMessage"), ensure_ascii=False)
                                                 if not isinstance(a0.get("lastAssistantMessage"), str)
                                                 else a0.get("lastAssistantMessage"))}
        if key in seen_subagent:  # 直挂+镜像双计去重（T04c：4/4 → 2 次派生）
            stats.drop("dsh_direct_dispatch_dup")
            # 确定性取舍（R1-2）：直挂/镜像两行 ts 可能差 2ms，取最小 ts 者为代表，禁止先到先得
            prev = seen_subagent[key]
            if prev and _iso_to_ms(ts) < _iso_to_ms(prev["event"]["ts"]):
                ev = prev["event"]
                ev["ts"] = ts
                ev["raw_ref"] = ref
                ev["payload"] = payload
                ev["event_id"] = _compute_event_id(ev)
            return out
        if ev_name == "subagent/start":
            seen_subagent[key] = None  # start 冗余，仅占位去重
            stats.drop("subagent_start_redundant_with_agent_created")
            return out
        mk("agent.stop", str(cid), payload, agent_id=str(cid), agent_role="subagent",
           stable_key=("subagent_end", str(runid)))  # 锚 runid，不含 ts（直挂/镜像 ts 互补漂移）
        seen_subagent[key] = {"event": out[-1]}
        return out

    if ev_name == "tools/execute":
        call_id = _d(a0.get("callId"))
        name = _d(a0.get("name")) or "unknown"
        root_call = _d(a0.get("rootCallId"))
        arguments = a0.get("arguments")
        args_hash = _content_hash(arguments if isinstance(arguments, str)
                                  else json.dumps(arguments, ensure_ascii=False))
        mk("tool.call", str(agent_id), {"tool_name": name, "call_id": call_id,
                                        "root_call_id": root_call, **args_hash},
           agent_id=agent_id)
        if name == "send_message":  # dsh 无独立消息事件：send_message 工具调用即 message.send
            mk("message.send", str(agent_id), {"call_id": call_id, "tool_name": name, **args_hash},
               agent_id=agent_id)
        elif name in ("write", "edit"):
            mk("artifact.write", str(agent_id), {"tool_name": name, "call_id": call_id,
                                                 "derived_from": "tools/execute"}, agent_id=agent_id)
        elif name in ("read", "glob", "grep", "read_image"):
            mk("artifact.ref", str(agent_id), {"tool_name": name, "call_id": call_id,
                                               "derived_from": "tools/execute"}, agent_id=agent_id)
        return out

    if ev_name == "tools/result":
        call_id = _d(a0.get("callId"))
        name = _d(a0.get("name")) or "unknown"
        a1 = args[1] if len(args) > 1 else {}
        is_err = _d(a1.get("isError")) is True
        result_hash = _content_hash(a1 if isinstance(a1, str) else json.dumps(a1, ensure_ascii=False))
        ev_type = "tool.error" if is_err else "tool.result"
        mk(ev_type, str(agent_id), {"tool_name": name, "call_id": call_id, "is_error": is_err,
                                    **result_hash}, agent_id=agent_id)
        return out

    if ev_name == "llm/stream":
        sid = _d(a0.get("sessionId")) or agent_id
        mk("llm.request", str(sid), {"provider": _d(a0.get("provider")), "model": _d(a0.get("model")),
                                     "max_tokens": _d(a0.get("maxTokens")),
                                     "messages_len": (a0.get("messages") or {}).get("len")},
           agent_id=str(sid))
        return out

    if ev_name == "agent/pre-step":
        turn = _d(a0.get("turn"))
        step = _d(a0.get("step"))
        msgs = a0.get("messages") or {}
        mk("step.start", str(agent_id), {"turn": turn, "step": step,
                                         "messages_len": msgs.get("len")}, agent_id=agent_id)
        return out

    if ev_name in ("agent/inbox/inserted", "agent/inbox/claimed"):
        msg = a0.get("message", {})
        mid = _d(msg.get("id"))
        via = ev_name.removeprefix("agent/inbox/")
        content = msg.get("content")
        csha = _content_hash(json.dumps(content, ensure_ascii=False)
                             if not isinstance(content, str) else content)
        if not mid:
            # 无 message.id 无法归并：单条直出（stable_key 含 ts，仍顺序无关）
            mk("message.recv", str(agent_id), {"message_id": None, "role": _d(msg.get("role")),
                                               "first_via": via, "via_set": [via], **csha},
               agent_id=agent_id, stable_key=(str(agent_id), "message.recv", ts))
            return out
        entry = msg_obs.get(str(mid))
        if entry is None:
            ev = _mk_event(run_id=run_id, ts=ts, session_id=str(agent_id), ev_type="message.recv",
                           platform="dsh", channel="sdk", raw_ref=ref,
                           labels={"sub_channel": "D"},
                           payload={"message_id": mid, "role": _d(msg.get("role")),
                                    "first_via": via, "via_set": [via], **csha},
                           agent_id=agent_id, stable_key=("inbox", str(mid)),
                           scrub_home=scrub_home)
            stats.ok("message.recv")
            msg_obs[str(mid)] = {"event": ev, "csha": csha.get("content_sha256"),
                                 "obs": [{"via": via, "ts": ts, "agent_id": agent_id, "ref": ref}]}
            out.append(ev)
            return out
        if entry["csha"] != csha.get("content_sha256"):
            # 同 message.id 内容冲突：不静默取舍，记 error 事件 + 冲突计数（R1-2 §2）
            stats.drop("inbox_message_content_conflict")
            mk("error", str(agent_id),
               {"kind": "inbox_message_content_conflict", "message_id": mid,
                "observed_via": via, "content_sha256": csha.get("content_sha256"),
                "previous_sha256": entry["csha"]}, agent_id=agent_id)
            return out
        entry["obs"].append({"via": via, "ts": ts, "agent_id": agent_id, "ref": ref})
        stats.drop("inbox_message_obs_merged")  # 折入既有事件（闭合 units_expanded 口径）
        _merge_msg_obs(entry)
        return out

    if ev_name == "agent/status":
        stats.drop("status_heartbeat")
        return out
    if ev_name == "agent/assistant-stream":
        stats.drop("assistant_stream_frame")  # 令牌帧，T06 聚合，不入事件流
        return out
    if ev_name.startswith("subagent/provider-"):
        stats.drop("provider_registration_noise")
        return out
    stats.drop(f"unhandled_event_type")
    return out


# ---------------------------------------------------------------------------
# 汇总：合并 → 去重 → seq 赋号 → 落盘
# ---------------------------------------------------------------------------

def _cross_channel_redundancy(events: list[dict]) -> dict:
    """merge 模式下统计 A/B 双源事件的重复度（R1-3）。

    配对规则：同 type + 同 tool_name/span_name + 时间近邻 ≤200ms。
    T03 已证实 traceparent ≠ traceId、traceId ≠ sessionId，无键级归并可能，
    只能时间对齐，且对齐精度实测 ≤200ms 而非精确相等。
    """
    dual = ("tool.call", "tool.result", "tool.error", "step.start", "step.end",
            "llm.request", "llm.response", "artifact.write", "artifact.ref")
    by: dict[str, dict[str, list]] = {}
    for ev in events:
        sub = (ev.get("labels") or {}).get("sub_channel")
        if sub not in ("A", "B") or ev["type"] not in dual:
            continue
        key = ev["payload"].get("tool_name") or ev["payload"].get("span_name") or ""
        by.setdefault(ev["type"], {}).setdefault(sub, []).append(
            (_iso_to_ms(ev["ts"]), key))
    out: dict[str, dict] = {}
    for t in dual:
        a = by.get(t, {}).get("A", [])
        b = by.get(t, {}).get("B", [])
        if not a and not b:
            continue
        overlap = 0
        for tms, key in a:
            if any(abs(tms - bms) <= 200 and key == bkey for bms, bkey in b):
                overlap += 1
        out[t] = {"A": len(a), "B": len(b), "time_neighbor_overlap_le_200ms": overlap}
    return out


def collect(
    *,
    run_id: str,
    hook_spool: Path | None = None,
    codebuddy_home: Path | None = None,
    dsh_events: Path | None = None,
    out_dir: Path,
    scrub_home: str | None = None,
    channel_policy: str = "primary_only",
) -> dict:
    """主入口：摄入多源 → 统一 NDJSON + 统计。返回统计 dict。

    channel_policy（R1-3）：primary_only（默认）| merge。merge 仅用于映射覆盖
    测绘，不得直接作为 T06 指标口径（A/B 双源重复近一倍，见 docs/channel-priority.md）。
    """
    if channel_policy not in ("primary_only", "merge"):
        raise ValueError(f"invalid channel_policy: {channel_policy!r}")
    all_events: list[dict] = []
    channel_stats: list[Stats] = []
    scrub = scrub_home or str(Path.home())

    if hook_spool is not None:
        st = Stats("hook")
        all_events += load_hook_spool(hook_spool, run_id, st, scrub)
        channel_stats.append(st)
    if codebuddy_home is not None:
        st = Stats("file")
        all_events += load_codebuddy_files(codebuddy_home, run_id, st, scrub,
                                           channel_policy=channel_policy)
        channel_stats.append(st)
    if dsh_events is not None:
        st = Stats("dsh")
        all_events += load_dsh_events(dsh_events, run_id, st, scrub)
        channel_stats.append(st)

    # 全局去重：确定性 event_id 相同即同一事件（重复摄入 / 多源重复均收敛）
    by_id: dict[str, dict] = {}
    dup_dropped = 0
    for ev in all_events:
        if ev["event_id"] in by_id:
            dup_dropped += 1
        else:
            by_id[ev["event_id"]] = ev
    events = assign_seq(list(by_id.values()))

    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "events.normalized.ndjson"
    with out_file.open("w", encoding="utf-8", newline="\n") as fh:
        for ev in events:
            ev.pop("_stable_key", None)  # 内部稳定标识不写入产物（schema additionalProperties:false）
            fh.write(json.dumps(ev, ensure_ascii=False, sort_keys=False) + "\n")

    totals: Counter[str] = Counter()
    total_dropped: Counter[str] = Counter()
    for st in channel_stats:
        totals.update(st.type_counts)
        total_dropped.update(st.drop_reasons)

    mapped = sum(totals.values())
    dropped = sum(total_dropped.values())
    result = {
        "run_id": run_id,
        "output": str(out_file),
        "channel_policy": channel_policy,
        # 双口径（R1-4）：lines_in = 原始行数；units_expanded = 展开单元数 = mapped + dropped。
        # 两者不可加和（账本快照/派生事件一行展开多单元）。
        "lines_in": sum(st.lines_in for st in channel_stats),
        "units_expanded": mapped + dropped,
        "mapped": mapped,
        "dedup_dropped": dup_dropped,
        "events_out": len(events),
        "dropped": dropped,
        "closure": "mapped + dropped == units_expanded",
        "type_counts": dict(sorted(totals.items())),
        "drop_reasons": dict(sorted(total_dropped.items())),
        "channels": [st.as_dict() for st in channel_stats],
        "seq_scope": "run",
        "deterministic": True,
    }
    if channel_policy == "merge":
        result["cross_channel_redundancy"] = _cross_channel_redundancy(all_events)
    return result


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="T05a 最小摄入管线（统一 TraceEvent NDJSON）")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--hook-spool", type=Path, default=None, help="hook spool 目录或文件")
    ap.add_argument("--codebuddy-home", type=Path, default=None, help="~/.codebuddy 根（通道 A/B/C）")
    ap.add_argument("--dsh-events", type=Path, default=None, help="dsh 事件 NDJSON（T04c 信封格式）")
    ap.add_argument("--out", type=Path, required=True, help="输出目录（events.normalized.ndjson）")
    ap.add_argument("--scrub-home", type=str, default=None, help="脱敏替换的 home 前缀（默认本机 home）")
    ap.add_argument("--channel-policy", choices=("primary_only", "merge"), default="primary_only",
                    help="通道归并策略（R1-3）：primary_only=双源事件取主源；merge=全量并入仅测绘用")
    args = ap.parse_args(argv)

    result = collect(
        run_id=args.run_id, hook_spool=args.hook_spool, codebuddy_home=args.codebuddy_home,
        dsh_events=args.dsh_events, out_dir=args.out, scrub_home=args.scrub_home,
        channel_policy=args.channel_policy)
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

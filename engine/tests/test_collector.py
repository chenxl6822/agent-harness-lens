"""T05a collector 单测：幂等重放、去重、seq 确定性、映射正确性。

运行（必须带 --basetemp，本机默认 tmp 在 D: 盘被沙箱拒绝，否则 6 例误报 ERROR）：
.venv/Scripts/python.exe -m pytest engine/tests/test_collector.py -q --basetemp=.tmp-pytest
"""

from __future__ import annotations

import hashlib
import json
import random
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "engine"))

from hs_core.collector import (  # noqa: E402
    EVENT_TYPES,
    assign_seq,
    collect,
    load_dsh_events,
    load_hook_spool,
    make_event_id,
)

RUN_ID = "test-t05a"
DSH_SAMPLE = REPO_ROOT / "probe" / "rc3c" / "raw" / "e1c-events.ndjson"
HOOK_SPOOL = REPO_ROOT / "probe" / "t05a" / "work" / "spool2"


# ---------------------------------------------------------------------------
# 纯函数层
# ---------------------------------------------------------------------------

def _mk(ts: str, kind: str, n: int = 0) -> dict:
    body = {"schema_version": "1.0.0", "run_id": RUN_ID, "ts": ts,
            "session_id": f"s{n}", "type": "tool.call",
            "source": {"platform": "generic", "channel": "sdk"},
            "labels": {}, "payload": {"kind": kind}}
    return {**body, "seq": 0, "event_id": make_event_id(ts, json.dumps(body, sort_keys=True))}


def test_event_id_is_deterministic_ulid():
    import re

    pat = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
    a = make_event_id("2026-10-25T10:00:00.000Z", "x")
    b = make_event_id("2026-10-25T10:00:00.000Z", "x")
    c = make_event_id("2026-10-25T10:00:00.000Z", "y")
    assert a == b and a != c
    assert pat.match(a), a


def test_assign_seq_is_order_independent():
    """G4（纯函数层）：乱序输入 3 次，输出 (seq, event_id) 序列一致。

    注意：本例只覆盖 assign_seq 纯函数；端到端管线性质由
    test_pipeline_is_order_independent 覆盖（R1-2 重写，勿再混滑口径）。
    """
    events = [_mk(f"2026-10-25T10:00:{i:02d}.000Z", f"k{i}") for i in range(50)]
    events += [_mk("2026-10-25T10:00:10.000Z", f"tie{i}") for i in range(5)]  # 同 ts 组
    hashes = set()
    for trial in range(3):
        shuf = events[:]
        random.Random(trial).shuffle(shuf)
        out = assign_seq([dict(e) for e in shuf])
        assert [e["seq"] for e in out] == list(range(len(out)))
        hashes.add(hashlib.sha256(
            json.dumps([(e["seq"], e["event_id"]) for e in out]).encode()).hexdigest())
    assert len(hashes) == 1


def test_assign_seq_does_not_mutate_input_order_semantics():
    e1 = [_mk(f"2026-10-25T10:00:{i:02d}.000Z", f"k{i}") for i in range(10)]
    e2 = [dict(x) for x in e1]
    random.Random(7).shuffle(e2)
    o1 = assign_seq(e1)
    o2 = assign_seq(e2)
    assert [x["event_id"] for x in o1] == [x["event_id"] for x in o2]
    assert [x["seq"] for x in o1] == [x["seq"] for x in o2]


# ---------------------------------------------------------------------------
# 去重 / 幂等
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not DSH_SAMPLE.exists(), reason="需要 T04c dsh 样本")
def test_dsh_ledger_dedup():
    """G5：session/event 账本快照按 (session.id, item.seq) 去重，6070 → 24。"""
    stats = type("S", (), {})()
    from hs_core.collector import Stats

    stats = Stats("dsh")
    events = load_dsh_events(DSH_SAMPLE, RUN_ID, stats)
    d = stats.as_dict()
    # 6070 - 24 = 6046 条重复被丢
    assert d["drop_reasons"].get("ledger_dup_session_seq") == 6046
    assert d["drop_reasons"].get("ledger_dup_session_seq", 0) + 24 <= 6070
    # 去重后 turn.start 每 agent 1 条
    assert sum(1 for e in events if e["type"] == "turn.start") == 3


@pytest.mark.skipif(not DSH_SAMPLE.exists(), reason="需要 T04c dsh 样本")
def test_dsh_direct_dispatch_dedup(tmp_path):
    """直挂+镜像双计：(event, runId) 去重后 subagent/start|end 各只剩唯一 runId 数。"""
    from hs_core.collector import Stats

    stats = Stats("dsh")
    events = load_dsh_events(DSH_SAMPLE, RUN_ID, stats)
    d = stats.as_dict()
    assert d["drop_reasons"].get("dsh_direct_dispatch_dup") == 4
    stops = [e for e in events if e["type"] == "agent.stop"]
    assert len({e["agent_id"] for e in stops}) == len(stops)  # 无重复 agent


def _collect_dsh(out: Path) -> str:
    collect(run_id=RUN_ID, dsh_events=DSH_SAMPLE, out_dir=out,
            scrub_home="C:/Users/<USER>")
    return (out / "events.normalized.ndjson").read_bytes()


@pytest.mark.skipif(not DSH_SAMPLE.exists(), reason="需要 T04c dsh 样本")
def test_idempotent_replay(tmp_path):
    """G3：同一输入重复摄入两次，输出 NDJSON sha256 一致。"""
    a = _collect_dsh(tmp_path / "run1")
    b = _collect_dsh(tmp_path / "run2")
    assert hashlib.sha256(a).hexdigest() == hashlib.sha256(b).hexdigest()


@pytest.mark.skipif(not DSH_SAMPLE.exists(), reason="需要 T04c dsh 样本")
def test_collect_dedups_across_double_ingest(tmp_path):
    """同一文件解析两次（模拟重复摄入同一 spool），确定性 event_id 完全对折。"""
    from hs_core.collector import Stats

    st = Stats("dsh")
    evs = load_dsh_events(DSH_SAMPLE, RUN_ID, st) + load_dsh_events(DSH_SAMPLE, RUN_ID, st)
    ids = {e["event_id"] for e in evs}
    assert len(ids) == len(evs) // 2  # 完全对折 → 幂等


# ---------------------------------------------------------------------------
# hook spool 与 schema 校验
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not HOOK_SPOOL.exists(), reason="需要 hook 重放 spool")
def test_hook_spool_loads_and_rebinds():
    from hs_core.collector import Stats

    stats = Stats("hook")
    events = load_hook_spool(HOOK_SPOOL, RUN_ID, stats)
    assert events
    for e in events:
        assert e["run_id"] == RUN_ID
        assert e["type"] in EVENT_TYPES
        assert e["source"]["channel"] == "hook"


@pytest.mark.skipif(not DSH_SAMPLE.exists(), reason="需要 T04c dsh 样本")
def test_output_passes_trace_event_schema(tmp_path):
    from hs_core.schema import load_schema
    from jsonschema import Draft202012Validator

    _collect_dsh(tmp_path)
    v = Draft202012Validator(load_schema("trace-event"))
    for line in (tmp_path / "events.normalized.ndjson").read_text(encoding="utf-8").splitlines():
        errs = list(v.iter_errors(json.loads(line)))
        assert not errs, errs[0].message[:200]


def test_collect_scrubs_home_in_payload(tmp_path):
    dsh = tmp_path / "mini.ndjson"
    line = {
        "ts": "2026-10-25T10:00:00.000Z", "kind": "event", "via": "dispatch",
        "event": "session/created",
        "args": [{"t": "object", "header": {"t": "object", "keys": ["id", "createdAt", "cwd"],
                                            "id": {"t": "string", "preview": "session-x", "len": 9},
                                            "createdAt": {"t": "number", "preview": "1792940393608", "len": 13},
                                            "cwd": {"t": "string", "preview": "C:/Users/testuser/secret/proj", "len": 27}}}],
    }
    dsh.write_text(json.dumps(line) + "\n", encoding="utf-8")
    collect(run_id=RUN_ID, dsh_events=dsh, out_dir=tmp_path / "out",
            scrub_home="C:/Users/testuser")
    text = (tmp_path / "out" / "events.normalized.ndjson").read_text(encoding="utf-8")
    assert "testuser" not in text
    assert "<WINHOME>" in text


def test_invalid_lines_dropped_not_fatal(tmp_path):
    dsh = tmp_path / "bad.ndjson"
    dsh.write_text("not-json\n{\"kind\":\"self\"}\n", encoding="utf-8")
    from hs_core.collector import Stats

    st = Stats("dsh")
    load_dsh_events(dsh, RUN_ID, st)
    d = st.as_dict()
    assert d["drop_reasons"].get("parse_error") == 1
    assert d["drop_reasons"].get("non_event_line") == 1


# ---------------------------------------------------------------------------
# R1 返工新增：event_id 稳定化 + 端到端顺序无关
# ---------------------------------------------------------------------------

def test_event_id_excludes_raw_ref():
    """R1-1：raw_ref（含行号）不得参与 event_id 摘要；稳定标识相同 → id 相同。"""
    from hs_core.collector import _mk_event

    common = dict(run_id=RUN_ID, ts="2026-10-25T10:00:00.000Z", session_id="s1",
                  ev_type="tool.call", platform="codebuddy", channel="file",
                  payload={"tool_name": "read"})
    a = _mk_event(**common, raw_ref="traces/1/trace_1.jsonl#span=span-1",
                  stable_key=("s1", "span-1", "span_start"))
    b = _mk_event(**common, raw_ref="traces/1/trace_1.jsonl@17",
                  stable_key=("s1", "span-1", "span_start"))
    assert a["event_id"] == b["event_id"]
    # 白名单摘要：不同 stable_key 必不同 id
    c = _mk_event(**common, raw_ref=a["raw_ref"], stable_key=("s1", "span-2", "span_start"))
    assert c["event_id"] != a["event_id"]


def _mini_dsh(path: Path) -> None:
    lines = [
        {"ts": "2026-10-25T10:00:00.000Z", "kind": "event", "event": "session/created",
         "args": [{"t": "object", "header": {"t": "object", "keys": ["id", "createdAt", "cwd"],
                                             "id": {"t": "string", "preview": "session-x", "len": 9},
                                             "createdAt": {"t": "number", "preview": "1792940393608", "len": 13},
                                             "cwd": {"t": "string", "preview": "/tmp/proj", "len": 9}}}]},
        {"ts": "2026-10-25T10:00:01.000Z", "kind": "event", "event": "tools/execute",
         "args": [{"t": "object", "keys": ["agent", "callId", "name"],
                   "agent": {"t": "object", "keys": ["id"], "id": {"t": "string", "preview": "session-x", "len": 9}},
                   "callId": {"t": "string", "preview": "call-1", "len": 6},
                   "name": {"t": "string", "preview": "read", "len": 4}}]},
    ]
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")


def test_event_id_stable_across_append(tmp_path):
    """R1-1 回归门禁：原文件尾部追加无关新行后重新摄入，原事件 event_id 必须不变。"""
    dsh = tmp_path / "mini.ndjson"
    _mini_dsh(dsh)
    scrub = "C:/Users/<USER>"
    collect(run_id=RUN_ID, dsh_events=dsh, out_dir=tmp_path / "out1", scrub_home=scrub)
    before = [json.loads(x)["event_id"]
              for x in (tmp_path / "out1" / "events.normalized.ndjson").read_text(encoding="utf-8").splitlines()]
    with dsh.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": "2026-10-25T10:00:02.000Z", "kind": "event",
                             "event": "tools/execute",
                             "args": [{"t": "object", "keys": ["agent", "callId", "name"],
                                       "agent": {"t": "object", "keys": ["id"],
                                                 "id": {"t": "string", "preview": "session-x", "len": 9}},
                                       "callId": {"t": "string", "preview": "call-2", "len": 6},
                                       "name": {"t": "string", "preview": "glob", "len": 4}}]}) + "\n")
    collect(run_id=RUN_ID, dsh_events=dsh, out_dir=tmp_path / "out2", scrub_home=scrub)
    after = [json.loads(x)["event_id"]
             for x in (tmp_path / "out2" / "events.normalized.ndjson").read_text(encoding="utf-8").splitlines()]
    assert set(before) <= set(after) and len(after) > len(before)  # 原 event_id 全部不变


@pytest.mark.skipif(not DSH_SAMPLE.exists(), reason="需要 T04c dsh 样本")
def test_pipeline_is_order_independent(tmp_path):
    """R1-2 G4 重写：真实数据乱序 3 次（seed 1/2/3）跑完整 collect()，整文件 sha256 全等。

    本例覆盖端到端摄入管线（含 raw_ref/stable_key/via 归并），非仅 assign_seq 纯函数。
    输出哈希与 docs/seq-assignment.md、probe/t05a/result.json 三处一致。
    """
    lines = DSH_SAMPLE.read_text(encoding="utf-8").splitlines()
    hashes = []
    for trial, seed in enumerate((1, 2, 3)):
        shuffled = lines[:]
        random.Random(seed).shuffle(shuffled)
        p = tmp_path / "shuffled.ndjson"  # 同名文件：raw_ref 含文件名，须排除命名差异
        p.write_text("\n".join(shuffled) + "\n", encoding="utf-8")
        collect(run_id=RUN_ID, dsh_events=p, out_dir=tmp_path / f"out{trial}",
                scrub_home="C:/Users/<USER>")
        h = hashlib.sha256((tmp_path / f"out{trial}" / "events.normalized.ndjson")
                           .read_bytes()).hexdigest()
        hashes.append(h)
    assert len(set(hashes)) == 1, hashes
    print(f"PIPELINE_SHA256={hashes[0]}")

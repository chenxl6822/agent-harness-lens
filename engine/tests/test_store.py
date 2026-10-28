"""T05b store 单测：E3 幂等四场景 + E5 一致性自检 + 策略门禁。

运行（必须带 --basetemp，本机默认 tmp 在 D: 盘被沙箱拒绝）：
.venv/Scripts/python.exe -m pytest engine/tests/test_store.py -q --basetemp=.tmp-pytest

测试用临时 DB（pytest tmp_path，位于 .tmp-pytest 下），结束即随 basetemp 清理。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "engine"))

from hs_core.collector import collect  # noqa: E402
from hs_core.store import SCHEMA_USER_VERSION, PolicyError, Store, digest_files  # noqa: E402

RUN_A = "t05b-run-a"
RUN_B = "t05b-run-b"
DSH_SAMPLE = REPO_ROOT / "probe" / "rc3c" / "raw" / "e1c-events.ndjson"
SCRUB = "C:/Users/<USER>"


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _collect(run_id: str, dsh: Path, out: Path, policy: str = "primary_only") -> list[dict]:
    collect(run_id=run_id, dsh_events=dsh, out_dir=out, scrub_home=SCRUB,
            channel_policy=policy)
    with (out / "events.normalized.ndjson").open("r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _dump_sha(store: Store, run_id: str) -> str:
    return hashlib.sha256(store.dump_run(run_id).encode("utf-8")).hexdigest()


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "test.db"
    yield path
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(path) + suffix)
        if p.exists():
            p.unlink()


def _mini_dsh(path: Path, *, extra_mid: bool = False) -> None:
    """两行迷你 dsh 源。extra_mid=True 追加一条 ts 落在两条既有事件中间的新行。"""
    lines = [
        {"ts": "2026-10-25T10:00:00.000Z", "kind": "event", "event": "session/created",
         "args": [{"t": "object", "header": {"t": "object", "keys": ["id", "createdAt"],
                                             "id": {"t": "string", "preview": "session-x", "len": 9},
                                             "createdAt": {"t": "number", "preview": "1792940393608", "len": 13}}}]},
        {"ts": "2026-10-25T10:00:10.000Z", "kind": "event", "event": "tools/execute",
         "args": [{"t": "object", "keys": ["agent", "callId", "name"],
                   "agent": {"t": "object", "keys": ["id"], "id": {"t": "string", "preview": "session-x", "len": 9}},
                   "callId": {"t": "string", "preview": "call-2", "len": 6},
                   "name": {"t": "string", "preview": "glob", "len": 4}}]},
    ]
    if extra_mid:
        lines.insert(1, {"ts": "2026-10-25T10:00:05.000Z", "kind": "event", "event": "tools/execute",
                         "args": [{"t": "object", "keys": ["agent", "callId", "name"],
                                   "agent": {"t": "object", "keys": ["id"], "id": {"t": "string", "preview": "session-x", "len": 9}},
                                   "callId": {"t": "string", "preview": "call-1", "len": 6},
                                   "name": {"t": "string", "preview": "read", "len": 4}}]})
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# E3-1 同输入两次摄入
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not DSH_SAMPLE.exists(), reason="需要 T04c dsh 样本")
def test_e3_1_same_input_twice(db, tmp_path):
    events = _collect(RUN_A, DSH_SAMPLE, tmp_path / "out")
    with Store(db) as s:
        r1 = s.ingest(RUN_A, events, source_digest=digest_files([DSH_SAMPLE]))
        rows1 = s.count_events(RUN_A)
        sha1 = _dump_sha(s, RUN_A)
        r2 = s.ingest(RUN_A, events, source_digest=digest_files([DSH_SAMPLE]))
        rows2 = s.count_events(RUN_A)
        sha2 = _dump_sha(s, RUN_A)
    assert r1.inserted == len(events)
    assert r2.inserted == 0 and r2.skipped == len(events)
    assert rows1 == rows2 == len(events)
    assert sha1 == sha2


def test_e3_1_mini_twice(db, tmp_path):
    dsh = tmp_path / "mini.ndjson"
    _mini_dsh(dsh)
    events = _collect(RUN_A, dsh, tmp_path / "out")
    with Store(db) as s:
        s.ingest(RUN_A, events)
        rows1, sha1 = s.count_events(RUN_A), _dump_sha(s, RUN_A)
        r2 = s.ingest(RUN_A, events)
        rows2, sha2 = s.count_events(RUN_A), _dump_sha(s, RUN_A)
    assert r2.inserted == 0 and rows1 == rows2 == len(events) and sha1 == sha2


# ---------------------------------------------------------------------------
# E3-2 增量摄入（方案 A：中间插入新事件 → 全量 seq 重算，dump 与一次性全量一致）
# ---------------------------------------------------------------------------

def test_e3_2_incremental_mid_insert_matches_one_shot(db, tmp_path):
    dsh = tmp_path / "mini.ndjson"
    _mini_dsh(dsh)
    events_before = _collect(RUN_A, dsh, tmp_path / "out1")
    with Store(db) as s:
        r1 = s.ingest(RUN_A, events_before)
        before = s.count_events(RUN_A)
        sha_before = _dump_sha(s, RUN_A)
    assert r1.inserted == 3  # 2 行源 → session.start + tool.call + artifact.ref(read 派生)

    # 尾部追加一条 ts 落在既有区间中间的行（正是任务卡 §2 警告的 seq 冲突场景）
    _mini_dsh(dsh, extra_mid=True)
    events_after = _collect(RUN_A, dsh, tmp_path / "out2")
    with Store(db) as s:
        r2 = s. ingest(RUN_A, events_after)
        after = s.count_events(RUN_A)
        sha_incr = _dump_sha(s, RUN_A)
        seqs = [e["seq"] for e in s.iter_events(RUN_A)]
    assert before == 3 and after == 5  # 新行 read → tool.call + artifact.ref 两条新事件
    assert r2.inserted == 2 and r2.skipped == 3
    assert r2.updated >= 1  # 既有行仅 seq 变更（方案 A 预期行为，result.json 已声明）
    assert seqs == [0, 1, 2, 3, 4]  # 全量重算后 seq 连续无洞
    # 方案 A 核心承诺：增量摄入 == 一次性摄入全量，逐字节一致
    with Store(tmp_path / "oneshot.db") as s2:
        s2.ingest(RUN_A, events_after)
        sha_oneshot = _dump_sha(s2, RUN_A)
    assert sha_incr == sha_oneshot
    assert sha_incr != sha_before  # 新事件入库 + seq 重算，dump 必然变化


def test_e3_2_existing_rows_content_unchanged(db, tmp_path):
    """增量摄入后既有行除 seq 外逐列不变（G3）。"""
    dsh = tmp_path / "mini.ndjson"
    _mini_dsh(dsh)
    with Store(db) as s:
        s.ingest(RUN_A, _collect(RUN_A, dsh, tmp_path / "out1"))
        rows_before = {e["event_id"]: e for e in s.iter_events(RUN_A)}
        _mini_dsh(dsh, extra_mid=True)
        s.ingest(RUN_A, _collect(RUN_A, dsh, tmp_path / "out2"))
        for ev in s.iter_events(RUN_A):
            old = rows_before.get(ev["event_id"])
            if old is None:
                continue
            assert {k: v for k, v in old.items() if k != "seq"} == \
                   {k: v for k, v in ev.items() if k != "seq"}


# ---------------------------------------------------------------------------
# E3-3 不同 run_id 摄入同一份源（复合主键隔离）
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not DSH_SAMPLE.exists(), reason="需要 T04c dsh 样本")
def test_e3_3_two_runs_same_source_isolated(db, tmp_path):
    events_a = _collect(RUN_A, DSH_SAMPLE, tmp_path / "outa")
    events_b = _collect(RUN_B, DSH_SAMPLE, tmp_path / "outb")
    assert {e["event_id"] for e in events_a} == {e["event_id"] for e in events_b}  # 同源同 event_id
    with Store(db) as s:
        s.ingest(RUN_A, events_a)
        s.ingest(RUN_B, events_b)
        assert s.count_events(RUN_A) == len(events_a)
        assert s.count_events(RUN_B) == len(events_b)
        # 两 run dump 仅 run_id 字段不同值（同源同 event_id，seq 序一致）
        assert _dump_sha(s, RUN_A) != _dump_sha(s, RUN_B)
        assert s.dump_run(RUN_A).replace(RUN_A, RUN_B) == s.dump_run(RUN_B)
        # 复合主键验证：两 run 的 event_id 集合完全重叠但行独立
        ids_a = {e["event_id"] for e in s.iter_events(RUN_A)}
        ids_b = {e["event_id"] for e in s.iter_events(RUN_B)}
        assert ids_a == ids_b and len(ids_a) == len(events_a)


# ---------------------------------------------------------------------------
# E3-4 并发写：2 个进程同时 ingest 同一 run
# ---------------------------------------------------------------------------

_INGEST_SNIPPET = """
import json, os, sys
sys.path.insert(0, {engine!r})
from pathlib import Path
from hs_core.collector import collect
from hs_core.store import Store

run_id, db_path, dsh = {run_id!r}, {db!r}, Path({dsh!r})
out = Path(os.environ["WORKER_OUT"])  # 每个 worker 独立 out 目录，避免 collect 并发写同一 NDJSON
collect(run_id=run_id, dsh_events=dsh, out_dir=out, scrub_home="C:/Users/<USER>")
events = [json.loads(l) for l in (out / "events.normalized.ndjson").read_text(encoding="utf-8").splitlines() if l.strip()]
with Store(db_path) as s:
    r = s.ingest(run_id, events)
    print(json.dumps({{"inserted": r.inserted, "skipped": r.skipped, "rows": s.count_events(run_id)}}))
"""


@pytest.mark.skipif(not DSH_SAMPLE.exists(), reason="需要 T04c dsh 样本")
def test_e3_4_concurrent_ingest(db, tmp_path):
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "engine")}
    code = _INGEST_SNIPPET.format(engine=str(REPO_ROOT / "engine"), run_id=RUN_A,
                                  db=str(db), dsh=str(DSH_SAMPLE), out=str(tmp_path / "out"))
    procs = [subprocess.Popen([sys.executable, "-c", code],
                              env={**env, "WORKER_OUT": str(tmp_path / f"out{i}")},
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              encoding="utf-8")
             for i in range(2)]
    outs = []
    for p in procs:
        out, err = p.communicate(timeout=180)
        outs.append((p.returncode, out.strip(), err))
    for rc, out, err in outs:
        assert rc == 0, f"子进程失败 rc={rc}: {err[-500:]}"
    results = [json.loads(out) for _, out, _ in outs]
    total_expected = len(_collect(RUN_A, DSH_SAMPLE, tmp_path / "verify"))
    # 两个进程都成功（靠 BEGIN IMMEDIATE + busy_timeout 串行化），零重复行
    assert all(r["rows"] == total_expected for r in results)
    rows = results[-1]["rows"]
    with Store(db) as s:
        assert s.count_events(RUN_A) == rows
    # 两个进程合计 inserted == 总行数（第二个进程全部 skipped，无重复写入）
    assert sum(r["inserted"] for r in results) == rows


# ---------------------------------------------------------------------------
# T05b-R1 并发首次打开同一个新建库（迁移竞态）
# ---------------------------------------------------------------------------

_OPEN_WORKER_SNIPPET = """
import sys, time
from pathlib import Path
sys.path.insert(0, {engine!r})
from hs_core.store import Store

db, t0 = Path({db!r}), {t0!r}
while time.time() < t0:  # 墙钟对齐：4 个进程在同一时刻进入 Store()，制造真正的首次建库竞态
    pass
s = Store(db)
s.close()
"""

# 单轮 4 进程碰撞概率实测约 10%（旧实现 40 轮失败 4 轮），故单测内跑多轮以提高回归捕获率。
_RACE_ROUNDS = 10


def test_concurrent_first_open_is_race_safe(tmp_path):
    """4 个进程同时 Store(同一个新建库)：全部退出码 0，schema 只建一份，库完好。"""
    import sqlite3
    import time as _time

    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "engine")}
    for rnd in range(_RACE_ROUNDS):
        db_path = tmp_path / f"race{rnd}.db"
        t0 = _time.time() + 0.25
        code = _OPEN_WORKER_SNIPPET.format(engine=str(REPO_ROOT / "engine"),
                                           db=str(db_path), t0=t0)
        procs = [subprocess.Popen([sys.executable, "-c", code], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, encoding="utf-8")
                 for _ in range(4)]
        outs = []
        for i, p in enumerate(procs):
            out, err = p.communicate(timeout=300)
            outs.append((i, p.returncode, out, err))
        for i, rc, out, err in outs:
            assert rc == 0, f"round{rnd}/worker{i} 失败 rc={rc}: {(err or out)[-800:]}"

        conn = sqlite3.connect(str(db_path))
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_USER_VERSION
            names = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
            assert names.count("runs") == 1 and names.count("events") == 1, names
            assert len(names) == len(set(names)), f"重复表: {names}"
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 策略门禁（G4）与 runs 元数据
# ---------------------------------------------------------------------------

def test_merge_rejected_by_default(db, tmp_path):
    dsh = tmp_path / "mini.ndjson"
    _mini_dsh(dsh)
    events = _collect(RUN_A, dsh, tmp_path / "out", policy="merge")
    with Store(db) as s:
        with pytest.raises(PolicyError):
            s.ingest(RUN_A, events, channel_policy="merge")
        assert s.count_events(RUN_A) == 0  # 拒绝即零写入
        # 显式放行后可写入，且策略落库
        s.ingest(RUN_A, events, channel_policy="merge", allow_merge=True)
        assert s.get_run(RUN_A)["channel_policy"] == "merge"
    for suffix in ("", "-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)


def test_primary_only_recorded_and_invalid_policy_rejected(db, tmp_path):
    dsh = tmp_path / "mini.ndjson"
    _mini_dsh(dsh)
    events = _collect(RUN_A, dsh, tmp_path / "out")
    with Store(db) as s:
        s.ingest(RUN_A, events)
        run = s.get_run(RUN_A)
        assert run["channel_policy"] == "primary_only"
        assert run["event_count"] == len(events)
        assert run["platform"] == "dsh"
        assert run["source_digest"] is None  # 未提供 digest 时不虚报
        with pytest.raises(PolicyError):
            s.ingest(RUN_A, events, channel_policy="bogus")


# ---------------------------------------------------------------------------
# E5 一致性自检 + dump_run 逐字节可比
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not DSH_SAMPLE.exists(), reason="需要 T04c dsh 样本")
def test_e5_verify_and_dump_roundtrip(db, tmp_path):
    from jsonschema import Draft202012Validator

    from hs_core.models import TraceEvent
    from hs_core.schema import load_schema

    ndjson_path = tmp_path / "out" / "events.normalized.ndjson"
    events = _collect(RUN_A, DSH_SAMPLE, tmp_path / "out")
    with Store(db) as s:
        s.ingest(RUN_A, events, source_digest=digest_files([DSH_SAMPLE]))
        report = s.verify_run(
            RUN_A, ndjson=ndjson_path,
            schema_validator=Draft202012Validator(load_schema("trace-event")),
            model=TraceEvent)
        assert report["rows"] == report["ndjson_rows"] == len(events)
        assert report["schema_invalid"] == 0 and report["model_invalid"] == 0
        assert report["seq_dense"] and report["dup_pk"] == 0 and report["ok"]
        # dump_run 与 collect() NDJSON 逐字节可比（dsh/file 通道事件均为 _mk_event 键序）
        dumped = s.dump_run(RUN_A).splitlines()
        assert dumped == ndjson_path.read_text(encoding="utf-8").splitlines()
        # 流式读 + 过滤
        assert len(list(s.iter_events(RUN_A, types=("tool.call",)))) >= 1
        assert all(e["type"] == "tool.call" for e in s.iter_events(RUN_A, types=("tool.call",)))
        sids = {e["session_id"] for e in s.iter_events(RUN_A)}
        some_sid = sorted(sids)[0]
        assert all(e["session_id"] == some_sid
                   for e in s.iter_events(RUN_A, session_id=some_sid))


def test_iter_events_order_by_ts(db, tmp_path):
    dsh = tmp_path / "mini.ndjson"
    _mini_dsh(dsh, extra_mid=True)
    events = _collect(RUN_A, dsh, tmp_path / "out")
    with Store(db) as s:
        s.ingest(RUN_A, events)
        by_seq = [e["event_id"] for e in s.iter_events(RUN_A, order_by="seq")]
        by_ts = [e["event_id"] for e in s.iter_events(RUN_A, order_by="ts")]
        assert by_seq == by_ts  # 方案 A：seq 全序 == (ts, event_id) 时序

"""T05b：SQLite 持久化 + Repository + 幂等重放。

范围（T05b 任务卡 §3）：
- E1 DDL：events 复合主键 (run_id, event_id)、runs 元数据表、WAL、user_version=1 + 迁移骨架
- E2 Repository：幂等 ingest（方案 A 整 run 重算 seq）、流式 iter_events、dump_run
- 策略门禁：merge 流默认拒绝入库（须显式 allow_merge=True），实际策略写 runs.channel_policy

硬约束（G-T05a-R1 §4）：
1. event_id 摘要不含 run_id → 同一源在两个 run 下 event_id 相同 → 主键必须复合 (run_id, event_id)。
2. collect() 默认 channel_policy=primary_only；merge 仅测绘用，Repository 层默认禁止入库。

seq 决策（T05b 任务卡 §2，唯一决策 = 方案 A 整 run 重算，理由与实测见 docs/storage.md）：
- 每次 ingest 对 (库内既有事件 ∪ 本批新事件) 全量按 (ts_ms, event_id) 重排，
  覆盖写 seq 0..n-1（与 collector.assign_seq 同一排序键，语义完全一致）。
- 代价 O(n) UPDATE，但保证：增量摄入后的库状态 == 一次性摄入全量的库状态（含 seq），
  dump_run sha256 逐字节一致，T06 可继续用 (run_id, seq) 做时序归并。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from hs_core.collector import EVENT_TYPES, _iso_to_ms

SCHEMA_USER_VERSION = 1

# ---------------------------------------------------------------------------
# DDL（v1）与迁移骨架
# ---------------------------------------------------------------------------

_DDL_V1: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS runs (
        run_id         TEXT PRIMARY KEY,
        created_at     TEXT NOT NULL,
        platform       TEXT,
        channel_policy TEXT NOT NULL,
        source_digest  TEXT,
        event_count    INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        run_id        TEXT NOT NULL,
        event_id      TEXT NOT NULL,
        seq           INTEGER NOT NULL,
        ts            TEXT NOT NULL,
        ts_ms         INTEGER NOT NULL,
        session_id    TEXT NOT NULL,
        type          TEXT NOT NULL,
        schema_version TEXT NOT NULL,
        source_platform TEXT NOT NULL,
        source_channel  TEXT NOT NULL,
        agent_id      TEXT,
        agent_role    TEXT,
        parent_agent_id TEXT,
        raw_ref       TEXT,
        labels        TEXT NOT NULL,
        payload       TEXT NOT NULL,
        PRIMARY KEY (run_id, event_id),
        FOREIGN KEY (run_id) REFERENCES runs(run_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_events_run_seq  ON events (run_id, seq)",
    "CREATE INDEX IF NOT EXISTS idx_events_run_ts   ON events (run_id, ts)",
    "CREATE INDEX IF NOT EXISTS idx_events_run_type ON events (run_id, type)",
    "CREATE INDEX IF NOT EXISTS idx_events_session  ON events (session_id)",
    "CREATE INDEX IF NOT EXISTS idx_events_agent    ON events (agent_id)",
)

MIGRATIONS: list[dict] = [
    {
        "version": 1,
        "ddl": _DDL_V1,
        "notes": "T05b 初版：events(run_id,event_id) 复合主键 + runs 元数据；seq 方案 A（整 run 重算）",
    },
]


class StoreError(Exception):
    """存储层基础错误。"""


class PolicyError(StoreError):
    """channel_policy 违规（merge 流未显式放行等）。"""


@dataclass
class IngestResult:
    """ingest 结果：inserted 新增 / updated 既有行仅 seq 变更 / skipped 幂等跳过 / total 本批条数。"""

    inserted: int
    updated: int
    skipped: int
    total: int

    def as_dict(self) -> dict:
        return {"inserted": self.inserted, "updated": self.updated,
                "skipped": self.skipped, "total": self.total}


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class Store:
    """SQLite 事件库。所有写操作走单事务（BEGIN IMMEDIATE），并发靠 busy_timeout 串行化。"""

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 30_000) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), timeout=busy_timeout_ms / 1000,
                                     isolation_level=None)  # autocommit；事务显式 BEGIN IMMEDIATE
        self._conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA synchronous = NORMAL")  # WAL 下 NORMAL：应用崩溃不丢，仅断电可能丢最后事务，换 ~10x 写入吞吐
        self._migrate()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- schema 版本管理 ----------------------------------------------------

    def _migrate(self) -> None:
        current = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if current > SCHEMA_USER_VERSION:
            raise StoreError(f"db user_version={current} > 引擎支持的 {SCHEMA_USER_VERSION}（请升级引擎）")
        for m in MIGRATIONS:
            if m["version"] <= current:
                continue
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for ddl in m["ddl"]:
                    self._conn.execute(ddl)
                self._conn.execute(f"PRAGMA user_version = {m['version']}")
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    # -- 写入 ----------------------------------------------------------------

    def ingest(
        self,
        run_id: str,
        events: Iterable[dict],
        *,
        channel_policy: str = "primary_only",
        allow_merge: bool = False,
        source_digest: str | None = None,
    ) -> IngestResult:
        """幂等写入（方案 A：整 run 重算 seq）。

        - merge 流默认拒绝：channel_policy="merge" 且未显式 allow_merge=True → PolicyError。
        - 对 (库内既有事件 ∪ 本批) 按 (ts_ms, event_id) 重排，覆盖写 seq 0..n-1。
          既有行只允许 seq 列变化，其余列不动（INSERT OR IGNORE）。
        - 单事务（BEGIN IMMEDIATE）：并发 ingest 串行化，INSERT OR IGNORE 保证零重复行。
        """
        if channel_policy not in ("primary_only", "merge"):
            raise PolicyError(f"invalid channel_policy: {channel_policy!r}")
        if channel_policy == "merge" and not allow_merge:
            raise PolicyError(
                "merge 流默认拒绝入库（约一倍 A/B 双源重复，仅测绘用）；"
                "确需写入请显式 allow_merge=True")

        batch: dict[str, dict] = {}
        for ev in events:
            _validate_event(ev, run_id)
            eid = ev["event_id"]
            if eid not in batch:  # 批内去重：同 id 取首条（与 collector 全局去重口径一致）
                batch[eid] = ev
        total = len(batch)

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._ensure_run(run_id, batch, channel_policy, source_digest)

            existing = {eid: (seq, ts_ms) for eid, seq, ts_ms in self._conn.execute(
                "SELECT event_id, seq, ts_ms FROM events WHERE run_id = ?", (run_id,))}

            # 方案 A：全量重排（库内既有 ∪ 本批），排序键与 collector.assign_seq 一致
            union: list[tuple[int, str]] = []
            for eid, (seq, ts_ms) in existing.items():
                if eid not in batch:
                    union.append((ts_ms, eid))
            for eid, ev in batch.items():
                union.append((_iso_to_ms(ev["ts"]), eid))
            union.sort()
            final_seq = {eid: i for i, (_, eid) in enumerate(union)}

            new_rows = []
            for eid, ev in batch.items():
                if eid in existing:
                    continue
                new_rows.append(_to_row(run_id, ev, final_seq[eid]))
            if new_rows:
                self._conn.executemany(
                    """INSERT OR IGNORE INTO events
                       (run_id, event_id, seq, ts, ts_ms, session_id, type, schema_version,
                        source_platform, source_channel, agent_id, agent_role, parent_agent_id,
                        raw_ref, labels, payload)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", new_rows)

            # 仅 seq 漂移的既有行：只 UPDATE seq 列
            seq_updates = [(final_seq[eid], run_id, eid) for eid in existing
                           if existing[eid][0] != final_seq[eid]]
            if seq_updates:
                self._conn.executemany(
                    "UPDATE events SET seq = ? WHERE run_id = ? AND event_id = ?", seq_updates)

            self._finalize_run(run_id, batch)
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        return IngestResult(inserted=len(new_rows), updated=len(seq_updates),
                            skipped=total - len(new_rows), total=total)

    def _ensure_run(self, run_id: str, batch: dict[str, dict],
                    channel_policy: str, source_digest: str | None) -> None:
        """先保 runs 行存在（events 外键引用），并刷新策略/摘要。"""
        self._conn.execute(
            """INSERT INTO runs (run_id, created_at, platform, channel_policy, source_digest, event_count)
               VALUES (?,?,?,?,?,0)
               ON CONFLICT(run_id) DO UPDATE SET
                 channel_policy = excluded.channel_policy,
                 source_digest  = COALESCE(excluded.source_digest, runs.source_digest)""",
            (run_id, datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
             None, channel_policy, source_digest))

    def _finalize_run(self, run_id: str, batch: dict[str, dict]) -> None:
        """事件写入完成后刷新 event_count 与 platform（本批多数派，确定性平局取字典序最小）。"""
        if batch:
            counts = Counter(ev["source"]["platform"] for ev in batch.values())
            platform = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        else:
            platform = None
        self._conn.execute(
            """UPDATE runs SET event_count = (SELECT COUNT(*) FROM events WHERE run_id = ?),
                                 platform = COALESCE(?, platform) WHERE run_id = ?""",
            (run_id, platform, run_id))

    # -- 读取 ----------------------------------------------------------------

    _COLS = ("run_id, event_id, seq, ts, session_id, type, schema_version, "
             "source_platform, source_channel, agent_id, agent_role, parent_agent_id, "
             "raw_ref, labels, payload")

    @staticmethod
    def _row_to_event(row: tuple) -> dict:
        (run_id, event_id, seq, ts, session_id, typ, sv, plat, chan,
         agent_id, agent_role, parent_agent_id, raw_ref, labels, payload) = row
        ev = {
            "schema_version": sv, "event_id": event_id, "run_id": run_id, "seq": seq,
            "ts": ts, "session_id": session_id, "type": typ,
            "source": {"platform": plat, "channel": chan},
            "labels": json.loads(labels), "payload": json.loads(payload),
        }
        for k, v in (("agent_id", agent_id), ("agent_role", agent_role),
                     ("parent_agent_id", parent_agent_id), ("raw_ref", raw_ref)):
            if v is not None:
                ev[k] = v
        return ev

    def iter_events(
        self,
        run_id: str,
        *,
        types: Iterable[str] | None = None,
        session_id: str | None = None,
        order_by: str = "seq",
    ) -> Iterator[dict]:
        """流式读（cursor 迭代，不整表 load）。order_by: "seq" | "ts"。"""
        if order_by not in ("seq", "ts"):
            raise ValueError(f"invalid order_by: {order_by!r}")
        where = ["run_id = ?"]
        params: list = [run_id]
        if types is not None:
            types = tuple(types)
            if not types:
                return
            where.append(f"type IN ({','.join('?' * len(types))})")
            params.extend(types)
        if session_id is not None:
            where.append("session_id = ?")
            params.append(session_id)
        order = "seq" if order_by == "seq" else "ts_ms, event_id"
        cur = self._conn.execute(
            f"SELECT {self._COLS} FROM events WHERE {' AND '.join(where)} ORDER BY {order}",
            params)
        for row in cur:
            yield self._row_to_event(row)

    def count_events(self, run_id: str) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM events WHERE run_id = ?", (run_id,)).fetchone()[0]

    def get_run(self, run_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT run_id, created_at, platform, channel_policy, source_digest, event_count"
            " FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        keys = ("run_id", "created_at", "platform", "channel_policy", "source_digest", "event_count")
        return dict(zip(keys, row))

    def list_runs(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT run_id, created_at, platform, channel_policy, source_digest, event_count"
            " FROM runs ORDER BY created_at, run_id").fetchall()
        keys = ("run_id", "created_at", "platform", "channel_policy", "source_digest", "event_count")
        return [dict(zip(keys, r)) for r in rows]

    # -- 导出 / 自检 ----------------------------------------------------------

    _DUMP_KEYS = ("schema_version", "event_id", "run_id", "seq", "ts", "session_id",
                  "type", "source", "labels", "payload",
                  "agent_id", "agent_role", "parent_agent_id", "raw_ref")

    def dump_run(self, run_id: str) -> str:
        """按 seq 序列化该 run 全部事件为 NDJSON 文本（与 collect() 产物逐字节可比）。

        键序固定为 collector._mk_event 的插入序；labels/payload 存的是原始 JSON 文本，
        回读后键序不变，因此 round-trip 无字节漂移。末尾带换行。
        """
        parts = []
        for ev in self.iter_events(run_id, order_by="seq"):
            ordered = {k: ev[k] for k in self._DUMP_KEYS if k in ev}
            parts.append(json.dumps(ordered, ensure_ascii=False, sort_keys=False))
        return "\n".join(parts) + ("\n" if parts else "")

    def verify_run(self, run_id: str, *, ndjson: Path | None = None,
                   schema_validator=None, model=None) -> dict:
        """E5 数据一致性自检。schema_validator/jsonschema 与 model/pydantic 由调用方注入。"""
        report: dict = {"run_id": run_id, "rows": 0, "schema_valid": 0, "schema_invalid": 0,
                        "model_valid": 0, "model_invalid": 0, "seq_dense": True,
                        "dup_pk": 0, "ndjson_rows": None, "rows_match_ndjson": None,
                        "errors": []}
        rows = self._conn.execute(
            f"SELECT {self._COLS} FROM events WHERE run_id = ? ORDER BY seq", (run_id,)).fetchall()
        report["rows"] = len(rows)

        dup = self._conn.execute(
            "SELECT COUNT(*) FROM (SELECT event_id FROM events WHERE run_id = ?"
            " GROUP BY event_id HAVING COUNT(*) > 1)", (run_id,)).fetchone()[0]
        report["dup_pk"] = dup

        seqs = [r[2] for r in rows]
        report["seq_dense"] = seqs == list(range(len(seqs)))

        for row in rows:
            ev = self._row_to_event(row)
            if model is not None:
                try:
                    model.model_validate(ev)
                    report["model_valid"] += 1
                except Exception as exc:  # noqa: BLE001
                    report["model_invalid"] += 1
                    report["errors"].append(f"model {ev['event_id']}: {exc}")
            if schema_validator is not None:
                try:
                    errs = list(schema_validator.iter_errors(ev))
                except Exception as exc:  # noqa: BLE001
                    errs = [exc]
                if errs:
                    report["schema_invalid"] += 1
                    report["errors"].append(f"schema {ev['event_id']}: {errs[0]}")
                else:
                    report["schema_valid"] += 1

        if ndjson is not None:
            n = sum(1 for line in ndjson.read_text(encoding="utf-8").splitlines() if line.strip())
            report["ndjson_rows"] = n
            report["rows_match_ndjson"] = (n == report["rows"])
        report["ok"] = (report["dup_pk"] == 0 and report["seq_dense"]
                        and report["schema_invalid"] == 0 and report["model_invalid"] == 0
                        and report["rows_match_ndjson"] is not False)
        return report


# ---------------------------------------------------------------------------
# 内部小件
# ---------------------------------------------------------------------------

_REQUIRED_KEYS = ("schema_version", "event_id", "run_id", "seq", "ts", "session_id",
                  "type", "source", "labels", "payload")


def _validate_event(ev: dict, run_id: str) -> None:
    if not isinstance(ev, dict):
        raise StoreError(f"event is not a dict: {type(ev).__name__}")
    missing = [k for k in _REQUIRED_KEYS if k not in ev]
    if missing:
        raise StoreError(f"event missing required keys {missing}: {ev.get('event_id')!r}")
    if ev["type"] not in EVENT_TYPES:
        raise StoreError(f"invalid event type: {ev['type']!r}")
    if ev["run_id"] != run_id:
        raise StoreError(
            f"event run_id {ev['run_id']!r} != ingest run_id {run_id!r}；"
            "请先由 collector 重绑 run_id（同源跨 run 复用 event_id 是设计行为，重绑在采集侧完成）")


def _to_row(run_id: str, ev: dict, seq: int) -> tuple:
    src = ev["source"]
    return (
        run_id, ev["event_id"], seq, ev["ts"], _iso_to_ms(ev["ts"]),
        ev["session_id"], ev["type"], ev["schema_version"],
        src["platform"], src["channel"],
        ev.get("agent_id"), ev.get("agent_role"), ev.get("parent_agent_id"),
        ev.get("raw_ref"),
        json.dumps(ev["labels"], ensure_ascii=False, sort_keys=False),
        json.dumps(ev["payload"], ensure_ascii=False, sort_keys=False),
    )


def digest_files(paths: Iterable[Path]) -> str | None:
    """摄入源内容摘要：对排序后的文件字节流串联取 sha256（判定「是否重复摄入同一份源」）。"""
    files = sorted(p for p in paths if p.is_file())
    if not files:
        return None
    h = hashlib.sha256()
    for f in files:
        h.update(f.name.encode("utf-8"))
        h.update(f.read_bytes())
    return h.hexdigest()

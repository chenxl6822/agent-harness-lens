"""T06 装配层单测：注册器可插拔 / 策略门禁 / 失败隔离 / schema 合规 / 可复现 / 隐私 / 可下钻。

运行（必须带 --basetemp，本机默认 tmp 在 D: 盘被沙箱拒绝）：
.venv/Scripts/python.exe -m pytest engine/tests/test_metrics.py -q --basetemp=.tmp-pytest

对应门禁：G1 注册器可插拔、G2 schema 合规、G3 策略门禁、G5 可下钻、G6 可复现、
G7 成本前向兼容（装配层）、G8 失败隔离、G10 隐私、以及 ext 字段完整性 / 三合一不变量。
纯函数口径的 golden 验算在 test_metrics_core.py。
"""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "engine"))

from hs_core import schema as hs_schema
from hs_core.metrics import (
    DEGRADE_REASONS,
    REGISTRY,
    MetricAssemblyError,
    MetricParams,
    MetricRegistryError,
    canonical_sha256,
    clear_registry,
    compute_run,
    get_metric,
    list_metrics,
    load_builtin_metrics,
    write_result,
)
from hs_core.store import Store

RUN = "t06-assembly"
PAYLOAD_USAGE = {"usage": {"input_tokens": 100, "output_tokens": 40}}


# ---------------------------------------------------------------------------
# 合成事件 / DB
# ---------------------------------------------------------------------------

def _ev(seq: int, typ: str, payload: dict | None = None, *, ts: str | None = None,
        run_id: str = RUN, raw_ref: str | None = None) -> dict:
    item = {
        "schema_version": "1.0.0",
        "event_id": f"EV{seq:06d}{run_id[-3:]}",
        "run_id": run_id,
        "seq": seq,
        "ts": ts or f"2026-10-26T00:00:{seq:02d}.000Z",
        "session_id": "sess-1",
        "type": typ,
        "source": {"platform": "codebuddy", "channel": "file"},
        "labels": {},
        "payload": payload or {},
    }
    if raw_ref:
        item["raw_ref"] = raw_ref        # 隐私门禁用：确认它不会漏进产物
    return item


def synthetic_events(run_id: str = RUN, *, usage: bool = True) -> list[dict]:
    """一条可完整算通的合成 run：2 步、4 次调用（3 成功 1 错误）、带 usage 的 llm.response。"""
    events = [
        _ev(0, "session.start", {"session_id": "sess-1"}, run_id=run_id,
            raw_ref="C:/Users/someone/.codebuddy/traces/x.json#L1"),
        _ev(1, "step.start", {"span_name": "step", "status": "ok", "duration_ms": 10}, run_id=run_id),
        _ev(2, "tool.call", {"tool_name": "Read", "span_id": "sp0",
                             "content_sha256": "h0", "content_bytes": 10}, run_id=run_id),
        _ev(3, "tool.result", {"tool_name": "Read", "span_id": "sp0",
                               "content_sha256": "h0", "content_bytes": 10}, run_id=run_id),
        _ev(4, "tool.call", {"tool_name": "Read", "span_id": "sp1",
                             "content_sha256": "h1", "content_bytes": 10}, run_id=run_id),
        _ev(5, "tool.result", {"tool_name": "Read", "span_id": "sp1",
                               "content_sha256": "h1", "content_bytes": 10}, run_id=run_id),
        _ev(6, "tool.call", {"tool_name": "Bash", "span_id": "sp2",
                             "content_sha256": "h2", "content_bytes": 10}, run_id=run_id),
        _ev(7, "tool.error", {"tool_name": "Bash", "span_id": "sp2",
                              "content_sha256": "h2", "content_bytes": 10}, run_id=run_id),
        _ev(8, "tool.call", {"tool_name": "Read", "span_id": "sp3",
                             "content_sha256": "h3", "content_bytes": 10}, run_id=run_id),
        _ev(9, "tool.result", {"tool_name": "Read", "span_id": "sp3",
                               "content_sha256": "h3", "content_bytes": 10}, run_id=run_id),
        _ev(10, "step.end", {"span_name": "step", "status": "ok", "duration_ms": 10}, run_id=run_id),
        _ev(11, "llm.response", dict(PAYLOAD_USAGE) if usage
            else {"span_name": "generation", "status": "ok", "duration_ms": 5}, run_id=run_id),
        _ev(12, "task.create", {"task_id": "t1"}, run_id=run_id),
        _ev(13, "task.complete", {"task_id": "t1"}, run_id=run_id),
        _ev(14, "metric.custom", {"metric": "trace_summary", "span_count": 1,
                                  "total_tokens": 0, "error_count": 1}, run_id=run_id),
    ]
    return events


@pytest.fixture
def store(tmp_path: Path):
    with Store(tmp_path / "metrics.db") as db:
        db.ingest(RUN, synthetic_events(), channel_policy="primary_only")
        yield db


@pytest.fixture(autouse=True)
def _clean_registry():
    """每个用例前后恢复「只含内置指标」的干净注册表（注册器是模块级全局状态）。"""
    clear_registry()
    load_builtin_metrics()
    yield
    clear_registry()
    load_builtin_metrics()


# ---------------------------------------------------------------------------
# G1 注册器可插拔
# ---------------------------------------------------------------------------

def test_registry_lists_13_builtin_metrics():
    keys = [spec.key for spec in list_metrics()]
    assert len(keys) == 13
    assert keys == sorted(keys)                                  # 确定性列举
    assert not [k for k in keys if k.startswith("collab.")]      # T07 未实现，一项不做
    assert [s.key for s in list_metrics(group="collab")] == []


def test_registry_group_alias_accepts_collab():
    from hs_core.metrics import register_metric

    @register_metric(key="collab.probe_alias", group="collab", label_zh="探针",
                     unit="ratio", direction=False)
    def _probe(events, params, ctx):
        raise AssertionError("不会被执行")

    assert get_metric("collab.probe_alias").group == "collaboration"
    assert [s.key for s in list_metrics(group="collaboration")] == ["collab.probe_alias"]


def test_registry_rejects_group_prefix_mismatch():
    from hs_core.metrics import register_metric

    with pytest.raises(MetricRegistryError, match="group 必须为"):
        @register_metric(key="process.probe_badgroup", group="cost", label_zh="探针")
        def _bad(events, params, ctx):
            raise AssertionError("不会被执行")


def test_registry_rejects_duplicate_key(tmp_path: Path, monkeypatch):
    """G1：第三方模块注册同 key → 显式 MetricRegistryError，不静默覆盖。"""
    mod = tmp_path / "ext_dup.py"
    mod.write_text(textwrap.dedent("""
        from hs_core.metrics import register_metric

        @register_metric(key="process.step_count", group="process",
                         label_zh="抢注同 key", unit="count")
        def dup(events, params, ctx):
            raise AssertionError("不应覆盖内置指标")
    """), encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(MetricRegistryError, match="重复注册指标"):
        load_builtin_metrics([mod.stem])
    assert REGISTRY["process.step_count"].label_zh == "步数（step.start 计数）"


def test_extra_module_registers_and_shows_in_list(tmp_path: Path, monkeypatch):
    """G1：第三方模块用同一装饰器注册 → 被 list_metrics 列出并在 compute_run 中产出。"""
    mod = tmp_path / "ext_probe.py"
    mod.write_text(textwrap.dedent("""
        from hs_core.metrics import MetricOutcome, evidence_from, register_metric

        @register_metric(key="process.probe_metric", group="process",
                         label_zh="探针指标", unit="count", direction=None)
        def probe(events, params, ctx):
            picked = [e for e in events if e["type"] == "step.start"]
            if not picked:
                return MetricOutcome.na("no_source", "无 step.start")
            return MetricOutcome.ok(float(len(picked)),
                                    evidence=evidence_from(picked, params=params, note="探针"))
    """), encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    load_builtin_metrics([mod.stem])
    assert "process.probe_metric" in [s.key for s in list_metrics()]
    assert [s.key for s in list_metrics(scope="run") if s.key == "process.probe_metric"]


def test_load_builtin_is_idempotent():
    before = sorted(REGISTRY)
    load_builtin_metrics()
    load_builtin_metrics()
    assert sorted(REGISTRY) == before


# ---------------------------------------------------------------------------
# G2 schema 合规 + ext 完整
# ---------------------------------------------------------------------------

def test_metricset_passes_schema_and_ext_is_complete(store, tmp_path: Path):
    result = compute_run(store, RUN, out_dir=tmp_path,
                         params=MetricParams(pricing={"default": {"in": 1e-6,
                                                                  "out": 2e-6}}))
    # 校验对象 = 实际落盘的主文件（与 `hs schema check --kind metric-set` 同一份字节）
    payload = json.loads((tmp_path / "metricset.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(hs_schema.load_schema("metric-set"))
    errors = list(validator.iter_errors(payload))
    assert errors == [], [f"{list(e.absolute_path)}: {e.message}" for e in errors[:3]]

    ext = result.ext
    for key in ("run_id", "channel_policy", "blocked", "blocked_reason", "event_count",
                "engine_version", "params_digest", "canonical_sha256", "computed_keys",
                "degraded_reasons", "key_count_check", "data_quality_flags", "data_quality_details",
                "tool_call_identity", "notes", "evidence_index", "elapsed_ms"):
        assert key in ext, key
    assert "degraded_metrics" not in ext        # R1-3：ext 里不得再有同名不同构的键
    assert ext["canonical_sha256"].startswith("sha256:")
    assert ext["params_digest"].startswith("sha256:")
    assert ext["channel_policy"] == "primary_only"
    assert ext["blocked"] is False and ext["blocked_reason"] is None
    # 每个降级 key 都要有结构化 reason + detail（Q6 枚举，禁止自由文本）
    for key, info in ext["degraded_reasons"].items():
        assert info["reason"] in DEGRADE_REASONS
        assert info["detail"]


def test_degraded_reason_lives_in_ext_because_schema_forbids_empty_evidence_ref(store, tmp_path: Path):
    """降级原因只能放 ext（+ notes）：`EvidenceRef.event_ids` 有 `minItems:1`，
    降级时没有 event_id 可引，硬造一个就是假证据（设计 §2 坑 3 的硬规则）。

    因此「reason 落 EvidenceRef.note」在 schema 不变的前提下**不可实现**，
    主文件里降级项固定是 `value=null + evidence=[]`，原因在 `metricset.ext.json`。
    """
    result = compute_run(store, RUN, out_dir=tmp_path)
    main = json.loads((tmp_path / "metricset.json").read_text(encoding="utf-8"))
    degraded_keys = set(main["degraded_metrics"])
    assert degraded_keys == {k for k, mv in result.by_key.items() if mv.value is None}
    for group in main["groups"].values():
        for mv in group:
            if mv["key"] in degraded_keys:
                assert mv["value"] is None
                assert mv["evidence"] == []          # 无证据块 → 也就没有 note 可写
    for key, info in result.ext["degraded_reasons"].items():
        assert key in degraded_keys
        assert info["reason"] in DEGRADE_REASONS     # reason 走枚举
        assert info["detail"]                        # detail 说清缺什么 / 归谁修
# ---------------------------------------------------------------------------
# R1-2 口径锁定 / R1-3 键名互斥 / R1-5 置信度反向断言
# ---------------------------------------------------------------------------

def test_key_count_is_self_consistent(store, tmp_path: Path):
    """R1-2：13 = 4 可算 + 9 降级，且实现侧 key 集合与口径常量逐字一致。"""
    from hs_core.metrics import EXPECTED_ALL_KEYS, EXPECTED_COMPUTED_KEYS, EXPECTED_DEGRADED_KEYS

    assert len(EXPECTED_ALL_KEYS) == 13
    assert len(EXPECTED_COMPUTED_KEYS) == 4 and len(EXPECTED_DEGRADED_KEYS) == 9
    assert set(EXPECTED_COMPUTED_KEYS) & set(EXPECTED_DEGRADED_KEYS) == set()

    # 与真实数据同形态：llm.response 无 usage → 恰好 4 可算 / 9 降级
    with Store(tmp_path / "nousage-count.db") as db:
        db.ingest(RUN, synthetic_events(RUN, usage=False), channel_policy="primary_only")
        result = compute_run(db, RUN, out_dir=tmp_path)
    check = result.ext["key_count_check"]
    assert check["total"] == 13
    assert check["computed"] + check["degraded"] == check["total"]
    assert check["consistent"] is True
    assert check["missing_keys"] == [] and check["extension_keys"] == []
    # 本次合成数据恰好 4 可算 / 9 降级（无 usage、无 run.start/end、无 verdict）
    assert check["computed"] == 4 and check["degraded"] == 9
    assert check["computed_matches_expected"] is True
    assert check["degraded_matches_expected"] is True
    # 与主文件 degraded_metrics 逐次相等（同一次运行的两个视图）
    assert check["degraded"] == len(result.metric_set.degraded_metrics)


def test_key_count_reports_data_drift_without_failing(tmp_path: Path):
    """R1-2：数据侧漂移（通道 A 补到 usage → cost.* 出值）只如实上报，**不报错**。"""
    with Store(tmp_path / "drift.db") as db:
        db.ingest("t06-drift", synthetic_events("t06-drift", usage=True),
                  channel_policy="primary_only")
        result = compute_run(db, "t06-drift",
                             params=MetricParams(pricing={"default": {"in": 1e-6, "out": 2e-6}}))
    check = result.ext["key_count_check"]
    assert check["consistent"] is True              # 实现侧 key 集合没变
    assert check["computed"] > 4                    # 但本次数据算出了更多
    assert check["computed_matches_expected"] is False
    assert check["missing_keys"] == []


def test_degraded_keys_naming_is_disjoint_between_files(store, tmp_path: Path):
    """R1-3 逐字检查：主文件 `degraded_metrics` 是 list[str]，ext `degraded_reasons` 是对象。"""
    compute_run(store, RUN, out_dir=tmp_path)
    main = json.loads((tmp_path / "metricset.json").read_text(encoding="utf-8"))
    ext = json.loads((tmp_path / "metricset.ext.json").read_text(encoding="utf-8"))

    assert isinstance(main["degraded_metrics"], list)
    assert all(isinstance(k, str) for k in main["degraded_metrics"])
    assert "degraded_reasons" not in main                  # 主文件不得出现 ext 的键名

    assert isinstance(ext["degraded_reasons"], dict)
    assert all(isinstance(v, dict) and v.get("reason") and v.get("detail")
               for v in ext["degraded_reasons"].values())
    assert "degraded_metrics" not in ext                   # ext 不得再出现同名不同构的键
    assert sorted(ext["degraded_reasons"]) == sorted(main["degraded_metrics"])


def test_every_valued_metric_has_confidence(store, tmp_path: Path):
    """R1-5 反向断言：遍历主文件，`value != null` 且 `confidence == null` 必须 0 命中。"""
    compute_run(store, RUN, out_dir=tmp_path)
    main = json.loads((tmp_path / "metricset.json").read_text(encoding="utf-8"))
    missing: list[str] = []
    degraded_with_conf: list[str] = []
    for group in main["groups"].values():
        for mv in group:
            if mv["value"] is not None:
                if mv.get("confidence") is None:
                    missing.append(mv["key"])
                assert mv["confidence"] in ("high", "medium", "low"), mv["key"]
            else:
                # 降级指标 schema 层省略 confidence（= null），且不得写 "low"
                if mv.get("confidence") is not None:
                    degraded_with_conf.append(mv["key"])
    assert missing == []
    assert degraded_with_conf == []


def test_degraded_detail_is_this_run_not_hardcoded_snapshot(tmp_path: Path):
    """R1-6：detail 里的数字来自本次运行；历史快照的数字只能以引用式出现。"""
    with Store(tmp_path / "nousage-detail.db") as db:
        db.ingest(RUN, synthetic_events(RUN, usage=False), channel_policy="primary_only")
        result = compute_run(db, RUN, out_dir=tmp_path)
    detail = result.ext["degraded_reasons"]["cost.tokens_total"]["detail"]
    # 本次 run 现场统计（synthetic run 只有 1 条 llm.response、1 条 trace_summary）
    assert "本 run `llm.response` 1 条，其中读到 usage 的 0 条" in detail
    assert "本 run `metric.custom`/trace_summary 1 条" in detail
    # 历史快照数字必须带「非本 run 观测」字样与出处
    assert "引用（**非本 run 观测**）" in detail and "docs/metrics-engine.md" in detail
    assert "723" in detail                                # 引用原文保留，但不冒充本次观测
    # 旧写法（把别人快照的数字说成本次实测）不得回归
    assert "实测命中 0" not in detail
    assert "实测 33/33" not in detail


# ---------------------------------------------------------------------------
# G3 策略门禁
# ---------------------------------------------------------------------------

def test_merge_run_blocked_all_null(tmp_path: Path):
    with Store(tmp_path / "merge.db") as db:
        db.ingest("t06-merge", synthetic_events("t06-merge"), channel_policy="merge",
                  allow_merge=True)
        result = compute_run(db, "t06-merge")
        assert result.blocked is True
        assert result.ext["blocked_reason"]
        assert "merge" in result.ext["blocked_reason"]
        assert set(result.metric_set.degraded_metrics) == set(REGISTRY)
        for group in (result.metric_set.groups.result, result.metric_set.groups.process,
                      result.metric_set.groups.collaboration, result.metric_set.groups.cost):
            for mv in group:
                assert mv.value is None
                assert mv.evidence == []
        assert all(info["reason"] == "blocked"
                   for info in result.ext["degraded_reasons"].values())


def test_merge_run_cli_exit_code_is_2(tmp_path: Path):
    from typer.testing import CliRunner

    from hs_core.cli import app

    db_path = tmp_path / "merge.db"
    with Store(db_path) as db:
        db.ingest("t06-merge-cli", synthetic_events("t06-merge-cli"),
                  channel_policy="merge", allow_merge=True)
    out = tmp_path / "out"
    res = CliRunner().invoke(app, ["metrics", "run", "--run-id", "t06-merge-cli",
                                   "--db", str(db_path), "--out-dir", str(out)])
    assert res.exit_code == 2, res.output
    assert "POLICY BLOCKED" in res.output


# ---------------------------------------------------------------------------
# G5 可下钻
# ---------------------------------------------------------------------------

def test_every_non_null_metric_has_resolvable_evidence(store, tmp_path: Path):
    result = compute_run(store, RUN, out_dir=tmp_path)
    checked = 0
    for key, idx in result.ext["evidence_index"].items():
        mv = result.by_key[key]
        assert mv.value is not None and mv.evidence          # 非空指标必有证据
        assert idx, key
        for item in idx:
            assert item["run_id"] == RUN
            assert store._conn.execute(                            # 与验收脚本同一条 SQL
                "SELECT 1 FROM events WHERE run_id=? AND event_id=?",
                (item["run_id"], item["event_id"])).fetchone() is not None
            checked += 1
    non_null = [k for k, mv in result.by_key.items() if mv.value is not None]
    assert checked > 0
    assert set(non_null) <= set(result.ext["evidence_index"])


def test_assembly_rejects_evidence_of_unknown_event(tmp_path: Path):
    """装配层硬断言：指标返回库里不存在的 event_id → MetricAssemblyError（不静默放行）。"""
    mod = tmp_path / "ext_bad_evidence.py"
    mod.write_text(textwrap.dedent("""
        from hs_core.metrics import MetricOutcome, register_metric
        from hs_core.models import EvidenceRef

        @register_metric(key="process.bad_evidence", group="process", label_zh="假证据",
                         unit="count", direction=None)
        def bad(events, params, ctx):
            return MetricOutcome.ok(1.0, evidence=(EvidenceRef(event_ids=["NOPE"], note="假"),))
    """), encoding="utf-8")
    import importlib
    import sys as _sys

    _sys.path.insert(0, str(tmp_path))
    try:
        load_builtin_metrics([mod.stem])
        with Store(tmp_path / "g5.db") as db:
            db.ingest(RUN, synthetic_events(), channel_policy="primary_only")
            with pytest.raises(MetricAssemblyError, match="不在 run"):
                compute_run(db, RUN)
    finally:
        _sys.path.remove(str(tmp_path))
        importlib.invalidate_caches()


# ---------------------------------------------------------------------------
# G6 可复现
# ---------------------------------------------------------------------------

def test_canonical_sha256_stable_across_two_runs(store):
    r1 = compute_run(store, RUN, at="2026-10-26T00:00:00Z")
    r2 = compute_run(store, RUN, at="2027-01-01T12:34:56Z")     # computed_at 不同
    assert r1.metric_set.computed_at != r2.metric_set.computed_at
    assert r1.ext["canonical_sha256"] == r2.ext["canonical_sha256"]
    assert canonical_sha256(r1.metric_set) == r1.ext["canonical_sha256"]
    # 主文件体积不同（computed_at 不同），但 canonical 摘要相同 → 证明 computed_at 被排除
    assert r1.metric_set.model_dump(mode="json") != r2.metric_set.model_dump(mode="json")


def test_output_json_has_no_nan_or_infinity(store, tmp_path: Path):
    compute_run(store, RUN, out_dir=tmp_path)
    text = (tmp_path / "metricset.json").read_text(encoding="utf-8")
    ext_text = (tmp_path / "metricset.ext.json").read_text(encoding="utf-8")
    for blob in (text, ext_text):
        assert "NaN" not in blob and "Infinity" not in blob and "-Infinity" not in blob
    json.loads(text)                                          # allow_nan=False 下必然合法
    json.loads(ext_text)


# ---------------------------------------------------------------------------
# G7 成本前向兼容（装配层双向）
# ---------------------------------------------------------------------------

def test_cost_metrics_present_when_usage_exists(tmp_path: Path):
    with Store(tmp_path / "usage.db") as db:
        db.ingest("t06-usage", synthetic_events("t06-usage", usage=True),
                  channel_policy="primary_only")
        result = compute_run(db, "t06-usage",
                             params=MetricParams(pricing={"default": {"in": 1e-6, "out": 2e-6}}))
        assert result.by_key["cost.tokens_total"].value == 140.0
        assert result.by_key["cost.tokens_in"].value == 100.0
        assert result.by_key["cost.tokens_out"].value == 40.0
        assert result.by_key["cost.cny"].value == pytest.approx(100 * 1e-6 + 40 * 2e-6)


def test_cost_metrics_degrade_on_current_real_shape(tmp_path: Path):
    with Store(tmp_path / "nousage.db") as db:
        db.ingest("t06-nousage", synthetic_events("t06-nousage", usage=False),
                  channel_policy="primary_only")
        result = compute_run(db, "t06-nousage")
        for key in ("cost.tokens_total", "cost.tokens_in", "cost.tokens_out", "cost.cny"):
            assert result.by_key[key].value is None
            info = result.ext["degraded_reasons"][key]
            assert info["reason"] == "no_source"
            assert "通道 B providerData.usage" in info["detail"] or "通道 B transcript" in info["detail"]
            assert "T05 v2" in info["detail"]


# ---------------------------------------------------------------------------
# G8 失败隔离
# ---------------------------------------------------------------------------

def test_failing_metric_is_isolated(tmp_path: Path, store):
    baseline = compute_run(store, RUN)
    baseline_ok = sorted(k for k, mv in baseline.by_key.items() if mv.value is not None)

    mod = tmp_path / "ext_boom.py"
    mod.write_text(textwrap.dedent("""
        from hs_core.metrics import register_metric

        @register_metric(key="process.boom", group="process", label_zh="必炸",
                         unit="count", direction=None)
        def boom(events, params, ctx):
            raise RuntimeError("C:/Users/someone/secret.db")
    """), encoding="utf-8")
    sys.path.insert(0, str(tmp_path))
    try:
        load_builtin_metrics([mod.stem])
        result = compute_run(store, RUN)
        boom = result.by_key["process.boom"]
        assert boom.value is None and boom.evidence == []
        info = result.ext["degraded_reasons"]["process.boom"]
        assert info["reason"] == "computation_error"
        assert "RuntimeError" in info["detail"]
        assert "C:/Users" not in info["detail"]            # 不记 traceback / 不泄漏路径
        after_ok = sorted(k for k, mv in result.by_key.items()
                          if mv.value is not None and k != "process.boom")
        assert after_ok == baseline_ok                      # 其余指标照常出值
    finally:
        sys.path.remove(str(tmp_path))
        import importlib
        importlib.invalidate_caches()


# ---------------------------------------------------------------------------
# G10 隐私
# ---------------------------------------------------------------------------

def test_outputs_contain_no_username_path_or_raw_ref(store, tmp_path: Path):
    result = compute_run(store, RUN, out_dir=tmp_path)
    blobs = [(tmp_path / "metricset.json").read_text(encoding="utf-8"),
             (tmp_path / "metricset.ext.json").read_text(encoding="utf-8")]
    raw_refs = [ev.get("raw_ref") for ev in synthetic_events() if ev.get("raw_ref")]
    assert raw_refs
    for blob in blobs:
        assert "C:/Users/" not in blob and "C:\\Users\\" not in blob
        assert "/home/" not in blob
        for ref in raw_refs:
            assert ref not in blob
    for note in result.ext["notes"].values():
        assert "raw_ref" not in note


# ---------------------------------------------------------------------------
# 三合一不变量 / 结构
# ---------------------------------------------------------------------------

def test_invariant_biconditional_holds_in_output(store):
    result = compute_run(store, RUN)
    degraded = set(result.metric_set.degraded_metrics)
    for group in (result.metric_set.groups.result, result.metric_set.groups.process,
                  result.metric_set.groups.collaboration, result.metric_set.groups.cost):
        for mv in group:
            assert (mv.value is None) == (mv.key in degraded) == (len(mv.evidence) == 0)


def test_groups_follow_key_prefix(store):
    result = compute_run(store, RUN)
    for prefix, field in (("result.", result.metric_set.groups.result),
                          ("process.", result.metric_set.groups.process),
                          ("cost.", result.metric_set.groups.cost)):
        assert field, prefix
        assert all(mv.key.startswith(prefix) for mv in field)
    assert result.metric_set.groups.collaboration == []      # T07 未落地 → 空数组


def test_tool_call_identity_flag_reported_without_touching_value(tmp_path: Path):
    """G15：恒等式不成立只登记 data_quality_flags，指标照常出值（结构性质 ≠ 指标正确性）。"""
    events = synthetic_events("t06-identity")
    events = [ev for ev in events if not (ev["type"] == "tool.result"
                                          and ev["payload"]["span_id"] == "sp3")]
    with Store(tmp_path / "identity.db") as db:
        db.ingest("t06-identity", events, channel_policy="primary_only")
        result = compute_run(db, "t06-identity")
        assert "tool_call_identity_violated" in result.ext["data_quality_flags"]
        assert "tool_call_identity_violated" in result.ext["data_quality_details"]
        assert result.by_key["process.tool_correctness"].value is not None
        assert "tool_call_identity_violated" not in result.metric_set.degraded_metrics


def test_tool_call_identity_consistent_data_has_no_flag(store):
    result = compute_run(store, RUN)
    assert result.ext["data_quality_flags"] == []
    ident = result.ext["tool_call_identity"]
    assert ident["tool_call"] == 4 and ident["tool_result"] == 3 and ident["tool_error"] == 1
    assert ident["identity_holds"] is True
    # R1-4：恒等式必须同时带快照标识，两份快照才可比
    assert ident["event_count"] == len(synthetic_events())
    assert ident["snapshot_digest"].startswith("sha256:")


def test_write_result_creates_both_files(store, tmp_path: Path):
    result = compute_run(store, RUN)
    paths = write_result(result, tmp_path / "out")
    assert paths["metricset"].exists() and paths["ext"].exists()
    ext = json.loads(paths["ext"].read_text(encoding="utf-8"))
    assert ext["canonical_sha256"] == result.ext["canonical_sha256"]


def test_params_digest_changes_with_config(store):
    r1 = compute_run(store, RUN, params=MetricParams(loop_max_period=5))
    r2 = compute_run(store, RUN, params=MetricParams(loop_max_period=3))
    assert r1.ext["params_digest"] != r2.ext["params_digest"]


# ---------------------------------------------------------------------------
# G9 阈值参数化（改 YAML 即生效，不改代码）
# ---------------------------------------------------------------------------

def test_yaml_overrides_change_results_without_touching_code(store, tmp_path: Path):
    from hs_core.metrics import load_params_config

    base = tmp_path / "metrics-a.yaml"
    base.write_text("loop_max_period: 5\nlow_confidence_n: 20\nidle_threshold_s: 30.0\n",
                    encoding="utf-8")
    tight = tmp_path / "metrics-b.yaml"
    tight.write_text("loop_max_period: 1\nlow_confidence_n: 2\nidle_threshold_s: 45.0\n",
                     encoding="utf-8")

    p_a = load_params_config(base)
    p_b = load_params_config(tight)
    assert (p_a.loop_max_period, p_a.low_confidence_n, p_a.idle_threshold_s) == (5, 20, 30.0)
    assert (p_b.loop_max_period, p_b.low_confidence_n, p_b.idle_threshold_s) == (1, 2, 45.0)

    r_a = compute_run(store, RUN, params=p_a)
    r_b = compute_run(store, RUN, params=p_b)
    # 同一份事件、同一份代码，仅配置不同 → 结论不同（证明阈值真的走配置）
    # R2-1：签名来源等级 = 原生（4 次调用哈希互异、无 span_end 回退），样本量阈值只在原生分支叠加
    assert r_a.by_key["process.redundant_loop"].confidence == "low"        # 原生但 4 < low_confidence_n=20
    assert r_b.by_key["process.redundant_loop"].confidence == "high"       # 原生且 4 ≥ 2
    assert r_a.ext["params_digest"] != r_b.ext["params_digest"]


def test_yaml_unknown_key_is_an_error(tmp_path: Path):
    """拼写错误必须炸，不静默失效（否则「以为改了阈值其实没改」）。"""
    from hs_core.metrics import MetricRunError, load_params_config

    bad = tmp_path / "bad.yaml"
    bad.write_text("loop_max_perid: 3\n", encoding="utf-8")     # 故意拼错
    with pytest.raises(MetricRunError, match="未知的 metrics 配置项"):
        load_params_config(bad)

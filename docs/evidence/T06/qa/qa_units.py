"""QA 独立复核脚本：G3 / G7 / G8 / G9 / G14 / G15②③ + 边界路径探测。

自己造数据、自己推导期望值，不引用实现方 result.json 的任何数字。
运行：.venv/Scripts/python.exe docs/evidence/T06/qa/qa_units.py
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]     # harness_scope 仓库根
assert (ROOT / "engine" / "hs_core").is_dir(), f"ROOT 推导错误: {ROOT}"
sys.path.insert(0, str(ROOT / "engine"))
sys.path.insert(0, str(Path(__file__).resolve().parent))   # qa_ext_boom 可被 import

from hs_core.metrics import (  # noqa: E402
    MetricParams,
    clear_registry,
    compute_run,
    load_builtin_metrics,
    load_params_config,
)
from hs_core.store import Store  # noqa: E402

QA_DIR = Path(__file__).resolve().parent
TMP = ROOT / ".tmp-qa"
SCHEMA_VERSION = "1.0.0"
H_A = hashlib.sha256(b"qa-args-a").hexdigest()
H_B = hashlib.sha256(b"qa-args-b").hexdigest()

FAILURES: list[str] = []
NOTES: list[str] = []


def check(name: str, ok: bool, detail: str) -> None:
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}: {detail}")
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def mk(seq: int, typ: str, payload: dict, *, raw_ref: str | None = None,
       run_id: str = "qa-unit") -> dict:
    ev = {
        "schema_version": SCHEMA_VERSION,
        "event_id": f"u{seq:05d}",
        "run_id": run_id,
        "seq": seq,
        "ts": f"2026-01-01T00:00:{seq:02d}.000Z",
        "session_id": "qa-sess",
        "type": typ,
        "source": {"platform": "codebuddy", "channel": "file"},
        "labels": {},
        "payload": payload,
    }
    if raw_ref:
        ev["raw_ref"] = raw_ref
    return ev


class FakeStore:
    def __init__(self, events: list[dict], policy: str = "primary_only") -> None:
        self._events = sorted(events, key=lambda e: int(e["seq"]))
        self._policy = policy

    def get_run(self, run_id: str) -> dict:
        return {"run_id": run_id, "platform": "codebuddy",
                "channel_policy": self._policy, "event_count": len(self._events)}

    def iter_events(self, run_id: str, order_by: str = "seq"):
        assert order_by == "seq", f"第二排序键 {order_by!r}"
        return iter(self._events)


def run(events: list[dict], *, policy: str = "primary_only", params: MetricParams | None = None,
        extra: tuple[str, ...] = (), run_id: str = "qa-unit"):
    clear_registry()
    load_builtin_metrics(extra)
    return compute_run(FakeStore(events, policy), run_id, params=params or MetricParams())


def call_events(n: int, tool: str = "Read", sha: str = H_A, start: int = 0) -> list[dict]:
    return [mk(start + i, "tool.call",
               {"tool_name": tool, "span_id": f"S{start + i}", "content_sha256": sha})
            for i in range(n)]


# ---------------------------------------------------------------------------
def g3_policy_gate() -> None:
    """G3：channel_policy='merge' 必须整 run 拒绝（真实 SQLite 入库，allow_merge=True）。"""
    print("\n== G3 策略门禁（真实 DB，channel_policy=merge）==")
    db = TMP / "qa-g3.db"
    if db.exists():
        db.unlink()
    evs = [mk(i, t, p, run_id="qa-merge-run") for i, (t, p) in enumerate([
        ("step.start", {}), ("step.start", {}),
        ("tool.call", {"tool_name": "Read", "span_id": "S1", "content_sha256": H_A}),
        ("tool.result", {"tool_name": "Read", "span_id": "S1", "content_sha256": H_A}),
    ])]
    with Store(db) as store:
        store.ingest("qa-merge-run", evs, channel_policy="merge", allow_merge=True)
        policy_row = store.get_run("qa-merge-run")
    check("G3-0 入库策略", policy_row["channel_policy"] == "merge",
          f"runs.channel_policy={policy_row['channel_policy']!r}")

    with Store(db) as store:
        res = compute_run(store, "qa-merge-run")
    all_null = all(mv.value is None for mv in res.by_key.values())
    all_deg = sorted(res.metric_set.degraded_metrics) == sorted(res.by_key)
    check("G3-1 blocked", res.blocked is True, f"ext.blocked={res.blocked}")
    check("G3-2 全部 value=null", all_null, f"{len(res.by_key)} 个 key 全 null={all_null}")
    check("G3-3 全 key 进 degraded_metrics", all_deg,
          f"degraded {len(res.metric_set.degraded_metrics)}/{len(res.by_key)}")
    check("G3-4 blocked_reason 非空", bool(res.ext.get("blocked_reason")),
          f"blocked_reason 长度 {len(res.ext.get('blocked_reason') or '')}")
    reasons = res.ext.get("degraded_reasons") or res.ext.get("degraded_metrics")
    all_blocked = all(v.get("reason") == "blocked" for v in reasons.values()) if isinstance(
        reasons, dict) else False
    check("G3-5 reason=blocked", all_blocked, f"reasons 全为 blocked={all_blocked}")


def g7_cost_forward_compat() -> None:
    """G7：含 usage 必须出值；真实形态（无 usage）必须 null+no_source 且 detail 含关键事实。"""
    print("\n== G7 成本前向兼容（双向）==")

    # 形态一：providerData.usage.{input_tokens,output_tokens}
    ev1 = [mk(0, "llm.response", {"span_name": "generation", "status": "ok", "duration_ms": 12,
                                  "providerData": {"model": "claude-x",
                                                   "usage": {"input_tokens": 1000,
                                                             "output_tokens": 200}}})]
    # 形态二：payload.usage.{prompt_tokens,completion_tokens,total_tokens}
    ev2 = [mk(1, "llm.response", {"span_name": "generation", "status": "ok", "duration_ms": 13,
                                  "usage": {"prompt_tokens": 500, "completion_tokens": 100,
                                            "total_tokens": 600}})]
    r = run(ev1 + ev2)
    k = r.by_key
    check("G7-1 tokens_in 出值", k["cost.tokens_in"].value == 1500.0,
          f"tokens_in={k['cost.tokens_in'].value}（期望 1000+500=1500）")
    check("G7-2 tokens_out 出值", k["cost.tokens_out"].value == 300.0,
          f"tokens_out={k['cost.tokens_out'].value}（期望 200+100=300）")
    check("G7-3 tokens_total 出值", k["cost.tokens_total"].value == 1800.0,
          f"tokens_total={k['cost.tokens_total'].value}（1000+200 与 600 累加=1800）")

    # cost.cny：给了 pricing 必须出值；未给单价必须降级
    pricing = {"claude-x": {"in": 0.001, "out": 0.005}, "default": {"in": 0.002, "out": 0.01}}
    r2 = run(ev1 + ev2, params=MetricParams(pricing=pricing))
    expect_cny = round(1000 * 0.001 + 200 * 0.005 + 500 * 0.002 + 100 * 0.01, 6)
    check("G7-4 cost.cny 出值", r2.by_key["cost.cny"].value == expect_cny,
          f"cny={r2.by_key['cost.cny'].value}（手算 {expect_cny}）")
    r3 = run(ev1 + ev2, params=MetricParams(pricing={}))
    check("G7-5 无单价 → 降级", r3.by_key["cost.cny"].value is None,
          f"cny={r3.by_key['cost.cny'].value} reason="
          f"{(r3.ext.get('degraded_reasons') or {}).get('cost.cny', {}).get('reason')}")

    # 反向：当前真实形态（无 usage）
    real = [mk(i, "llm.response", {"span_name": "generation", "status": "ok",
                                   "duration_ms": 100 + i}) for i in range(3)]
    real += [mk(10 + i, "metric.custom",
                {"metric": "trace_summary", "span_count": 1, "total_tokens": 0,
                 "error_count": 1}) for i in range(3)]
    r4 = run(real)
    k4 = r4.by_key
    check("G7-6 真实形态 cost 全降级",
          all(k4[x].value is None for x in ("cost.tokens_total", "cost.tokens_in",
                                            "cost.tokens_out", "cost.cny")),
          "tokens_total/in/out/cny 全为 null")
    reasons4 = r4.ext.get("degraded_reasons") or r4.ext.get("degraded_metrics") or {}
    detail = str(reasons4.get("cost.tokens_total", {}))
    check("G7-7 detail 含『providerData.usage』", "providerData.usage" in detail, "命中")
    check("G7-8 detail 含『T05 v2』", "T05 v2" in detail, "命中")
    check("G7-9 detail 含 trace_summary 占位事实", "trace_summary" in detail or
          "total_tokens" in detail, "命中")
    check("G7-10 降级字段类型", isinstance(r4.metric_set.degraded_metrics, list),
          f"metricset.degraded 是 {type(r4.metric_set.degraded_metrics).__name__}")


def g14_conditional_second_path() -> None:
    """G14：① 开关 False 时不出值；② True 且请求级全 0 → 出值；③ 有非零请求级 → 只走①；④ in/out 恒 null。"""
    print("\n== G14 条件二路径（4 个用例）==")
    summary = [mk(0, "metric.custom", {"metric": "trace_summary", "span_count": 1,
                                       "total_tokens": 500, "error_count": 0}),
               mk(1, "metric.custom", {"metric": "trace_summary", "span_count": 1,
                                       "total_tokens": 700, "error_count": 0})]
    zero_req = [mk(2, "llm.response", {"span_name": "generation", "status": "ok",
                                       "duration_ms": 1})]
    nonzero_req = [mk(3, "llm.response", {"span_name": "generation", "status": "ok",
                                          "duration_ms": 1,
                                          "usage": {"total_tokens": 120}})]

    r1 = run(summary + zero_req)
    check("G14-① 开关 False → null", r1.by_key["cost.tokens_total"].value is None,
          f"tokens_total={r1.by_key['cost.tokens_total'].value}")

    params_on = MetricParams(tokens_trace_summary_fallback=True)
    r2 = run(summary + zero_req, params=params_on)
    v2 = r2.by_key["cost.tokens_total"].value
    check("G14-② 开关 True + 请求级全 0 → 出值", v2 == 1200.0, f"tokens_total={v2}（期望 500+700）")
    note2 = (r2.ext.get("notes") or {}).get("cost.tokens_total", "")
    check("G14-②b note 含『trace 级汇总』", "trace 级汇总" in note2, f"note={note2[:60]}…")

    r3 = run(summary + nonzero_req, params=params_on)
    v3 = r3.by_key["cost.tokens_total"].value
    check("G14-③ 非零请求级 → 只走①（防双计）", v3 == 120.0,
          f"tokens_total={v3}（期望 120；若双计会是 1320）")

    for case, r in (("②", r2), ("③", r3)):
        check(f"G14-④{case} tokens_in 恒 null", r.by_key["cost.tokens_in"].value is None,
              f"tokens_in={r.by_key['cost.tokens_in'].value}")
        check(f"G14-④{case} tokens_out 恒 null", r.by_key["cost.tokens_out"].value is None,
              f"tokens_out={r.by_key['cost.tokens_out'].value}")


def g8_failure_isolation() -> None:
    """G8：注入必抛异常探针，其余指标必须照常出值。"""
    print("\n== G8 失败隔离 ==")
    evs = call_events(3) + [mk(100, "step.start", {})]
    base = run(evs)
    base_computed = sorted(k for k, v in base.by_key.items() if v.value is not None)
    clear_registry()
    r = run(evs, extra=("qa_ext_boom",))
    k = r.by_key
    computed = sorted(x for x, v in k.items() if v.value is not None)
    reasons = r.ext.get("degraded_reasons") or r.ext.get("degraded_metrics") or {}
    check("G8-1 探针自身降级", k.get("process.qa_boom_metric") is not None
          and k["process.qa_boom_metric"].value is None, "探针 value=null")
    check("G8-2 reason=computation_error",
          reasons.get("process.qa_boom_metric", {}).get("reason") == "computation_error",
          f"reason={reasons.get('process.qa_boom_metric', {}).get('reason')!r}")
    check("G8-3 其余指标数量不变", computed == base_computed,
          f"基线 {base_computed} vs 注入后 {computed}")
    detail = str(reasons.get("process.qa_boom_metric", {}))
    check("G8-4 不记 traceback（无文件路径）", ".py" not in detail and "File \"" not in detail,
          f"detail={detail[:80]}…")


def g9_param_effective() -> None:
    """G9：改 YAML（不改代码）即生效。"""
    print("\n== G9 阈值参数化（改配置不改代码）==")
    # [A,B,A,B,A,B] → p=2 循环；把 loop_max_period 改成 1 后该循环不再被检出
    alt = [mk(i, "tool.call", {"tool_name": "Read" if i % 2 == 0 else "Bash",
                               "span_id": f"S{i}",
                               "content_sha256": H_A if i % 2 == 0 else H_B})
           for i in range(6)]
    yml5 = TMP / "qa-p5.yaml"
    yml1 = TMP / "qa-p1.yaml"
    yml5.write_text("loop_max_period: 5\nlow_confidence_n: 20\nidle_threshold_s: 30.0\n",
                    encoding="utf-8")
    yml1.write_text("loop_max_period: 1\nlow_confidence_n: 20\nidle_threshold_s: 99.5\n",
                    encoding="utf-8")
    p5 = load_params_config(yml5)
    p1 = load_params_config(yml1)
    check("G9-0 两份 params_digest 不同", p5.digest() != p1.digest(), "digest 不同")
    check("G9-0b YAML 载入值正确",
          (p5.loop_max_period, p1.loop_max_period, p1.idle_threshold_s) == (5, 1, 99.5),
          f"p5={p5.loop_max_period} p1={p1.loop_max_period} idle={p1.idle_threshold_s}")
    clear_registry(); load_builtin_metrics()
    v5 = compute_run(FakeStore(alt), "qa-unit", params=p5).by_key["process.redundant_loop"].value
    clear_registry(); load_builtin_metrics()
    v1 = compute_run(FakeStore(alt), "qa-unit", params=p1).by_key["process.redundant_loop"].value
    check("G9-1 loop_max_period 生效", (v5, v1) == (2.0, 0.0),
          f"period=5 → {v5}；period=1 → {v1}（期望 2.0 / 0.0）")

    # low_confidence_n 生效校验：6 次调用，阈值 100 → 6<100 标 low；阈值 3 → 6≥3 不该标 low
    yml_lo = TMP / "qa-lo.yaml"
    yml_lo.write_text("low_confidence_n: 3\n", encoding="utf-8")
    yml_hi = TMP / "qa-hi.yaml"
    yml_hi.write_text("low_confidence_n: 100\n", encoding="utf-8")
    for label, path, impl_want, ruling_want in (("low_confidence_n=3（n=6≥3）", yml_lo, None, "high"),
                                                ("low_confidence_n=100（n=6<100）", yml_hi,
                                                 "low", "low")):
        clear_registry(); load_builtin_metrics()
        rr = compute_run(FakeStore(alt), "qa-unit", params=load_params_config(path))
        got = rr.by_key["process.retry_rate"].confidence
        got = got.value if got is not None else None
        check(f"G9-2 {label} 参数确实生效（confidence 随之变化）", got == impl_want,
              f"实现给出 confidence={got!r}；但 Q7/T06-09b 裁定应为 {ruling_want!r}"
              f"{'（一致）' if got == ruling_want else '（★违反裁定）'}")


def g15_identity_report_only() -> None:
    """G15②：真实数据 only-report（不失败）；G15③：不成立时 data_quality_flags 追加 flag。"""
    print("\n== G15 分母恒等式：真实数据只上报不失败 ==")
    impl_db = ROOT / "runs" / "t06" / "t06.db"
    if not impl_db.exists():
        check("G15-2 实现方快照存在", False, f"缺失 {impl_db.name}")
        return
    with Store(impl_db) as store:
        rows = store.get_run("t06-codebuddy-primary")
        clear_registry(); load_builtin_metrics()
        res = compute_run(store, "t06-codebuddy-primary")
    print(f"  实现方快照 event_count={rows['event_count']}")
    ident = res.ext.get("tool_call_identity")
    flags = res.ext.get("data_quality_flags", [])
    holds = ident["identity_holds"] if ident else None
    print(f"  tool.call {ident['tool_call']} / result {ident['tool_result']} / "
          f"error {ident['tool_error']} → identity_holds={holds}")
    check("G15-2a 不因恒等式不成立而失败（仍有 4 项出值）",
          len(res.ext["computed_keys"]) == 4,
          f"computed_keys={res.ext['computed_keys']}")
    if not holds:
        check("G15-3 data_quality_flags 追加 tool_call_identity_violated",
              "tool_call_identity_violated" in flags, f"flags={flags}")
        check("G15-3b flag 不进 degraded_metrics",
              "tool_call_identity_violated" not in res.metric_set.degraded_metrics,
              "flag 未污染降级清单")
    else:
        check("G15-3 快照恒等式成立时无 flag", "tool_call_identity_violated" not in flags,
              f"flags={flags}（本快照成立，属正常）")


def edge_probes() -> None:
    """门禁未覆盖的边界路径探测。"""
    print("\n== 边界与异常路径探测（QA 自选）==")
    # E1 空事件流
    r = run([])
    check("E1 空 run 不崩且全降级", len(r.metric_set.degraded_metrics) == len(r.by_key) == 13,
          f"degraded={len(r.metric_set.degraded_metrics)} keys={len(r.by_key)}")
    r_ = run([mk(0, "step.start", {})])
    check("E2 单事件 run", r_.by_key["process.step_count"].value == 1.0,
          f"step_count={r_.by_key['process.step_count'].value}")
    # E3 raw_ref 不得落产物
    evs = [mk(i, "step.start", {}, raw_ref="C:\\Users\\<QAUSER>\\traces\\x.jsonl#12")  # noqa: S105
           for i in range(3)]
    r2 = run(evs)
    blob = json.dumps(r2.ext, ensure_ascii=False) + json.dumps(
        r2.metric_set.model_dump(mode="json"), ensure_ascii=False, default=str)
    check("E3 raw_ref 不出现在 note/detail", "QAUSER" not in blob and "x.jsonl" not in blob,
          "raw_ref 原文 0 命中")
    # E4 证据截断裂 note
    many = call_events(120, start=0)
    r3 = run(many, params=MetricParams(max_evidence_ids=5))
    note3 = " ".join(ref.note or "" for ref in r3.by_key["process.step_count"].evidence) or \
        " ".join(ref.note or "" for ref in r3.by_key["process.tool_correctness"].evidence)
    check("E4 120 条证据 → 截断到 5 且 note 写明总数",
          all(len(ref.event_ids) <= 5 for ref in r3.by_key["process.tool_correctness"].evidence)
          and "共 120 条" in note3, f"note={note3[-60:]!r}")
    # E5 内容哈希为空常量时必须排除出循环判定
    empty_sha = hashlib.sha256(b"").hexdigest()
    evs5 = [mk(i, "tool.call", {"tool_name": "Bash", "span_id": f"E{i}",
                                "content_sha256": empty_sha}) for i in range(10)]
    r5 = run(evs5)
    v5 = r5.by_key["process.redundant_loop"].value
    check("E5 空输入哈希不得算成同签名循环", v5 is None,
          f"redundant_loop={v5}（期望 null 而非 9）")
    check("E5b redundant_loop 降级 reason=insufficient_data",
          (r5.ext.get("degraded_reasons") or {}).get("process.redundant_loop", {}).get("reason")
          == "insufficient_data", "reason 正确")
    # E6 非法 confidence 应被装配层拦下
    from hs_core.metrics import MetricAssemblyError, register_metric
    from hs_core.models import EvidenceRef

    clear_registry(); load_builtin_metrics()

    @register_metric(key="process.qa_bad_conf", group="process", label_zh="非法置信度探针",
                     unit="count")
    def _bad(events, params, ctx):
        from hs_core.metrics import MetricOutcome
        return MetricOutcome.ok(1.0, evidence=(EvidenceRef(event_ids=["u00000"]),),
                                confidence="ultra-high")

    try:
        compute_run(FakeStore(evs), "qa-unit")
        check("E6 非法 confidence 被拒", False, "未抛异常")
    except MetricAssemblyError as exc:
        check("E6 非法 confidence 被拒", "confidence" in str(exc), str(exc)[:70])
    # E7 指标返回未注册类型
    clear_registry(); load_builtin_metrics()

    @register_metric(key="process.qa_bad_ret", group="process", label_zh="非法返回探针",
                     unit="count")
    def _bad2(events, params, ctx):
        return {"value": 1}

    try:
        compute_run(FakeStore(evs), "qa-unit")
        check("E7 非法返回类型被拒", False, "未抛异常")
    except MetricAssemblyError as exc:
        check("E7 非法返回类型被拒", True, str(exc)[:70])


def param_semantics_probe() -> None:
    """QA 自选：loop_min_repeat 是否真按「循环节最少重复次数」生效。"""
    print("\n== 参数语义探测：loop_min_repeat ==")
    from hs_core.metrics_core import _scan_loops
    h = hashlib.sha256(b"qa-same").hexdigest()
    sigs3 = [("T", h)] * 3          # 同一动作连续 3 次
    sigs4 = [("T", h)] * 4          # 连续 4 次
    r2_3 = _scan_loops(sigs3, MetricParams(loop_min_repeat=2))[0]
    r3_3 = _scan_loops(sigs3, MetricParams(loop_min_repeat=3))[0]
    r2_4 = _scan_loops(sigs4, MetricParams(loop_min_repeat=2))[0]
    r3_4 = _scan_loops(sigs4, MetricParams(loop_min_repeat=3))[0]
    print(f"    连续 3 次：min_repeat=2 → {r2_3}，min_repeat=3 → {r3_3}")
    print(f"    连续 4 次：min_repeat=2 → {r2_4}，min_repeat=3 → {r3_4}")
    same = (r2_3 == r3_3) and (r2_4 == r3_4)
    check("loop_min_repeat 2→3 语义应有变化（文档称『循环节最少重复次数』）", not same,
          f"改 2→3 结果完全不变（{r2_3}/{r2_4}）：该参数当前只作用于越界剪枝 "
          "(`i + min_repeat*p > n`)，实际判定恒为『重复 2 次』，属参数名与语义不符")


def main() -> int:
    TMP.mkdir(parents=True, exist_ok=True)
    print("QA T06 单元级门禁复核（独立脚本，非实现方测试）")
    g3_policy_gate()
    g7_cost_forward_compat()
    g14_conditional_second_path()
    g8_failure_isolation()
    g9_param_effective()
    g15_identity_report_only()
    edge_probes()
    param_semantics_probe()
    print("\n" + "=" * 60)
    print(json.dumps({"total_failures": len(FAILURES), "failures": FAILURES},
                     ensure_ascii=False, indent=2))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())

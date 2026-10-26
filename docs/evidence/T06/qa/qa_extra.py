"""QA 自选探测：真实数据永远走不到的分支（result.success 有 verdict / group 聚合 / 墙钟有 membership）。

这些路径在真实 run 上恒降级，实现方的 E2E 实测覆盖不到，故单独验证。
运行：.venv/Scripts/python.exe docs/evidence/T06/qa/qa_extra.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
assert (ROOT / "engine" / "hs_core").is_dir(), f"ROOT 推导错误: {ROOT}"
sys.path.insert(0, str(ROOT / "engine"))

from hs_core.metrics import (  # noqa: E402
    MetricContext,
    MetricParams,
    MetricValue,
    RunMembership,
    VerdictRecord,
    clear_registry,
    compute_group,
    compute_run,
    get_metric,
    load_builtin_metrics,
)
from hs_core.metrics_core import _merge_message_recv  # noqa: E402
from hs_core.models import EvidenceRef  # noqa: E402

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def mk(seq: int, typ: str, payload: dict) -> dict:
    return {
        "schema_version": "1.0.0", "event_id": f"x{seq:05d}", "run_id": "qa-extra",
        "seq": seq, "ts": f"2026-01-01T00:00:{seq:02d}.000Z", "session_id": "s",
        "type": typ, "source": {"platform": "codebuddy", "channel": "file"},
        "labels": {}, "payload": payload,
    }


class FakeStore:
    def __init__(self, events, policy="primary_only"):
        self._e = sorted(events, key=lambda x: int(x["seq"]))
        self._p = policy

    def get_run(self, rid):
        return {"run_id": rid, "platform": "codebuddy", "channel_policy": self._p,
                "event_count": len(self._e)}

    def iter_events(self, rid, order_by="seq"):
        assert order_by == "seq"
        return iter(self._e)


def mv_of(key: str, value: float | None) -> MetricValue:
    """成员 MetricValue：带真实证据锚点（聚合器 `_with_group_evidence` 依赖它取锚点）。"""
    spec = get_metric(key)
    evidence = [] if value is None else [EvidenceRef(event_ids=["x00000"], note="合成成员锚点")]
    return MetricValue(key=key, label_zh=spec.label_zh, value=value, evidence=evidence)


def success_paths() -> None:
    print("\n== result.success：注入 T09 verdicts 的三条路径 ==")
    evs = [mk(0, "task.create", {"task_id": "T1"}), mk(1, "task.complete", {"task_id": "T1"}),
           mk(2, "step.start", {})]
    base: list = []
    for i, (label, verdicts, expect) in enumerate([
        ("anchored", [VerdictRecord(run_id="qa-extra", task_id="T1", passed=True)], 1.0),
        ("unanchored(task_id 未在事件流里)", [VerdictRecord(run_id="qa-extra", task_id=None,
                                                     passed=True)], 1.0),
        ("verdict 属别的 run", [VerdictRecord(run_id="other", task_id="T1", passed=True)], None),
        ("3 条 verdict 过 2", [VerdictRecord(run_id="qa-extra", task_id="T1", passed=True),
                          VerdictRecord(run_id="qa-extra", task_id="T1", passed=True),
                          VerdictRecord(run_id="qa-extra", task_id="T1", passed=False)], round(2 / 3, 6)),
    ]):
        clear_registry(); load_builtin_metrics()
        r = compute_run(FakeStore(evs), "qa-extra", verdicts=verdicts)
        got = r.by_key["result.success"].value
        check(f"S{i+1} {label}", got == expect,
              f"value={got}（期望 {expect}）conf="
              f"{r.by_key['result.success'].confidence} note="
              f"{(r.ext.get('notes') or {}).get('result.success', '')[:70]}")
        base.append(got)


def wall_clock_with_membership() -> None:
    print("\n== cost.wall_clock_p50/p95：注入 T10 membership 的两个方向 ==")
    evs = [mk(0, "step.start", {})]
    clear_registry(); load_builtin_metrics()
    r = compute_run(FakeStore(evs), "qa-extra",
                    membership=RunMembership(wall_clock_ms=5000))
    v = r.by_key["cost.wall_clock_p50"].value
    check("W1 有 membership 但事件流无 run.start/run.end", v is None,
          f"value={v}（无 run 级起止事件→无证据锚点，按 G5 降级符合预期）")
    check("W1b 降级 reason=no_source",
          (r.ext.get("degraded_reasons") or {}).get("cost.wall_clock_p50", {}).get("reason")
          == "no_source", "reason 正确")

    evs2 = [mk(0, "run.start", {}), mk(1, "step.start", {}), mk(2, "run.end", {})]
    clear_registry(); load_builtin_metrics()
    r2 = compute_run(FakeStore(evs2), "qa-extra",
                     membership=RunMembership(wall_clock_ms=9000))
    v2 = r2.by_key["cost.wall_clock_p50"].value
    v3 = r2.by_key["cost.wall_clock_p95"].value
    check("W2 有起止事件 → 出值（取 membership 值 9000）", (v2, v3) == (9000.0, 9000.0),
          f"p50={v2} p95={v3}")


def group_aggregators() -> None:
    print("\n== scope=group 聚合器（PRD 裁定4：本轮只交接口 + 合成 membership 证明算法）==")
    params = MetricParams()
    # pass_k：3 组，每组 k=2，(A:全过)(B:一过一不过)(C:只跑了1次→不完整)
    items = [
        (mv_of("result.success", 1.0), RunMembership(task_id="t1", variant_id="v1", k=2)),
        (mv_of("result.success", 1.0), RunMembership(task_id="t1", variant_id="v1", k=2)),
        (mv_of("result.success", 1.0), RunMembership(task_id="t2", variant_id="v1", k=2)),
        (mv_of("result.success", 0.0), RunMembership(task_id="t2", variant_id="v1", k=2)),
        (mv_of("result.success", 1.0), RunMembership(task_id="t3", variant_id="v1", k=2)),
    ]
    clear_registry(); load_builtin_metrics()
    g = compute_group("result.pass_k", items, params=params, run_ids=["r1", "r2", "r3"])
    check("P1 pass_k = 1/2（第3组不完整已排除）", g.value == 0.5, f"value={g.value}（期望 0.5）")
    # 全部无归属元数据
    clear_registry(); load_builtin_metrics()
    g2 = compute_group("result.pass_k", [(mv_of("result.success", 1.0), None)], params=params)
    check("P2 无归属元数据 → null", g2.value is None, f"value={g2.value}")

    # trajectory_efficiency：同组最短步数为参考
    vitems = [
        (mv_of("process.step_count", 10.0), RunMembership(task_id="t1", variant_id="v1")),
        (mv_of("process.step_count", 20.0), RunMembership(task_id="t1", variant_id="v1")),
    ]
    clear_registry(); load_builtin_metrics()
    g3 = compute_group("process.trajectory_efficiency", vitems, params=params)
    check("T1 轨迹效率 = (10/10 + 20/10)/2 = 1.5", g3.value == 1.5, f"value={g3.value}")

    # wall_clock 分位数：10..90 线性插值 P50=50 P95=86
    # 注意： members 必须自带证据锚点，否则 `_with_group_evidence` 直接降级（见 W3 结论）
    witems = [(MetricValue(key="cost.wall_clock_p50", label_zh="p50", value=10.0 * i,
                           evidence=[EvidenceRef(event_ids=[f"x{i:05d}"])]),
               RunMembership(wall_clock_ms=10 * i)) for i in range(1, 10)]
    clear_registry(); load_builtin_metrics()
    g4 = compute_group("cost.wall_clock_p50", witems, params=params)
    clear_registry(); load_builtin_metrics()
    g5 = compute_group("cost.wall_clock_p95", witems, params=params)
    check("U1 P50 = 50（线性插值）", g4.value == 50.0, f"value={g4.value}")
    check("U2 P95 = 86（线性插值）", g5.value == 86.0, f"value={g5.value}")
    clear_registry(); load_builtin_metrics()
    g6 = compute_group("cost.wall_clock_p50", [(mv_of("cost.wall_clock_p50", None), None)],
                       params=params)
    check("U3 无 membership → null", g6.value is None, f"value={g6.value}")
    # 链式依赖：成员 MV 无证据时，即使 membership 有 wall_clock_ms 也出不了值
    noev = [(mv_of("cost.wall_clock_p50", None), RunMembership(wall_clock_ms=100 * i))
            for i in range(1, 4)]
    clear_registry(); load_builtin_metrics()
    g7 = compute_group("cost.wall_clock_p50", noev, params=params)
    check("W3 成员无证据 → 即使 T10 供了 wall_clock_ms 也 null（链式依赖记录）",
          g7.value is None,
          f"value={g7.value}；结合 W1：run 级缺 run.start/run.end 则无证据锚点 → "
          "墙钟链路实际需要 T10 同时合成起止事件，不只是填 membership 字段")


def message_merge() -> None:
    print("\n== T06-11 message.recv stale 变体二次归并 ==")
    evs = [
        mk(0, "message.recv", {"message_id": "m1", "role": "teammate"}),
        mk(1, "message.recv", {"message_id": "m2", "role": "teammate"}),
        mk(2, "message.recv", {"message_id": "m1", "role": "teammate"}),   # stale 变体
        mk(3, "message.recv", {"role": "teammate"}),                        # 无 id，应保留
    ]
    params = MetricParams()
    merged = _merge_message_recv(evs, params)
    ids = [e["event_id"] for e in merged]
    check("M1 同 message_id 保留最小 seq", ids == ["x00000", "x00001", "x00003"],
          f"merged={ids}（期望 ['x00000','x00001','x00003']）")
    # 乱序追加：后到的更早
    evs2 = [mk(5, "message.recv", {"message_id": "m9"}), mk(2, "message.recv", {"message_id": "m9"})]
    m2 = _merge_message_recv(evs2, params)
    check("M2 乱序追加取最小 seq", [e["event_id"] for e in m2] == ["x00002"],
          f"merged={[e['event_id'] for e in m2]}")


def low_sample_confidence_ruling() -> None:
    print("\n== T06-09b confidence 裁定逐情形复核（合成充足样本 n≥20）==")
    evs = [mk(i, "step.start", {}) for i in range(25)]
    evs += [mk(100 + i, "tool.call", {"tool_name": "Read", "span_id": f"R{i}",
                                      "content_sha256": f"h{i}"}) for i in range(25)]
    clear_registry(); load_builtin_metrics()
    r = compute_run(FakeStore(evs), "qa-extra")
    expect = {
        "process.step_count": "high",           # n=25 原生充足
        "process.redundant_loop": "high",       # n=25 原生充足（本例无循环，值 0）
        "process.retry_rate": "high",           # 分母 25
        "process.tool_correctness": "low",      # 上界口径 → 恒 low
    }
    for key, want in expect.items():
        mv = r.by_key[key]
        got = mv.confidence.value if mv.confidence is not None else None
        check(f"C1 {key} (value={mv.value})", got == want,
              f"裁定 {want!r} / 实测 {got!r}"
              + ("　→ value 非 null 却 confidence=null：按裁定判失败" if got is None else ""))
    for key in ("result.success", "cost.tokens_total"):
        mv = r.by_key[key]
        got = mv.confidence.value if mv.confidence is not None else None
        check(f"C2 降级项 {key} confidence 必须 null", mv.value is None and got is None,
              f"value={mv.value} confidence={got!r}")


def main() -> int:
    print("QA T06 自选探测：真实数据覆盖不到的分支")
    success_paths()
    wall_clock_with_membership()
    group_aggregators()
    message_merge()
    low_sample_confidence_ruling()
    print("\n" + "=" * 60)
    print(json.dumps({"total_failures": len(FAILURES), "failures": FAILURES},
                     ensure_ascii=False, indent=2))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())

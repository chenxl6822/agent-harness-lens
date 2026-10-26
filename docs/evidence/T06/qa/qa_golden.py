"""QA 独立复核脚本：G4 口径正确性（golden trace，人工期望值内联推导，不引用实现方数字）。

独立于实现方的 test_metrics_core.py：本脚本自己造事件、自己推导期望值、自己断言。
运行：.venv/Scripts/python.exe docs/evidence/T06/qa/qa_golden.py
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "engine"))

from hs_core.metrics import (  # noqa: E402
    MetricContext,
    MetricOutcome,
    MetricParams,
    MetricRegistryError,
    clear_registry,
    compute_run,
    load_builtin_metrics,
    register_metric,
)

SCHEMA_VERSION = "1.0.0"
BASE_MS = 1_700_000_000_000
# 真实 recover 用的内容哈希（绝不能等于 sha256(b"")，否则会被判定为空输入签名）
H_READ = hashlib.sha256(b"qa-read-args").hexdigest()
H_BASH = hashlib.sha256(b"qa-bash-args").hexdigest()


def mk(seq: int, typ: str, payload: dict, **extra) -> dict:
    ev = {
        "schema_version": SCHEMA_VERSION,
        "event_id": f"qaev{seq:04d}",
        "run_id": "qa-golden",
        "seq": seq,
        "ts": f"2026-01-01T00:00:{seq:02d}.000Z",
        "session_id": "qa-session",
        "type": typ,
        "source": {"platform": "codebuddy", "channel": "file"},
        "labels": {},
        "payload": payload,
    }
    ev.update(extra)
    return ev


class FakeStore:
    """内存替身，只实现 compute_run 实际用到的两个方法；顺序 = ORDER BY seq。"""

    def __init__(self, events: list[dict], policy: str = "primary_only") -> None:
        self._events = sorted(events, key=lambda e: int(e["seq"]))
        self._policy = policy

    def get_run(self, run_id: str) -> dict:
        return {"run_id": run_id, "platform": "codebuddy",
                "channel_policy": self._policy, "event_count": len(self._events)}

    def iter_events(self, run_id: str, order_by: str = "seq"):
        assert order_by == "seq", f"出现第二排序键 {order_by!r}（T06-04 禁止）"
        return iter(self._events)


# ---------------------------------------------------------------------------
# golden trace 构造（每步都写出推导）
# ---------------------------------------------------------------------------
# seq 顺序如下（0 起）：
#   0  step.start
#   1  tool.call  n1 (Read, H_READ) span=S1
#   2  tool.result r1 span=S1
#   3  tool.call  n2 (Bash, H_BASH) span=S2
#   4  tool.error  e1 tool_name=Bash content_sha256=H_BASH（无 span_id/call_id）
#   5  step.start
#   6  tool.call  n3 (Read, H_READ) span=S3
#   7  tool.result r3 span=S3
#   8  tool.call  n4 (Bash, H_BASH) span=S4
#   9  tool.result r4 span=S4
#  10  tool.call  n5 (Read, H_READ) span=S5
#  11  tool.result r5 span=S5
#  12  tool.call  n6 (Bash, H_BASH) span=S6
#  13  tool.result r6 span=S6
#  14  step.start
#
# 【人工期望推导】
# step_count      = step.start 计数 = seq 0/5/14 → 3
# 恒等式          tool.call 6 == tool.result 5 + tool.error 1 → 成立
# tool_correctness 配对：e1 无 span_id/call_id → 走第③层同签名时序最近邻；
#                  同签名 (Bash,H_BASH) 的 call 是 n2(seq3)/n4(seq8)/n6(seq12)；
#                  distance = call.seq - err.seq 须 >0 且 ≤ retry_window_events(10)：
#                  n2 → -1（排除）；n4 → 4；n6 → 8。取最小正距离 = n4。
#                  → errored_calls = 1 → (6-1)/6 = 0.833333
# redundant_loop  参与判定的签名序列（按 seq）= [A,B,A,B,A,B]，A=(Read,H_READ) B=(Bash,H_BASH)
#                  i=0: p=1 → sigs[0]!=sigs[1] 不匹配；p=2 → sigs[0]==sigs[2] 且 sigs[1]==sigs[3]
#                       匹配 → p=2，向右扩展至 j=6，covered=6 → redundant = 6//2 - 1 = 2
# retry_rate      e1 同签名 (Bash,H_BASH) 向后找 call；窗口 ms 与 seq 兜底都放行 → 命中 n4
#                  → matched=1，分母 = tool.call = 6 → 1/6 = 0.166667
# ---------------------------------------------------------------------------

def build_golden_events() -> list[dict]:
    evs: list[dict] = []
    plan = [
        ("step.start", {}),
        ("tool.call", {"tool_name": "Read", "span_id": "S1", "content_sha256": H_READ}),
        ("tool.result", {"tool_name": "Read", "span_id": "S1", "content_sha256": H_READ}),
        ("tool.call", {"tool_name": "Bash", "span_id": "S2", "content_sha256": H_BASH}),
        ("tool.error", {"tool_name": "Bash", "content_sha256": H_BASH}),
        ("step.start", {}),
        ("tool.call", {"tool_name": "Read", "span_id": "S3", "content_sha256": H_READ}),
        ("tool.result", {"tool_name": "Read", "span_id": "S3", "content_sha256": H_READ}),
        ("tool.call", {"tool_name": "Bash", "span_id": "S4", "content_sha256": H_BASH}),
        ("tool.result", {"tool_name": "Bash", "span_id": "S4", "content_sha256": H_BASH}),
        ("tool.call", {"tool_name": "Read", "span_id": "S5", "content_sha256": H_READ}),
        ("tool.result", {"tool_name": "Read", "span_id": "S5", "content_sha256": H_READ}),
        ("tool.call", {"tool_name": "Bash", "span_id": "S6", "content_sha256": H_BASH}),
        ("tool.result", {"tool_name": "Bash", "span_id": "S6", "content_sha256": H_BASH}),
        ("step.start", {}),
    ]
    for seq, (typ, payload) in enumerate(plan):
        evs.append(mk(seq, typ, payload))
    return evs


EXPECTED_VALUES = {
    "process.step_count": 3.0,
    "process.tool_correctness": round(5 / 6, 6),
    "process.redundant_loop": 2.0,
    "process.retry_rate": round(1 / 6, 6),
}
# T06-09b / Q7 裁定：n<20 → low；上界口径 → low；降级 → null
EXPECTED_CONFIDENCE = {
    "process.step_count": "low",                 # n=3 < 20
    "process.tool_correctness": "low",           # 上界口径恒 low（且 n=6<20）
    "process.redundant_loop": "low",             # n=6 < 20
    "process.retry_rate": "low",                 # 分母 6 < 20
}


def main() -> int:
    failures: list[str] = []
    clear_registry()
    load_builtin_metrics()
    result = compute_run(FakeStore(build_golden_events()), "qa-golden",
                         params=MetricParams())
    by_key = result.by_key

    print("== G4 golden trace 口径复核 ==")
    for key, want in EXPECTED_VALUES.items():
        got = by_key[key].value
        ok = got == want
        print(f"  [{'OK ' if ok else 'FAIL'}] {key:30s} 期望 {want}  实测 {got}")
        if not ok:
            failures.append(f"{key}: 期望 {want}，实测 {got}")

    print("== G4b confidence 规则复核（Q7 / T06-09b）==")
    for key, want in EXPECTED_CONFIDENCE.items():
        got = by_key[key].confidence
        got = got.value if got is not None else None
        ok = got == want
        print(f"  [{'OK ' if ok else 'FAIL'}] {key:30s} 裁定应填 {want!r:8} 实测 {got!r}")
        if not ok:
            failures.append(f"{key}.confidence: 裁定 {want!r}，实测 {got!r}")

    print("== G15① 合成用例恒等式硬断言 ==")
    types = [ev["type"] for ev in build_golden_events()]
    c, r, e = types.count("tool.call"), types.count("tool.result"), types.count("tool.error")
    print(f"  tool.call {c} == tool.result {r} + tool.error {e} = {r + e} → {c == r + e}")
    if c != r + e:
        failures.append("golden 恒等式不成立")

    print("== G1b 重复注册同 key 必须显式报错 ==")
    try:
        @register_metric(key="process.step_count", group="process",
                         label_zh="重复注册测试", unit="count")
        def _dup(events, params, ctx):
            return MetricOutcome.na("no_source", "dup")
        failures.append("重复注册同 key 未抛异常（静默覆盖 = G1 不通过）")
        print("  [FAIL] 重复注册 process.step_count 未抛异常")
    except MetricRegistryError as exc:
        print(f"  [OK ] 抛出 MetricRegistryError: {str(exc)[:60]}…")

    print()
    print(json.dumps({"failures": failures, "n_fail": len(failures)},
                     ensure_ascii=False, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""T06 指标纯函数单测：golden trace 验算 + 降级语义 + 前向兼容 + 参数化。

运行（必须带 --basetemp，本机默认 tmp 在 D: 盘被沙箱拒绝）：
.venv/Scripts/python.exe -m pytest engine/tests/test_metrics_core.py -q --basetemp=.tmp-pytest

golden trace 的期望值全部写在用例里（G4：数值与人工期望一致）。
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "engine"))

from hs_core.metrics import (
    MetricContext,
    MetricParams,
    RunMembership,
    VerdictRecord,
    clear_registry,
    compute_group,
    list_metrics,
    load_builtin_metrics,
)
from hs_core.metrics_core import (
    _call_signatures,
    _merge_message_recv,
    _scan_loops,
    _usage_of,
    cost_cny,
    redundant_loop,
    retry_rate,
    step_count,
    success_rate,
    tokens_in,
    tokens_out,
    tokens_total,
    tool_correctness,
    trajectory_efficiency,
    wall_clock_p50,
    wall_clock_p95,
)

RUN = "t06-unit"
EMPTY_SHA = hashlib.sha256(b"").hexdigest()


# ---------------------------------------------------------------------------
# golden trace 小件
# ---------------------------------------------------------------------------

def ev(seq: int, typ: str, payload: dict | None = None, *, ts: str | None = None,
       agent_id: str | None = None, labels: dict | None = None) -> dict:
    """构造一条事件（只填指标函数会读的字段）。"""
    item = {
        "schema_version": "1.0.0",
        "event_id": f"EV{seq:024d}",
        "run_id": RUN,
        "seq": seq,
        "ts": ts or f"2026-10-26T00:00:{seq:02d}.000Z",
        "session_id": "s1",
        "type": typ,
        "source": {"platform": "codebuddy", "channel": "file"},
        "labels": labels or {},
        "payload": payload or {},
    }
    if agent_id is not None:
        item["agent_id"] = agent_id
    return item


def ctx(*, verdicts=None, membership=None) -> MetricContext:
    return MetricContext(run_id=RUN, platform="codebuddy", channel_policy="primary_only",
                         event_count=0, verdicts=verdicts, membership=membership)


def call(seq: int, name: str, sha: str, *, span: str | None = None,
         ts: str | None = None) -> dict:
    payload = {"tool_name": name, "span_id": span or f"span_{seq}", "content_sha256": sha,
               "content_bytes": 10}
    return ev(seq, "tool.call", payload, ts=ts)


def result(seq: int, name: str, sha: str, *, span: str | None = None,
           is_error: bool = False) -> dict:
    payload = {"tool_name": name, "span_id": span or f"span_{seq}", "content_sha256": sha,
               "content_bytes": 10}
    return ev(seq, "tool.error" if is_error else "tool.result", payload)


# ---------------------------------------------------------------------------
# process.step_count
# ---------------------------------------------------------------------------

def test_step_count_golden():
    events = [ev(0, "step.start"), ev(1, "step.start"), ev(2, "step.end"),
              ev(3, "tool.call", {"tool_name": "Bash", "content_sha256": "x"})]
    out = step_count(events, MetricParams(), ctx())
    assert out.value == 2.0                       # 只数 step.start，不数 step.end
    assert out.unit is None                       # 用注册元信息里的 count
    assert "agent_id 非空 0/2" in out.evidence[0].note
    assert "不可按成员拆分" in out.evidence[0].note


def test_step_count_no_source_when_absent():
    out = step_count([ev(0, "tool.call", {"tool_name": "Read"})], MetricParams(), ctx())
    assert out.value is None and out.reason == "no_source"
    assert "无 step.start" in out.detail


# --- R1-1 口径锁定（防回归：只能用 step.start 事件计数） ---

def test_step_count_uses_step_start_events():
    """3 条 `step.start` → 3；`agent/step` 形态的事件与 `step.end` 都不进分子。"""
    events = [
        ev(0, "step.start"), ev(1, "step.start"), ev(2, "step.end"),
        ev(3, "step.start"),
        # dsh 的 `agent/step` 只发一次/会话，被误用会让步数恒为 1 —— 造出来证明它不被数
        ev(4, "metric.custom", {"event": "agent/step", "step": 99}),
    ]
    out = step_count(events, MetricParams(), ctx())
    assert out.value == 3.0
    assert "事件计数 3" in out.note

    # 只有 `agent/step` 形态、没有任何 step.start → 如实降级，不拿它当步数
    only_ledger = [ev(0, "metric.custom", {"event": "agent/step", "step": 42})]
    out2 = step_count(only_ledger, MetricParams(), ctx())
    assert out2.value is None and out2.reason == "no_source"


def test_step_count_crosscheck_with_pre_step():
    """自造 dsh 形态的 step.start（payload 带 turn/step）验证交叉校验分支。"""
    # 一致：事件计数 3，payload step 最大值 3，差异 0
    ok_events = [ev(0, "step.start", {"turn": 1, "step": 1, "messages_len": 2}),
                 ev(1, "step.start", {"turn": 1, "step": 2, "messages_len": 4}),
                 ev(2, "step.start", {"turn": 2, "step": 3, "messages_len": 6})]
    out = step_count(ok_events, MetricParams(), ctx())
    assert out.value == 3.0
    assert "`pre-step` 的 `step` 最大值 3，差异 0" in out.note
    assert "以事件计数为准" not in out.note          # 一致时不加这句

    # 不一致（payload step 最大 7 > 事件计数 3）→ 以事件计数为准，差异写进 note
    bad_events = [ev(0, "step.start", {"turn": 1, "step": 1}),
                  ev(1, "step.start", {"turn": 1, "step": 2}),
                  ev(2, "step.start", {"turn": 3, "step": 7})]
    out2 = step_count(bad_events, MetricParams(), ctx())
    assert out2.value == 3.0                        # 事件计数赢，不被 payload 覆盖
    assert "`pre-step` 的 `step` 最大值 7，差异 -4" in out2.note
    assert "两口径不一致，以事件计数为准" in out2.note
    assert "含 `step` 字段的 step.start 3/3 条" in out2.note


# ---------------------------------------------------------------------------
# process.tool_correctness（分母 = tool.call）
# ---------------------------------------------------------------------------

def test_tool_correctness_golden():
    """golden：4 次调用 = 3 次成功 result + 1 次 error；错误调用 1 次 → 3/4 = 0.75。

    通道 A 的真实形态：error 的 span_id 与对应 call 的 span_id 相同（同一 span 的 start/end）。
    """
    events = [call(0, "Read", "a", span="sp0"), call(1, "Read", "b", span="sp1"),
              result(2, "Read", "a", span="sp0"), result(3, "Read", "b", span="sp1"),
              call(4, "Write", "c", span="sp4"), call(5, "Write", "d", span="sp5"),
              result(6, "Write", "d", span="sp5"),
              result(7, "Write", "c", span="sp4", is_error=True)]
    out = tool_correctness(events, MetricParams(), ctx())
    assert out.value == 0.75
    assert out.reason is None
    assert "deny/permission 信号当前无数据源（本 run 扫描 8 条事件，deny/permission 标记命中 0）" in out.note
    assert "本值为正确率上界，口径为派生" in out.note
    assert out.confidence == "low"          # Q7：上界/派生口径恒 low
    assert "分母=tool.call 4 次" in out.note
    assert "分子=无配对 error 的调用 3 次（错误调用 1 次）" in out.note
    assert "配对层级命中 span_id 1" in out.note
    # G15：合成 golden 里硬断言分母恒等式成立
    assert "tool.call 4 vs tool.result 3 + tool.error 1 = 4（成立）" in out.note
    assert len(out.evidence) == 2 and out.evidence[1].event_ids      # 错误调用 + 全量调用两段证据
    assert len(out.evidence[1].event_ids) == 4                        # 分母证据 = 全部 4 次调用


def test_tool_correctness_double_count_union():
    """同一事件既 tool.error 又 is_error=True 只计一次（取并集，不双计）。"""
    events = [call(0, "Read", "a", span="sp0"), call(1, "Read", "b", span="sp1"),
              ev(2, "tool.error", {"tool_name": "Read", "span_id": "sp0",
                                   "content_sha256": "a", "is_error": True}),
              result(3, "Read", "b", span="sp1")]
    out = tool_correctness(events, MetricParams(), ctx())
    assert out.value == 0.5                       # 1 次错误调用 / 2 次调用，不是 2/2


def test_tool_correctness_zero_denominator_degrades():
    out = tool_correctness([ev(0, "step.start")], MetricParams(), ctx())
    assert out.value is None and out.reason == "insufficient_data"
    assert "分母为 0" in out.detail


def test_tool_correctness_two_errors_same_call_count_once():
    """一次调用被两个 error 命中仍只算 1 次错误调用（按调用去重，不重复扣分）。"""
    events = [call(0, "Read", "a", span="sp0"),
              result(1, "Read", "a", span="sp0", is_error=True),
              result(2, "Read", "a", span="sp0", is_error=True)]
    out = tool_correctness(events, MetricParams(), ctx())
    assert out.value == 0.0
    assert "错误调用 1 次" in out.note


def test_tool_correctness_matches_by_call_id_sigpair_and_reports_unmatched():
    """配对层级：call_id 命中 + 无键 error 报 unmatched（不因配不上而假装调用正确）。"""
    events = [
        ev(0, "tool.call", {"tool_name": "Bash", "call_id": "c1", "content_sha256": "h1"}),
        ev(1, "tool.error", {"tool_name": "Bash", "call_id": "c1"}),
        ev(2, "tool.call", {"tool_name": "Read", "call_id": "c2", "content_sha256": "h2"}),
        # 无任何配对键的 error：归属不明 → 报 unmatched_errors，不硬塞给某次调用
        ev(3, "tool.error", {"tool_name": "Unknown"}),
    ]
    out = tool_correctness(events, MetricParams(), ctx())
    assert out.value == 0.5                       # 1 次错误调用 / 2 次调用
    assert "配对层级命中 span_id 0 / call_id 1 / 同签名最近邻 0" in out.note
    assert "unmatched_errors 1" in out.note


def test_tool_correctness_reports_unmatchable_calls():
    """三种配对键全无的调用计入 unmatchable_calls（无法配对 → 只能算无配对，属上界口径）。"""
    events = [call(0, "Read", "a", span="sp0"), result(1, "Read", "a", span="sp0"),
              ev(2, "tool.call", {"tool_name": "Read"})]      # 无 span_id/call_id/签名
    out = tool_correctness(events, MetricParams(), ctx())
    assert out.value == 1.0
    assert "unmatchable_calls 1 次" in out.note


# ---------------------------------------------------------------------------
# process.redundant_loop
# ---------------------------------------------------------------------------

def test_scan_loops_period_two():
    sigs = [("A", "1"), ("B", "2"), ("A", "1"), ("B", "2"), ("A", "1"), ("B", "2")]
    total, segments = _scan_loops(sigs, MetricParams())
    assert total == 2                             # 3 轮循环、周期 2 → 冗余 2 次
    assert segments[0]["period"] == 2


def test_redundant_loop_golden():
    events = [call(0, "Read", "a"), call(1, "Read", "b"),
              call(2, "Read", "a"), call(3, "Read", "b"),
              call(4, "Read", "a"), call(5, "Read", "b")]
    out = redundant_loop(events, MetricParams(), ctx())
    assert out.value == 2.0
    assert "循环段 1 个" in out.note
    assert out.evidence[0].event_ids              # 冗余重复的调用可下钻


def test_redundant_loop_none_found_is_zero_not_null():
    events = [call(0, "Read", "a"), call(1, "Read", "b"), call(2, "Grep", "c")]
    out = redundant_loop(events, MetricParams(), ctx())
    assert out.value == 0.0 and out.reason is None
    assert "未检出周期循环" in out.evidence[0].note


def test_redundant_loop_uses_span_end_hash_when_call_hash_empty():
    """通道 A：tool.call 的 content_sha256 是空输入常量哈希，需按 span_id 取 span_end 的哈希。"""
    events = [
        call(0, "Bash", EMPTY_SHA, span="sp1"), result(4, "Bash", "h1", span="sp1"),
        call(1, "Read", EMPTY_SHA, span="sp2"), result(5, "Read", "h2", span="sp2"),
        call(2, "Bash", EMPTY_SHA, span="sp3"), result(6, "Bash", "h1", span="sp3"),
        call(3, "Read", EMPTY_SHA, span="sp4"), result(7, "Read", "h2", span="sp4"),
    ]
    out = redundant_loop(events, MetricParams(), ctx())
    assert out.value == 1.0                       # [h1,h2,h1,h2] → 周期 2，冗余 1
    assert "4 次调用的哈希由 span_id" in out.note
    assert "结果内容哈希" in out.note       # R1-6：签名语义必须声明
    # R2-1：content_sha256 去重取值数 == 1（全为空常量）→ 代理来源，样本量再小也是 low
    assert out.confidence == "low"
    assert "签名来源=代理" in out.note


def test_native_signature_count_ignores_missing_key():
    """R2-1 反例（QA 构造）：「键缺失 + 有兄弟哈希」的 call **不得**被算成原生。

    禁止用 `native = total - empty_own` 反推——`empty_own` 只统计「值为空常量」，
    「键缺失」不在内，两者不是补集；一条键缺失但仍从 span_end 拿到代理签名的 call
    会被误算成原生，进而走 n 阈值标 high，而它的签名 100% 是代理键。
    必须正向计数（native 只数自身可用哈希、proxy 只数 span_end 补齐），
    且 confidence 走「未知」分支：30 次调用（n ≥ low_confidence_n=20）也必须是 low。
    """
    events = []
    for i in range(30):
        span = f"sp{i:03d}"
        # tool.call **不带** content_sha256 键（键缺失 ≠ 值为空常量），span_end 才有哈希
        events.append(ev(2 * i, "tool.call", {"tool_name": "Bash", "span_id": span}))
        events.append(ev(2 * i + 1, "tool.result",
                         {"tool_name": "Bash", "span_id": span, "content_sha256": f"h{i:03d}"}))
    calls = [e for e in events if e["type"] == "tool.call"]
    sigs, joined, empty_own, native = _call_signatures(calls, events)
    assert native == 0                            # 正向计数：没有一个 call 有自身可用哈希
    assert joined == 30                           # proxy > 0：全部经 span_id 从 span_end 补齐
    assert empty_own == 0                         # 键缺失不等于「值为空常量」——两者不是补集
    assert all(sig is not None for sig in sigs)   # 签名确实全拿到了（代理的）
    out = redundant_loop(events, MetricParams(), ctx())
    assert out.confidence == "low"                # n=30 ≥ 20 也救不回来：键缺失 → 未知分支
    assert "签名来源未采集" in out.note
    out2 = retry_rate(events, MetricParams(), ctx())
    assert out2.confidence == "low"
    assert "签名来源未采集" in out2.note


def test_signature_confidence_rises_to_high_with_real_hashes():
    """R2-1 前向兼容：T05 v2 补到真实 content 后，签名自动从代理变回原生 → 自动升 high。

    这是「必须动态、不能写死」的验收面：写死 low 等于把「现在测不准」固化成「永远测不准」。
    """
    events = [call(i, "Bash", f"h{i:03d}") for i in range(30)]   # 30 个互不相同的真实哈希
    out = redundant_loop(events, MetricParams(), ctx())
    assert out.confidence == "high"
    assert "签名来源=原生" in out.note
    # 样本不足在原生基础上**再降**：同样原生口径，n < low_confidence_n → low
    few = [call(i, "Bash", f"h{i:03d}") for i in range(3)]
    assert redundant_loop(few, MetricParams(), ctx()).confidence == "low"


def test_redundant_loop_real_shape_value_locked_and_bounded():
    """R2-2：三份独立快照（主理人 9098 / 架构师 9809 / QA 12411 事件）实测 redundant_loop == 1。

    合成等形轨迹（tool.call 自身哈希全为空常量、签名由 span_end 补齐、921 次调用中恰有
    一处相邻重复）锁定该值。反向断言 ≤ 50：架构师实测「按 (tool_name, content_sha256)
    关键名字段做签名」会得到 **602**（539 次 Bash 全同签名）——602 是签名口径失效时的
    特征值，出现即视为缺陷；这条断言能在 T05 v2 之前拦住「签名悄悄退回代理键且没被识别」。
    """
    events = []
    seq = 0
    for i in range(921):
        span = f"sp{i:04d}"
        # 921 次调用中仅 i==500 与 i==501 共享同一哈希（一次相邻重复 → 冗余 1 次）
        sha = "dup" if i in (500, 501) else f"h{i:04d}"
        events.append(call(seq, "Bash", EMPTY_SHA, span=span))
        seq += 1
        events.append(result(seq, "Bash", sha, span=span))
        seq += 1
    out = redundant_loop(events, MetricParams(), ctx())
    assert out.value == 1.0
    assert out.value <= 50                        # 反向断言：> 50 = 签名失效特征值（反事实 602）
    assert out.confidence == "low"                # R2-1：代理签名 → low


def test_redundant_loop_all_ineligible_degrades():
    events = [call(0, "Bash", EMPTY_SHA, span="s1"), call(1, "Bash", EMPTY_SHA, span="s2")]
    out = redundant_loop(events, MetricParams(), ctx())
    assert out.value is None and out.reason == "insufficient_data"
    assert "自身为空输入常量哈希 2 次" in out.detail


# ---------------------------------------------------------------------------
# process.retry_rate
# ---------------------------------------------------------------------------

def test_retry_rate_golden():
    events = [
        call(0, "Write", "a", ts="2026-10-26T00:00:00.000Z"),
        ev(1, "tool.error", {"tool_name": "Write", "span_id": "span_0", "content_sha256": "a"},
           ts="2026-10-26T00:00:01.000Z"),
        call(2, "Write", "a", ts="2026-10-26T00:00:02.000Z"),   # 1s 后同签名重发 → 命中
        call(3, "Read", "b", ts="2026-10-26T00:00:03.000Z"),
    ]
    out = retry_rate(events, MetricParams(), ctx())
    assert out.value == pytest.approx(1 / 3, abs=1e-6)
    assert "matched 1 / unmatched 0" in out.note


def test_retry_rate_outside_window_does_not_match():
    events = [
        ev(0, "tool.error", {"tool_name": "Write", "span_id": "span_9", "content_sha256": "a"},
           ts="2026-10-26T00:00:00.000Z"),
        call(1, "Write", "a", ts="2026-10-26T00:05:00.000Z"),   # 300s > 60s 窗口
        call(2, "Read", "b", ts="2026-10-26T00:05:01.000Z"),
    ]
    out = retry_rate(events, MetricParams(), ctx())
    assert out.value == 0.0
    assert "matched 0 / unmatched 1" in out.note


def test_retry_rate_seq_fallback_when_timestamp_unparsable():
    events = [
        ev(0, "tool.error", {"tool_name": "Write", "span_id": "span_9", "content_sha256": "a"},
           ts="not-a-timestamp"),
        call(3, "Write", "a", ts="also-bad"),
        call(20, "Write", "a", ts="also-bad"),                 # seq 距离 20 > 10，不算
    ]
    out = retry_rate(events, MetricParams(), ctx())
    assert out.value == pytest.approx(1 / 2, abs=1e-6)
    assert "退化 seq 窗口=10" in out.note


def test_retry_rate_zero_when_no_calls():
    out = retry_rate([ev(0, "step.start")], MetricParams(), ctx())
    assert out.value is None and out.reason == "insufficient_data"


# ---------------------------------------------------------------------------
# cost.* —— 前向兼容（G7）
# ---------------------------------------------------------------------------

def test_usage_of_reads_all_known_shapes():
    assert _usage_of(ev(0, "llm.response", {"usage": {"input_tokens": 10, "output_tokens": 5}})) == \
        {"in": 10, "out": 5, "total": 15, "cached_in": None}
    assert _usage_of(ev(0, "llm.response", {"providerData": {"usage": {"prompt_tokens": 7,
                                                                     "completion_tokens": 3}}})) == \
        {"in": 7, "out": 3, "total": 10, "cached_in": None}
    assert _usage_of(ev(0, "llm.response", {"inputTokens": 2, "outputTokens": 4})) == \
        {"in": 2, "out": 4, "total": 6, "cached_in": None}
    assert _usage_of(ev(0, "llm.response", {"totalTokens": 9})) == \
        {"in": None, "out": None, "total": 9, "cached_in": None}
    assert _usage_of(ev(0, "llm.response", {"usage": {"input_tokens": "12", "output_tokens": 1}})) == \
        {"in": 12, "out": 1, "total": 13, "cached_in": None}
    assert _usage_of(ev(0, "llm.response", {"usage": {"input_tokens": -3}})) is None
    assert _usage_of(ev(0, "llm.response", {"span_name": "x", "status": "ok"})) is None


def test_tokens_metrics_read_usage_when_present():
    events = [
        ev(0, "llm.response", {"usage": {"input_tokens": 10, "output_tokens": 5}}),
        ev(1, "llm.response", {"providerData": {"usage": {"promptTokens": 7, "completionTokens": 3}}}),
    ]
    assert tokens_total(events, MetricParams(), ctx()).value == 25.0
    assert tokens_in(events, MetricParams(), ctx()).value == 17.0
    assert tokens_out(events, MetricParams(), ctx()).value == 8.0
    assert tokens_total(events, MetricParams(), ctx()).confidence == "low"  # n=2 < 20


def test_tokens_metrics_partial_coverage_marks_low_confidence():
    events = [
        ev(0, "llm.response", {"usage": {"input_tokens": 10, "output_tokens": 5}}),
        ev(1, "llm.response", {"span_name": "generation", "status": "ok", "duration_ms": 12}),
    ]
    out = tokens_total(events, MetricParams(), ctx())
    assert out.value == 15.0
    assert out.confidence == "low"
    assert "覆盖度 1/2" in out.note


def test_tokens_metrics_no_source_current_real_shape():
    """当前真实形态：llm.response 只有 {span_name, status, duration_ms}。"""
    events = [ev(i, "llm.response", {"span_name": "generation", "status": "ok",
                                     "duration_ms": 100}) for i in range(3)]
    for fn in (tokens_total, tokens_in, tokens_out):
        out = fn(events, MetricParams(), ctx())
        assert out.value is None and out.reason == "no_source"
        assert "通道 B transcript" in out.detail
        assert "T05 v2" in out.detail
        # R1-6：detail 的数字必须是本 run 现场统计，历史快照只能引用式出现
        assert "本 run `llm.response` 3 条" in out.detail
        assert "本 run `metric.custom`/trace_summary 0 条" in out.detail
        assert "引用（**非本 run 观测**）" in out.detail
        assert "实测命中 0" not in out.detail
        assert "33/33" not in out.detail


# --- 条件二路径（metric.custom/trace_summary），G14 四情形 ---

def _trace_summary(seq: int, tokens) -> dict:
    return ev(seq, "metric.custom", {"metric": "trace_summary", "span_count": 1,
                                     "total_tokens": tokens, "error_count": 0})


def test_tokens_trace_fallback_off_by_default_even_with_total_tokens():
    """① 开关默认 False：即使 metric.custom 有 total_tokens，tokens_total 仍 null。"""
    events = [_trace_summary(0, 5000)]
    out = tokens_total(events, MetricParams(), ctx())
    assert out.value is None and out.reason == "no_source"
    assert MetricParams().tokens_trace_summary_fallback is False


def test_tokens_trace_fallback_on_yields_value_with_note():
    """② 开关 True 且请求级 usage 全无 → 出值，note 标注「trace 级汇总」。"""
    events = [_trace_summary(0, 5000), _trace_summary(1, 1000),
              ev(2, "llm.response", {"span_name": "g", "status": "ok", "duration_ms": 5})]
    out = tokens_total(events, MetricParams(tokens_trace_summary_fallback=True), ctx())
    assert out.value == 6000.0
    assert "trace 级汇总" in out.note and "非逐请求累加" in out.note
    assert out.confidence == "low"
    assert out.evidence[0].event_ids                     # 可下钻（G5）


def test_tokens_trace_fallback_does_not_double_count_request_level():
    """③ 开关 True 但 run 内有非零请求级 usage → 只走请求级（值 == 请求级累加，不相加）。"""
    events = [_trace_summary(0, 5000),
              ev(1, "llm.response", {"usage": {"input_tokens": 10, "output_tokens": 5}})]
    out = tokens_total(events, MetricParams(tokens_trace_summary_fallback=True), ctx())
    assert out.value == 15.0                             # 不是 5015


def test_tokens_in_out_never_use_trace_fallback():
    """④ 任一情形下 tokens_in / tokens_out 恒 null（trace_summary 只有 total）。"""
    events = [_trace_summary(0, 5000)]
    params = MetricParams(tokens_trace_summary_fallback=True)
    for fn in (tokens_in, tokens_out):
        out = fn(events, params, ctx())
        assert out.value is None and out.reason == "no_source"


def test_tokens_total_all_zero_usage_is_no_source():
    """provider 报了 usage 但全 0（占位字段形态）→ 不把 0 当「免费」。"""
    events = [ev(0, "llm.response", {"usage": {"input_tokens": 0, "output_tokens": 0}})]
    out = tokens_total(events, MetricParams(), ctx())
    assert out.value is None and out.reason == "no_source"
    assert "合计为 0" in out.detail


def test_trace_summary_tokens_probe_counts():
    from hs_core.metrics_core import trace_summary_tokens
    events = [_trace_summary(0, 0), _trace_summary(1, 0), _trace_summary(2, 7),
              ev(3, "metric.custom", {"metric": "other", "total_tokens": 100})]
    probe = trace_summary_tokens(events)
    assert probe == {"events": 3, "with_total": 3, "positive": 1, "sum": 7}


def test_cost_cny_golden_with_pricing():
    events = [ev(0, "llm.response", {"model": "m1",
                                     "usage": {"input_tokens": 1000, "output_tokens": 500}})]
    params = MetricParams(pricing={"m1": {"in": 0.001, "out": 0.002}})
    out = cost_cny(events, params, ctx())
    assert out.value == pytest.approx(2.0)        # 1000*0.001 + 500*0.002 = 2.0
    assert out.confidence == "low"          # 派生（换算式）口径恒 low
    assert out.reason is None


def test_cost_cny_default_price_and_placeholder():
    events = [ev(0, "llm.response", {"model": "m2",
                                     "usage": {"input_tokens": 10, "output_tokens": 10}})]
    out = cost_cny(events, MetricParams(pricing={"default": {"in": 0.5, "out": 0.5}}), ctx())
    assert out.value == pytest.approx(10.0) and out.confidence == "low"


def test_cost_cny_unconfigured_price_degrades():
    """pricing.yaml 本轮单价留 null：不得编造，必须降级并写明「单价表未配置」。"""
    events = [ev(0, "llm.response", {"model": "m1",
                                     "usage": {"input_tokens": 10, "output_tokens": 5}})]
    out = cost_cny(events, MetricParams(pricing={"default": {"in": None, "out": None}}), ctx())
    assert out.value is None and out.reason == "no_source"
    assert "单价表未配置" in out.detail


def test_cost_cny_degrades_when_tokens_missing():
    out = cost_cny([ev(0, "llm.response", {"duration_ms": 5})], MetricParams(), ctx())
    assert out.value is None and out.reason == "no_source"
    assert "连带" in out.detail


# ---------------------------------------------------------------------------
# cost.cny 缓存拆分（T05d）
# ---------------------------------------------------------------------------

_GLM = {"in": 0.0000008, "in_cached": 0.00000023, "out": 0.0000028}


def _usage_event(seq: int, model: str, *, cached: int | None, cached_camel: bool = False):
    """构造一条带 usage 的 llm.response（in=1000, cached=?, out=100）。"""
    usage: dict = {"input_tokens": 1000, "output_tokens": 100}
    if cached is not None:
        detail = {"cached_tokens": cached}
        if cached_camel:
            usage["inputTokensDetails"] = [detail]
        else:
            usage["input_tokens_details"] = detail
    return ev(seq, "llm.response", {"model": model, "usage": usage})


def test_cost_cny_cache_split_golden():
    """25 条 usage（in=1000, cached=800, out=100）：命中段按 in_cached，未命中段按 in。"""
    events = [_usage_event(i, "glm-5.3-flash", cached=800) for i in range(25)]
    expected = 25 * (200 * 0.8e-6 + 800 * 0.23e-6 + 100 * 2.8e-6)
    out = cost_cny(events, MetricParams(pricing={"glm-5.3-flash": dict(_GLM)}), ctx())
    assert out.value == pytest.approx(expected, abs=1e-6)
    assert out.confidence == "low"
    assert "缓存拆分" in out.note and "应用 25 条" in out.note


def test_cost_cny_cache_split_camelcase_detail():
    """camelCase 形态 inputTokensDetails[0].cached_tokens 同样读到。"""
    events = [_usage_event(0, "glm-5.3-flash", cached=800, cached_camel=True)]
    expected = 200 * 0.8e-6 + 800 * 0.23e-6 + 100 * 2.8e-6
    out = cost_cny(events, MetricParams(pricing={"glm-5.3-flash": dict(_GLM)}), ctx())
    assert out.value == pytest.approx(expected, abs=1e-6)


def test_usage_of_cached_in_none_when_missing():
    """cached_in 读不到是 None，绝不填 0（0 与「未采集」是两件事）。"""
    got = _usage_of(ev(0, "llm.response", {"usage": {"input_tokens": 10, "output_tokens": 5}}))
    assert got == {"in": 10, "out": 5, "total": 15, "cached_in": None}
    got = _usage_of(_usage_event(0, "m", cached=800))
    assert got["cached_in"] == 800


def test_cost_cny_cache_detail_missing_falls_back_to_miss_price():
    """cached_in 未采集 → 整段按未命中价（上界），note 写明回落原因。"""
    events = [_usage_event(i, "glm-5.3-flash", cached=None) for i in range(3)]
    expected = 3 * (1000 * 0.8e-6 + 100 * 2.8e-6)
    out = cost_cny(events, MetricParams(pricing={"glm-5.3-flash": dict(_GLM)}), ctx())
    assert out.value == pytest.approx(expected, abs=1e-6)
    assert "缓存明细未采集" in out.note


def test_cost_cny_no_cached_price_falls_back():
    """in_cached 为 null → 回落按未命中价，note 写明「无缓存单价」。"""
    events = [_usage_event(0, "m1", cached=800)]
    out = cost_cny(events, MetricParams(pricing={"m1": {"in": 0.001, "in_cached": None,
                                                        "out": 0.002}}), ctx())
    assert out.value == pytest.approx(1000 * 0.001 + 100 * 0.002, abs=1e-6)
    assert "无缓存单价" in out.note


def test_cost_cny_cached_greater_than_in_is_clamped():
    """cached > in（脏数据）→ 被 clamp 到 in，不出现负数成本。"""
    events = [_usage_event(0, "glm-5.3-flash", cached=5000)]
    # clamp 后 cached=1000，未命中段为 0：cost = 1000*0.23e-6 + 100*2.8e-6
    expected = 1000 * 0.23e-6 + 100 * 2.8e-6
    out = cost_cny(events, MetricParams(pricing={"glm-5.3-flash": dict(_GLM)}), ctx())
    assert out.value == pytest.approx(expected, abs=1e-6)
    assert out.value is not None and out.value >= 0


def test_cost_cny_unregistered_model_still_no_source():
    """未登记模型：default 也空 → no_source 且 detail 列出模型名（行为与 T05c 一致）。"""
    events = [_usage_event(0, "no-such-model", cached=800)]
    out = cost_cny(events, MetricParams(pricing={"default": {"in": None, "in_cached": None,
                                                             "out": None}}), ctx())
    assert out.value is None and out.reason == "no_source"
    assert "no-such-model" in out.detail


# ---------------------------------------------------------------------------
# cost.wall_clock_*
# ---------------------------------------------------------------------------

def test_wall_clock_no_source_without_run_events():
    for fn in (wall_clock_p50, wall_clock_p95):
        out = fn([ev(0, "session.start")], MetricParams(), ctx())
        assert out.value is None and out.reason == "no_source"
        assert "T10 编排层合成" in out.detail


def test_wall_clock_from_run_start_end_events():
    events = [ev(0, "run.start", ts="2026-10-26T00:00:00.000Z"),
              ev(1, "step.start", ts="2026-10-26T00:00:02.000Z"),
              ev(2, "run.end", ts="2026-10-26T00:00:10.000Z")]
    out = wall_clock_p50(events, MetricParams(), ctx())
    assert out.value == 10000.0
    assert out.confidence == "low"                # 单样本
    assert out.evidence[0].event_ids == ["EV000000000000000000000000", "EV000000000000000000000002"]


def test_wall_clock_from_membership():
    out = wall_clock_p95([], MetricParams(), ctx(membership=RunMembership(wall_clock_ms=5000)))
    # membership 有值但事件流里没有 run.start/run.end 可作锚点 → 如实降级（G5）
    assert out.value is None and out.reason == "no_source"
    assert "证据锚点" in out.detail


# ---------------------------------------------------------------------------
# result.*
# ---------------------------------------------------------------------------

def test_success_needs_input_without_verdicts():
    out = success_rate([], MetricParams(), ctx())
    assert out.value is None and out.reason == "needs_input"
    assert "T09" in out.detail


def test_success_golden_with_verdicts():
    events = [ev(0, "task.create", {"task_id": "t1"}),
              ev(1, "task.complete", {"task_id": "t1"}),
              ev(2, "task.create", {"task_id": "t2"}),
              ev(3, "task.complete", {"task_id": "t2"})]
    verdicts = (VerdictRecord(run_id=RUN, task_id="t1", passed=True),
                VerdictRecord(run_id=RUN, task_id="t2", passed=False))
    out = success_rate(events, MetricParams(), ctx(verdicts=verdicts))
    assert out.value == 0.5
    assert out.evidence[0].task_ids == ["t1", "t2"]


def test_success_ignores_other_runs_verdicts():
    verdicts = (VerdictRecord(run_id="other-run", task_id="t1", passed=True),)
    out = success_rate([ev(0, "task.complete", {"task_id": "t1"})], MetricParams(),
                       ctx(verdicts=verdicts))
    assert out.value is None and out.reason == "needs_input"
    assert "无一条 run_id == 本 run" in out.detail


def test_success_falls_back_to_last_event_when_unanchored():
    events = [ev(0, "task.create", {"task_id": "zz"}), ev(1, "step.start")]
    verdicts = (VerdictRecord(run_id=RUN, task_id="t1", passed=True),)
    out = success_rate(events, MetricParams(), ctx(verdicts=verdicts))
    assert out.value == 1.0
    assert "锚点退化为 run 末事件" in out.evidence[0].note


def test_pass_k_and_trajectory_degrade_at_run_level():
    assert trajectory_efficiency([], MetricParams(), ctx()).reason == "no_source"
    from hs_core.metrics_core import pass_k
    out = pass_k([], MetricParams(), ctx())
    assert out.value is None and out.reason == "no_source"
    assert "task_id, variant_id, k" in out.detail


# ---------------------------------------------------------------------------
# 跨 run 聚合（T06-10，用合成 membership 证明算法正确）
# ---------------------------------------------------------------------------

def _mv(key: str, value, seq_ev: str = "EV1") -> object:
    from hs_core.models import EvidenceRef, MetricValue
    return MetricValue(key=key, label_zh="测试", value=value,
                        evidence=[EvidenceRef(event_ids=[seq_ev], note="合成")])


def test_compute_group_pass_k_algorithm():
    load_builtin_metrics()
    items = [
        (_mv("result.success", 1.0, "EV1"), RunMembership(task_id="t1", variant_id="v1", k=3)),
        (_mv("result.success", 1.0, "EV2"), RunMembership(task_id="t1", variant_id="v1", k=3)),
        (_mv("result.success", 1.0, "EV3"), RunMembership(task_id="t1", variant_id="v1", k=3)),
        (_mv("result.success", 1.0, "EV4"), RunMembership(task_id="t2", variant_id="v1", k=3)),
        (_mv("result.success", 0.0, "EV5"), RunMembership(task_id="t2", variant_id="v1", k=3)),
        (_mv("result.success", 1.0, "EV6"), RunMembership(task_id="t2", variant_id="v1", k=3)),
    ]
    mv = compute_group("result.pass_k", items)
    assert mv.value == 0.5                        # t1 全通过、t2 有失败 → 1/2
    assert mv.evidence and mv.evidence[0].event_ids


def test_compute_group_pass_k_requires_membership():
    load_builtin_metrics()
    mv = compute_group("result.pass_k", [(_mv("result.success", 1.0), None)])
    assert mv.value is None
    assert any(mv.key == "result.pass_k" for m in list_metrics())


def test_compute_group_trajectory_efficiency():
    load_builtin_metrics()
    items = [
        (_mv("process.step_count", 10.0, "EV1"), RunMembership(task_id="t1", variant_id="v1")),
        (_mv("process.step_count", 20.0, "EV2"), RunMembership(task_id="t1", variant_id="v1")),
        (_mv("process.step_count", 30.0, "EV3"), RunMembership(task_id="t1", variant_id="v1")),
    ]
    mv = compute_group("process.trajectory_efficiency", items)
    # 参考步数 = 10 → 10/10=1, 20/10=2, 30/10=3 → 均值 2.0
    assert mv.value == 2.0


def test_compute_group_wall_clock_quantiles():
    load_builtin_metrics()
    items = [(_mv("cost.wall_clock_p50", None, "EV1"), RunMembership(wall_clock_ms=ms))
             for ms in (1000, 2000, 3000, 4000, 5000)]
    assert compute_group("cost.wall_clock_p50", items).value == 3000.0
    assert compute_group("cost.wall_clock_p95", items).value == pytest.approx(4800.0)


def test_compute_group_wall_clock_no_source_when_missing():
    load_builtin_metrics()
    items = [(_mv("cost.wall_clock_p50", None), RunMembership())]
    mv = compute_group("cost.wall_clock_p50", items)
    assert mv.value is None


# ---------------------------------------------------------------------------
# T06-11 message.recv 二次归并
# ---------------------------------------------------------------------------

def test_merge_message_recv_keeps_min_seq():
    events = [
        ev(5, "message.recv", {"message_id": "m1", "role": "user"}),
        ev(2, "message.recv", {"message_id": "m1", "role": "user"}),   # 乱序追加的 stale 变体
        ev(3, "message.recv", {"message_id": "m2", "role": "user"}),
        ev(4, "message.recv", {"message_id": None, "role": "user"}),    # 无 ID 原样保留
    ]
    merged = _merge_message_recv(events, MetricParams())
    assert [e["seq"] for e in merged] == [2, 3, 4]
    assert len(merged) == 3


# ---------------------------------------------------------------------------
# 阈值参数化（G9）
# ---------------------------------------------------------------------------

def test_params_change_loop_max_period_takes_effect():
    events = [call(0, "Read", "a"), call(1, "Read", "b"),
              call(2, "Read", "a"), call(3, "Read", "b")]
    assert redundant_loop(events, MetricParams(loop_max_period=1), ctx()).value == 0.0
    assert redundant_loop(events, MetricParams(loop_max_period=5), ctx()).value == 1.0


def test_params_change_loop_min_repeat_takes_effect():
    """R1-7：`loop_min_repeat` 必须真的参与判定（旧实现里改成 3 结果不变 = 半死参数）。"""
    two_rounds = [call(0, "Read", "a"), call(1, "Read", "b"),
                  call(2, "Read", "a"), call(3, "Read", "b")]
    assert redundant_loop(two_rounds, MetricParams(loop_min_repeat=2), ctx()).value == 1.0
    assert redundant_loop(two_rounds, MetricParams(loop_min_repeat=3), ctx()).value == 0.0
    three_rounds = two_rounds + [call(4, "Read", "a"), call(5, "Read", "b")]
    assert redundant_loop(three_rounds, MetricParams(loop_min_repeat=3), ctx()).value == 2.0
    # note 必须写明生效的判定轮数，避免「看起来能配」的错觉
    assert "判定=连续重复 ≥ 3 轮（loop_min_repeat=3）" in \
        redundant_loop(three_rounds, MetricParams(loop_min_repeat=3), ctx()).note


def test_params_change_low_confidence_n_takes_effect():
    """`low_confidence_n` 是唯一决定 high/low 的阈值（Q7），改配置必须改变档位。"""
    events = [ev(0, "step.start"), ev(1, "step.start"), ev(2, "step.start"),
              ev(3, "step.end")]
    assert step_count(events, MetricParams(low_confidence_n=20), ctx()).confidence == "low"
    assert step_count(events, MetricParams(low_confidence_n=2), ctx()).confidence == "high"
    # 派生/上界口径不受样本量影响：tool_correctness 恒 low
    calls = [call(0, "Read", "a"), call(1, "Read", "b")]
    assert tool_correctness(calls, MetricParams(low_confidence_n=1), ctx()).confidence == "low"


def test_params_change_message_merge_key_takes_effect():
    events = [ev(0, "message.recv", {"message_id": "m1", "call_id": "c1"}),
              ev(1, "message.recv", {"message_id": "m2", "call_id": "c1"})]
    assert len(_merge_message_recv(events, MetricParams(message_merge_key="message_id"))) == 2
    assert len(_merge_message_recv(events, MetricParams(message_merge_key="call_id"))) == 1


def test_params_digest_reflects_reserved_t07_thresholds():
    base = MetricParams()
    assert base.digest() != MetricParams(idle_threshold_s=45.0).digest()
    assert base.digest() == MetricParams().digest()      # 稳定


def test_registry_has_13_keys_and_no_collab_yet():
    clear_registry()
    load_builtin_metrics()
    keys = [spec.key for spec in list_metrics()]
    assert len(keys) == 13                              # 2 result + 5 process + 6 cost
    assert not [k for k in keys if k.startswith("collab.")]   # T07 未实现，一项都不做

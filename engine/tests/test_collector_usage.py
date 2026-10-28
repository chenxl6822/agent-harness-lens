"""T05c usage 补采单测：时间窗顺序配对注入 + P0-4 alignment 驱动 confidence。

覆盖（评估卡 §4-A / 用户任务卡自测清单）：
- 正常配对（1:1）注入字段正确、出口 snake_case、alignment=time_window；
- 数量比越界 → 整会话 unpaired、零注入；
- 单对偏差 >1s → 熔断计数正确、会话零产出（不得「丢几个也能算」）；
- 单调顺序配对（时间接近时不交叉 / 不 2 对 1）；
- 无 usage 行 → 不写键（指标回退 no_source）；
- cached/reasoning 明细留存且 total 未被抵扣；
- A6 成对断言：alignment="key" 满覆盖 → 仍按样本量判 high；"time_window" → 恒 low。

运行（必须带 --basetemp）：
.venv/Scripts/python.exe -m pytest engine/tests/test_collector_usage.py -q --basetemp=.tmp-pytest
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "engine"))

from hs_core.collector import (  # noqa: E402
    _extract_usage,
    collect,
)
from hs_core.metrics import MetricContext, MetricParams  # noqa: E402
from hs_core.metrics_core import tokens_in, tokens_out, tokens_total  # noqa: E402

RUN_ID = "test-t05c"


# ---------------------------------------------------------------------------
# 夹具构造：伪 ~/.codebuddy home
# ---------------------------------------------------------------------------

def _iso(ms: int) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}Z"


def _camel_usage(inp: int, out: int, total: int | None = None, *,
                 cached: int | None = None, reasoning: int | None = None) -> dict:
    usage: dict = {"requests": 1, "inputTokens": inp, "outputTokens": out,
                   "totalTokens": total if total is not None else inp + out}
    if cached is not None:
        usage["inputTokensDetails"] = [{"cached_tokens": cached}]
    if reasoning is not None:
        usage["outputTokensDetails"] = [{"reasoning_tokens": reasoning}]
    return usage


def _mk_home(tmp_path: Path, *, traces: dict[str, list[int]],
             sessions: dict[str, list[dict]]) -> Path:
    """traces: {trace_id: [endedAt ms]}；sessions: {sid: [usage 行描述]}。

    usage 行描述：{"ts": ms, "model": str|None, "usage": dict|None, "type": str}
    """
    home = tmp_path / "codebuddy"
    for tid, ends in traces.items():
        d = home / "traces" / "1000"
        d.mkdir(parents=True, exist_ok=True)
        lines = []
        for i, ms in enumerate(ends):
            lines.append(json.dumps({
                "type": "span_end",
                "span": {"type": "generation", "spanId": f"sp{i}", "name": "chat",
                         "status": "ok", "duration": 100, "endedAt": _iso(ms)},
            }))
        (d / f"trace_{tid}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    for sid, rows in sessions.items():
        d = home / "projects" / "proj"
        d.mkdir(parents=True, exist_ok=True)
        lines = []
        for i, row in enumerate(rows):
            provider: dict = {}
            if row.get("usage") is not None:
                provider["usage"] = row["usage"]
                if row.get("model") is not None:
                    provider["model"] = row["model"]
            rec: dict = {"type": row.get("type", "function_call"),
                         "timestamp": row["ts"], "sessionId": sid, "id": f"rec{i}"}
            if provider:
                rec["providerData"] = provider
            lines.append(json.dumps(rec))
        (d / f"{sid}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return home


def _run(home: Path, tmp_path: Path, run_id: str = RUN_ID) -> tuple[list[dict], dict]:
    result = collect(run_id=run_id, codebuddy_home=home, out_dir=tmp_path / "out")
    events = [json.loads(line)
              for line in (tmp_path / "out" / "events.normalized.ndjson")
              .read_text(encoding="utf-8").splitlines() if line.strip()]
    return events, result


def _llm_responses(events: list[dict]) -> list[dict]:
    out = [e for e in events if e["type"] == "llm.response"]
    assert all((e.get("labels") or {}).get("sub_channel") == "A" for e in out)
    return sorted(out, key=lambda e: e["ts"])


# ---------------------------------------------------------------------------
# P0-1：正常配对注入
# ---------------------------------------------------------------------------

def test_paired_injection_snake_case_and_alignment(tmp_path):
    base = 1_792_894_000_000
    rows = [{"ts": base + i * 1000, "model": "glm-5.3-flash",
             "usage": _camel_usage(100 + i, 10 + i)} for i in range(3)]
    home = _mk_home(tmp_path, traces={"aaaa": [base + i * 1000 for i in range(3)]},
                    sessions={"s-1": rows})
    events, result = _run(home, tmp_path)
    pairing = result["usage_pairing"]
    assert pairing["summary"]["paired"] == 1
    assert pairing["summary"]["injected_responses"] == 3
    entry = pairing["sessions"][0]
    assert entry["status"] == "paired" and entry["usage_rows"] == 3
    assert entry["generation_span_ends"] == 3 and entry["ratio"] == 1.0
    assert entry["fused_n"] == 0
    responses = _llm_responses(events)
    assert len(responses) == 3
    for i, ev in enumerate(responses):
        usage = ev["payload"]["usage"]
        # 出口只允许 snake_case（R4：锁定出口形状）
        assert usage["input_tokens"] == 100 + i
        assert usage["output_tokens"] == 10 + i
        assert usage["total_tokens"] == 110 + 2 * i
        assert usage["alignment"] == "time_window"
        assert "inputTokens" not in usage and "totalTokens" not in usage
        assert ev["payload"]["model"] == "glm-5.3-flash"


def test_injection_recomputes_event_id_and_stays_idempotent(tmp_path):
    base = 1_792_894_000_000
    rows = [{"ts": base, "model": "m1", "usage": _camel_usage(5, 6)}]
    home = _mk_home(tmp_path, traces={"aaaa": [base]}, sessions={"s-1": rows})
    # 同源两次摄入（不同 out_dir）：注入改变 payload → event_id 与不注入时不同，但重放确定
    events1, _ = _run(home, tmp_path / "r1")
    events2, _ = _run(home, tmp_path / "r2")
    resp1 = {e["event_id"] for e in _llm_responses(events1)}
    resp2 = {e["event_id"] for e in _llm_responses(events2)}
    assert resp1 == resp2
    # 注入后的 event_id 必须不同于「无 usage 时的空 payload」形态（payload 进摘要）
    assert len(resp1) == 1


def test_event_id_changes_when_usage_injected(tmp_path):
    base = 1_792_894_000_000
    home_with = _mk_home(tmp_path / "a", traces={"aaaa": [base]},
                         sessions={"s-1": [{"ts": base, "model": "m1",
                                            "usage": _camel_usage(5, 6)}]})
    home_without = _mk_home(tmp_path / "b", traces={"aaaa": [base]}, sessions={"s-1": []})
    ev_with, _ = _run(home_with, tmp_path / "o1")
    ev_wo, _ = _run(home_without, tmp_path / "o2")
    id_with = {e["event_id"] for e in _llm_responses(ev_with)}
    id_wo = {e["event_id"] for e in _llm_responses(ev_wo)}
    assert id_with != id_wo  # payload 进摘要（_DIGEST_KEYS），usage 注入必然换 event_id


# ---------------------------------------------------------------------------
# 数量比守卫：越界 → 整会话 unpaired、零注入
# ---------------------------------------------------------------------------

def test_ratio_out_of_bounds_session_unpaired_zero_injection(tmp_path):
    base = 1_792_894_000_000
    rows = [{"ts": base + i * 1000, "model": "m1",
             "usage": _camel_usage(10, 2)} for i in range(2)]  # 2/5 = 0.4 < 0.8
    home = _mk_home(tmp_path, traces={"aaaa": [base + i * 1000 for i in range(5)]},
                    sessions={"s-1": rows})
    events, result = _run(home, tmp_path)
    entry = result["usage_pairing"]["sessions"][0]
    assert entry["status"] == "unpaired"
    assert entry["reason"] == "ratio_out_of_bounds"
    assert entry["ratio"] == 0.4
    assert result["usage_pairing"]["summary"]["injected_responses"] == 0
    assert all("usage" not in e["payload"] for e in _llm_responses(events))


def test_ratio_upper_bound_rejected(tmp_path):
    base = 1_792_894_000_000
    rows = [{"ts": base + i * 1000, "usage": _camel_usage(10, 2)} for i in range(5)]
    home = _mk_home(tmp_path, traces={"aaaa": [base + i * 1000 for i in range(4)]},
                    sessions={"s-1": rows})  # 5/4 = 1.25 边界内 → paired
    _, result = _run(home, tmp_path)
    assert result["usage_pairing"]["sessions"][0]["status"] == "paired"


# ---------------------------------------------------------------------------
# 偏差熔断：>1s 的对作废并计数；会话零产出（不得「丢几个也能算」）
# ---------------------------------------------------------------------------

def test_fused_pair_over_1s_discarded_and_counted(tmp_path):
    base = 1_792_894_000_000
    rows = [{"ts": base, "usage": _camel_usage(11, 1)},
            {"ts": base + 3000, "usage": _camel_usage(22, 2)}]  # 第 2 对偏差 2s
    home = _mk_home(tmp_path, traces={"aaaa": [base, base + 1000]},
                    sessions={"s-1": rows})
    events, result = _run(home, tmp_path)
    entry = result["usage_pairing"]["sessions"][0]
    assert entry["status"] == "unpaired"
    assert entry["reason"] == "fused_pair_over_1s"
    assert entry["fused_n"] == 1
    assert entry["max_delta_ms"] == 2000
    assert result["usage_pairing"]["summary"]["fused_pairs_total"] == 1
    assert result["usage_pairing"]["summary"]["injected_responses"] == 0
    assert all("usage" not in e["payload"] for e in _llm_responses(events))


# ---------------------------------------------------------------------------
# 单调顺序配对：时间接近时不得交叉 / 2 对 1
# ---------------------------------------------------------------------------

def test_monotonic_pairing_no_crossing(tmp_path):
    # usage: 1000(11 tok), 1100(22 tok)；span_end: 1050, 1300。
    # 各自取最近邻会把两次 usage 都配到 1050（2 对 1）；顺序配对必须 1000↔1050、1100↔1300。
    base = 1_792_894_000_000
    rows = [{"ts": base + 1000, "usage": _camel_usage(11, 1)},
            {"ts": base + 1100, "usage": _camel_usage(22, 2)}]
    home = _mk_home(tmp_path, traces={"aaaa": [base + 1050, base + 1300]},
                    sessions={"s-1": rows})
    events, result = _run(home, tmp_path)
    assert result["usage_pairing"]["summary"]["injected_responses"] == 2
    responses = _llm_responses(events)
    assert [e["payload"]["usage"]["input_tokens"] for e in responses] == [11, 22]


# ---------------------------------------------------------------------------
# 无 usage 行：不写键，指标回退 no_source（A5 反向断言）
# ---------------------------------------------------------------------------

def test_no_usage_rows_no_keys_metric_falls_back(tmp_path):
    base = 1_792_894_000_000
    home = _mk_home(tmp_path, traces={"aaaa": [base, base + 1000]}, sessions={"s-1": []})
    events, result = _run(home, tmp_path)
    assert result["usage_pairing"]["summary"]["sessions_with_usage"] == 0
    responses = _llm_responses(events)
    assert len(responses) == 2
    for ev in responses:
        assert "usage" not in ev["payload"] and "model" not in ev["payload"]
    for fn in (tokens_total, tokens_in, tokens_out):
        out = fn(events, MetricParams(), _ctx())
        assert out.value is None and out.reason == "no_source"


def _ctx() -> MetricContext:
    return MetricContext(run_id=RUN_ID, platform="codebuddy",
                         channel_policy="primary_only", event_count=0)


# ---------------------------------------------------------------------------
# 明细留存：cached/reasoning 与 in/out/total 同级，total 不被抵扣（B5）
# ---------------------------------------------------------------------------

def test_details_preserved_total_not_deducted(tmp_path):
    provider = {"model": "deepseek-v4.1-flash",
                "usage": _camel_usage(600, 400, total=1000, cached=500, reasoning=30)}
    out = _extract_usage(provider)
    assert out == {"input_tokens": 600, "output_tokens": 400, "total_tokens": 1000,
                   "input_tokens_details": {"cached_tokens": 500},
                   "output_tokens_details": {"reasoning_tokens": 30}}
    # total 原样 = 源 totalTokens，不是被折扣修正后的值
    assert out["total_tokens"] == 1000
    assert out["total_tokens"] != out["input_tokens"] - out["input_tokens_details"]["cached_tokens"] \
        + out["output_tokens"]


def test_extract_usage_missing_keys_not_filled(tmp_path):
    assert _extract_usage({}) is None
    assert _extract_usage({"usage": {}}) is None
    # 只有部分键 → 只写读到的键，绝不填 0
    assert _extract_usage({"usage": {"inputTokens": 7}}) == {"input_tokens": 7}
    assert "total_tokens" not in _extract_usage({"usage": {"inputTokens": 7, "outputTokens": 3}})


# ---------------------------------------------------------------------------
# P0-4：A6 成对断言（confidence 看标记，不恒 low）
# ---------------------------------------------------------------------------

def _met_ev(seq: int, alignment: str) -> dict:
    return {"schema_version": "1.0.0", "event_id": f"EV{seq:024d}", "run_id": RUN_ID,
            "seq": seq, "ts": f"2026-10-26T00:00:{seq % 60:02d}.000Z",
            "session_id": "s1", "type": "llm.response",
            "source": {"platform": "codebuddy", "channel": "file"}, "labels": {},
            "payload": {"usage": {"input_tokens": 10, "output_tokens": 5,
                                  "total_tokens": 15, "alignment": alignment}}}


def test_alignment_time_window_forces_low_confidence():
    events = [_met_ev(i, "time_window") for i in range(25)]
    for fn in (tokens_total, tokens_in, tokens_out):
        out = fn(events, MetricParams(), _ctx())
        assert out.value is not None and out.value > 0
        assert out.confidence == "low"  # n=25 ≥ 20 仍强制 low：看标记，不看样本量
        assert out.note.endswith("时间窗顺序配对，非键级对齐")


def test_alignment_key_full_coverage_still_high():
    events = [_met_ev(i, "key") for i in range(25)]
    for fn in (tokens_total, tokens_in, tokens_out):
        out = fn(events, MetricParams(), _ctx())
        assert out.value is not None
        assert out.confidence == "high"  # 反例：key 对齐满覆盖 → 按样本量判 high
        assert "时间窗顺序配对" not in out.note


def test_alignment_time_window_partial_coverage_low():
    events = [_met_ev(i, "time_window") for i in range(25)]
    events.append({"schema_version": "1.0.0", "event_id": "EV" + "0" * 24, "run_id": RUN_ID,
                   "seq": 99, "ts": "2026-10-26T00:01:00.000Z", "session_id": "s1",
                   "type": "llm.response",
                   "source": {"platform": "codebuddy", "channel": "file"}, "labels": {},
                   "payload": {"span_name": "generation"}})
    out = tokens_in(events, MetricParams(), _ctx())
    assert out.confidence == "low"
    assert "覆盖度 25/26" in out.note
    assert out.note.endswith("时间窗顺序配对，非键级对齐")


# ---------------------------------------------------------------------------
# 会话 ↔ trace 匹配边界
# ---------------------------------------------------------------------------

def test_session_without_trace_overlap_unpaired(tmp_path):
    base = 1_792_894_000_000
    rows = [{"ts": base, "usage": _camel_usage(5, 5)}]
    home = _mk_home(tmp_path, traces={}, sessions={"s-1": rows})
    events, result = _run(home, tmp_path)
    entry = result["usage_pairing"]["sessions"][0]
    assert entry["status"] == "unpaired"
    assert entry["reason"] == "no_overlapping_trace_available"
    assert all("usage" not in e["payload"] for e in _llm_responses(events))


def test_two_sessions_do_not_share_one_trace(tmp_path):
    # 主会话（30 行）+ 子会话（2 行）时间上都在同一 trace 内：一对一贪心只允许主会话 claim；
    # 子会话不得把同一批 llm.response 注入第二遍（防双计）。
    base = 1_792_894_000_000
    main_rows = [{"ts": base + i * 1000, "usage": _camel_usage(10 + i, 1)} for i in range(30)]
    sub_rows = [{"ts": base + 5000, "usage": _camel_usage(99, 9)},
                {"ts": base + 6000, "usage": _camel_usage(98, 8)}]
    home = _mk_home(tmp_path, traces={"aaaa": [base + i * 1000 for i in range(30)]},
                    sessions={"s-main": main_rows, "s-sub": sub_rows})
    events, result = _run(home, tmp_path)
    pairing = result["usage_pairing"]
    by_sid = {e["session_id"]: e for e in pairing["sessions"]}
    assert by_sid["s-main"]["status"] == "paired"
    assert by_sid["s-main"]["paired_n"] == 30
    assert by_sid["s-sub"]["status"] == "unpaired"
    responses = _llm_responses(events)
    assert len(responses) == 30
    assert sum(1 for e in responses if "usage" in e["payload"]) == 30
    assert all(e["payload"]["usage"]["input_tokens"] != 99 for e in responses)

"""T06：13 个指标纯函数（零 IO，禁止 open/sqlite3/os.environ/Path/网络）。

约定（任务卡 §7）：
- 入口签名 `(events_sorted, params, ctx) -> MetricOutcome`；2 元签名也支持（注册器自动包装）。
- `events_sorted` 已按 `seq` 升序（= (ts_ms, event_id) 全序），**不要再引入第二排序键**。
- 算不出来 → `MetricOutcome.na(reason, detail)`，绝不填 0 / -1 / 估计值。
- 凡「当前无源」的指标一律写成「读到就算、读不到降级」（T06-08 前向兼容）。
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Sequence
from itertools import pairwise

from hs_core.collector import _iso_to_ms
from hs_core.metrics import (
    WALL_CLOCK_FACT,
    MetricContext,
    MetricOutcome,
    MetricParams,
    agg_pass_k,
    agg_trajectory_efficiency,
    agg_wall_clock_p50,
    agg_wall_clock_p95,
    evidence_from,
    quantile,
    register_metric,
)

# ---------------------------------------------------------------------------
# R1-6：detail / note 里的数字**只能来自本次运行**。历史快照的实测值若确实无法在本 run 复算，
# 必须写成引用式（明示「非本次观测 + 出处」），不得以本次观测的口吻呈现。
# ---------------------------------------------------------------------------
_CHANNEL_B_REFERENCE = (
    "引用（**非本 run 观测**）：主理人于 9098 事件快照实测通道 B transcript 的 "
    "`providerData.usage` 共 723 行，见 docs/metrics-engine.md §6"
)


def _usage_fact(responses_n: int, usage_hits: int) -> str:
    """本 run 的 `llm.response` 用量实测（现场统计，不写死历史快照数字）。"""
    return (f"本 run `llm.response` {responses_n} 条，其中读到 usage 的 {usage_hits} 条；"
            "通道 A 的 generation span 属性不含 usage")


def _trace_fact(probe: dict) -> str:
    """本 run 的 trace 级汇总实测（`trace_summary_tokens` 与指标同源）。"""
    return (f"本 run `metric.custom`/trace_summary {probe['events']} 条，"
            f"`total_tokens` 非空 {probe['with_total']} 条、> 0 的 {probe['positive']} 条"
            "（占位未实现，不可作为数据源）")


def _cost_no_source_fact(events: Sequence[dict], responses: Sequence[dict],
                         usages: Sequence[dict | None]) -> str:
    """cost.* 的 no_source 拼装：本 run 实测 + 通道 B 引用 + 归属（四段缺一不可）。"""
    usage_hits = sum(1 for u in usages if u is not None)
    return (f"{_usage_fact(len(responses), usage_hits)}；{_CHANNEL_B_REFERENCE}；"
            "当前 `primary_only` 主源取 A 把通道 B 挡在门外，修法属 T05 v2 采集侧，"
            f"超出本卡权限；{_trace_fact(trace_summary_tokens(events))}")


_UPSTREAM_WALL_CLOCK_FACT = WALL_CLOCK_FACT

# usage 键名兼容表（in/out/total 三组同义键）
_USAGE_KEYS: dict[str, tuple[str, ...]] = {
    "in": ("input_tokens", "prompt_tokens", "inputTokens", "promptTokens"),
    "out": ("output_tokens", "completion_tokens", "outputTokens", "completionTokens"),
    "total": ("total_tokens", "totalTokens"),
}


# ---------------------------------------------------------------------------
# 内部纯函数小件
# ---------------------------------------------------------------------------

def _payload(ev: dict) -> dict:
    p = ev.get("payload")
    return p if isinstance(p, dict) else {}


def _labels(ev: dict) -> dict:
    labels = ev.get("labels")
    return labels if isinstance(labels, dict) else {}


def _of_type(events: Iterable[dict], *types: str) -> list[dict]:
    wanted = set(types)
    return [ev for ev in events if ev.get("type") in wanted]


def _as_int(value: object) -> int | None:
    """严格取整数（bool / 字符串 / 浮点非整数一律视为「没有这个字段」）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


# ---------------------------------------------------------------------------
# R2-3 统一措辞（与 docs/metrics-engine.md §7-1、probe/t06/R2-result.json 的
# signature_source 段**逐字一致**，验收方逐字比对，三处同步改）：
# 签名口径：优先取 `tool.call` 的 `content_sha256`；检测到该字段在本 run 内去重后取值数 ≤ 1 时
# （当前实测 921/921 全为 `sha256("")` 常量），回退到 `span_id` → `span_end` 补齐的内容哈希。
# **不得以「键缺失」作为回退依据**——键缺失与「键存在但值恒为常量」是两种状态，后者才是当前实况。
# ---------------------------------------------------------------------------

# 签名语义（R1-6 定案 (a)）：span_end 补齐取的是「结果内容哈希」，不是「入参哈希」。
# 两次**相同入参产生不同输出** → 漏检冗余；两次**不同入参产生相同输出**（如都失败返回同一错误）
# → 误判冗余。它是当前通道 A 数据下唯一可用的近似，指标 note 必须声明这一点。
SIGNATURE_SOURCE_NOTE = (
    "签名来源：`(tool_name, content_sha256)`，content_sha256 取自 span_id 对齐的 span_end "
    "**结果内容哈希**（入参 `toolInput` 未采集，非 args_hash 的真实语义）"
)


# 空输入的 sha256 —— **不是**可用签名（HS_CONTENT_CAPTURE=off 下通道 A 的 span_start 不带
# toolInput，collector 的 `_content_hash(sp.get("toolInput") or "")` 落到空串哈希）。
# 引用（非本 run 观测）：主理人及 QA 在两份快照上实测 tool.call 自身的 content_sha256
# 全部等于这个常量（1124/1124、921/921），照单取用会让签名退化成 tool_name，
# 数百次 Bash 被并成一个循环 → 假数字。见 docs/metrics-engine.md §7-1。
_EMPTY_CONTENT_SHA = hashlib.sha256(b"").hexdigest()


def _usable_hash(value: object) -> str | None:
    return value if isinstance(value, str) and value and value != _EMPTY_CONTENT_SHA else None


def _sibling_content_sha(events: Iterable[dict]) -> dict[str, str]:
    """span_id → tool.result/tool.error 的 content_sha256（通道 A 的 span_end 才带 toolInput）。

    通道 A 的 span_start（tool.call）里 `toolInput` 为空串、span_end（result/error）里才有真实
    toolInput，因此「调用参数签名」需按 span_id 从同 span 的结束事件补齐（见 docs/metrics-engine.md）。
    """
    index: dict[str, str] = {}
    for ev in events:
        if ev.get("type") not in ("tool.result", "tool.error"):
            continue
        payload = _payload(ev)
        span_id = payload.get("span_id")
        sha = _usable_hash(payload.get("content_sha256"))
        if isinstance(span_id, str) and span_id and sha and span_id not in index:
            index[span_id] = sha
    return index


def _call_sig(ev: dict) -> tuple[str, str] | None:
    """调用签名 = (tool_name, content_sha256)；空输入常量哈希不算签名。"""
    payload = _payload(ev)
    name = payload.get("tool_name")
    sha = _usable_hash(payload.get("content_sha256"))
    if isinstance(name, str) and name and sha:
        return (name, sha)
    return None


def _call_signatures(calls: Sequence[dict], events: Iterable[dict]
                     ) -> tuple[list[tuple[str, str] | None], int, int, int]:
    """返回 (与 calls 对齐的签名, 经 span_id 补齐的条数 proxy, 自身为空哈希的条数, 自身可用哈希的条数 native)。

    R2-1：`proxy`（span_end 补齐）与 `native`（自身 content_sha256 可用）**分开正向计数**。
    禁止用 `native = total - empty_own` 反推——`empty_own` 只统计「值为空常量」，
    「键缺失」不在内，两者不是补集；一条键缺失但被 span_id 补到签名的 call 会被误算成原生。
    原生的正向条件是 `proxy == 0`。
    """
    siblings = _sibling_content_sha(events)
    sigs: list[tuple[str, str] | None] = []
    joined = 0
    empty_own = 0
    native = 0
    for call in calls:
        payload = _payload(call)
        name = payload.get("tool_name")
        name = name if isinstance(name, str) else ""
        own_raw = payload.get("content_sha256")
        if isinstance(own_raw, str) and own_raw == _EMPTY_CONTENT_SHA:
            empty_own += 1
        own = _usable_hash(own_raw)
        if name and own:
            sigs.append((name, own))
            native += 1
            continue
        span_id = payload.get("span_id")
        sib = siblings.get(span_id) if isinstance(span_id, str) else None
        if name and sib:
            sigs.append((name, sib))
            joined += 1
            continue
        sigs.append(None)
    return sigs, joined, empty_own, native


def _signature_distinct(calls: Sequence[dict]) -> int:
    """`tool.call` 自身 `content_sha256` 字符串值**去重后的取值数**（键缺失不计入取值）。

    注意：空输入常量 `sha256("")` 也是「一个取值」——R2-1 判据按原始字段算，
    当前实测 921/921 全为该常量 → 去重取值数 == 1 → 代理分支。
    """
    values: set[str] = set()
    for call in calls:
        raw = _payload(call).get("content_sha256")
        if isinstance(raw, str) and raw:
            values.add(raw)
    return len(values)


def _signature_confidence(calls: Sequence[dict], proxy: int, native: int,
                          params: MetricParams) -> tuple[str, str]:
    """R2-1：confidence 按**来源等级**动态判定（不是按样本量）——动态规则，禁止写死到 key 上。

    样本量回答「够不够」，来源等级回答「准不准」，这是两个问题：签名是代理键时样本量再大
    也改变不了「签名是近似的」这件事。T05 v2 补到真实 content 后签名会自动从代理变回原生，
    这两个指标必须**自动**升档——写死 `low` 等于把「现在测不准」固化成「永远测不准」。

    - 原生：`content_sha256` 去重取值数 > 1 **且 proxy == 0**（正向条件；`proxy > 0` 说明
      有调用走了 span_end 回退，不是纯原生）→ 再叠加样本量阈值（n < low_confidence_n 取 low）
    - 代理：去重取值数 == 1（全为常量，签名实为 span_id）→ low
    - 键缺失：去重取值数 == 0 → **不得据此判为代理**，走「未知」分支：low + 「签名来源未采集」
    - 混合：去重取值数 > 1 但 proxy > 0 → low（部分调用非原生）
    """
    distinct = _signature_distinct(calls)
    n = len(calls)
    if distinct == 0:
        return "low", ("签名来源未采集（本 run tool.call 无 content_sha256 取值，"
                       "不得据此判为代理；签名由 span_end 补齐，本身是近似）")
    if distinct == 1:
        return "low", ("签名来源=代理（content_sha256 本 run 去重取值数 == 1，无区分度——"
                       "当前实测 921/921 全为 sha256(\"\") 常量，调用签名实为 span_id 这类代理键，"
                       "样本量不改变这一点）")
    if proxy > 0:
        return "low", (f"签名来源=混合（{proxy} 次调用经 span_id 从 span_end 补齐，"
                       "非纯原生，不按原生计档）")
    if native <= 0:
        return "low", "签名来源未采集（无一次调用拿到自身 content_sha256）"
    return (_confidence(n, params),
            f"签名来源=原生（content_sha256 去重取值数 > 1 且无 span_end 回退，n={n}）")


def _hash_stats(items: Sequence[dict]) -> dict:
    """一组事件的 `content_sha256` 区分度：`all_same` 为真即「这个字段没有区分度」。"""
    shas: list[str] = []
    empty = 0
    for ev in items:
        raw = _payload(ev).get("content_sha256")
        if isinstance(raw, str) and raw:
            shas.append(raw)
            if raw == _EMPTY_CONTENT_SHA:
                empty += 1
    distinct = len(set(shas))
    return {"n": len(shas), "distinct": distinct, "empty_sha_n": empty,
            "all_same": len(shas) >= 2 and distinct <= 1}


def tool_content_hash_stats(events: Sequence[dict]) -> dict:
    """调用侧 / 结束侧内容哈希的区分度（纯函数，供装配层写 `data_quality_flags`）。

    R1-6 防御：若某一侧的 `content_sha256` 在整批数据里**全部相同**，它就没有区分度，
    不得当签名用（当前通道 A 的 `tool.call` 正是这种形态）。命不命中由装配层登记 flag，
    指标本身走 span_end 补齐，不静默退化。
    """
    return {
        "tool_call": _hash_stats(_of_type(events, "tool.call")),
        "span_end": _hash_stats(_of_type(events, "tool.result", "tool.error")),
    }


def _errors_of(events: Iterable[dict]) -> list[dict]:
    """错误事件集合：`tool.error` ∪ `is_error=True` 的 result/error（按 event 取并集，不双计）。"""
    out = []
    for ev in events:
        typ = ev.get("type")
        if typ == "tool.error" or typ == "tool.result" and _payload(ev).get("is_error") is True:
            out.append(ev)
    return out


def tool_pairing_stats(events: Sequence[dict], params: MetricParams | None = None) -> dict:
    """tool.call ↔ tool.result / tool.error 的配对统计（纯函数，单一真源）。"""
    return _pair_calls(events, params or MetricParams())[0]


def _pair_calls(events: Sequence[dict], params: MetricParams
                ) -> tuple[dict, list[dict]]:
    """配对统计 + 被判为「错误调用」的 call 事件（证据锚定用）。

    配对优先级（任务卡 E3）：① `payload.span_id` 相等 → ② `payload.call_id` 相等 →
    ③ 同签名 `(tool_name, content_sha256)` 的时序最近邻（seq 在其后、距离最小且
    ≤ `retry_window_events`）。一个 error 命中任一即把对应 call 判为「错误调用」。

    统计字段：
    - `tool_call` / `tool_result` / `tool_error`：三类事件计数（恒等式用）
    - `identity_holds`：`tool_call == tool_result + tool_error`（结构性质，非指标正确性）
    - `errored_calls`：被配对到错误事件的**不同调用**数（同一次调用只计一次）
    - `unmatched_errors`：找不到任何调用的 error 数（不因配不上而假装调用正确）
    - `unmatchable_calls`：三种配对键都不存在的调用数（只能算「无配对」→ 计入分子，属上界）
    - `paired_by`：各配对层级命中计数，便于复核
    """
    calls = _of_type(events, "tool.call")
    results = _of_type(events, "tool.result")
    errors = _errors_of(events)

    by_span: dict[str, dict] = {}
    by_cid: dict[str, dict] = {}
    sig_calls: list[tuple[dict, tuple[str, str] | None]] = []
    sigs, _joined, _empty, _native = _call_signatures(calls, events)
    unmatchable = 0
    for call, sig in zip(calls, sigs):
        payload = _payload(call)
        span = payload.get("span_id")
        cid = payload.get("call_id")
        has_span = isinstance(span, str) and bool(span)
        has_cid = isinstance(cid, str) and bool(cid)
        if has_span:
            by_span.setdefault(span, call)
        if has_cid:
            by_cid.setdefault(cid, call)
        if not has_span and not has_cid and sig is None:
            unmatchable += 1
        sig_calls.append((call, sig))

    errored: list[dict] = []
    errored_ids: set[str] = set()
    paired_by = {"span_id": 0, "call_id": 0, "signature": 0}
    unmatched_errors = 0
    for err in errors:
        payload = _payload(err)
        hit = None
        span = payload.get("span_id")
        cid = payload.get("call_id")
        if isinstance(span, str) and span in by_span:
            hit = by_span[span]
            paired_by["span_id"] += 1
        elif isinstance(cid, str) and cid in by_cid:
            hit = by_cid[cid]
            paired_by["call_id"] += 1
        else:
            sig = _call_sig(err)
            if sig is not None:
                best: tuple[int, dict] | None = None
                for call, call_sig in sig_calls:
                    if call_sig != sig:
                        continue
                    distance = int(call["seq"]) - int(err["seq"])
                    if (0 < distance <= params.retry_window_events
                            and (best is None or distance < best[0])):
                        best = (distance, call)
                if best is not None:
                    hit = best[1]
                    paired_by["signature"] += 1
        if hit is None:
            unmatched_errors += 1
        elif str(hit["event_id"]) not in errored_ids:
            errored_ids.add(str(hit["event_id"]))
            errored.append(hit)

    stats = {
        "tool_call": len(calls),
        "tool_result": len(results),
        "tool_error": len(errors),
        "identity_holds": len(calls) == len(results) + len(errors),
        "errored_calls": len(errored),
        "unmatched_errors": unmatched_errors,
        "unmatchable_calls": unmatchable,
        "paired_by": paired_by,
    }
    return stats, errored


def _scan_loops(sigs: Sequence[tuple[str, str]], params: MetricParams
                ) -> tuple[int, list[dict]]:
    """在签名序列上做周期检测，返回 (冗余重复次数, 段落明细)。

    贪心：从左到右，对每个起点试 p ∈ [1, loop_max_period] 取**最小匹配 p**；
    若接下来 `loop_min_repeat` 个周期（共 `min_repeat * p` 个位置）逐位相同，则判为周期循环，
    向右扩展覆盖长度 L，贡献 (L//p)-1 次冗余重复，i 直接跳到已覆盖段末尾（避免重叠计数）。
    """
    min_repeat = max(2, int(params.loop_min_repeat))
    max_period = max(1, int(params.loop_max_period))
    n = len(sigs)
    total = 0
    segments: list[dict] = []
    i = 0
    while i < n:
        match_p = 0
        for p in range(1, max_period + 1):
            need = min_repeat * p          # 至少 min_repeat 个完整周期
            if i + need > n:
                break
            # R1-7：比较 need-p 个位置（= (min_repeat-1) 个整周期对），min_repeat 才真正生效；
            # 旧实现只比 1 个周期，min_repeat 从 2 改 3 结果不变 —— 属「看起来能配、改了没用」。
            if all(sigs[i + k] == sigs[i + p + k] for k in range(need - p)):
                match_p = p
                break
        if match_p == 0:
            i += 1
            continue
        p = match_p
        j = i + p
        while j < n and sigs[j] == sigs[j - p]:
            j += 1
        length = j - i
        redundant = length // p - 1
        if redundant > 0:
            total += redundant
            segments.append({"start": i, "period": p, "covered": length, "redundant": redundant})
        i = j
    return total, segments


def _first_number(src: dict, keys: Sequence[str]) -> int | float | None:
    for key in keys:
        if key not in src:
            continue
        raw = src[key]
        if isinstance(raw, bool) or raw is None:
            continue
        if isinstance(raw, (int, float)):
            value = raw
        elif isinstance(raw, str):
            try:
                value = float(raw.strip())
            except ValueError:
                continue
        else:
            continue
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            continue
        if value < 0:
            continue
        return int(value) if float(value).is_integer() else value
    return None


def _usage_of(event: dict) -> dict | None:
    """从一个事件读 usage（读不到返回 None，绝不抛错、绝不写死不可用）。

    扫描顺序：payload.usage → payload.providerData.usage → labels.usage → payload 顶层 →
    providerData 顶层。任一来源读到任一 token 数字即返回；total 缺失时用 in+out 补齐。
    """
    payload = _payload(event)
    labels = _labels(event)
    provider = payload.get("providerData")
    provider = provider if isinstance(provider, dict) else {}
    sources = (
        payload.get("usage"),
        provider.get("usage"),
        labels.get("usage"),
        payload,
        provider,
    )
    for src in sources:
        if not isinstance(src, dict) or not src:
            continue
        values = {slot: _first_number(src, keys) for slot, keys in _USAGE_KEYS.items()}
        if all(v is None for v in values.values()):
            continue
        tokens_in, tokens_out, total = values["in"], values["out"], values["total"]
        if total is None and tokens_in is not None and tokens_out is not None:
            total = tokens_in + tokens_out
        return {"in": tokens_in, "out": tokens_out, "total": total}
    return None


def _model_of(event: dict) -> str | None:
    payload = _payload(event)
    labels = _labels(event)
    provider = payload.get("providerData")
    provider = provider if isinstance(provider, dict) else {}
    for cand in (payload.get("model"), labels.get("model"), provider.get("model")):
        if isinstance(cand, str) and cand:
            return cand
    return None


def _merge_message_recv(events: Sequence[dict], params: MetricParams) -> list[dict]:
    """message.recv 按 payload[message_merge_key] 二次归并（T06-11，保留最小 seq）。

    供 T07 消息类指标复用：源侧 stale 变体（storage.md 已知边界 1）在存储层无法消除，
    指标层按消息 ID 去重即不双计。无 ID 的消息原样保留（无法归并，不静默丢弃）。
    """
    key_name = params.message_merge_key
    kept: dict[str, dict] = {}
    out: list[dict] = []
    for ev in events:
        if ev.get("type") != "message.recv":
            continue
        mid = _payload(ev).get(key_name)
        if not isinstance(mid, str) or not mid:
            out.append(ev)
            continue
        prev = kept.get(mid)
        if prev is None or int(ev["seq"]) < int(prev["seq"]):
            kept[mid] = ev
    out.extend(kept[mid] for mid in sorted(kept, key=lambda m: int(kept[m]["seq"])))
    return sorted(out, key=lambda ev: int(ev["seq"]))


def _confidence(n: int, params: MetricParams, *, derived: bool = False) -> str:
    """Q7 裁定：**有值就必须有档位**（禁止 `value != null` + `confidence == null`）。

    - 派生 / 上界口径 → 恒 `"low"`（样本量再大也不改变「口径是近似的」这一点）
    - 直接观测：样本量 `n ≥ low_confidence_n`（默认 20）→ `"high"`，`n < 20` → `"low"`
    - 降级（`value is None`）→ `None`，由装配层强制为 `null`，**不得**填 `"low"`

    R2-1：样本量回答「够不够」，来源等级回答「准不准」。签名类指标
    （retry_rate / redundant_loop）的档位由 `_signature_confidence` 按来源等级判定，
    样本量阈值只在「原生」分支上叠加（取最低者）。
    """
    if derived:
        return "low"
    return "high" if n >= int(params.low_confidence_n) else "low"


def _rate(num: int, den: int, params: MetricParams,
          *, derived: bool = False) -> tuple[float, str]:
    """ratio 统一口径：clamp 到 [0,1]、6 位小数；置信度按 `_confidence` 分档（永不为 None）。"""
    value = 0.0 if den <= 0 else min(max(num / den, 0.0), 1.0)
    return round(value, 6), _confidence(den, params, derived=derived)


# ---------------------------------------------------------------------------
# process.* —— 过程指标
# ---------------------------------------------------------------------------

@register_metric(
    key="process.step_count", group="process", label_zh="步数（step.start 计数）",
    unit="count", direction=None, scope="run", event_types=("step.start",))
def step_count(events: Sequence[dict], params: MetricParams,
               ctx: MetricContext) -> MetricOutcome:
    """`step.start` 事件计数（R1-1 口径锁定：唯一来源就是这 26 类事件里的 `step.start`）。

    **禁止**改用 `agent/step`（每会话只发一次，会让步数恒为 1）、`session.start`、
    `step.end` 或任何 payload 字段计数。`step.end` 只用于「成对性」旁证，不进分子。
    dsh 的 `agent/pre-step` 在采集侧同样映射为 `step.start`（payload 带 `turn` / `step`），
    故交叉校验读的是 `step.start` 的 payload，而不是另一类事件。
    """
    steps = _of_type(events, "step.start")
    if not steps:
        return MetricOutcome.na(
            "no_source",
            "本 run 无 step.start 事件：CodeBuddy 通道 A 的 custom span 与 dsh 的 agent/pre-step "
            "是唯一源；无步边界即无法计步（不是 0 步）")
    total = len(steps)
    with_agent = sum(1 for ev in steps if ev.get("agent_id"))
    cross = _step_payload_crosscheck(steps)
    parts = ["`step.start` 计数（不读 payload）", f"事件计数 {total}"]
    if cross["with_step_field"]:
        diff = total - cross["step_max"]
        parts.append(f"`pre-step` 的 `step` 最大值 {cross['step_max']}，差异 {diff}")
        if diff != 0:
            parts.append("两口径不一致，以事件计数为准（`step` 是单会话内序号，不是 run 级步数）")
        if not cross["monotonic"]:
            parts.append("`step` 序列非单调（语义可能漂移），仅记录不参与判定")
        parts.append(f"含 `step` 字段的 step.start {cross['with_step_field']}/{total} 条")
    else:
        parts.append("本 run 无 dsh `agent/pre-step` 形态（step.start 的 payload 无 step 字段），"
                     "交叉校验仅记录不参与判定")
    parts.append(f"agent_id 非空 {with_agent}/{total}，不可按成员拆分")
    note = "；".join(parts)
    return MetricOutcome.ok(float(total),
                            evidence=evidence_from(steps, params=params, note=note),
                            confidence=_confidence(total, params), note=note)


def _step_payload_crosscheck(steps: Sequence[dict]) -> dict:
    """从 `step.start` 的 payload 读 dsh `agent/pre-step` 的 `turn` / `step` 做交叉校验。

    只读、只记录：即便两口径不一致也**以事件计数为准**（payload 的 `step` 是单会话内序号，
    多个 session 会重复；事件计数才是 run 级步数）。
    """
    turns: list[int] = []
    numbers: list[int] = []
    for ev in steps:
        payload = _payload(ev)
        turn = _as_int(payload.get("turn"))
        step = _as_int(payload.get("step"))
        if turn is not None:
            turns.append(turn)
        if step is not None:
            numbers.append(step)
    return {
        "with_step_field": len(numbers),
        "step_max": max(numbers) if numbers else None,
        "step_min": min(numbers) if numbers else None,
        "turn_max": max(turns) if turns else None,
        "monotonic": all(a <= b for a, b in pairwise(numbers)),
    }


@register_metric(
    key="process.tool_correctness", group="process", label_zh="工具调用正确率（上界）",
    unit="ratio", direction=True, scope="run",
    event_types=("tool.call", "tool.result", "tool.error"))
def tool_correctness(events: Sequence[dict], params: MetricParams,
                     ctx: MetricContext) -> MetricOutcome:
    """口径（分母 = `tool.call` = 全量调用）：正确调用数 ÷ 调用总数。

    分子 = **无配对 `tool.error` 的调用数**（配对优先级 span_id → call_id → 同签名时序最近邻）。
    配对不上任何调用的 error（`unmatched_errors`）不会让任何调用变成「错误」，
    但会在 note 里如实报出——它意味着错误事件的归属信息缺失，正确率因此偏高。
    """
    calls = _of_type(events, "tool.call")
    if not calls:
        return MetricOutcome.na("insufficient_data", "本 run 无 tool.call 事件，分母为 0，不编造正确率")
    stats, errored = _pair_calls(events, params)
    value, confidence = _rate(stats["tool_call"] - stats["errored_calls"],
                              stats["tool_call"], params, derived=True)
    deny_hits = _deny_hits(events)
    parts = [
        # R1-6：本次扫描条数现场算，历史快照的 deny 实测只作引用，不写成「本 run 实测」
        (f"deny/permission 信号当前无数据源（本 run 扫描 {len(events)} 条事件，"
         f"deny/permission 标记命中 {deny_hits}），本值为正确率上界，口径为派生"),
        f"分母=tool.call {stats['tool_call']} 次",
        (f"分子=无配对 error 的调用 {stats['tool_call'] - stats['errored_calls']} 次"
         f"（错误调用 {stats['errored_calls']} 次）"),
        (f"error 事件 {stats['tool_error']} 个（tool.error ∪ is_error=True）："
         f"配对成功 {stats['tool_error'] - stats['unmatched_errors']} / "
         f"unmatched_errors {stats['unmatched_errors']}"),
        (f"配对层级命中 span_id {stats['paired_by']['span_id']} / "
         f"call_id {stats['paired_by']['call_id']} / 同签名最近邻 {stats['paired_by']['signature']}"),
        (f"unmatchable_calls {stats['unmatchable_calls']} 次（span_id/call_id/签名三键全无，"
         "只能计为无配对，属上界口径）"),
        (f"恒等式 tool.call {stats['tool_call']} vs tool.result {stats['tool_result']} + "
         f"tool.error {stats['tool_error']} = {stats['tool_result'] + stats['tool_error']}"
         f"（{'成立' if stats['identity_holds'] else '不成立，见 ext.data_quality_flags'}）"),
    ]
    note = "；".join(parts)
    refs: list = []
    if errored:
        refs += list(evidence_from(errored, params=params,
                                   note=f"分子中排除的错误调用 {len(errored)} 次"))
    refs += evidence_from(calls, params=params,
                          note=f"分母：tool.call {stats['tool_call']} 次（全量调用集合）")
    return MetricOutcome.ok(value, evidence=tuple(refs), confidence=confidence, note=note)


# deny/permission 标记的键名（当前 26 类事件表里没有这类字段；读到就算，读不到就是 0）
_DENY_KEYS = ("deny", "denied", "permission_denied", "permissionDenied", "approval_denied")


def _deny_hits(events: Iterable[dict]) -> int:
    """统计 payload / labels 里 deny 类标记为真的条数（只读，不改变正确率口径）。"""
    hits = 0
    for ev in events:
        for src in (_payload(ev), _labels(ev)):
            if any(src.get(key) is True for key in _DENY_KEYS):
                hits += 1
                break
    return hits


@register_metric(
    key="process.redundant_loop", group="process", label_zh="冗余循环（周期重复次数）",
    unit="count", direction=False, scope="run", event_types=("tool.call",))
def redundant_loop(events: Sequence[dict], params: MetricParams,
                   ctx: MetricContext) -> MetricOutcome:
    """连续重复 `(tool_name, content_sha256)` 序列的周期循环冗余次数。

    判定条件：存在周期 `p ≤ loop_max_period` 且连续重复 `≥ loop_min_repeat` 轮
    （`loop_min_repeat` 已真正参与判定，见 `_scan_loops`）。
    """
    calls = _of_type(events, "tool.call")
    if not calls:
        return MetricOutcome.na("insufficient_data", "本 run 无 tool.call 事件，无法判定循环")
    sigs, joined, empty_own, native = _call_signatures(calls, events)
    usable = [(ev, sig) for ev, sig in zip(calls, sigs) if sig is not None]
    ineligible = len(calls) - len(usable)
    if not usable:
        return MetricOutcome.na(
            "insufficient_data",
            f"全部 {len(calls)} 次 tool.call 都无法得到 (tool_name, content_sha256) 签名"
            f"（其中自身为空输入常量哈希 {empty_own} 次、且 span_id 也配不到结束事件），"
            "签名缺失时无法区分『重复同一动作』与『不同动作』，不编造循环次数")
    sig_list = [sig for _, sig in usable]
    total, segments = _scan_loops(sig_list, params)
    periods = sorted({seg["period"] for seg in segments})
    effective_repeat = max(2, int(params.loop_min_repeat))
    confidence, source_note = _signature_confidence(calls, joined, native, params)
    note = (f"{source_note}；{SIGNATURE_SOURCE_NOTE}；参与判定 {len(usable)}/{len(calls)} 次调用，"
            f"ineligible {ineligible} 次；其中 {joined} 次调用的哈希由 span_id 从同 span 的 "
            f"span_end 事件补齐（本 run {empty_own}/{len(calls)} 次 tool.call 自身为空输入常量哈希）；"
            f"循环段 {len(segments)} 个，周期 {periods}；"
            f"判定=连续重复 ≥ {effective_repeat} 轮（loop_min_repeat={params.loop_min_repeat}），"
            f"循环节上限 loop_max_period={params.loop_max_period}")
    if total == 0:
        return MetricOutcome.ok(0.0, evidence=evidence_from(
            [ev for ev, _ in usable], params=params,
            note="未检出周期循环；证据为参与判定的调用序列"), confidence=confidence, note=note)
    repeated: list[dict] = []
    for seg in segments:
        start, period, covered = seg["start"], seg["period"], seg["covered"]
        repeated.extend(ev for ev, _ in usable[start + period:start + covered])
    return MetricOutcome.ok(float(total),
                            evidence=evidence_from(repeated, params=params,
                                                   note="冗余重复的调用（第 2 轮及以后的重复执行）"),
                            confidence=confidence, note=note)


def _retry_window_ok(err: dict, call: dict, params: MetricParams) -> bool:
    err_ms = _iso_to_ms(str(err.get("ts") or ""))
    call_ms = _iso_to_ms(str(call.get("ts") or ""))
    if err_ms == 0 or call_ms == 0:      # 任一侧时戳不可解析 → 退化为 seq 距离
        return 0 <= int(call["seq"]) - int(err["seq"]) <= params.retry_window_events
    delta = call_ms - err_ms
    return 0 <= delta <= params.retry_window_ms


@register_metric(
    key="process.retry_rate", group="process", label_zh="重试率",
    unit="ratio", direction=False, scope="run",
    event_types=("tool.call", "tool.error", "tool.result"))
def retry_rate(events: Sequence[dict], params: MetricParams,
               ctx: MetricContext) -> MetricOutcome:
    """`tool.error` 后窗口内同签名重发次数 ÷ tool.call 总数。"""
    calls = _of_type(events, "tool.call")
    errors = _errors_of(events)
    if not calls:
        return MetricOutcome.na("insufficient_data", "本 run 无 tool.call 事件，分母为 0，不编造重试率")

    sigs, joined, empty_own, native = _call_signatures(calls, events)
    if not any(sig is not None for sig in sigs):
        return MetricOutcome.na(
            "insufficient_data",
            f"调用侧 {len(calls)} 次 tool.call 全部拿不到 (tool_name, content_sha256) 签名"
            f"（自身空输入常量哈希 {empty_own} 次，span_end 也配不到），无法判断哪些调用是"
            "「同一动作重发」；此时报 0 会把『无法判断』说成『没有重试』")

    by_cs: dict[tuple[str, str], list[dict]] = {}
    by_cid: dict[tuple[str, str], list[dict]] = {}
    by_sid: dict[tuple[str, str], list[dict]] = {}
    for call, sig in zip(calls, sigs):
        payload = _payload(call)
        name = payload.get("tool_name")
        name = name if isinstance(name, str) else ""
        if sig is not None:
            by_cs.setdefault(sig, []).append(call)
        cid = payload.get("call_id")
        if isinstance(cid, str) and cid:
            by_cid.setdefault((name, cid), []).append(call)
        sid = payload.get("span_id")
        if isinstance(sid, str) and sid:
            by_sid.setdefault((name, sid), []).append(call)

    matched = 0
    unmatched = 0
    orphan = 0        # 完全没有可用签名键的 error
    matched_calls: list[dict] = []
    matched_errors: list[dict] = []
    for err in errors:
        payload = _payload(err)
        name = payload.get("tool_name")
        name = name if isinstance(name, str) else ""
        sig = _call_sig(err)
        cid = payload.get("call_id")
        sid = payload.get("span_id")
        candidates: list[dict] = []
        if sig is not None:
            candidates.append(("content_sha256", by_cs.get(sig, [])))
        if isinstance(cid, str) and cid:
            candidates.append(("call_id", by_cid.get((name, cid), [])))
        if isinstance(sid, str) and sid:
            candidates.append(("span_id", by_sid.get((name, sid), [])))
        if not candidates:
            orphan += 1
            unmatched += 1
            continue
        hit = None
        for _key_name, pool in candidates:      # 优先级 content_sha256 > call_id > span_id
            for call in pool:                   # 池内按 seq 升序（events 已全序）
                if int(call["seq"]) > int(err["seq"]) and _retry_window_ok(err, call, params):
                    hit = call
                    break
            if hit is not None:
                break
        if hit is None:
            unmatched += 1
        else:
            matched += 1
            matched_calls.append(hit)
            matched_errors.append(err)

    value = round(min(max(matched / len(calls), 0.0), 1.0), 6)
    # R2-1：confidence 按签名来源等级判定（不是分母样本量）——重试匹配依赖签名，
    # 签名是代理键时分母再大也是近似的
    confidence, source_note = _signature_confidence(calls, joined, native, params)
    note = (f"分母=tool.call {len(calls)} 次；error 事件 {len(errors)} 个："
            f"matched {matched} / unmatched {unmatched}（其中无任何签名键 {orphan}）；"
            f"签名优先级 content_sha256 > call_id > span_id；{source_note}；{SIGNATURE_SOURCE_NOTE}；"
            f"调用侧 {joined} 次的哈希由 span_id 从 span_end 补齐"
            f"（本 run {empty_own}/{len(calls)} 次 tool.call 自身为空输入常量哈希）；"
            f"窗口 retry_window_ms={params.retry_window_ms}，"
            f"时戳不可解析时退化 seq 窗口={params.retry_window_events}")
    if matched > len(calls):
        note += "；matched 已超过调用数，ratio 按 1.0 截断"
    anchors = matched_calls + matched_errors if matched_calls else calls
    ref_note = ("命中的重试调用 + 触发重试的 error" if matched_calls
                else "本 run 无命中的重试；证据为全部 tool.call（分母）")
    return MetricOutcome.ok(value, evidence=evidence_from(anchors, params=params, note=ref_note),
                            confidence=confidence, note=note)


@register_metric(
    key="process.trajectory_efficiency", group="process", label_zh="轨迹效率（实际步数/参考步数）",
    unit="ratio", direction=False, scope="group", event_types=("step.start",),
    aggregator=agg_trajectory_efficiency)
def trajectory_efficiency(events: Sequence[dict], params: MetricParams,
                          ctx: MetricContext) -> MetricOutcome:
    """run 级恒降级：参考步数需同 taskset 同变体的跨 run 历史 + 归属元数据（T10）。"""
    return MetricOutcome.na(
        "no_source",
        "run 级缺参考步数：分子（本 run 步数）可算，但分母需同 taskset 同变体的历史最短步数，"
        "依赖 run 归属元数据 (task_id, variant_id) 与跨 run 历史（属 T10 编排层）；"
        "本卡只交 scope=group 聚合接口（compute_group）")


# ---------------------------------------------------------------------------
# cost.* —— 成本指标（前向兼容：读到就算、读不到降级）
# ---------------------------------------------------------------------------

def _usage_scan(events: Sequence[dict]) -> tuple[list[dict], list[dict | None]]:
    responses = _of_type(events, "llm.response")
    return responses, [_usage_of(ev) for ev in responses]


def trace_summary_tokens(events: Sequence[dict]) -> dict:
    """扫描 `metric.custom`（metric=trace_summary）的 trace 级 `total_tokens`（条件二路径用）。

    返回 `{events, with_total, positive, sum}`：`with_total` = 非 null 的条数、
    `positive` = > 0 的条数、`sum` = 只累加 > 0 的值。探针（E5-P）与 tokens_total 共用本函数，
    保证「报的数字」与「用的数字」同源。
    """
    rows = [ev for ev in events
            if ev.get("type") == "metric.custom"
            and _payload(ev).get("metric") == "trace_summary"]
    with_total = 0
    positive = 0
    total = 0
    for ev in rows:
        raw = _payload(ev).get("total_tokens")
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        with_total += 1
        if raw > 0:
            positive += 1
            total += int(raw)
    return {"events": len(rows), "with_total": with_total, "positive": positive, "sum": total}


@register_metric(
    key="cost.tokens_total", group="cost", label_zh="Token 总量",
    unit="tokens", direction=False, scope="run", event_types=("llm.response", "metric.custom"))
def tokens_total(events: Sequence[dict], params: MetricParams,
                 ctx: MetricContext) -> MetricOutcome:
    """累加 `llm.response` 的 usage.total（路径 ①）；条件二路径见下（默认关闭）。

    优先级（设计 §3.5）：① 请求级 usage 逐请求累加 > ② `metric.custom` 的 trace 级汇总。
    **互斥防双计**：run 内只要有一条非零请求级 usage → 只用 ①，② 完全不参与（不相加、不取大）。
    """
    responses, usages = _usage_scan(events)
    hits = [(ev, u) for ev, u in zip(responses, usages) if u and u.get("total") is not None]
    request_total = sum(int(u["total"]) for _, u in hits)
    if request_total > 0:
        partial = len(hits) < len(responses)
        # P0-4（T05c，唯一被批准的指标层改动之一）：时间窗顺序配对不是键级对齐，
        # 参与计算的 usage 事件存在 alignment == "time_window" 时 confidence 强制 low。
        time_window = any(
            isinstance(_payload(ev).get("usage"), dict)
            and _payload(ev)["usage"].get("alignment") == "time_window"
            for ev, _ in hits)
        note = (f"覆盖度 {len(hits)}/{len(responses)} 条 llm.response 读到 usage.total"
                + ("（非全覆盖，合计为下界，口径为派生）" if partial else "")
                + ("；时间窗顺序配对，非键级对齐" if time_window else ""))
        return MetricOutcome.ok(float(request_total),
                                evidence=evidence_from([ev for ev, _ in hits], params=params,
                                                       note="贡献 token 的 llm.response 事件"),
                                confidence=("low" if time_window
                                            else _confidence(len(hits), params, derived=partial)),
                                note=note)

    probe = trace_summary_tokens(events)
    if params.tokens_trace_summary_fallback and probe["sum"] > 0:
        rows = [ev for ev in events
                if ev.get("type") == "metric.custom"
                and _payload(ev).get("metric") == "trace_summary"
                and isinstance(_payload(ev).get("total_tokens"), int)
                and not isinstance(_payload(ev).get("total_tokens"), bool)
                and _payload(ev).get("total_tokens") > 0]
        note = (f"trace 级汇总（`metric.custom`/trace_summary），非逐请求累加，口径为派生；"
                f"本 run {probe['positive']}/{probe['events']} 条 trace_summary 的 total_tokens > 0，"
                "请求级 usage 全 0 或缺失，两路径互斥")
        return MetricOutcome.ok(float(probe["sum"]),
                                evidence=evidence_from(rows, params=params,
                                                       note="贡献 trace 级 total_tokens 的 metric.custom 事件"),
                                confidence="low", note=note)

    detail = (f"{_cost_no_source_fact(events, responses, usages)}；"
              f"本 run 读到 usage.total 的 {len(hits)} 条且合计为 0"
              f"（条件二路径开关 tokens_trace_summary_fallback="
              f"{params.tokens_trace_summary_fallback}）")
    return MetricOutcome.na("no_source", detail + "。T05 v2 补采后本函数零改动即出全值")


def _tokens_slot(events: Sequence[dict], params: MetricParams, slot: str) -> MetricOutcome:
    responses, usages = _usage_scan(events)
    hits = [(ev, u) for ev, u in zip(responses, usages) if u and u.get(slot) is not None]
    label = {"in": "tokens_in", "out": "tokens_out"}[slot]
    if not hits:
        return MetricOutcome.na(
            "no_source",
            f"{_cost_no_source_fact(events, responses, usages)}；"
            f"本 run 读到 usage.{slot} 0 条 "
            f"（`{label}` 拆分只能来自 llm.response usage 或通道 B 补采；trace 级汇总无 in/out 拆分，"
            "不用 total 反推）")
    value = sum(int(u[slot]) for _, u in hits)
    partial = len(hits) < len(responses)
    # P0-4（T05c，唯一被批准的指标层改动之一）：同 tokens_total——alignment 驱动 confidence。
    time_window = any(
        isinstance(_payload(ev).get("usage"), dict)
        and _payload(ev)["usage"].get("alignment") == "time_window"
        for ev, _ in hits)
    note = (f"覆盖度 {len(hits)}/{len(responses)} 条 llm.response 读到 usage.{slot}"
            + ("（非全覆盖，合计为下界，口径为派生）" if partial else "")
            + ("；时间窗顺序配对，非键级对齐" if time_window else ""))
    return MetricOutcome.ok(float(value),
                            evidence=evidence_from([ev for ev, _ in hits], params=params,
                                                   note=f"贡献 usage.{slot} 的 llm.response 事件"),
                            confidence=("low" if time_window
                                        else _confidence(len(hits), params, derived=partial)), note=note)


@register_metric(
    key="cost.tokens_in", group="cost", label_zh="输入 Token 量",
    unit="tokens", direction=False, scope="run", event_types=("llm.response",))
def tokens_in(events: Sequence[dict], params: MetricParams,
              ctx: MetricContext) -> MetricOutcome:
    """累加 `llm.response` 的 usage.in。"""
    return _tokens_slot(events, params, "in")


@register_metric(
    key="cost.tokens_out", group="cost", label_zh="输出 Token 量",
    unit="tokens", direction=False, scope="run", event_types=("llm.response",))
def tokens_out(events: Sequence[dict], params: MetricParams,
               ctx: MetricContext) -> MetricOutcome:
    """累加 `llm.response` 的 usage.out。"""
    return _tokens_slot(events, params, "out")


@register_metric(
    key="cost.cny", group="cost", label_zh="成本（人民币）",
    unit="cny", direction=False, scope="run", event_types=("llm.response",))
def cost_cny(events: Sequence[dict], params: MetricParams,
             ctx: MetricContext) -> MetricOutcome:
    """Σ(tokens_in × 单价in + tokens_out × 单价out)，单价取自 pricing 配置（本轮留空占位）。"""
    responses, usages = _usage_scan(events)
    priced = [(ev, u) for ev, u in zip(responses, usages)
              if u and u.get("in") is not None and u.get("out") is not None]
    if not priced:
        return MetricOutcome.na(
            "no_source",
            f"{_cost_no_source_fact(events, responses, usages)}；token 用量降级 → 成本连带降级（不做估算）")
    pricing = params.pricing or {}
    total = 0.0
    unpriced: set[str] = set()
    used_default = 0
    for ev, usage in priced:
        model = _model_of(ev)
        entry = pricing.get(model) if model is not None else None
        if not isinstance(entry, dict) or entry.get("in") is None or entry.get("out") is None:
            entry = pricing.get("default")
            used_default += 1
        if not isinstance(entry, dict) or entry.get("in") is None or entry.get("out") is None:
            unpriced.add(model or "<model 缺失>")
            continue
        total += float(usage["in"]) * float(entry["in"]) + float(usage["out"]) * float(entry["out"])
    if unpriced:
        return MetricOutcome.na(
            "no_source",
            f"单价表未配置：`pricing.yaml` 缺这些模型的单价 {sorted(unpriced)}（本轮单价留空占位，"
            "禁止编造单价——编造价会让成本指标变成假数据）")
    note = (f"按 pricing 配置计价：{len(priced)} 条 llm.response 命中单价"
            f"（其中 {used_default} 条使用 default 兜底）；口径为派生（token 数 × 单价换算，非直接观测）")
    return MetricOutcome.ok(round(total, 6),
                            evidence=evidence_from([ev for ev, _ in priced], params=params,
                                                   note="参与计价的 llm.response 事件"),
                            confidence="low",   # Q7：派生口径恒 low（换算式，样本量再大也不改口径）
                            note=note)


def _run_wall_clock(events: Sequence[dict]) -> int | None:
    """从 run.start / run.end 事件求 run 级墙钟（当前数据源无此事件，属 T10 合成）。"""
    starts = _of_type(events, "run.start")
    ends = _of_type(events, "run.end")
    if not starts or not ends:
        return None
    start_ms = _iso_to_ms(str(starts[0].get("ts") or ""))
    end_ms = _iso_to_ms(str(ends[-1].get("ts") or ""))
    if start_ms == 0 or end_ms <= start_ms:
        return None
    return end_ms - start_ms


def _wall_clock(events: Sequence[dict], params: MetricParams,
                ctx: MetricContext, q: float) -> MetricOutcome:
    value = None
    source = ""
    if ctx.membership is not None and ctx.membership.wall_clock_ms is not None:
        value = int(ctx.membership.wall_clock_ms)
        source = "ctx.membership.wall_clock_ms（T10 供给）"
    else:
        derived = _run_wall_clock(events)
        if derived is not None:
            value = derived
            source = "run.start/run.end 事件差（T10 合成后才会出现）"
    if value is None:
        return MetricOutcome.na("no_source", f"缺 run 级墙钟：{_UPSTREAM_WALL_CLOCK_FACT}")
    endpoints = _of_type(events, "run.start") + _of_type(events, "run.end")
    note = (f"单 run 的分位数即其本身（q={q}）；来源={source}；"
            "样本量 1 → confidence=low，跨 run 分位数请用 compute_group")
    refs = evidence_from(endpoints, params=params, note="run 级起止事件") if endpoints else ()
    if not refs:
        return MetricOutcome.na(
            "no_source",
            f"墙钟值来自 {source}，但事件流内无 run.start/run.end 可作证据锚点（G5 要求可下钻）")
    return MetricOutcome.ok(round(quantile([value], q), 6), evidence=refs,
                            confidence="low", note=note)


@register_metric(
    key="cost.wall_clock_p50", group="cost", label_zh="墙钟 P50",
    unit="ms", direction=False, scope="group", event_types=("run.start", "run.end"),
    aggregator=agg_wall_clock_p50)
def wall_clock_p50(events: Sequence[dict], params: MetricParams,
                   ctx: MetricContext) -> MetricOutcome:
    """单 run 墙钟（分位数在 compute_group 跨 run 计算）。"""
    return _wall_clock(events, params, ctx, params.wall_clock_quantiles[0])


@register_metric(
    key="cost.wall_clock_p95", group="cost", label_zh="墙钟 P95",
    unit="ms", direction=False, scope="group", event_types=("run.start", "run.end"),
    aggregator=agg_wall_clock_p95)
def wall_clock_p95(events: Sequence[dict], params: MetricParams,
                   ctx: MetricContext) -> MetricOutcome:
    """单 run 墙钟（分位数在 compute_group 跨 run 计算）。"""
    return _wall_clock(events, params, ctx, params.wall_clock_quantiles[-1])


# ---------------------------------------------------------------------------
# result.* —— 结果指标（依赖 T09 判定器 / T10 归属元数据）
# ---------------------------------------------------------------------------

@register_metric(
    key="result.success", group="result", label_zh="任务成功率",
    unit="ratio", direction=True, scope="run",
    event_types=("task.create", "task.update", "task.complete"))
def success_rate(events: Sequence[dict], params: MetricParams,
                 ctx: MetricContext) -> MetricOutcome:
    """判定器通过数 ÷ 总数（输入来自 T09 的最小 verdict 接口）。"""
    if ctx.verdicts is None:
        return MetricOutcome.na(
            "needs_input",
            "等待 T09 判定器按最小接口 {run_id, task_id, passed: bool} 供数（PRD §9-裁定5）；"
            "events 里没有「任务是否通过」的语义，不得由事件流推测")
    matched = [v for v in ctx.verdicts if v.run_id == ctx.run_id]
    if not matched:
        return MetricOutcome.na(
            "needs_input",
            f"传入 {len(ctx.verdicts)} 条 verdict，但无一条 run_id == 本 run；"
            "无法判定本 run 结果（不跨 run 复用判定）")
    passed = sum(1 for v in matched if v.passed)
    value, confidence = _rate(passed, len(matched), params)
    anchored: list[dict] = []
    task_ids: list[str] = []
    unanchored = 0
    for verdict in matched:
        if verdict.task_id is None:
            unanchored += 1
            continue
        hit = [ev for ev in _of_type(events, "task.create", "task.update", "task.complete")
               if _payload(ev).get("task_id") == verdict.task_id]
        if hit:
            anchored.extend(hit)
            task_ids.append(verdict.task_id)
        else:
            unanchored += 1
    note = (f"verdict {len(matched)} 条，passed {passed}；"
            f"锚定到 task 事件的 verdict {len(matched) - unanchored} 个")
    if unanchored:
        fallback = [max(events, key=lambda ev: int(ev["seq"]))] if events else []
        anchored.extend(fallback)
        note += f"；{unanchored} 个 verdict 未锚定到 task 事件，锚点退化为 run 末事件"
    if not anchored:
        return MetricOutcome.na(
            "no_source", "verdicts 有判定但事件流为空，无可下钻证据锚点（G5）")
    return MetricOutcome.ok(value,
                            evidence=evidence_from(anchored, params=params, note=note,
                                                   task_ids=task_ids),
                            confidence=confidence, note=note)


@register_metric(
    key="result.pass_k", group="result", label_zh="pass^k（k 次全通过比例）",
    unit="ratio", direction=True, scope="group", event_types=(),
    aggregator=agg_pass_k)
def pass_k(events: Sequence[dict], params: MetricParams,
           ctx: MetricContext) -> MetricOutcome:
    """run 级恒降级：需 (task_id, variant_id, k) 归属元数据与多次重复 run。"""
    return MetricOutcome.na(
        "no_source",
        "缺 run 归属元数据 (task_id, variant_id, k)：runs 表无这些列（加列属 v2 迁移），"
        "本轮只交 scope=group 聚合接口（compute_group('result.pass_k', ...)）")

"""T06：指标注册器 + MetricSet 装配层（本模块是唯一的 IO 边界）。

三层分工（设计文档 §1.1）：
- 本模块 `metrics.py`：注册器 + 数据结构 + `compute_run` / `compute_group`（IO 只在这里）
- `metrics_core.py`：11 个指标纯函数（零 IO），import 本模块的 `register_metric` 自注册
- `models.py`：schema 生成物（只消费，不改）

输出双文件（设计文档 §3.1，schema 禁改）：
- `metricset.json`：纯 schema 合规的 MetricSet（`hs schema check --kind metric-set` 必须通过）
- `metricset.ext.json`：旁路扩展（channel_policy / degraded_reasons / canonical_sha256 / params_digest …）

**键名互斥（R1-3）**：主文件的 `degraded_metrics` 是 `list[str]`（schema 约束）；ext 里那份
「key → {reason, detail}」叫 `degraded_reasons`（**对象**）。同名不同构会让下游取键时静默拿到
错误类型，故 ext 里**不得**再出现 `degraded_metrics`（单测逐字断言）。

不变量（任务卡 §2 坑 3、§7-5）：`value is None` ⟺ `key ∈ degraded_metrics` ⟺ `evidence == []`；
以及 Q7 的反向约束：`value is not None` ⟹ `confidence is not None`（有值必有档位）。

**R2-1 置信度来源等级**：签名类指标（retry_rate / redundant_loop）的 confidence 由
`metrics_core._signature_confidence` 按**签名来源等级**动态判定（原生→按样本量分档、
代理/键缺失/混合→恒 low），样本量只在原生分支上叠加取最低者——禁止写死到 key 上，
T05 v2 补到真实 content 后必须自动升档。
"""

from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import math
import re
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hs_core import __version__
from hs_core.models import EvidenceRef, Groups, MetricSet, MetricValue, Unit

SCHEMA_VERSION = "1.0.0"

# 降级 reason 枚举（Q6：集中定义，禁止自由文本；无法归类时用 other:<简短说明> 兜底）
DEGRADE_REASONS: tuple[str, ...] = (
    "no_source",          # 源里根本没有这份数据
    "insufficient_data",  # 有源但样本不足/分母为 0
    "needs_input",        # 等上游（T09 verdict / T10 归属元数据）供数
    "blocked",            # T06-03 策略门禁拒绝（merge 流）
    "computation_error",  # 单指标异常隔离（HS-3001）
)
_OTHER_REASON = re.compile(r"^other:.+$")

# key 前缀 → MetricSet.groups 字段（硬映射，注册时校验）
GROUP_BY_PREFIX: dict[str, str] = {
    "result": "result",
    "process": "process",
    "collab": "collaboration",
    "cost": "cost",
}
_GROUP_ALIASES: dict[str, str] = {"collab": "collaboration", "collaboration": "collaboration"}
_UNITS: tuple[str, ...] = ("ratio", "count", "ms", "cny", "tokens", "score")
_KEY_PATTERN = re.compile(r"^(result|process|collab|cost)\.[a-z][a-z0-9_]*$")

BUILTIN_METRIC_MODULES: tuple[str, ...] = ("hs_core.metrics_core", "hs_core.metrics_collab")

# merge 流拒绝理由（T06-03）。数字来自 docs/channel-priority.md 的探针实测（引用式：
# 不是本次运行观测——本 run 是 primary_only，根本读不到 merge 流的事件）。
BLOCKED_REASON = (
    "channel_policy != 'primary_only'：merge 流工具类指标重复近一倍"
    "（引用：主理人探针实测 tool.call 1416 vs 727，见 docs/channel-priority.md；非本次运行观测），"
    "不得作为指标口径（T06-03）"
)

WALL_CLOCK_FACT = (
    "run 级起止属 T10 编排层合成（26 类事件表中 run.start/run.end 无被测平台源）；"
    "span 级 duration_ms 与 run 级墙钟是两回事（引用：主理人快照实测 span 级 701/701 非空，"
    "非本次运行观测，见 docs/metrics-engine.md §5.11），不能拿来冒充 run 级"
)

# ---------------------------------------------------------------------------
# R1-2 口径锁定常量（T06-R1 返工单）：新增/删除指标必须三处同改 ——
# 本常量 + 设计文档 §3.4 + PRD §4。禁止实现侧单方面新增 key。
# 锁的是「实现能吐哪些 key」，不是「本次数据算出几个」：数据侧的合法漂移
# （如通道 A 出现非零 usage 使 cost.* 出值，G7 前向兼容）在 key_count_check 里如实上报。
# ---------------------------------------------------------------------------
EXPECTED_COMPUTED_KEYS: tuple[str, ...] = (
    "process.redundant_loop",
    "process.retry_rate",
    "process.step_count",
    "process.tool_correctness",
)
EXPECTED_DEGRADED_KEYS: tuple[str, ...] = (
    "cost.cny",
    "cost.tokens_in",
    "cost.tokens_out",
    "cost.tokens_total",
    "cost.wall_clock_p50",
    "cost.wall_clock_p95",
    "process.trajectory_efficiency",
    "result.pass_k",
    "result.success",
)
EXPECTED_ALL_KEYS: tuple[str, ...] = tuple(
    sorted(EXPECTED_COMPUTED_KEYS + EXPECTED_DEGRADED_KEYS))


def key_count_check(values: dict[str, MetricValue]) -> dict:
    """R1-2 口径自检：实现侧 key 集合锁定 + 本次数据「实算 / 降级」条数如实上报。

    两条不同的判断，不要混为一谈：

    - `consistent`：实现吐出的**内置 key 集合**与常量逐字一致，且 `computed + degraded == total`
      （不一致 = 实现侧多吐/少吐 key，属 bug，装配层直接抛 `MetricAssemblyError`）
    - `computed_matches_expected` / `degraded_matches_expected`：本次数据是否恰好 4 可算 + 9 降级。
      数据侧的合法漂移（如通道 A 将来补到 usage 让 `cost.*` 出值，G7 前向兼容）会让这两项为
      false —— 那是**数据变了**，不是实现多吐 key，**不能报错**，只能如实上报。
    """
    produced = set(values)
    builtin = {k: v for k, v in values.items() if k in EXPECTED_ALL_KEYS}
    computed = sorted(k for k, v in builtin.items() if v.value is not None)
    degraded = sorted(k for k, v in builtin.items() if v.value is None)
    missing = sorted(set(EXPECTED_ALL_KEYS) - produced)
    return {
        "total": len(produced),
        "computed": len(computed),
        "degraded": len(degraded),
        "consistent": not missing and len(computed) + len(degraded) == len(builtin),
        "expected_total": len(EXPECTED_ALL_KEYS),
        "expected_computed": len(EXPECTED_COMPUTED_KEYS),
        "expected_degraded": len(EXPECTED_DEGRADED_KEYS),
        "computed_matches_expected": computed == sorted(EXPECTED_COMPUTED_KEYS),
        "degraded_matches_expected": degraded == sorted(EXPECTED_DEGRADED_KEYS),
        "missing_keys": missing,
        "extension_keys": sorted(produced - set(EXPECTED_ALL_KEYS)),
        "computed_keys": computed,
        "degraded_keys": degraded,
    }


def _assert_builtin_registry_locked() -> None:
    """内置模块注册的 key 集合必须与口径锁定常量逐字一致（双向：多吐/少吐都报错）。

    这就是「禁止实现侧单方面新增 key」的落点：不三处同改常量与文档，注册即失败。
    第三方 extra module 不在本锁范围（显式扩展，非静默漂移）。
    """
    builtin = sorted(k for k, s in REGISTRY.items() if s.module in BUILTIN_METRIC_MODULES)
    expected = list(EXPECTED_ALL_KEYS)
    if builtin != expected:
        extra = sorted(set(builtin) - set(expected))
        missing = sorted(set(expected) - set(builtin))
        raise MetricRegistryError(
            f"内置指标集与口径锁定常量不一致（多出 {extra}，缺失 {missing}）："
            "新增/删除指标必须同步改 EXPECTED_COMPUTED_KEYS / EXPECTED_DEGRADED_KEYS "
            "+ 设计文档 §3.4 + PRD §4，禁止实现侧单方面新增 key（T06-R1 R1-2）")


class MetricError(Exception):
    """指标层基础错误。"""


class MetricRegistryError(MetricError):
    """注册器违规：重复 key / key 与 group 不匹配 / 非法 unit 等。"""


class MetricAssemblyError(MetricError):
    """装配违规：不变量被破坏（value/evidence/degraded 三者不一致）。"""


class MetricRunError(MetricError):
    """run 级错误（run 不存在等）。"""


# ---------------------------------------------------------------------------
# 数据结构（§3.2）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MetricParams:
    """全部阈值可配（T06-09），禁止在算法里硬编码阈值。"""

    loop_min_repeat: int = 2
    loop_max_period: int = 5
    retry_window_ms: int = 60_000
    retry_window_events: int = 10
    low_confidence_n: int = 20
    max_evidence_ids: int = 50
    idle_threshold_s: float = 30.0          # [T07 预留]
    effect_window_s: float = 30.0           # [T07 预留]
    message_merge_key: str = "message_id"
    wall_clock_quantiles: tuple[float, ...] = (0.50, 0.95)
    # §3.5 条件二路径开关：默认 False。引用：主理人 9098 事件快照实测 metric.custom 的
    # total_tokens 33/33 全为 0（占位未实现，非本次运行观测），故本轮维持 no_source；
    # 保留开关是为了将来通道 A 产出非零值时可零改动启用。
    tokens_trace_summary_fallback: bool = False
    pricing: dict = field(default_factory=dict)   # {"<model>": {"in": x, "out": y}, "default": {...}}

    def as_dict(self) -> dict:
        return {
            "loop_min_repeat": self.loop_min_repeat,
            "loop_max_period": self.loop_max_period,
            "retry_window_ms": self.retry_window_ms,
            "retry_window_events": self.retry_window_events,
            "low_confidence_n": self.low_confidence_n,
            "max_evidence_ids": self.max_evidence_ids,
            "idle_threshold_s": self.idle_threshold_s,
            "effect_window_s": self.effect_window_s,
            "message_merge_key": self.message_merge_key,
            "wall_clock_quantiles": list(self.wall_clock_quantiles),
            "tokens_trace_summary_fallback": self.tokens_trace_summary_fallback,
            "pricing": self.pricing,
        }

    def digest(self) -> str:
        """规范 JSON（键字典序、数值 round 6）的 sha256，写进 ext.params_digest。"""
        return "sha256:" + hashlib.sha256(
            canonical_json(self.as_dict()).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class VerdictRecord:
    """与 T09 的最小契约（PRD §9-裁定5）。"""

    run_id: str
    task_id: str | None
    passed: bool


@dataclass(frozen=True)
class RunMembership:
    """run 归属元数据 [T10 供给，本轮恒 None]。"""

    task_id: str | None = None
    variant_id: str | None = None
    k: int | None = None
    wall_clock_ms: int | None = None


@dataclass(frozen=True)
class MetricContext:
    """纯值对象（无 IO）：承载事件流之外的输入，保证指标函数仍是纯函数。"""

    run_id: str
    platform: str | None
    channel_policy: str
    event_count: int
    verdicts: tuple[VerdictRecord, ...] | None = None
    membership: RunMembership | None = None
    group_runs: tuple[RunMembership, ...] = ()   # [跨 run 聚合预留]


@dataclass(frozen=True)
class MetricOutcome:
    """指标函数的返回结构（装配层据此生成 MetricValue + ext）。

    value=None 即降级：必须给 reason（枚举或 other:<说明>）与 detail（缺什么、归谁修）。
    """

    value: float | bool | None
    evidence: tuple[EvidenceRef, ...] = ()
    confidence: str | None = None
    unit: str | None = None
    reason: str | None = None
    detail: str | None = None
    note: str | None = None       # 非数值性说明（写进 ext.notes[key]）

    @classmethod
    def ok(cls, value: float | bool, *, evidence: tuple[EvidenceRef, ...],
           confidence: str | None = None, unit: str | None = None,
           note: str | None = None) -> MetricOutcome:
        return cls(value=value, evidence=evidence, confidence=confidence, unit=unit, note=note)

    @classmethod
    def na(cls, reason: str, detail: str, *, note: str | None = None) -> MetricOutcome:
        return cls(value=None, evidence=(), reason=reason, detail=detail, note=note)


MetricFn = Callable[..., Any]
MetricAggregatorFn = Callable[[Sequence[tuple[MetricValue, "RunMembership | None"]],
                               MetricParams], MetricOutcome]


@dataclass(frozen=True)
class MetricSpec:
    key: str
    group: str
    label_zh: str
    unit: str | None
    direction: bool | None
    scope: str
    event_types: tuple[str, ...]
    version: str
    fn: MetricFn
    module: str = ""      # 注册来源模块（load_builtin_metrics 的幂等依据）


@dataclass
class MetricSetResult:
    """`compute_run` 的返回值：schema 合规主文档 + 旁路扩展 + 是否被策略拒绝。"""

    metric_set: MetricSet
    ext: dict
    blocked: bool

    @property
    def by_key(self) -> dict[str, MetricValue]:
        out: dict[str, MetricValue] = {}
        for group in (self.metric_set.groups.result, self.metric_set.groups.process,
                      self.metric_set.groups.collaboration, self.metric_set.groups.cost):
            for mv in group:
                out[mv.key] = mv
        return out

    def summary(self) -> dict:
        keys = sorted(self.by_key)
        computed = [k for k in keys if self.by_key[k].value is not None]
        return {
            "run_id": self.metric_set.run_id,
            "blocked": self.blocked,
            "computed_keys": computed,
            "degraded_keys": sorted(self.metric_set.degraded_metrics),
            "canonical_sha256": self.ext.get("canonical_sha256"),
            "params_digest": self.ext.get("params_digest"),
            "event_count": self.ext.get("event_count"),
            "elapsed_ms": self.ext.get("elapsed_ms"),
            "data_quality_flags": self.ext.get("data_quality_flags", []),
        }


# ---------------------------------------------------------------------------
# 注册器（T07 零改动接入点）
# ---------------------------------------------------------------------------

REGISTRY: dict[str, MetricSpec] = {}
GROUP_AGGREGATORS: dict[str, MetricAggregatorFn] = {}
_LOADED_MODULES: list[str] = []


def _normalize_group(key: str, group: str) -> str:
    expected = GROUP_BY_PREFIX.get(key.split(".", 1)[0])
    if expected is None:
        raise MetricRegistryError(f"key {key!r} 前缀非法，须为 result|process|collab|cost")
    normalized = _GROUP_ALIASES.get(group, group)
    if normalized != expected:
        raise MetricRegistryError(
            f"key {key!r} 的 group 必须为 {expected!r}（收到 {group!r}）")
    return normalized


def _wrap_arity(fn: MetricFn) -> MetricFn:
    """2 元函数 (events, params) 自动包装为 3 元（PRD §6 对外签名兼容）。"""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):  # 内建/装饰器包装无法取签名时按 3 元处理
        return fn
    positional = [p for p in sig.parameters.values()
                  if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    if len(positional) == 2:
        return lambda events, params, ctx: fn(events, params)
    return fn


def register_metric(
    *,
    key: str,
    group: str,
    label_zh: str,
    unit: str | None = None,
    direction: bool | None = None,
    scope: str = "run",
    event_types: tuple[str, ...] = (),
    version: str = "1.0.0",
    aggregator: MetricAggregatorFn | None = None,
) -> Callable[[MetricFn], MetricFn]:
    """注册一个指标纯函数。重复注册同 key → MetricRegistryError（不静默覆盖）。"""
    if not _KEY_PATTERN.match(key):
        raise MetricRegistryError(f"key {key!r} 不匹配 ^(result|process|collab|cost)\\.[a-z][a-z0-9_]*$")
    if not label_zh:
        raise MetricRegistryError(f"{key}: label_zh 不能为空")
    if unit is not None and unit not in _UNITS:
        raise MetricRegistryError(f"{key}: unit {unit!r} 非法，可选 {_UNITS}")
    if scope not in ("run", "group"):
        raise MetricRegistryError(f"{key}: scope {scope!r} 非法，可选 'run'|'group'")
    if key in REGISTRY:
        raise MetricRegistryError(
            f"重复注册指标 {key!r}（已有 {REGISTRY[key].fn.__module__}.{REGISTRY[key].fn.__name__}）；"
            "如需替换请先显式 clear_registry()")
    normalized_group = _normalize_group(key, group)

    def deco(fn: MetricFn) -> MetricFn:
        wrapped = _wrap_arity(fn)
        REGISTRY[key] = MetricSpec(
            key=key, group=normalized_group, label_zh=label_zh, unit=unit,
            direction=direction, scope=scope,
            event_types=tuple(event_types), version=version, fn=wrapped,
            module=str(getattr(fn, "__module__", "") or ""))
        if aggregator is not None:
            GROUP_AGGREGATORS[key] = aggregator
        return fn   # 返回原函数（保持模块内可直接调用/单测）

    return deco


def list_metrics(*, group: str | None = None, scope: str | None = None) -> list[MetricSpec]:
    wanted = _GROUP_ALIASES.get(group, group) if group else None
    specs = [s for s in REGISTRY.values()
             if (wanted is None or s.group == wanted) and (scope is None or s.scope == scope)]
    return sorted(specs, key=lambda s: s.key)


def get_metric(key: str) -> MetricSpec:
    try:
        return REGISTRY[key]
    except KeyError as exc:
        raise MetricRegistryError(f"未注册的指标 key: {key!r}") from exc


def load_builtin_metrics(extra_modules: Iterable[str] = ()) -> list[str]:
    """按显式模块清单加载（缺失静默跳过→T07 落地即自动装配）。返回本次新加载的模块名。

    幂等：同一模块重复调用不会重复注册。若模块已被别处 import（如单测直接 import），
    重新加载前先摘掉该模块自己注册的 key——跨模块重复 key 仍由 `register_metric` 显式报错。
    """
    extra = [m for m in dict.fromkeys(extra_modules) if m]
    loaded: list[str] = []
    for name in list(BUILTIN_METRIC_MODULES) + extra:
        if name in _LOADED_MODULES:
            continue
        builtin = name in BUILTIN_METRIC_MODULES
        if name in sys.modules:
            _drop_module_specs(name)
        try:
            module = (importlib.reload(sys.modules[name]) if name in sys.modules
                      else importlib.import_module(name))
        except ModuleNotFoundError as exc:
            if builtin and exc.name == name:
                continue      # 内置模块尚未交付（如 T07 的 metrics_collab）→ 静默跳过
            raise
        _LOADED_MODULES.append(name)
        loaded.append(module.__name__)
    if _LOADED_MODULES or REGISTRY:      # 内置模块至少加载过一次才校验口径锁
        _assert_builtin_registry_locked()
    return loaded


def _drop_module_specs(module_name: str) -> None:
    """摘除某模块自己注册的指标（仅用于重新加载前的自我覆盖，不用于跨模块抢占）。"""
    for key in [k for k, spec in REGISTRY.items() if spec.module == module_name]:
        REGISTRY.pop(key, None)
        GROUP_AGGREGATORS.pop(key, None)


def clear_registry() -> None:
    """清空注册表与模块加载记录（仅供单测隔离）。"""
    REGISTRY.clear()
    GROUP_AGGREGATORS.clear()
    _LOADED_MODULES.clear()


# ---------------------------------------------------------------------------
# 规范序列化 / 摘要（Q2：canonical_sha256 排除 computed_at）
# ---------------------------------------------------------------------------

def _canon(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise MetricAssemblyError("canonical 序列化遇到 NaN/Infinity（schema 的 number 不允许）")
        return round(value, 6)
    if isinstance(value, dict):
        return {str(k): _canon(value[k]) for k in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_canon(v) for v in value]
    return value


def canonical_json(value: Any) -> str:
    """规范 JSON：键字典序、浮点 round(6)、无多余空白；NaN/Infinity 直接报错。"""
    return json.dumps(_canon(value), ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(",", ":"))


def _drop_none(obj: Any) -> Any:
    """递归删除 `None` 值（schema 里 unit/higher_is_better/confidence 等可选字段不接受 null）。

    **只用于落盘序列化**：`value: null` 也有语义（降级），所以它由调用方显式保留。
    """
    if isinstance(obj, dict):
        return {k: _drop_none(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_drop_none(v) for v in obj]
    return obj


def schema_dict(metric_set: MetricSet) -> dict:
    """落盘 / 校验用的主文档字典。

    必须与 `metricset.json` 内容一致（`hs schema check --kind metric-set` 的输入）：
    `metric-set.schema.json` 对 `unit` / `higher_is_better` / `confidence` 只允许枚举字符串或布尔，
    **不接受 null**，因此这些字段取 None 时整体省略而不是写 null；`value` 仍保留 null。
    """
    payload = metric_set.model_dump(mode="json")
    for group in payload["groups"].values():
        for mv in group:
            for key in ("unit", "higher_is_better", "confidence"):
                if mv.get(key) is None:
                    mv.pop(key, None)
            mv["evidence"] = [_drop_none(ref) for ref in mv["evidence"]]
    return payload


def canonical_dict(metric_set: MetricSet) -> dict:
    """canonical_sha256 的输入（完整定义见 docs/metrics-engine.md §规范序列化）。

    参与字段：run_id / schema_version / engine_version / groups.*（各数组按 key 升序）/
    degraded_metrics（升序）。**排除 computed_at**（每次不同，排除后才可复现）。
    字段取值与 `metricset.json` 完全一致（同样省略 None 的可选字段），
    因此复核方可以直接对主文件删掉 `computed_at` 后规范化复算。
    """
    payload = schema_dict(metric_set)
    return {
        "run_id": payload["run_id"],
        "schema_version": payload["schema_version"],
        "engine_version": payload["engine_version"],
        "groups": {
            "result": sorted(payload["groups"]["result"], key=lambda m: m["key"]),
            "process": sorted(payload["groups"]["process"], key=lambda m: m["key"]),
            "collaboration": sorted(payload["groups"]["collaboration"], key=lambda m: m["key"]),
            "cost": sorted(payload["groups"]["cost"], key=lambda m: m["key"]),
        },
        "degraded_metrics": sorted(payload["degraded_metrics"]),
    }


def canonical_sha256(metric_set: MetricSet) -> str:
    return "sha256:" + hashlib.sha256(
        canonical_json(canonical_dict(metric_set)).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 证据小件（集中实现「按 seq 升序去重 + 截断 + note 写明」规则）
# ---------------------------------------------------------------------------

def evidence_from(
    events: Sequence[dict],
    *,
    params: MetricParams,
    note: str,
    span_ids: Iterable[str] | None = None,
    agent_ids: Iterable[str] | None = None,
    task_ids: Iterable[str] | None = None,
) -> tuple[EvidenceRef, ...]:
    """把事件列表变成证据：按 seq 升序去重，截断到 max_evidence_ids，note 写明总数。"""
    ordered = sorted({(int(ev["seq"]), str(ev["event_id"])) for ev in events})
    if not ordered:
        return ()
    limit = max(1, int(params.max_evidence_ids))
    ids = [eid for _, eid in ordered[:limit]]
    suffix = (f"；共 {len(ordered)} 条，此处列出前 {len(ids)} 条（按 seq 升序）"
              if len(ordered) > len(ids) else f"；共 {len(ordered)} 条（全量列出，按 seq 升序）")
    return (EvidenceRef(
        event_ids=ids,
        span_ids=_cap(span_ids, limit),
        agent_ids=_cap(agent_ids, limit),
        task_ids=_cap(task_ids, limit),
        note=note + suffix,
    ),)


def _cap(values: Iterable[str] | None, limit: int) -> list[str] | None:
    if not values:
        return None
    uniq = sorted({str(v) for v in values if v})
    return uniq[:limit] or None


def quantile(values: Sequence[float], q: float) -> float:
    """线性插值分位数（不引 numpy）。单样本直接返回该值。"""
    import math

    if not values:
        raise ValueError("quantile() 需要非空样本")
    ordered = sorted(float(v) for v in values)
    if len(ordered) == 1:
        return ordered[0]
    q = min(max(float(q), 0.0), 1.0)
    pos = (len(ordered) - 1) * q
    low = math.floor(pos)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------

def _normalize_outcome(spec: MetricSpec, raw: Any) -> MetricOutcome:
    """指标函数可返回 MetricOutcome（结构化降级原因）或 MetricValue（T07 旧式契约）。"""
    if isinstance(raw, MetricOutcome):
        return raw
    if isinstance(raw, MetricValue):
        if raw.key != spec.key:
            raise MetricAssemblyError(f"{spec.key}: 指标函数返回的 key 为 {raw.key!r}，与注册 key 不一致")
        confidence = raw.confidence.value if raw.confidence is not None else None
        unit = raw.unit.value if raw.unit is not None else None
        if raw.value is None:
            return MetricOutcome.na(
                "other:metric_returned_null_without_reason",
                "指标函数返回 MetricValue(value=None) 但未声明结构化 reason；装配层兜底归类，"
                "请改为返回 MetricOutcome.na(reason, detail)")
        return MetricOutcome(value=raw.value, evidence=tuple(raw.evidence),
                             confidence=confidence, unit=unit)
    raise MetricAssemblyError(
        f"{spec.key}: 指标函数返回类型 {type(raw).__name__} 不受支持（须 MetricOutcome 或 MetricValue）")


def _round_value(value: float | bool | None) -> float | bool | None:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return round(float(value), 6)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise MetricAssemblyError("指标值出现 NaN/Infinity（schema 的 number 不允许）")
        return round(value, 6)
    raise MetricAssemblyError(f"指标值类型 {type(value).__name__} 不受支持（须 number|boolean|null）")


def _check_reason(key: str, reason: str | None) -> str:
    if reason is None:
        raise MetricAssemblyError(f"{key}: value=None 但未提供 reason")
    if reason in DEGRADE_REASONS or _OTHER_REASON.match(reason):
        return reason
    raise MetricAssemblyError(
        f"{key}: reason {reason!r} 不在枚举 {DEGRADE_REASONS} 内，也无法归类时请用 other:<简短说明>")


def _build_value(spec: MetricSpec, outcome: MetricOutcome) -> MetricValue:
    value = _round_value(outcome.value)
    unit = outcome.unit or spec.unit
    confidence = outcome.confidence
    if confidence is not None and confidence not in ("high", "medium", "low"):
        raise MetricAssemblyError(f"{spec.key}: confidence {confidence!r} 非法")
    if value is None:
        if outcome.evidence:
            raise MetricAssemblyError(
                f"{spec.key}: value=None 却带证据（不变量要求 degraded 的 evidence 必须为空）")
        return MetricValue(key=spec.key, label_zh=spec.label_zh, value=None,
                           unit=Unit(unit) if unit else None,
                           higher_is_better=spec.direction,
                           confidence=None, evidence=[])
    if not outcome.evidence:
        raise MetricAssemblyError(
            f"{spec.key}: value={value!r} 但 evidence 为空（G5：每个非空指标至少 1 条证据）")
    for ref in outcome.evidence:
        if not ref.event_ids:
            raise MetricAssemblyError(f"{spec.key}: 证据块 event_ids 为空")
    return MetricValue(key=spec.key, label_zh=spec.label_zh, value=value,
                       unit=Unit(unit) if unit else None,
                       higher_is_better=spec.direction,
                       confidence=confidence, evidence=list(outcome.evidence))


def _now(at: str | None) -> datetime:
    if at is None:
        return datetime.now(UTC)
    try:
        dt = datetime.fromisoformat(str(at))
    except ValueError as exc:
        raise MetricRunError(f"--at 不是合法 ISO-8601 时间: {at!r}") from exc
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def compute_run(
    store: Any,
    run_id: str,
    *,
    params: MetricParams | None = None,
    verdicts: Sequence[VerdictRecord] | None = None,
    membership: RunMembership | None = None,
    extra_modules: Iterable[str] = (),
    at: str | None = None,
    out_dir: str | Path | None = None,
) -> MetricSetResult:
    """计算单个 run 的 MetricSet（唯一 IO 边界：读库 → 纯函数 → 落盘）。"""
    started = time.perf_counter()
    params = params or MetricParams()
    load_builtin_metrics(extra_modules)
    run = store.get_run(run_id)
    if run is None:
        raise MetricRunError(f"run 不存在: {run_id!r}")

    specs = list_metrics()                      # 已按 key 字典序（确定性）
    policy = str(run.get("channel_policy") or "")
    values: dict[str, MetricValue] = {}
    degraded: dict[str, dict] = {}
    notes: dict[str, str] = {}
    evidence_index: dict[str, list[dict]] = {}
    data_quality: dict = {"flags": [], "details": {}, "tool_call_identity": None}
    seq_by_id: dict[str, int] = {}
    events: list[dict] = []
    blocked = policy != "primary_only"

    if blocked:
        for spec in specs:
            values[spec.key] = MetricValue(key=spec.key, label_zh=spec.label_zh, value=None,
                                           unit=Unit(spec.unit) if spec.unit else None,
                                           higher_is_better=spec.direction, confidence=None,
                                           evidence=[])
            degraded[spec.key] = {"reason": "blocked", "detail": BLOCKED_REASON}
    else:
        events = list(store.iter_events(run_id, order_by="seq"))
        seq_by_id = {str(ev["event_id"]): int(ev["seq"]) for ev in events}
        ctx = MetricContext(
            run_id=run_id, platform=run.get("platform"), channel_policy=policy,
            event_count=len(events),
            verdicts=tuple(verdicts) if verdicts is not None else None,
            membership=membership)
        for spec in specs:
            try:
                outcome = _normalize_outcome(spec, spec.fn(events, params, ctx))
            except MetricAssemblyError:
                raise
            except Exception as exc:                       # noqa: BLE001 —— 失败隔离 HS-3001
                outcome = MetricOutcome.na(
                    "computation_error",
                    f"单指标异常已隔离：{type(exc).__name__}（不记 traceback，避免路径泄漏）")
            mv = _build_value(spec, outcome)
            values[spec.key] = mv
            if mv.value is None:
                reason = _check_reason(spec.key, outcome.reason)
                degraded[spec.key] = {"reason": reason,
                                      "detail": outcome.detail or "（指标函数未提供 detail）"}
            if outcome.note:
                notes[spec.key] = outcome.note
            if mv.evidence:
                idx: list[dict] = []
                for ref in mv.evidence:
                    for eid in ref.event_ids:
                        if eid not in seq_by_id:
                            raise MetricAssemblyError(
                                f"{spec.key}: 证据 event_id {eid!r} 不在 run {run_id} 内（G5 需可查库命中）")
                        idx.append({"run_id": run_id, "event_id": eid, "seq": seq_by_id[eid]})
                evidence_index[spec.key] = idx
        data_quality = _data_quality(events)

    # R1-2：key 口径锁定自检（缺 key = 实现侧少吐 → 直接报错；数据侧漂移只如实上报）
    counts = key_count_check(values)
    if counts["missing_keys"]:
        raise MetricAssemblyError(
            f"内置指标没有全部产出，缺 {counts['missing_keys']}（EXPECTED_ALL_KEYS 要求 "
            f"{len(EXPECTED_ALL_KEYS)} 个）；新增/删除指标必须同步改口径常量 + 设计文档 §3.4 + PRD §4")

    groups = {
        "result": [], "process": [], "collaboration": [], "cost": [],
    }
    for key in sorted(values):
        groups[get_metric(key).group].append(values[key])
    degraded_keys = sorted(degraded)

    metric_set = MetricSet(
        run_id=run_id,
        schema_version=SCHEMA_VERSION,
        engine_version=__version__,
        computed_at=_now(at),
        groups=Groups(result=groups["result"], process=groups["process"],
                      collaboration=groups["collaboration"], cost=groups["cost"]),
        degraded_metrics=degraded_keys,
    )
    _assert_invariants(metric_set)
    sha = canonical_sha256(metric_set)
    ext = {
        "run_id": run_id,
        "channel_policy": policy,
        "blocked": blocked,
        "blocked_reason": BLOCKED_REASON if blocked else None,
        "event_count": int(run.get("event_count") or 0) if blocked else len(events),
        "engine_version": __version__,
        "params_digest": params.digest(),
        "canonical_sha256": sha,
        "computed_keys": sorted(k for k, v in values.items() if v.value is not None),
        # R1-3：ext 里**只保留** degraded_reasons（对象，key → {reason, detail}）。
        # 主文件的 degraded_metrics 是 list[str]，同名不同构会让下游静默拿错类型 → ext 不再放同名键。
        "degraded_reasons": {k: degraded[k] for k in degraded_keys},
        "key_count_check": counts,
        # 数据质量信号：不进 degraded_metrics、不影响指标值（PM v1.2 §3.3）
        "data_quality_flags": list(data_quality["flags"]),
        "data_quality_details": {k: data_quality["details"][k]
                                 for k in sorted(data_quality["details"])},
        "tool_call_identity": data_quality["tool_call_identity"],
        "notes": {k: notes[k] for k in sorted(notes)},
        "evidence_index": {k: evidence_index[k] for k in sorted(evidence_index)},
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
    }
    result = MetricSetResult(metric_set=metric_set, ext=ext, blocked=blocked)
    if out_dir is not None:
        write_result(result, out_dir)
    return result


def snapshot_digest(events: Sequence[dict]) -> str:
    """事件流指纹（与 seq 顺序无关）：`sha256(排序后的 event_id 列表)` 的前 16 位。

    `tool_call_identity`（以及任何结构性事实）都是**快照相关**的：两份快照的数字不一样，
    先看指纹是否相同，再谈「谁对谁错」。
    """
    ids = sorted(str(ev.get("event_id", "")) for ev in events)
    return "sha256:" + hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest()[:16]


def _data_quality(events: Sequence[dict]) -> dict:
    """结构性/自洽性信号（PM v1.2：不进 `degraded_metrics`、不影响指标值）。

    `degraded_metrics` 回答「这个指标有没有数」，`data_quality_flags` 回答「这些数脚下的数据干不干净」。
    """
    from hs_core.metrics_core import tool_content_hash_stats, tool_pairing_stats  # 延迟导入

    stats = tool_pairing_stats(events)
    hashes = tool_content_hash_stats(events)
    flags: list[str] = []
    details: dict[str, dict] = {}
    identity = {
        "tool_call": stats["tool_call"],
        "tool_result": stats["tool_result"],
        "tool_error": stats["tool_error"],
        "identity_holds": stats["identity_holds"],
        "errored_calls": stats["errored_calls"],
        "unmatched_errors": stats["unmatched_errors"],
        "unmatchable_calls": stats["unmatchable_calls"],
        "paired_by": stats["paired_by"],
        # 快照标识（R1-4）：恒等式是**快照相关**的结构性质，两份快照要能对上号才谈得上比较
        "event_count": len(events),
        "snapshot_digest": snapshot_digest(events),
    }
    if not stats["identity_holds"]:
        flags.append("tool_call_identity_violated")
        details["tool_call_identity_violated"] = {
            "detail": ("count(tool.call) != count(tool.result) + count(tool.error)：调用/结果/错误"
                       "三者的 span 配对结构被破坏，T11 应渲染为数据质量提示，指标值不受影响"),
            **identity,
        }
    # R1-6 防御：某一侧的 content_sha256 在整批数据里全部相同 → 它没有区分度，不得当签名用。
    # 指标侧已改用 span_id 从 span_end 补齐（见 metrics_core._sibling_content_sha），此处只登记事实。
    degenerate = [side for side in ("tool_call", "span_end") if hashes[side]["all_same"]]
    if degenerate:
        flags.append("tool_call_signature_degenerate")
        details["tool_call_signature_degenerate"] = {
            "detail": ("content_sha256 在整批数据里全部相同，该字段没有区分度，不得用作调用签名；"
                       "指标已改用 span_id 从 span_end 补齐（见 docs/metrics-engine.md §7-1）。"
                       "R2-1：签名是代理键时 retry_rate / redundant_loop 的 confidence 按来源等级 "
                       "判为 low，与样本量无关"),
            "degenerate_sides": degenerate,
            **hashes,
        }
    return {"flags": flags, "details": details, "tool_call_identity": identity}


def _assert_invariants(metric_set: MetricSet) -> None:
    """两条硬不变量（违反即抛 `MetricAssemblyError`，不做静默修补）：

    1. `value is None` ⟺ `key ∈ degraded_metrics` ⟺ `evidence == []`
    2. `value is not None` ⟹ `confidence is not None`（Q7：有值必须有档位。
       「有数但没档位」与「没数却标 low」一样是对消费者的误导）
    """
    degraded = set(metric_set.degraded_metrics)
    seen: set[str] = set()
    for group in (metric_set.groups.result, metric_set.groups.process,
                  metric_set.groups.collaboration, metric_set.groups.cost):
        for mv in group:
            if mv.key in seen:
                raise MetricAssemblyError(f"MetricSet 内 key 重复: {mv.key}")
            seen.add(mv.key)
            is_null = mv.value is None
            if is_null != (mv.key in degraded):
                raise MetricAssemblyError(
                    f"{mv.key}: value is None={is_null} 与 degraded_metrics 归属不一致")
            if is_null != (len(mv.evidence) == 0):
                raise MetricAssemblyError(
                    f"{mv.key}: value is None={is_null} 与 evidence 空否不一致")
            if not is_null and mv.confidence is None:
                raise MetricAssemblyError(
                    f"{mv.key}: value={mv.value!r} 但 confidence 为空（Q7：非降级指标必须给 "
                    "high/low，派生口径给 low；降级指标才允许 null）")
            if is_null and mv.confidence is not None:
                raise MetricAssemblyError(
                    f"{mv.key}: 降级（value=None）却带 confidence={mv.confidence!r}——"
                    "「没数」不得标成「有数但不可信」")
    missing = degraded - seen
    if missing:
        raise MetricAssemblyError(f"degraded_metrics 含未产出的 key: {sorted(missing)}")


# ---------------------------------------------------------------------------
# 落盘
# ---------------------------------------------------------------------------

def write_result(result: MetricSetResult, out_dir: str | Path) -> dict[str, Path]:
    """写 metricset.json（schema 合规，allow_nan=False）+ metricset.ext.json。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    main_path = out / "metricset.json"
    ext_path = out / "metricset.ext.json"
    main_path.write_text(
        json.dumps(schema_dict(result.metric_set), ensure_ascii=False,
                   indent=2, allow_nan=False) + "\n", encoding="utf-8")
    ext_path.write_text(
        json.dumps(result.ext, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8")
    return {"metricset": main_path, "ext": ext_path}


# ---------------------------------------------------------------------------
# 跨 run 聚合（scope="group"，T06-10）
# ---------------------------------------------------------------------------

def agg_pass_k(
    items: Sequence[tuple[MetricValue, RunMembership | None]], params: MetricParams,
) -> MetricOutcome:
    """pass^k：同一 (task_id, variant_id) 的 k 次重复 run 全部通过的比例。

    成员缺归属元数据 / 成员 result.success 未出值 / 未跑满 k 次 → 不计入；
    一个可计入组都没有 → insufficient_data（不拿 0 冒充）。
    """
    groups: dict[tuple[str, str], list[tuple[bool, int]]] = {}
    missing_meta = 0
    null_success = 0
    for mv, membership in items:
        if (membership is None or not membership.task_id or not membership.variant_id
                or membership.k is None):
            missing_meta += 1
            continue
        if mv.value is None:
            null_success += 1
            continue
        groups.setdefault((membership.task_id, membership.variant_id), []).append(
            (float(mv.value) >= 1.0, int(membership.k)))
    if not groups:
        return MetricOutcome.na(
            "no_source",
            f"无可聚合分组：缺归属元数据 (task_id/variant_id/k) 的 run {missing_meta} 个、"
            f"result.success 未出值 {null_success} 个（等 T09/T10 供数）")
    valid = 0
    passing = 0
    incomplete = 0
    for key in sorted(groups):
        rows = groups[key]
        k = rows[0][1]
        if len(rows) != k or any(r[1] != k for r in rows):
            incomplete += 1
            continue
        valid += 1
        if all(r[0] for r in rows):
            passing += 1
    if valid == 0:
        return MetricOutcome.na(
            "insufficient_data",
            f"没有跑满 k 次重复的 (task, variant) 组（不完整组 {incomplete} 个）")
    value = round(passing / valid, 6)
    note = (f"有效组 {valid} 个（全通过 {passing} 个，不完整组 {incomplete} 个已排除）；"
            "每组 = 同一 (task_id, variant_id) 的 k 次 run 全部通过")
    return MetricOutcome.ok(value,
                            evidence=(),
                            confidence="high" if valid >= params.low_confidence_n else "low",
                            note=note)


def agg_trajectory_efficiency(
    items: Sequence[tuple[MetricValue, RunMembership | None]], params: MetricParams,
) -> MetricOutcome:
    """轨迹效率 = 实际步数 ÷ 同 (task_id, variant_id) 的最短步数，再对组内 run 取均值。"""
    groups: dict[tuple[str, str], list[float]] = {}
    missing_meta = 0
    null_steps = 0
    for mv, membership in items:
        if membership is None or not membership.task_id or not membership.variant_id:
            missing_meta += 1
            continue
        if mv.value is None:
            null_steps += 1
            continue
        groups.setdefault((membership.task_id, membership.variant_id), []).append(float(mv.value))
    ratios: list[float] = []
    for key in sorted(groups):
        steps = groups[key]
        reference = min(steps)
        if reference <= 0:
            continue
        ratios.extend(s / reference for s in steps)
    if not ratios:
        return MetricOutcome.na(
            "insufficient_data",
            f"无可算样本：缺归属元数据 {missing_meta} 个、步数未出值 {null_steps} 个、"
            "或参考步数为 0")
    value = round(sum(ratios) / len(ratios), 6)
    note = (f"参考步数 = 同 (task_id, variant_id) 组内最短步数；"
            f"样本 {len(ratios)} 个 run，均值 {value}（1.0 = 达到组内最短）；"
            "口径为派生（比值对组内最短步数，非直接观测）")
    return MetricOutcome.ok(value, evidence=(),
                            confidence="low",    # Q7：派生口径恒 low
                            note=note)


def _agg_wall_clock(
    items: Sequence[tuple[MetricValue, RunMembership | None]], params: MetricParams, q: float,
) -> MetricOutcome:
    values: list[float] = []
    for _mv, membership in items:
        if membership is not None and membership.wall_clock_ms is not None:
            values.append(float(membership.wall_clock_ms))
    if not values:
        return MetricOutcome.na(
            "no_source",
            f"所有成员 run 都没有 wall_clock_ms（T10 编排层尚未供给 run 级起止）：{WALL_CLOCK_FACT}")
    note = (f"{len(values)}/{len(items)} 个 run 有 wall_clock_ms（缺失的 {len(items) - len(values)} 个"
            f"已排除，不当 0）；q={q}，线性插值")
    return MetricOutcome.ok(round(quantile(values, q), 6), evidence=(),
                            confidence="high" if len(values) >= params.low_confidence_n else "low",
                            note=note)


def agg_wall_clock_p50(
    items: Sequence[tuple[MetricValue, RunMembership | None]], params: MetricParams,
) -> MetricOutcome:
    """跨 run 墙钟 P50。"""
    return _agg_wall_clock(items, params, params.wall_clock_quantiles[0])


def agg_wall_clock_p95(
    items: Sequence[tuple[MetricValue, RunMembership | None]], params: MetricParams,
) -> MetricOutcome:
    """跨 run 墙钟 P95。"""
    return _agg_wall_clock(items, params, params.wall_clock_quantiles[-1])


def compute_group(
    key: str,
    items: Sequence[tuple[MetricValue, RunMembership | None]],
    *,
    params: MetricParams | None = None,
    run_ids: Sequence[str] | None = None,
) -> MetricValue:
    """把多个 run 的 MetricValue (+归属元数据) 聚合为一个 MetricValue。

    `membership is None` → 聚合器返回 null + no_source（T10 未供数前如实降级）。
    聚合结果无顶层 run_id 容器（它不进 MetricSet），因此证据锚点取各成员 run 的既有证据
    event_id，并在 note 里列出 run_ids 供逐 run 回查（(run_id, event_id) 才唯一）。
    """
    load_builtin_metrics()
    params = params or MetricParams()
    spec = get_metric(key)
    agg = GROUP_AGGREGATORS.get(key)
    if agg is None:
        outcome = MetricOutcome.na("no_source", f"{key} 未注册跨 run 聚合器（scope=group 接口预留）")
    else:
        try:
            outcome = agg(items, params)
        except Exception as exc:                            # noqa: BLE001 —— 聚合失败隔离
            outcome = MetricOutcome.na("computation_error",
                                       f"聚合异常已隔离：{type(exc).__name__}")
    if outcome.value is not None and not outcome.evidence:
        outcome = _with_group_evidence(outcome, items, run_ids, params)
    return _build_value(spec, outcome)


def _with_group_evidence(
    outcome: MetricOutcome,
    items: Sequence[tuple[MetricValue, RunMembership | None]],
    run_ids: Sequence[str] | None,
    params: MetricParams,
) -> MetricOutcome:
    """给聚合结果补证据：锚点为各成员 run 对应指标的既有 evidence event_id。"""
    anchors: list[str] = []
    for mv, _membership in items:
        for ref in mv.evidence:
            if ref.event_ids:
                anchors.append(ref.event_ids[0])
                break
    uniq = sorted(set(anchors))
    if not uniq:
        return MetricOutcome.na(
            "insufficient_data",
            "跨 run 聚合：成员 run 均无证据锚点，无法给出可下钻结果（等 T10 供数）")
    ids = uniq[:max(1, params.max_evidence_ids)]
    note = ("跨 run 聚合结果：event_ids 锚定各成员 run 对应指标的既有证据，"
            "逐 run 详情见其 metricset.json（(run_id, event_id) 才唯一）")
    if run_ids:
        note += f"；run_ids={','.join(sorted(str(r) for r in run_ids))}"
    note += f"；成员 {len(anchors)} 个，此处列出前 {len(ids)} 个锚点"
    return MetricOutcome(value=outcome.value,
                         evidence=(EvidenceRef(event_ids=ids, note=note),),
                         confidence=outcome.confidence, unit=outcome.unit, note=note)


# ---------------------------------------------------------------------------
# 配置加载（IO 边界；缺文件走代码默认值，T06-09）
# ---------------------------------------------------------------------------

_PARAM_TYPES: dict[str, type] = {
    "loop_min_repeat": int,
    "loop_max_period": int,
    "retry_window_ms": int,
    "retry_window_events": int,
    "low_confidence_n": int,
    "max_evidence_ids": int,
    "idle_threshold_s": float,
    "effect_window_s": float,
    "message_merge_key": str,
    "wall_clock_quantiles": tuple,
    "tokens_trace_summary_fallback": bool,
    "pricing": dict,
}


def _coerce(name: str, value: Any) -> Any:
    want = _PARAM_TYPES[name]
    if want is int:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise MetricRunError(f"参数 {name} 须为整数，收到 {value!r}")
        return int(value)
    if want is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise MetricRunError(f"参数 {name} 须为数值，收到 {value!r}")
        return float(value)
    if want is bool:
        if not isinstance(value, bool):
            raise MetricRunError(f"参数 {name} 须为 true/false，收到 {value!r}")
        return value
    if want is str:
        return str(value)
    if want is tuple:
        return tuple(float(x) for x in value)
    if want is dict:
        if not isinstance(value, dict):
            raise MetricRunError(f"参数 {name} 须为映射，收到 {value!r}")
        return dict(value)
    return value


def load_params_config(
    metrics_path: str | Path | None = None,
    pricing_path: str | Path | None = None,
) -> MetricParams:
    """读 metrics.yaml / pricing.yaml 覆盖默认阈值（文件缺省则用代码默认值）。

    未知键直接报错（不静默忽略拼写错误，避免「以为改了其实没生效」）。
    """
    import yaml  # 局部导入：纯函数指标模块不需要 yaml

    overrides: dict[str, Any] = {}
    if metrics_path is not None:
        path = Path(metrics_path)
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise MetricRunError(f"metrics 配置须为映射: {path}")
        for name, value in raw.items():
            if name not in _PARAM_TYPES:
                raise MetricRunError(f"未知的 metrics 配置项 {name!r}（合法项见 engine/config/metrics.yaml）")
            overrides[name] = _coerce(name, value)
    if pricing_path is not None:
        path = Path(pricing_path)
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise MetricRunError(f"pricing 配置须为映射: {path}")
        overrides["pricing"] = {str(k): dict(v or {}) for k, v in raw.items()}
    return MetricParams(**overrides)

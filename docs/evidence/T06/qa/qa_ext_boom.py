"""QA 独立复核：G8 失败隔离用的必抛异常探针指标。

注册后立即抛异常，用于验证「单指标异常不影响其余指标出值」。
"""

from hs_core.metrics import MetricContext, MetricOutcome, MetricParams, register_metric


@register_metric(
    key="process.qa_boom_metric", group="process", label_zh="QA 异常探针（必抛）",
    unit="count", direction=False, scope="run", event_types=())
def qa_boom_metric(events, params: MetricParams, ctx: MetricContext) -> MetricOutcome:
    """每次调用都抛异常，专门用于验证失败隔离。"""
    raise RuntimeError("QA 注入的必抛异常（用于 G8 失败隔离验证）")

"""QA 独立复核：G1/G8 用的第三方指标探针模块（注册器可插拔实测）。

用法：PYTHONPATH=<本目录> hs metrics run --extra-module qa_ext_probe ...
"""

from hs_core.metrics import MetricContext, MetricOutcome, MetricParams, register_metric
from hs_core.models import EvidenceRef


@register_metric(
    key="process.qa_probe_metric", group="process", label_zh="QA 探针（第三方注册）",
    unit="count", direction=False, scope="run", event_types=("step.start",))
def qa_probe_metric(events, params: MetricParams, ctx: MetricContext) -> MetricOutcome:
    """第三方模块注册的最小可用指标：数 step.start。"""
    steps = [ev for ev in events if ev.get("type") == "step.start"]
    if not steps:
        return MetricOutcome.na("no_source", "探针：本 run 无 step.start")
    return MetricOutcome.ok(
        float(len(steps)),
        evidence=(EvidenceRef(event_ids=[str(steps[0]["event_id"])],
                              note="QA 第三方探针证据"),),
        note="第三方模块注册的探针指标",
        confidence="low",
    )

"""G1 门禁探针：第三方模块用公开装饰器注册一个指标（T06-01 / G1 的可复跑取证）。

用法（仓库根）：
  PYTHONPATH=docs/evidence/T06 .venv/Scripts/python.exe -m hs_core.cli metrics list
  PYTHONPATH=docs/evidence/T06 .venv/Scripts/python.exe -m hs_core.cli metrics run \\
      --run-id <run> --db <db> --extra-module ext_probe --out-dir <dir>

验证点：① `hs metrics list` 能看到 process.probe_metric；② `compute_run` 会真的执行它；
③ 本文件不改 `hs_core/metrics.py` 任何一行（注册器即扩展点）。
"""

from __future__ import annotations

from hs_core.metrics import MetricOutcome, evidence_from, register_metric


@register_metric(
    key="process.probe_metric",
    group="process",
    label_zh="G1 探针指标（step.start 计数）",
    unit="count",
    direction=None,
    scope="run",
    event_types=("step.start",),
    version="0.0.1",
)
def probe_metric(events, params, ctx) -> MetricOutcome:
    """最小可验证指标：数 step.start，出值即可下钻（G5 也要成立）。"""
    picked = [ev for ev in events if ev.get("type") == "step.start"]
    if not picked:
        return MetricOutcome.na("no_source", "本 run 无 step.start（探针指标）")
    return MetricOutcome.ok(float(len(picked)),
                            evidence=evidence_from(picked, params=params,
                                                   note="G1 探针：step.start 计数"),
                            # Q7 不变量：非降级指标必须给档位（装配层会硬断言，见 metrics._assert_invariants）
                            confidence="low",
                            note="ext_probe.process.probe_metric")

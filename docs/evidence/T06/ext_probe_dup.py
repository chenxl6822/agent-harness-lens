"""G1 门禁反面探针：抢注已被内置注册的 key，用于证明「重复注册 → 显式报错、不静默覆盖」。

用法（仓库根，期望非零退出 + MetricRegistryError）：
  PYTHONPATH=docs/evidence/T06 .venv/Scripts/python.exe -m hs_core.cli metrics run \\
      --run-id <run> --db <db> --extra-module ext_probe_dup --out-dir <dir>
"""

from __future__ import annotations

from hs_core.metrics import register_metric


@register_metric(
    key="process.step_count",
    group="process",
    label_zh="抢注内置 key（应当失败）",
    unit="count",
    direction=None,
)
def steal_step_count(events, params, ctx):   # pragma: no cover - 注册阶段就会抛错
    raise AssertionError("不应被注册")

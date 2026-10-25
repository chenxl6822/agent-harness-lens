"""黄金输入（4 份 example 必须过各自 schema）+ 反例必败 + 契约一致性断言 + config 安全默认。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from hs_core import schema as hs_schema
from hs_core.config import load_config

REPO = hs_schema.REPO_ROOT
EXAMPLES = REPO / "packages" / "schema" / "examples"

# 任务卡 §4.1 的 26 个事件类型（逐字一致，缺一即返工）
EXPECTED_EVENT_TYPES = [
    "run.start",
    "run.end",
    "session.start",
    "session.end",
    "agent.spawn",
    "agent.stop",
    "turn.start",
    "turn.end",
    "step.start",
    "step.end",
    "llm.request",
    "llm.response",
    "tool.call",
    "tool.result",
    "tool.error",
    "task.create",
    "task.claim",
    "task.update",
    "task.complete",
    "message.send",
    "message.recv",
    "artifact.write",
    "artifact.ref",
    "context.compact",
    "error",
    "metric.custom",
]


def _load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("kind", "filename"),
    [
        ("trace-event", "trace-event.sample.json"),
        ("metric-set", "metric-set.sample.json"),
        ("taskset", "taskset.sample.yaml"),
        ("capability", "capability.sample.json"),
    ],
)
def test_example_valid(kind: str, filename: str) -> None:
    path = EXAMPLES / filename
    obj = _load_json(path) if path.suffix == ".json" else yaml.safe_load(path.read_text("utf-8"))
    assert hs_schema.validate(kind, obj) == []


def test_broken_trace_event_fails() -> None:
    """故意删掉必填字段 event_id，必须校验失败。"""
    obj = _load_json(EXAMPLES / "trace-event.sample.json")
    del obj["event_id"]
    errors = hs_schema.validate("trace-event", obj)
    assert errors, "缺少 event_id 必须报错"


def test_broken_metric_set_fails() -> None:
    """evidence 里 event_ids 置空，必须校验失败。"""
    obj = _load_json(EXAMPLES / "metric-set.sample.json")
    obj["groups"]["collaboration"][0]["evidence"][0]["event_ids"] = []
    errors = hs_schema.validate("metric-set", obj)
    assert errors


def test_event_type_enum_exact() -> None:
    """契约一致性：type 枚举数 == 26，且与任务卡 §4.1 列表逐字相同。"""
    schema = hs_schema.load_schema("trace-event")
    enum = schema["$defs"]["EventType"]["enum"]
    assert len(enum) == 26
    assert enum == EXPECTED_EVENT_TYPES


def test_load_ts_types_version_matches() -> None:
    assert hs_schema.load_ts_types_version() == "1.0.0"


def test_config_safe_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "HS_HOME",
        "HS_SPOOL_DIR",
        "HS_DB_PATH",
        "HS_CONTENT_CAPTURE",
        "HS_CLOUD_JUDGE",
        "HS_DEBUG",
        "HS_DEFAULT_BUDGET_CNY",
    ):
        monkeypatch.delenv(name, raising=False)
    cfg = load_config()
    assert cfg.content_capture is False, "HS_CONTENT_CAPTURE 默认必须 off"
    assert cfg.cloud_judge is False, "HS_CLOUD_JUDGE 默认必须 off"


def test_config_handles_nan(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HS_DEFAULT_BUDGET_CNY", "nan")
    monkeypatch.setenv("HS_CONTENT_CAPTURE", "on")
    cfg = load_config()
    assert cfg.default_budget_cny == 0.5, "NaN 预算必须回落到安全默认"
    assert cfg.content_capture is True, "显式开启才允许内容采集"

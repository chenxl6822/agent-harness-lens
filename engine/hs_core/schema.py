"""JSON Schema 加载与校验。

契约唯一真源：`<仓库根>/packages/schema/*.schema.json`。
所有路径相对本文件位置解析（parents[2] = 仓库根），禁止硬编码绝对路径。
"""

from __future__ import annotations

import json
import re
from functools import cache
from pathlib import Path

from jsonschema import Draft202012Validator

# hs_core/schema.py -> hs_core -> engine -> 仓库根
REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = REPO_ROOT / "packages" / "schema"

SCHEMA_FILES: dict[str, str] = {
    "trace-event": "trace-event.schema.json",
    "metric-set": "metric-set.schema.json",
    "taskset": "taskset.schema.json",
    "capability": "capability.schema.json",
}

SCHEMA_VERSION = "1.0.0"


class UnknownKindError(ValueError):
    """未知的 schema kind。"""


@cache
def load_schema(kind: str) -> dict:
    """按 kind 加载 schema 文件并返回 dict。"""
    if kind not in SCHEMA_FILES:
        raise UnknownKindError(
            f"unknown kind {kind!r}, expected one of {sorted(SCHEMA_FILES)}"
        )
    path = SCHEMA_DIR / SCHEMA_FILES[kind]
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def validate(kind: str, obj: object) -> list[str]:
    """校验 obj 是否符合 kind 对应的 schema。返回错误列表；空列表 = 通过。"""
    schema = load_schema(kind)
    validator = Draft202012Validator(schema)
    errors: list[str] = []
    for err in sorted(validator.iter_errors(obj), key=lambda e: list(e.absolute_path)):
        loc = "/".join(str(p) for p in err.absolute_path) or "<root>"
        errors.append(f"{loc}: {err.message}")
    return errors


def load_ts_types_version() -> str:
    """从 packages/schema/index.ts 读取 SCHEMA_VERSION 常量（跨语言一致性锚点）。"""
    index_ts = SCHEMA_DIR / "index.ts"
    text = index_ts.read_text(encoding="utf-8")
    m = re.search(r"SCHEMA_VERSION\s*=\s*'([0-9]+\.[0-9]+\.[0-9]+)'\s*as const", text)
    if m is None:
        raise RuntimeError(f"SCHEMA_VERSION not found in {index_ts}")
    return m.group(1)

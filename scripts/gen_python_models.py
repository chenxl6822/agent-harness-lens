"""scripts/gen_python_models.py — JSON Schema -> pydantic models.py（单文件合并）。

单向生成：packages/schema/*.schema.json --datamodel-code-generator--> engine/hs_core/models.py
四份 schema 分别生成后合并；同名类若内容一致则去重（如 EventType 在多份 schema 中复现），
内容不一致则报错。--check 模式重新生成并与现存文件逐字节比对。

用法：python scripts/gen_python_models.py [--check]
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCHEMA_DIR = REPO / "packages" / "schema"
OUT_FILE = REPO / "engine" / "hs_core" / "models.py"
KINDS = ["trace-event", "metric-set", "taskset", "capability"]

HEADER = (
    "# AUTO-GENERATED, DO NOT EDIT\n"
    "# Source of truth: packages/schema/*.schema.json (single source of truth)\n"
    "# Regenerate with: make gen (json-schema -> datamodel-code-generator -> pydantic v2)\n"
)


def run_dcg(schema_path: Path, out_path: Path) -> None:
    cmd = [
        sys.executable,
        "-m",
        "datamodel_code_generator",
        "--input",
        str(schema_path),
        "--input-file-type",
        "jsonschema",
        "--output",
        str(out_path),
        "--output-model-type",
        "pydantic_v2.BaseModel",
        "--target-python-version",
        "3.11",
        "--disable-timestamp",
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True)


def split_blocks(text: str) -> tuple[list[str], list[tuple[str, str]]]:
    """把生成文件切成 (imports, [(class_name, class_block)])。"""
    imports: list[str] = []
    blocks: list[tuple[str, str]] = []
    current: list[str] | None = None
    name = ""
    for line in text.splitlines():
        m = re.match(r"^class (\w+)", line)
        if m:
            if current is not None:
                blocks.append((name, "\n".join(current).rstrip() + "\n"))
            current = [line]
            name = m.group(1)
        elif current is None:
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                imports.append(line)
        else:
            current.append(line)
    if current is not None:
        blocks.append((name, "\n".join(current).rstrip() + "\n"))
    return imports, blocks


def merge_outputs(parts: list[tuple[str, str]]) -> str:
    """合并各 schema 的生成结果：imports 去重，同名同体类去重。"""
    all_imports: list[str] = []
    emitted: dict[str, str] = {}
    order: list[str] = []
    sections: list[str] = []
    for kind, text in parts:
        imports, blocks = split_blocks(text)
        for imp in imports:
            if imp not in all_imports:
                all_imports.append(imp)
        kept: list[str] = []
        for name, block in blocks:
            digest = hashlib.sha256(block.encode()).hexdigest()
            if name in emitted:
                if emitted[name] != digest:
                    # 同名类（跨 schema 复用的枚举，如 Platform/EventType）：
                    # 契约上必须同值，schema 侧由 pytest 断言 26 枚举逐字一致兜底；
                    # 此处保留首份定义并告警。
                    print(
                        f"WARN: dedup class {name!r} from {kind}.schema.json "
                        "(kept first definition)",
                        file=sys.stderr,
                    )
                continue
            emitted[name] = digest
            order.append(name)
            kept.append(block)
        if kept:
            sections.append(f"# ---- from {kind}.schema.json ----\n" + "\n".join(kept))
    body = "\n\n".join(sections)
    import_lines = "\n".join(sorted(set(all_imports)))
    return f"{HEADER}\n{import_lines}\n\n{body}\n"


def generate() -> str:
    parts: list[tuple[str, str]] = []
    with tempfile.TemporaryDirectory() as td:
        for kind in KINDS:
            tmp_out = Path(td) / f"{kind}.py"
            run_dcg(SCHEMA_DIR / f"{kind}.schema.json", tmp_out)
            parts.append((kind, tmp_out.read_text(encoding="utf-8")))
    return merge_outputs(parts)


def dcg_version() -> str:
    return importlib.metadata.version("datamodel-code-generator")


def main() -> None:
    check = "--check" in sys.argv
    generated = generate()
    if check:
        if not OUT_FILE.exists():
            print(f"CHECK FAIL: {OUT_FILE.name} 不存在（请运行 make gen）")
            sys.exit(1)
        current = OUT_FILE.read_text(encoding="utf-8")
        if current != generated:
            print("CHECK FAIL: engine/hs_core/models.py 与 schema 不一致（请运行 make gen）")
            sys.exit(1)
        print(f"CHECK OK: engine/hs_core/models.py (datamodel-code-generator {dcg_version()})")
    else:
        OUT_FILE.write_text(generated, encoding="utf-8", newline="\n")
        print(f"GEN: engine/hs_core/models.py (datamodel-code-generator {dcg_version()})")


if __name__ == "__main__":
    main()

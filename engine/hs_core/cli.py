"""hs CLI（占位骨架）。本卡只提供 version / schema list / schema check；
run / eval 等业务命令属于后续任务卡（T05-T11），禁止在本卡实现。"""

from __future__ import annotations

import json
from pathlib import Path

import typer
import yaml

from hs_core import __version__
from hs_core import schema as hs_schema

app = typer.Typer(
    name="hs",
    help="harness_scope CLI（智能体过程评测与优化工作台）",
    no_args_is_help=True,
)
schema_app = typer.Typer(help="schema 契约相关子命令", no_args_is_help=True)
app.add_typer(schema_app, name="schema")


@app.command()
def version() -> None:
    """打印引擎与契约版本。"""
    engine = __version__
    contract = hs_schema.load_ts_types_version()
    typer.echo(f"hs (harness-scope-engine) {engine}")
    typer.echo(f"schema_version {contract}")
    raise typer.Exit(code=0)


@schema_app.command("list")
def schema_list() -> None:
    """列出四份契约 schema。"""
    for kind in sorted(hs_schema.SCHEMA_FILES):
        schema_id = f"https://harness-scope.dev/schema/{kind}.json"
        typer.echo(f"{kind}\t{schema_id}")
    raise typer.Exit(code=0)


@schema_app.command("check")
def schema_check(
    file: Path = typer.Argument(  # noqa: B008
        ..., exists=True, readable=True, help="待校验的 JSON/YAML 文件"
    ),
    kind: str = typer.Option(
        ...,
        "--kind",
        help="schema 类型: trace-event|metric-set|taskset|capability",
    ),
) -> None:
    """用指定 schema 校验一个 JSON/YAML 文件。通过退出码 0，失败退出码 1。"""
    if file.suffix.lower() in {".yaml", ".yml"}:
        try:
            obj = yaml.safe_load(file.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            typer.echo(f"ERROR: invalid YAML in {file}: {exc}", err=True)
            raise typer.Exit(code=1) from exc
    else:
        try:
            obj = json.loads(file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            typer.echo(f"ERROR: invalid JSON in {file}: {exc}", err=True)
            raise typer.Exit(code=1) from exc

    errors = hs_schema.validate(kind, obj)
    if errors:
        typer.echo(f"FAIL {kind}: {file}", err=True)
        for err in errors:
            typer.echo(f"  - {err}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"OK {kind}: {file}")
    raise typer.Exit(code=0)


def main() -> None:
    app()


if __name__ == "__main__":
    main()

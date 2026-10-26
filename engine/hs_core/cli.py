"""hs CLI。T05b 增补 ingest / db verify；T06 增补 metrics run / metrics list。

退出码约定：成功 0；schema/策略/装配违规 2；IO 错误 1。
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
import yaml

from hs_core import __version__
from hs_core import schema as hs_schema
from hs_core.collector import collect
from hs_core.store import PolicyError, Store, StoreError, digest_files

app = typer.Typer(
    name="hs",
    help="harness_scope CLI（智能体过程评测与优化工作台）",
    no_args_is_help=True,
)
schema_app = typer.Typer(help="schema 契约相关子命令", no_args_is_help=True)
app.add_typer(schema_app, name="schema")
db_app = typer.Typer(help="SQLite 存储层子命令（T05b）", no_args_is_help=True)
app.add_typer(db_app, name="db")
metrics_app = typer.Typer(help="指标计算子命令（T06）", no_args_is_help=True)
app.add_typer(metrics_app, name="metrics")

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"


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


def _expand_source_files(path: Path | None, kind: str) -> list[Path]:
    """展开摄入源文件（供 source_digest 计算；与 collect() 实际读取范围一致）。"""
    if path is None:
        return []
    if path.is_file():
        return [path]
    if not path.is_dir():
        return []
    if kind == "hook":  # spool 目录：*.jsonl（与 load_hook_spool 一致）
        return sorted(path.glob("*.jsonl"))
    if kind == "codebuddy":  # ~/.codebuddy：仅 collect() 实际读取的三个子树
        return sorted(p for sub in ("traces", "projects", "tasks")
                      for p in (path / sub).rglob("*") if p.is_file()) if path.exists() else []
    return [path]


@app.command()
def ingest(
    run_id: str = typer.Option(..., "--run-id", help="评测执行单元 ID"),
    db: Path = typer.Option(..., "--db", help="SQLite 库文件路径"),
    dsh_events: Path | None = typer.Option(None, "--dsh-events", help="dsh 事件 NDJSON（T04c 信封格式）"),
    codebuddy_home: Path | None = typer.Option(None, "--codebuddy-home", help="~/.codebuddy 根（通道 A/B/C）"),
    hook_spool: Path | None = typer.Option(None, "--hook-spool", help="hook spool 目录或文件"),
    channel_policy: str = typer.Option("primary_only", "--channel-policy",
                                       help="通道归并策略：primary_only（默认）| merge"),
    allow_merge: bool = typer.Option(False, "--allow-merge",
                                     help="显式放行 merge 流入库（默认拒绝，仅测绘用）"),
    out_dir: Path | None = typer.Option(None, "--out-dir",
                                        help="collect() NDJSON 输出目录（默认 runs/<run_id>，runs/ 已 gitignore）"),
    scrub_home: str | None = typer.Option(None, "--scrub-home", help="脱敏替换的 home 前缀（默认本机 home）"),
) -> None:
    """摄入多源 → 统一 TraceEvent → 幂等写入 SQLite（复用 T05a collect()，不复制摄入逻辑）。"""
    if dsh_events is None and codebuddy_home is None and hook_spool is None:
        typer.echo("ERROR: 至少提供一个摄入源（--dsh-events / --codebuddy-home / --hook-spool）", err=True)
        raise typer.Exit(code=2)
    out = out_dir or Path("runs") / run_id
    try:
        result = collect(
            run_id=run_id, hook_spool=hook_spool, codebuddy_home=codebuddy_home,
            dsh_events=dsh_events, out_dir=out, scrub_home=scrub_home,
            channel_policy=channel_policy)
        sources = (_expand_source_files(dsh_events, "dsh")
                   + _expand_source_files(codebuddy_home, "codebuddy")
                   + _expand_source_files(hook_spool, "hook"))
        with Store(db) as store:
            with (out / "events.normalized.ndjson").open("r", encoding="utf-8") as fh:
                events = (json.loads(line) for line in fh if line.strip())
                ing = store.ingest(run_id, events, channel_policy=channel_policy,
                                   allow_merge=allow_merge,
                                   source_digest=digest_files(sources))
            summary = {
                "run_id": run_id, "db": str(db), "channel_policy": channel_policy,
                "lines_in": result["lines_in"], "units_expanded": result["units_expanded"],
                "mapped": result["mapped"], "dropped": result["dropped"],
                **ing.as_dict(), "events_in_db": store.count_events(run_id),
            }
        typer.echo(json.dumps(summary, ensure_ascii=False, indent=2))
        raise typer.Exit(code=0)
    except PolicyError as exc:
        typer.echo(f"POLICY VIOLATION: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    except StoreError as exc:
        typer.echo(f"SCHEMA VIOLATION: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    except OSError as exc:
        typer.echo(f"IO ERROR: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@db_app.command("verify")
def db_verify(
    run_id: str = typer.Option(..., "--run-id", help="待自检的 run"),
    db: Path = typer.Option(..., "--db", help="SQLite 库文件路径"),
    ndjson: Path | None = typer.Option(None, "--ndjson",
                                       help="对照的 NDJSON 文件（可选，核对行数一致性）"),
) -> None:
    """E5 数据一致性自检：行数 / schema 校验 / seq 连续 / 复合主键无重复。"""
    from jsonschema import Draft202012Validator

    from hs_core.models import TraceEvent

    try:
        with Store(db) as store:
            report = store.verify_run(
                run_id, ndjson=ndjson,
                schema_validator=Draft202012Validator(hs_schema.load_schema("trace-event")),
                model=TraceEvent)
    except OSError as exc:
        typer.echo(f"IO ERROR: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    report["errors"] = report["errors"][:10]
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))
    raise typer.Exit(code=0 if report["ok"] else 2)


# ---------------------------------------------------------------------------
# T06：指标计算
# ---------------------------------------------------------------------------

@metrics_app.command("run")
def metrics_run(
    run_id: str = typer.Option(..., "--run-id", help="待计算指标的 run"),
    db: Path = typer.Option(..., "--db", help="SQLite 库文件路径"),
    out_dir: Path | None = typer.Option(
        None, "--out-dir", help="产物目录（默认 runs/<run_id>/metrics，runs/ 已 gitignore）"),
    params: Path | None = typer.Option(
        None, "--params", help="阈值配置 YAML（默认 engine/config/metrics.yaml，缺失走代码默认值）"),
    pricing: Path | None = typer.Option(
        None, "--pricing", help="单价表 YAML（默认 engine/config/pricing.yaml）"),
    at: str | None = typer.Option(None, "--at", help="冻结 computed_at（ISO-8601，用于逐字节复现）"),
    extra_module: list[str] = typer.Option(
        None, "--extra-module", help="额外指标模块（可重复，用于第三方指标注册）"),
    pretty: bool = typer.Option(False, "--pretty", help="摘要以缩进 JSON 输出"),
) -> None:
    """计算单个 run 的 MetricSet，写 metricset.json + metricset.ext.json。"""
    from hs_core.metrics import (
        MetricError,
        compute_run,
        load_params_config,
    )

    out = out_dir or Path("runs") / run_id / "metrics"
    params_path = params if params is not None else (CONFIG_DIR / "metrics.yaml")
    pricing_path = pricing if pricing is not None else (CONFIG_DIR / "pricing.yaml")
    # 显式指定的配置文件不存在 → 报错（不静默退回代码默认值，否则「以为改了阈值其实没改」）
    for label, path, explicit in (("--params", params_path, params is not None),
                                  ("--pricing", pricing_path, pricing is not None)):
        if explicit and not path.exists():
            typer.echo(f"IO ERROR: {label} 指向的文件不存在: {path}", err=True)
            raise typer.Exit(code=1)
    try:
        metric_params = load_params_config(
            params_path if params_path.exists() else None,
            pricing_path if pricing_path.exists() else None)
        with Store(db) as store:
            result = compute_run(store, run_id, params=metric_params,
                                 extra_modules=extra_module or (), at=at, out_dir=out)
    except OSError as exc:
        typer.echo(f"IO ERROR: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    except MetricError as exc:
        typer.echo(f"METRICS ERROR: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    summary = {**result.summary(), "out_dir": str(out),
               "computed_keys_n": len(result.ext["computed_keys"]),
               "degraded_keys_n": len(result.metric_set.degraded_metrics),
               "blocked_reason": result.ext["blocked_reason"]}
    if result.blocked:
        typer.echo(f"POLICY BLOCKED: {result.ext['blocked_reason']}", err=True)
        typer.echo(json.dumps(summary, ensure_ascii=False, indent=2))
        raise typer.Exit(code=2)
    typer.echo(json.dumps(summary, ensure_ascii=False, indent=2 if pretty else None))
    raise typer.Exit(code=0)


@metrics_app.command("list")
def metrics_list(
    group: str | None = typer.Option(None, "--group", help="按组过滤：result|process|collab|cost"),
    scope: str | None = typer.Option(None, "--scope", help="按作用域过滤：run|group"),
) -> None:
    """列出已注册指标（key / group / unit / direction / scope）。"""
    from hs_core.metrics import list_metrics, load_builtin_metrics

    load_builtin_metrics()
    rows = [("KEY", "GROUP", "UNIT", "DIRECTION", "SCOPE", "VERSION")]
    for spec in list_metrics(group=group, scope=scope):
        direction = "-" if spec.direction is None else ("up" if spec.direction else "down")
        rows.append((spec.key, spec.group, spec.unit or "-", direction, spec.scope, spec.version))
    widths = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
    for row in rows:
        typer.echo("  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    raise typer.Exit(code=0)


def main() -> None:
    app()

if __name__ == "__main__":
    main()

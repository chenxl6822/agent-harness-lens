#!/usr/bin/env python
"""T06 门禁自检：对**真实库**跑一遍可复核的 G1–G15，输出结构化报告（证据归档用）。

用法（仓库根）：
  .venv/Scripts/python.exe scripts/t06_selfcheck.py --db runs/t06/t06.db \\
      --run-id t06-codebuddy-primary --out .tmp-t06

报告为 JSON（stdout），每条门禁都带 `ok` / 实测数字 / 复跑命令。所有路径按仓库根相对化，
本机 home 前缀会被脱敏，可直接贴进 docs/evidence/。

设计原则：**只报实测值，不为了好看放宽口径**。任何拿不到的项写 null + 原因。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "engine"))

from hs_core import schema as hs_schema
from hs_core.metrics import (
    DEGRADE_REASONS,
    REGISTRY,
    MetricParams,
    MetricRegistryError,
    canonical_json,
    clear_registry,
    compute_run,
    list_metrics,
    load_builtin_metrics,
    load_params_config,
)
from hs_core.store import Store
from jsonschema import Draft202012Validator

HOME = os.path.expanduser("~")
PY = sys.executable


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _scrub(text: str) -> str:
    """脱敏：本机 home 前缀 / 用户名一律替换（隐私红线，证据也要能进仓库）。"""
    user = Path(HOME).name
    escaped = json.dumps(HOME)[1:-1]           # JSON 里的 "C:\\Users\\x" 转义形态
    out = text.replace(HOME, "<HOME>").replace(escaped, "<HOME>")
    out = out.replace(HOME.replace("\\", "/"), "<HOME>")
    out = re.sub(r"[A-Za-z]:[\\/]{1,2}Users[\\/]{1,2}" + re.escape(user), "<HOME>", out)
    if user and len(user) > 2:
        out = out.replace(user, "<USER>")
    return out


def _rel(path: Path) -> str:
    try:
        return _scrub(str(path.resolve().relative_to(REPO)))
    except ValueError:
        return _scrub(str(path))


def _run_cli(args: list[str], extra_path: Path | None = None) -> tuple[int, str]:
    """在子进程里跑 CLI，拿真实退出码（typer 的 Exit 在进程内不便断言）。"""
    paths = [str(REPO / "engine")]
    if extra_path is not None:
        paths.insert(0, str(extra_path))
    if os.environ.get("PYTHONPATH"):
        paths.append(os.environ["PYTHONPATH"])
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(paths))
    proc = subprocess.run([PY, "-m", "hs_core.cli", *args], capture_output=True,
                          text=True, cwd=str(REPO), env=env, timeout=300, check=False)
    return proc.returncode, _scrub((proc.stdout or "") + (proc.stderr or ""))


def _cli_metrics_run(db: Path, run_id: str, out: Path, *, at: str | None = None,
                     extra_modules: list[str] | None = None,
                     extra_path: Path | None = None) -> tuple[int, str]:
    args = ["metrics", "run", "--run-id", run_id, "--db", str(db), "--out-dir", str(out)]
    if at:
        args += ["--at", at]
    for mod in extra_modules or []:
        args += ["--extra-module", mod]
    return _run_cli(args, extra_path=extra_path)


# ---------------------------------------------------------------------------
# G1 注册器可插拔
# ---------------------------------------------------------------------------

def gate_g1(tmp: Path, db: Path, run_id: str) -> dict:
    clear_registry()
    modules = load_builtin_metrics()
    builtin_keys = [s.key for s in list_metrics()]
    extra = REPO / "docs" / "evidence" / "T06" / "ext_probe.py"
    detail: dict = {
        "builtin_modules": modules,
        "builtin_keys_n": len(builtin_keys),
        "builtin_keys": builtin_keys,
        "collab_keys": [k for k in builtin_keys if k.startswith("collab.")],
    }
    # 第三方模块注册 → 能被 list 出来
    if extra.exists():
        sys.path.insert(0, str(extra.parent))
        try:
            load_builtin_metrics([extra.stem])
            listed = [s.key for s in list_metrics()]
            detail["extra_module"] = _rel(extra)
            detail["extra_key_visible"] = "process.probe_metric" in listed
            code, out = _cli_metrics_run(db, run_id, tmp / "g1",
                                         extra_modules=[extra.stem],
                                         extra_path=extra.parent)
            detail["cli_extra_module_exit"] = code
            detail["cli_lists_probe_key"] = "process.probe_metric" in out
        finally:
            sys.path.remove(str(extra.parent))
            clear_registry()
            load_builtin_metrics()
    else:
        detail["extra_module"] = None
        detail["extra_key_visible"] = None
    # 重复注册同 key → 显式异常（不静默覆盖）
    dup_module = REPO / "docs" / "evidence" / "T06" / "ext_probe_dup.py"
    if dup_module.exists():
        sys.path.insert(0, str(dup_module.parent))
        try:
            load_builtin_metrics([dup_module.stem])
            detail["duplicate_key_error"] = False
        except MetricRegistryError as exc:
            detail["duplicate_key_error"] = True
            detail["duplicate_key_message"] = _scrub(str(exc))[:160]
        finally:
            sys.path.remove(str(dup_module.parent))
            clear_registry()
            load_builtin_metrics()
    else:
        detail["duplicate_key_error"] = None
    detail["ok"] = bool(detail["builtin_keys_n"] == 13
                        and detail.get("extra_key_visible")
                        and detail.get("duplicate_key_error"))
    return detail


# ---------------------------------------------------------------------------
# G2 schema 合规 + G10 隐私
# ---------------------------------------------------------------------------

def gate_g2(out: Path) -> dict:
    main = out / "metricset.json"
    payload = json.loads(main.read_text(encoding="utf-8"))
    errors = list(Draft202012Validator(hs_schema.load_schema("metric-set")).iter_errors(payload))
    code, cli_out = _run_cli(["schema", "check", str(main), "--kind", "metric-set"])
    diff = subprocess.run(["git", "diff", "--stat", "packages/schema/"], cwd=str(REPO),
                          capture_output=True, text=True, check=False)
    untracked = subprocess.run(["git", "status", "--porcelain", "packages/schema/"],
                               cwd=str(REPO), capture_output=True, text=True, check=False)
    return {
        "jsonschema_errors": len(errors),
        "first_errors": [f"{list(e.absolute_path)}: {e.message}" for e in errors[:3]],
        "cli_exit": code,
        "cli_stdout": _scrub(cli_out.strip())[:120],
        "git_diff_packages_schema": diff.stdout.strip() or "",
        "git_status_packages_schema": untracked.stdout.strip() or "",
        "ok": (not errors) and code == 0 and not diff.stdout.strip() and not untracked.stdout.strip(),
    }


def gate_g10(out: Path, store: Store, run_id: str) -> dict:
    blobs = {name: (out / name).read_text(encoding="utf-8")
             for name in ("metricset.json", "metricset.ext.json")}
    user = Path(HOME).name
    raw_refs = [r[0] for r in store._conn.execute(
        "SELECT raw_ref FROM events WHERE run_id = ? AND raw_ref IS NOT NULL", (run_id,))]
    checks = {
        "home_prefix": sum(b.count(HOME) for b in blobs.values()),
        "windows_user_dir": sum(len(re.findall(r"[A-Za-z]:[\\/]Users[\\/]", b)) for b in blobs.values()),
        "posix_home_dir": sum(b.count("/home/") for b in blobs.values()),
        "username_literal": sum(b.count(user) for b in blobs.values()) if len(user) > 2 else 0,
        "raw_ref_hits": sum(b.count(ref) for b in blobs.values() for ref in raw_refs if ref),
    }
    return {**checks, "raw_ref_rows_in_db": len(raw_refs),
            "ok": all(v == 0 for v in checks.values())}


# ---------------------------------------------------------------------------
# G3 策略门禁（merge 流）
# ---------------------------------------------------------------------------

def gate_g3(tmp: Path, store: Store, run_id: str) -> dict:
    """把真实 run 的一小批事件（≤200 条）以 merge 策略另存为独立 run，验证被拒绝。"""
    merge_db = tmp / "merge-gate.db"
    merge_run = f"{run_id}-merge-probe"
    events = []
    for i, ev in enumerate(store.iter_events(run_id, order_by="seq")):
        if i >= 200:
            break
        ev = dict(ev, run_id=merge_run)
        events.append(ev)
    with Store(merge_db) as db:
        db.ingest(merge_run, events, channel_policy="merge", allow_merge=True)
        result = compute_run(db, merge_run, params=MetricParams())
    all_null = all(mv.value is None
                   for group in (result.metric_set.groups.result,
                                 result.metric_set.groups.process,
                                 result.metric_set.groups.collaboration,
                                 result.metric_set.groups.cost)
                   for mv in group)
    code, _ = _cli_metrics_run(merge_db, merge_run, tmp / "g3")
    return {
        "merge_run": merge_run,
        "events_used": len(events),
        "blocked": result.blocked,
        "blocked_reason": _scrub(result.ext["blocked_reason"] or ""),
        "all_values_null": all_null,
        "all_keys_degraded": len(result.metric_set.degraded_metrics) == len(REGISTRY),
        "degraded_reasons": sorted({v["reason"] for v in result.ext["degraded_reasons"].values()}),
        "cli_exit": code,
        "ok": (result.blocked and all_null
               and len(result.metric_set.degraded_metrics) == len(REGISTRY) and code == 2),
    }


# ---------------------------------------------------------------------------
# G5 可下钻 / G6 可复现
# ---------------------------------------------------------------------------

def gate_g5(out: Path, store: Store, run_id: str) -> dict:
    ext = json.loads((out / "metricset.ext.json").read_text(encoding="utf-8"))
    main = json.loads((out / "metricset.json").read_text(encoding="utf-8"))
    values = {mv["key"]: mv["value"] for group in main["groups"].values() for mv in group}
    non_null = [k for k, v in values.items() if v is not None]
    total_ids = 0
    resolved = 0
    missing_non_null = []
    empty_evidence = []
    for key in non_null:
        refs = next(mv for group in main["groups"].values() for mv in group if mv["key"] == key)
        if not refs["evidence"]:
            empty_evidence.append(key)
        if not ext["evidence_index"].get(key):
            missing_non_null.append(key)
    for key, items in ext["evidence_index"].items():
        for item in items:
            total_ids += 1
            hit = store._conn.execute(
                "SELECT 1 FROM events WHERE run_id = ? AND event_id = ?",
                (item["run_id"], item["event_id"])).fetchone()
            resolved += 1 if hit else 0
    return {
        "non_null_metrics": len(non_null),
        "non_null_without_evidence_index": missing_non_null,
        "non_null_with_empty_evidence": empty_evidence,
        "event_ids_total": total_ids,
        "event_ids_resolved_in_db": resolved,
        "ok": not missing_non_null and not empty_evidence and total_ids > 0
              and resolved == total_ids,
    }


def _sha_from_file(path: Path) -> str:
    """按 docs/metrics-engine.md §规范序列化 从**主文件字节**复算 canonical_sha256。

    步骤：读 JSON → 删掉 `computed_at` → groups 各数组按 key 升序、degraded_metrics 升序
    → 键字典序、浮点 round(6) 的紧凑 JSON → sha256。
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop("computed_at", None)
    payload["groups"] = {name: sorted(items, key=lambda m: m["key"])
                         for name, items in payload["groups"].items()}
    payload["degraded_metrics"] = sorted(payload["degraded_metrics"])
    return "sha256:" + hashlib.sha256(
        canonical_json(payload).encode("utf-8")).hexdigest()


def gate_g6(store: Store, run_id: str, params: MetricParams, tmp: Path) -> dict:
    r1 = compute_run(store, run_id, params=params, at="2026-10-26T00:00:00Z",
                     out_dir=tmp / "g6-a")
    r2 = compute_run(store, run_id, params=params, at="2027-12-31T23:59:59Z",
                     out_dir=tmp / "g6-b")
    blobs = [(tmp / "g6-a" / name).read_text(encoding="utf-8")
             for name in ("metricset.json", "metricset.ext.json")]
    blobs += [(tmp / "g6-b" / name).read_text(encoding="utf-8")
              for name in ("metricset.json", "metricset.ext.json")]
    nan_hits = sum(len(re.findall(r"\b(NaN|-?Infinity)\b", b)) for b in blobs)
    from_file = _sha_from_file(tmp / "g6-a" / "metricset.json")
    return {
        "canonical_sha256_run1": r1.ext["canonical_sha256"],
        "canonical_sha256_run2": r2.ext["canonical_sha256"],
        "recomputed_from_file_bytes": from_file,
        "file_recompute_matches": from_file == r1.ext["canonical_sha256"],
        "identical": r1.ext["canonical_sha256"] == r2.ext["canonical_sha256"],
        "computed_at_run1": r1.metric_set.computed_at.isoformat(),
        "computed_at_run2": r2.metric_set.computed_at.isoformat(),
        "computed_at_excluded": r1.metric_set.computed_at != r2.metric_set.computed_at,
        "nan_or_infinity_hits": nan_hits,
        "ok": (r1.ext["canonical_sha256"] == r2.ext["canonical_sha256"] and nan_hits == 0
               and from_file == r2.ext["canonical_sha256"]),
    }


# ---------------------------------------------------------------------------
# G13 降级诚实
# ---------------------------------------------------------------------------

PRD_EXPECTED = {
    "result.success": "needs_input",
    "result.pass_k": "no_source",
    "process.trajectory_efficiency": "no_source",
    "cost.tokens_total": "no_source",
    "cost.tokens_in": "no_source",
    "cost.tokens_out": "no_source",
    "cost.cny": "no_source",
    "cost.wall_clock_p50": "no_source",
    "cost.wall_clock_p95": "no_source",
}


def gate_g13(out: Path) -> dict:
    ext = json.loads((out / "metricset.ext.json").read_text(encoding="utf-8"))
    got = {k: v["reason"] for k, v in ext["degraded_reasons"].items()}
    mismatch = {k: {"expected": PRD_EXPECTED[k], "actual": got.get(k)}
                for k in PRD_EXPECTED if got.get(k) != PRD_EXPECTED[k]}
    unknown = [r for r in got.values() if r not in DEGRADE_REASONS and not r.startswith("other:")]
    return {
        "prd_expected": PRD_EXPECTED,
        "actual": got,
        "mismatch": mismatch,
        "reasons_outside_enum": unknown,
        "ok": not mismatch and not unknown,
    }


# ---------------------------------------------------------------------------
# G15 恒等式（真实数据只上报）
# ---------------------------------------------------------------------------

def gate_g15(out: Path) -> dict:
    ext = json.loads((out / "metricset.ext.json").read_text(encoding="utf-8"))
    ident = ext["tool_call_identity"]
    note = ext["notes"].get("process.tool_correctness", "")
    return {
        **ident,
        "data_quality_flags": ext["data_quality_flags"],
        "note_excerpt": note[:400],
        "ok": True,      # 真实数据只上报，不因恒等式不成立而失败（PM v1.2）
    }


# ---------------------------------------------------------------------------
# E5-P 探针：token 源
# ---------------------------------------------------------------------------

def token_source_probe(store: Store, run_id: str) -> dict:
    from hs_core.metrics_core import _usage_of, trace_summary_tokens

    responses = list(store.iter_events(run_id, types=("llm.response",), order_by="seq"))
    usage_hits = sum(1 for ev in responses if _usage_of(ev) is not None)
    summary = trace_summary_tokens(list(store.iter_events(run_id, order_by="seq")))
    return {
        "llm_response_total": len(responses),
        "llm_response_with_usage": usage_hits,
        "metric_custom_total": summary["events"],
        "metric_custom_with_total_tokens": summary["with_total"],
        "metric_custom_positive_total_tokens": summary["positive"],
        "metric_custom_tokens_sum": summary["sum"],
    }


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="T06 门禁自检（真实库）")
    ap.add_argument("--db", default="runs/t06/t06.db")
    ap.add_argument("--run-id", default="t06-codebuddy-primary")
    ap.add_argument("--out", default=".tmp-t06")
    ap.add_argument("--params", default="engine/config/metrics.yaml")
    ap.add_argument("--pricing", default="engine/config/pricing.yaml")
    args = ap.parse_args()

    db_path = (REPO / args.db) if not Path(args.db).is_absolute() else Path(args.db)
    tmp = (REPO / args.out) if not Path(args.out).is_absolute() else Path(args.out)
    tmp.mkdir(parents=True, exist_ok=True)
    params = load_params_config(REPO / args.params if (REPO / args.params).exists() else None,
                               REPO / args.pricing if (REPO / args.pricing).exists() else None)

    report: dict = {"db": _rel(db_path), "run_id": args.run_id,
                    "params_digest": params.digest(), "python": Path(PY).name}
    with Store(db_path) as store:
        run = store.get_run(args.run_id)
        if run is None:
            print(json.dumps({"error": f"run 不存在: {args.run_id}"}, ensure_ascii=False))
            return 2
        report["run"] = {"channel_policy": run["channel_policy"],
                         "platform": run["platform"], "event_count": run["event_count"]}

        out = tmp / "run"
        code, cli_text = _cli_metrics_run(db_path, args.run_id, out)
        report["G11_cli"] = {"exit": code, "stdout": _scrub(cli_text.strip())[:1200],
                             "ok": code == 0}

        result = compute_run(store, args.run_id, params=params, out_dir=out)
        report["G1_registry"] = gate_g1(tmp, db_path, args.run_id)
        report["G2_schema"] = gate_g2(out)
        report["G3_policy_gate"] = gate_g3(tmp, store, args.run_id)
        report["G5_drilldown"] = gate_g5(out, store, args.run_id)
        report["G6_reproducibility"] = gate_g6(store, args.run_id, params, tmp)
        report["G10_privacy"] = gate_g10(out, store, args.run_id)
        report["G13_degraded"] = gate_g13(out)
        report["G15_tool_call_identity"] = gate_g15(out)
        report["E5P_token_source_probe"] = token_source_probe(store, args.run_id)
        report["G11_real_data"] = {
            "run_id": result.metric_set.run_id,
            "event_count": result.ext["event_count"],
            "elapsed_ms": result.ext["elapsed_ms"],
            "computed_keys_n": len(result.ext["computed_keys"]),
            "degraded_keys_n": len(result.metric_set.degraded_metrics),
            **{k: v.value for k, v in result.by_key.items() if v.value is not None},
        }

    report["ok"] = all(v.get("ok") for k, v in report.items()
                       if isinstance(v, dict) and "ok" in v)
    print(_scrub(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

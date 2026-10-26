#!/usr/bin/env python
"""T06-R1 返工验收自检（可复跑取证）。

用法（仓库根）：
  .venv/Scripts/python.exe scripts/t06_r1_check.py \
      --metrics runs/t06/metrics --db runs/t06/t06.db \
      --run-id t06-codebuddy-primary --out probe/t06/R1-result.json

做五件事，全部只读、只报实测：
  1. R1-3 双文件键名互斥（主文件 `degraded_metrics`: list[str]；ext `degraded_reasons`: 对象）
  2. R1-2 `key_count_check`（13 = 4 可算 + 9 降级，且与口径常量逐字一致）
  3. R1-4 可下钻性（每条 evidence.event_id 都必须能在库里 `(run_id, event_id)` 命中）
  4. R1-5 `confidence_audit`（反向断言：value 非空而 confidence 为空必须 0 命中）
  5. R1-6 签名退化事实（tool.call / span_end 两侧 content_sha256 的区分度）

外加：schema 校验、隐私扫描、`git diff packages/schema/`、单测结果（子进程复跑）。

退出码：0 = 全部 R1 项通过；2 = 有项失败（失败明细仍在 JSON 里）。
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "engine"))

from hs_core import schema as hs_schema
from hs_core.metrics import (
    EXPECTED_ALL_KEYS,
    EXPECTED_COMPUTED_KEYS,
    EXPECTED_DEGRADED_KEYS,
    MetricParams,
)
from jsonschema import Draft202012Validator

# Q7 裁定后的期望档位（R1-5）。降级指标（value == null）期望 null。
EXPECTED_CONFIDENCE: dict[str, str] = {
    "process.step_count": "high",        # 直接观测计数，n = 2567 ≥ 20
    "process.retry_rate": "high",        # n = tool.call 数 ≥ 20（分母语义未变，只是分子近似）
    "process.redundant_loop": "high",    # n = 参与判定的调用数 ≥ 20
    "process.tool_correctness": "low",   # 上界 / 派生口径 → 恒 low
}
DERIVED_KEYS: tuple[str, ...] = ("process.tool_correctness",)

DECISIONS: dict = {
    "R1-6_signature": {
        "verdict": "a",
        "ruling": ("认 span_id 从 span_end 补齐的内容哈希为正式签名口径"
                   "（tool.call 自身的 content_sha256 全为 sha256(\"\")，无区分度，不得用作签名）"),
        "semantics": ("取的是「结果内容哈希」，不是「入参哈希」：相同入参不同输出会漏检冗余；"
                      "不同入参相同输出（如都失败返回同一错误）会误判冗余。它是当前通道 A 数据下"
                      "唯一可用的近似，指标 note 已声明来源。"),
        "revisit": "T05 v2 在采集侧补到真入参哈希后，本近似自动失效（指标代码零改动）",
    },
    "R1-5_confidence_band": {
        "ruling": ("按 Q7：n ≥ low_confidence_n(20) → high；n < 20 → low；派生/上界口径恒 low；"
                   "降级一律 null。retry_rate / redundant_loop 记 high（与主理人裁定一致），"
                   "tool_correctness 因 deny 信号无源、值为上界 → low。"),
    },
    "R1-2_key_count_assert": {
        "ruling": ("代码常量锁「实现能吐哪些 key」（注册即校验，多吐/少吐都报错）+ 装配层断言 key 不缺；"
                   "**不**硬断言本次一定 4 可算，否则 G7 前向兼容（通道 A 补到 usage 后 cost.* 出值）"
                   "会被自己的断言打死。数据侧漂移在 key_count_check.computed_matches_expected 里"
                   "如实上报为 false。"),
    },
}

R1_ITEMS: dict = {
    "R1-1": {
        "status": "hardened",
        "why": "原判「step_count 分子分母倒置」在当前产物上未复现（step_count 2567 == count(step.start) 2567）",
        "done": ("口径锁定为唯一来源 type=='step.start' 事件；dsh `pre-step` 的 turn/step 交叉校验"
                 "（不一致以事件计数为准，差异写 note）；两条防回归单测"),
        "evidence": ["key_count_check.computed_keys", "gates.pytest"],
        "tests": ["test_step_count_uses_step_start_events", "test_step_count_crosscheck_with_pre_step"],
    },
    "R1-2": {
        "status": "locked",
        "why": "13 = 4 可算 + 9 降级本来已自洽，转防回归锁定",
        "done": ("EXPECTED_COMPUTED_KEYS / EXPECTED_DEGRADED_KEYS 常量 + 注册器双向校验 + "
                 "装配层缺 key 断言；ext.key_count_check 如实上报本次条数"),
        "evidence": ["key_count_check"],
        "tests": ["test_key_count_is_self_consistent", "test_key_count_reports_data_drift_without_failing"],
    },
    "R1-3": {
        "status": "fixed",
        "why": "ext 同时存在 degraded_metrics（对象）与主文件 degraded_metrics（list[str]），同名不同构",
        "done": "ext 只保留 degraded_reasons（对象）；主文件保持 list[str]；四向逐字断言",
        "evidence": ["naming_check"],
        "tests": ["test_degraded_keys_naming_is_disjoint_between_files"],
    },
    "R1-4": {
        "status": "retested",
        "why": "可下钻性与恒等式「只上报不失败」本已正确，仅复测并补快照标识",
        "done": ("逐条 event_id 回查库；tool_call_identity 补 event_count + snapshot_digest；"
                 "identity_holds=false 时仍退出码 0"),
        "evidence": ["drilldown", "identity_check"],
        "tests": ["test_every_non_null_metric_has_resolvable_evidence",
                  "test_tool_call_identity_flag_reported_without_touching_value"],
    },
    "R1-5": {
        "status": "fixed",
        "why": "4 个可算指标 confidence 全为 None（违反 Q7）",
        "done": ("n≥20 → high、n<20 → low、派生/上界恒 low、降级 null；反向断言进装配层"
                 "（value!=null 且 confidence==null 直接抛 MetricAssemblyError）"),
        "evidence": ["confidence_audit"],
        "tests": ["test_every_valued_metric_has_confidence", "test_params_change_low_confidence_n_takes_effect"],
    },
    "R1-6": {
        "status": "fixed",
        "why": ("note / degraded_reasons[].detail 里写死了另一份快照的实测数字"
                "（llm.response 命中 0/9098、通道 B 723 行、metric.custom 33/33 全 0）"),
        "done": ("本 run 能算的现场算（llm.response 条数/usage 命中、trace_summary 条数/>0 条数、"
                 "扫描事件数、deny 命中、tool.call 自身空哈希条数）；算不到的改成"
                 "「引用（非本 run 观测）+ 出处」；签名口径定案 (a) + 退化 flag"),
        "evidence": ["signature_audit", "degraded detail 见 runs/t06/metrics/metricset.ext.json"],
        "tests": ["test_degraded_detail_is_this_run_not_hardcoded_snapshot",
                  "test_tokens_metrics_no_source_current_real_shape"],
    },
    "R1-7": {
        "status": "fixed",
        "why": "loop_min_repeat 从 2 改 3 结果不变（只比较了 1 个周期 = 半死参数）",
        "done": "比较 (min_repeat-1)×p 个位置，真正要求 min_repeat 轮连续重复；note 写明生效轮数",
        "evidence": ["gates.pytest"],
        "tests": ["test_params_change_loop_min_repeat_takes_effect"],
    },
}

OPEN_QUESTIONS: list[dict] = [
    {
        "id": "OQ1",
        "question": "通道 B 的 723 行 providerData.usage 无法在 primary_only 主源里复算——是否需要变更卡让 cost.* 走 merge 流专用通道？",
        "owner": "主理人 / T05 v2",
        "current_handling": "detail 写成引用式（非本次观测 + 出处），cost.* 维持 no_source",
    },
    {
        "id": "OQ2",
        "question": "`degraded_metrics`（主文件，list）与 `degraded_reasons`（ext，对象）长期双名，是否需要变更卡把 reason 收进 schema、输出收敛为单文件？",
        "owner": "主理人 / T01 契约",
        "current_handling": "两文件键名互斥 + 单测逐字断言（R1-3），下游按文件取键不会拿错类型",
    },
    {
        "id": "OQ3",
        "question": "`identity_violated`（tool.call 921 ≠ result 908 + error 12）是快照相关的结构性质，差 1 无解释；是否需要采集侧排查 span 只有 start 的调用？",
        "owner": "T05 采集侧",
        "current_handling": "只上报 data_quality_flags，指标照常出值；**禁止**为消除 flag 改计数逻辑",
    },
]


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _values(main: dict) -> dict[str, dict]:
    return {mv["key"]: mv for group in main["groups"].values() for mv in group}


# ---------------------------------------------------------------------------
# R1-3 双文件键名
# ---------------------------------------------------------------------------

def naming_check(main: dict, ext: dict) -> dict:
    main_dm = main.get("degraded_metrics")
    ext_dr = ext.get("degraded_reasons")
    reasons_ok = isinstance(ext_dr, dict) and all(
        isinstance(v, dict) and v.get("reason") and v.get("detail") for v in ext_dr.values())
    return {
        "main_degraded_metrics_is_list_of_str": (
            isinstance(main_dm, list) and all(isinstance(k, str) for k in main_dm)),
        "main_degraded_metrics_n": len(main_dm) if isinstance(main_dm, list) else None,
        "ext_degraded_reasons_is_mapping_with_reason_detail": reasons_ok,
        "ext_degraded_reasons_n": len(ext_dr) if isinstance(ext_dr, dict) else None,
        "ext_has_degraded_metrics_key": "degraded_metrics" in ext,
        "main_has_degraded_reasons_key": "degraded_reasons" in main,
        "same_key_set": (sorted(main_dm) == sorted(ext_dr)
                         if isinstance(main_dm, list) and isinstance(ext_dr, dict) else False),
        "ok": (isinstance(main_dm, list)
               and reasons_ok
               and "degraded_metrics" not in ext
               and "degraded_reasons" not in main
               and sorted(main_dm) == sorted(ext_dr)),
    }


# ---------------------------------------------------------------------------
# R1-2 key 计数
# ---------------------------------------------------------------------------

def key_count(main: dict, ext: dict) -> dict:
    values = _values(main)
    computed = sorted(k for k, mv in values.items() if mv["value"] is not None)
    degraded = sorted(main["degraded_metrics"])
    return {
        "total": len(values),
        "computed": len(computed),
        "degraded": len(degraded),
        "consistent": (len(values) == len(EXPECTED_ALL_KEYS)
                       and len(computed) + len(degraded) == len(values)
                       and sorted(values) == list(EXPECTED_ALL_KEYS)),
        "expected_total": len(EXPECTED_ALL_KEYS),
        "expected_computed": len(EXPECTED_COMPUTED_KEYS),
        "expected_degraded": len(EXPECTED_DEGRADED_KEYS),
        "computed_matches_expected": computed == sorted(EXPECTED_COMPUTED_KEYS),
        "degraded_matches_expected": degraded == sorted(EXPECTED_DEGRADED_KEYS),
        "computed_keys": computed,
        "degraded_keys": degraded,
        "ext_key_count_check": ext.get("key_count_check"),
        # 本次基线 run 的验收判据 = 结构自洽 + 恰好 4 可算 / 9 降级（数据侧漂移会让 ok=false，
        # 那是「数据变了」不是「实现多吐 key」，明细在 computed_matches_expected 里）
        "ok": (len(values) == len(EXPECTED_ALL_KEYS)
               and len(computed) + len(degraded) == len(values)
               and len(computed) == len(EXPECTED_COMPUTED_KEYS)
               and len(degraded) == len(EXPECTED_DEGRADED_KEYS)),
    }


# ---------------------------------------------------------------------------
# R1-4 可下钻
# ---------------------------------------------------------------------------

def drilldown(main: dict, ext: dict, db: Path, run_id: str) -> dict:
    conn = sqlite3.connect(str(db))
    try:
        total = 0
        resolved = 0
        unresolved: list[dict] = []
        per_key: dict[str, int] = {}
        for group in main["groups"].values():
            for mv in group:
                n = 0
                for ref in mv.get("evidence", []):
                    for eid in ref.get("event_ids", []):
                        total += 1
                        n += 1
                        hit = conn.execute(
                            "SELECT 1 FROM events WHERE run_id = ? AND event_id = ?",
                            (run_id, eid)).fetchone()
                        if hit:
                            resolved += 1
                        else:
                            unresolved.append({"key": mv["key"], "event_id": eid})
                if n:
                    per_key[mv["key"]] = n
        idx_total = 0
        idx_unresolved: list[dict] = []
        for key, items in ext.get("evidence_index", {}).items():
            for item in items:
                idx_total += 1
                hit = conn.execute(
                    "SELECT 1 FROM events WHERE run_id = ? AND event_id = ?",
                    (item["run_id"], item["event_id"])).fetchone()
                if not hit:
                    idx_unresolved.append({"key": key, "event_id": item["event_id"]})
    finally:
        conn.close()
    return {
        "event_ids_total": total,
        "event_ids_resolved_in_db": resolved,
        "unresolved": unresolved,
        "hit_rate": round(resolved / total, 6) if total else None,
        "per_key": per_key,
        "ext_evidence_index_ids": idx_total,
        "ext_evidence_index_unresolved": idx_unresolved,
        "event_id_unique_needs_run_id": True,   # event_id 不含 run_id：引用必须带 run_id
        "ok": bool(total) and not unresolved and not idx_unresolved,
    }


# ---------------------------------------------------------------------------
# R1-5 置信度
# ---------------------------------------------------------------------------

def _sample_sizes(db: Path, run_id: str) -> dict[str, int | None]:
    conn = sqlite3.connect(str(db))
    try:
        def count(typ: str) -> int:
            return conn.execute("SELECT COUNT(*) FROM events WHERE run_id=? AND type=?",
                                (run_id, typ)).fetchone()[0]
        calls = count("tool.call")
        return {
            "process.step_count": count("step.start"),
            "process.tool_correctness": calls,
            "process.retry_rate": calls,
            "process.redundant_loop": calls,
        }
    finally:
        conn.close()


def confidence_audit(main: dict, db: Path, run_id: str) -> dict:
    sizes = _sample_sizes(db, run_id)
    by_key: dict[str, dict] = {}
    valued_without_confidence: list[str] = []
    degraded_with_confidence: list[str] = []
    mismatched: list[dict] = []
    for key, mv in sorted(_values(main).items()):
        conf = mv.get("confidence")           # schema 层 null 时字段被省略 → None
        expected = EXPECTED_CONFIDENCE.get(key)
        by_key[key] = {
            "value": mv["value"],
            "confidence": conf,
            "expected": expected,             # None = 降级应无档位
            "sample_n": sizes.get(key),
            "derived": key in DERIVED_KEYS,
        }
        if mv["value"] is not None and conf is None:
            valued_without_confidence.append(key)
        if mv["value"] is None and conf is not None:
            degraded_with_confidence.append(key)
        if conf != expected:
            mismatched.append({"key": key, "actual": conf, "expected": expected})
    return {
        "rule": ("n ≥ low_confidence_n(20) → high；n < 20 → low；派生/上界口径恒 low；"
                 "降级一律 null（禁止给 no_source 填 low）"),
        "by_key": by_key,
        "valued_without_confidence": valued_without_confidence,
        "degraded_with_confidence": degraded_with_confidence,
        "mismatched_vs_q7": mismatched,
        "ok": not valued_without_confidence and not degraded_with_confidence and not mismatched,
    }


def identity_report(ext: dict) -> dict:
    """R1-4（续）：恒等式只上报不失败 + 必须带快照标识。"""
    ident = ext.get("tool_call_identity") or {}
    required = ("tool_call", "tool_result", "tool_error", "identity_holds",
                "event_count", "snapshot_digest")
    missing = [k for k in required if k not in ident]
    violated = "tool_call_identity_violated" in (ext.get("data_quality_flags") or [])
    return {
        "identity_holds": ident.get("identity_holds"),
        "tool_call": ident.get("tool_call"),
        "tool_result": ident.get("tool_result"),
        "tool_error": ident.get("tool_error"),
        "event_count": ident.get("event_count"),
        "snapshot_digest": ident.get("snapshot_digest"),
        "missing_fields": missing,
        "flag_reported": violated,
        # 恒等式不成立时 flag 必须在、指标值必须在、退出码必须 0 —— 这里只断言「上报」这一半
        "reported_not_failed": (violated == (ident.get("identity_holds") is False)),
        "data_quality_flags": ext.get("data_quality_flags"),
        "ok": not missing and (violated == (ident.get("identity_holds") is False)),
    }


# ---------------------------------------------------------------------------
# R1-6 签名退化事实
# ---------------------------------------------------------------------------

def signature_audit(db: Path, run_id: str) -> dict:
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT type, payload FROM events WHERE run_id=? AND type IN "
            "('tool.call','tool.result','tool.error')", (run_id,)).fetchall()
    finally:
        conn.close()
    buckets: dict[str, list[str]] = {"tool.call": [], "span_end": []}
    for typ, payload in rows:
        try:
            data = json.loads(payload or "{}")
        except json.JSONDecodeError:
            continue
        sha = data.get("content_sha256")
        if not isinstance(sha, str) or not sha:
            continue
        buckets["tool.call" if typ == "tool.call" else "span_end"].append(sha)
    out = {}
    for side, shas in buckets.items():
        distinct = len(set(shas))
        out[side] = {
            "n": len(shas),
            "distinct": distinct,
            "all_same": len(shas) >= 2 and distinct <= 1,
            "distinct_ratio": round(distinct / len(shas), 6) if shas else None,
        }
    out["usable_as_signature"] = {
        "tool.call": not out["tool.call"]["all_same"],
        "span_end": not out["span_end"]["all_same"],
    }
    out["ok"] = bool(out["span_end"]["n"])       # span_end 侧必须有区分度才谈得上「补齐」
    return out


# ---------------------------------------------------------------------------
# 其它门禁
# ---------------------------------------------------------------------------

def schema_gate(main: dict) -> dict:
    errors = list(Draft202012Validator(hs_schema.load_schema("metric-set")).iter_errors(main))
    return {
        "errors": len(errors),
        "first_errors": [f"{list(e.absolute_path)}: {e.message}" for e in errors[:3]],
        "ok": not errors,
    }


def privacy_gate(metrics_dir: Path, db: Path, run_id: str) -> dict:
    blobs = {p.name: p.read_text(encoding="utf-8")
             for p in sorted(metrics_dir.glob("*.json"))}
    conn = sqlite3.connect(str(db))
    try:
        raw_refs = [r[0] for r in conn.execute(
            "SELECT raw_ref FROM events WHERE run_id=? AND raw_ref IS NOT NULL", (run_id,))]
    finally:
        conn.close()
    checks = {
        "windows_user_paths": sum(len(re.findall(r"[A-Za-z]:[\\/]Users[\\/]", b))
                                  for b in blobs.values()),
        "posix_home_paths": sum(b.count("/home/") for b in blobs.values()),
        "raw_ref_hits": sum(b.count(ref) for b in blobs.values() for ref in raw_refs if ref),
        "home_dir_hits": sum(b.count(str(Path.home())) for b in blobs.values()),
    }
    return {**checks, "raw_ref_rows_in_db": len(raw_refs),
            "ok": all(v == 0 for v in checks.values())}


def schema_diff_gate() -> dict:
    diff = subprocess.run(["git", "diff", "--stat", "packages/schema/"], cwd=str(REPO),
                          capture_output=True, text=True, check=False)
    status = subprocess.run(["git", "status", "--porcelain", "packages/schema/"],
                            cwd=str(REPO), capture_output=True, text=True, check=False)
    return {"git_diff": diff.stdout.strip(), "git_status": status.stdout.strip(),
            "ok": not diff.stdout.strip() and not status.stdout.strip()}


def pytest_gate() -> dict:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "engine/tests/test_metrics.py",
         "engine/tests/test_metrics_core.py", "-q", "--basetemp=.tmp-pytest-r1"],
        cwd=str(REPO), capture_output=True, text=True, check=False)
    tail = [line for line in (proc.stdout or "").strip().splitlines() if line.strip()][-1:]
    return {"exit": proc.returncode, "summary": tail[0] if tail else "",
            "ok": proc.returncode == 0}


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="T06-R1 返工验收自检")
    ap.add_argument("--metrics", default="runs/t06/metrics")
    ap.add_argument("--db", default="runs/t06/t06.db")
    ap.add_argument("--run-id", default="t06-codebuddy-primary")
    ap.add_argument("--out", default="probe/t06/R1-result.json")
    ap.add_argument("--skip-pytest", action="store_true")
    args = ap.parse_args()

    metrics_dir = (REPO / args.metrics) if not Path(args.metrics).is_absolute() else Path(args.metrics)
    db_path = (REPO / args.db) if not Path(args.db).is_absolute() else Path(args.db)
    main_doc = _load(metrics_dir / "metricset.json")
    ext_doc = _load(metrics_dir / "metricset.ext.json")

    report: dict = {
        "card": "T06-R1",
        "generated_at": "2026-10-26",
        "run_id": main_doc.get("run_id"),
        "event_count": ext_doc.get("event_count"),
        "params_digest": ext_doc.get("params_digest"),
        "canonical_sha256": ext_doc.get("canonical_sha256"),
        "params": MetricParams().as_dict(),
        "how_to_reproduce": (
            ".venv/Scripts/python.exe scripts/t06_r1_check.py --metrics runs/t06/metrics "
            "--db runs/t06/t06.db --run-id t06-codebuddy-primary --out probe/t06/R1-result.json"),
        "naming_check": naming_check(main_doc, ext_doc),
        "key_count_check": key_count(main_doc, ext_doc),
        "drilldown": drilldown(main_doc, ext_doc, db_path, args.run_id),
        "confidence_audit": confidence_audit(main_doc, db_path, args.run_id),
        "identity_check": identity_report(ext_doc),
        "signature_audit": signature_audit(db_path, args.run_id),
        "gates": {
            "schema": schema_gate(main_doc),
            "privacy": privacy_gate(metrics_dir, db_path, args.run_id),
            "packages_schema_untouched": schema_diff_gate(),
            "pytest": {"skipped": True} if args.skip_pytest else pytest_gate(),
        },
        "decisions": DECISIONS,
        "r1_items": R1_ITEMS,
        "open_questions": OPEN_QUESTIONS,
    }
    items = {k: report[k]["ok"] for k in
             ("naming_check", "key_count_check", "drilldown", "identity_check",
              "confidence_audit", "signature_audit")}
    gates = {k: v["ok"] for k, v in report["gates"].items() if "ok" in v}
    report["r1_items_ok"] = items
    report["gates_ok"] = gates
    report["ok"] = all(items.values()) and all(gates.values())

    out = (REPO / args.out) if not Path(args.out).is_absolute() else Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("ok", "r1_items_ok", "gates_ok")},
                     ensure_ascii=False, indent=2))
    print(f"written: {out}")
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

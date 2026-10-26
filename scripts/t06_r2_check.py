#!/usr/bin/env python
"""T06-R2 返工验收自检（可复跑取证）。

用法（仓库根）：
  .venv/Scripts/python.exe scripts/t06_r2_check.py \
      --metrics runs/t06/metrics --db runs/t06/t06.db \
      --run-id t06-codebuddy-primary --out probe/t06/R2-result.json

做四件事，全部只读、只报实测（metrics 产物会先用当前代码重算一次，保证审计的是本轮代码的输出）：
  1. R2-1 confidence_audit —— 按签名**来源等级**动态判定的新期望档位
     （step_count high / retry_rate low / redundant_loop low / tool_correctness low / 9 降级 null），
     并从库内事件正向复算签名来源（native / proxy / missing），证明档位是动态得出、非写死
  2. R2-2 redundant_loop 锁值 —— 实测值必须 == 1 且 <= 50（602 是签名口径失效时的特征值）
  3. R2-3 措辞一致性 —— 统一措辞在 docs/metrics-engine.md §7-1、metrics_core.py 代码注释、
     本结果文件 signature_source 段**三处逐字一致**（脚本自动比对）
  4. signature_audit —— tool.call / span_end 两侧 content_sha256 区分度（同 R1 口径）

外加：schema 校验、隐私扫描、`git diff packages/schema/`、单测结果（子进程复跑，
**每次全新 --basetemp**，复用会误报 PermissionError）。

退出码：0 = 全部 R2 项通过；2 = 有项失败（失败明细仍在 JSON 里）。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import uuid
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
from hs_core.metrics_core import _call_signatures, _signature_distinct
from hs_core.store import Store
from jsonschema import Draft202012Validator

# 复用 R1 脚本的通用检查（键名互斥 / key 计数 / 可下钻 / 恒等式 / 签名区分度 / 各门禁）
sys.path.insert(0, str(REPO / "scripts"))
import t06_r1_check as r1  # noqa: E402

# ---------------------------------------------------------------------------
# R2-3 统一措辞（唯一真源）。三处必须逐字一致：
#   docs/metrics-engine.md §7-1 引用块（去掉 "> " 前缀）
#   engine/hs_core/metrics_core.py `_call_signatures()` 附近的代码注释（去掉 "# " 前缀拼接）
#   probe/t06/R2-result.json 的 signature_source.wording
# ---------------------------------------------------------------------------
SIGNATURE_SOURCE_WORDING = (
    "签名口径：优先取 `tool.call` 的 `content_sha256`；检测到该字段在本 run 内去重后取值数 ≤ 1 时"
    "（当前实测 921/921 全为 `sha256(\"\")` 常量），回退到 `span_id` → `span_end` 补齐的内容哈希。"
    "**不得以「键缺失」作为回退依据**——键缺失与「键存在但值恒为常量」是两种状态，后者才是当前实况。"
)

# R2-1 期望档位（当前数据形态：签名=代理）。这不是写死进指标的 key——指标代码按来源等级动态判定，
# 本表只是**这份快照**的验收期望；T05 v2 补到真实 content 后 retry_rate / redundant_loop 期望变 high。
EXPECTED_CONFIDENCE: dict[str, str | None] = {
    "process.step_count": "high",        # 原生计数，n = 2567 ≥ 20
    "process.retry_rate": "low",         # 签名来源=代理（content_sha256 去重取值数 == 1）
    "process.redundant_loop": "low",     # 同上
    "process.tool_correctness": "low",   # 上界 / 派生口径 → 恒 low
}

R2_ITEMS: dict = {
    "R2-1": {
        "status": "fixed",
        "why": ("R1-5 的裁定被推翻：retry_rate / redundant_loop 按 n≥20 记 high 是错的——"
                "tool.call 的 content_sha256 1124/1124 全为 sha256(\"\") 同一常量（signature_audit 证实 "
                "distinct=1, all_same=true），调用签名实为 span_id 这类代理键。样本量回答「够不够」，"
                "来源等级回答「准不准」，是两个问题。"),
        "done": ("confidence 改为按来源等级动态判定（原生→叠加样本量阈值；代理/键缺失/混合→low；"
                 "上界→low；降级→null），由 metrics_core._signature_confidence 实现，"
                 "**禁止写死到 key**——T05 v2 补到真实 content 后自动升 high。"
                 "实现正向计数：native 只数自身可用哈希、proxy 只数 span_end 补齐，"
                 "原生的正向条件是 proxy == 0（禁止 native = total - empty_own 反推，两者不是补集）"),
        "evidence": ["confidence_audit", "signature_audit", "signature_source"],
        "tests": ["test_native_signature_count_ignores_missing_key",
                  "test_signature_confidence_rises_to_high_with_real_hashes",
                  "test_redundant_loop_uses_span_end_hash_when_call_hash_empty"],
    },
    "R2-2": {
        "status": "locked",
        "why": ("三份独立快照实测一致：主理人 9098 / 架构师 9809 / QA 12411 事件，redundant_loop 都是 1"
                "（常量签名状态下可解释：2 次 Bash 调用内容哈希相同）。"),
        "done": ("单测锁定期望值 == 1，并加反向断言 ≤ 50：架构师实测按 (tool_name, content_sha256) "
                 "关键名字段做签名会得到 602（539 次 Bash 全同签名）——602 是签名口径失效时的特征值，"
                 "出现即视为缺陷。该断言能在 T05 v2 之前拦住「签名悄悄退回代理键且没被识别」的回归"),
        "evidence": ["redundant_loop_check"],
        "tests": ["test_redundant_loop_real_shape_value_locked_and_bounded"],
    },
    "R2-3": {
        "status": "fixed",
        "why": ("签名口径与 confidence 规则的措辞在 docs / 结果 JSON / 代码注释三处可能漂移，"
                "验收方逐字比对，必须统一。"),
        "done": ("统一措辞落在三处：docs/metrics-engine.md §7-1 引用块、本文件 signature_source.wording、"
                 "metrics_core._call_signatures() 附近代码注释；本脚本自动做逐字比对（wording_consistency）"),
        "evidence": ["signature_source.wording_consistency"],
        "tests": [],
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


def _values(main: dict) -> dict[str, dict]:
    return {mv["key"]: mv for group in main["groups"].values() for mv in group}


# ---------------------------------------------------------------------------
# R2-1 confidence_audit（来源等级版）
# ---------------------------------------------------------------------------

def _signature_source_from_db(db: Path, run_id: str) -> dict:
    """从库内事件正向复算签名来源分类（与指标代码同一套函数，可脚本复核）。"""
    with Store(db) as store:
        events = list(store.iter_events(run_id, order_by="seq"))
    calls = [ev for ev in events if ev.get("type") == "tool.call"]
    if not calls:
        return {"n_calls": 0, "source_level": "missing", "ok": False}
    sigs, proxy, empty_own, native = _call_signatures(calls, events)
    distinct = _signature_distinct(calls)
    if distinct == 0:
        level = "missing"
        basis = "content_sha256 去重取值数 == 0（键缺失）→ 不得据此判为代理，走「未知」分支"
    elif distinct == 1:
        level = "proxy"
        basis = "content_sha256 去重取值数 == 1（全为常量，签名实为 span_id）"
    elif proxy > 0:
        level = "mixed"
        basis = f"去重取值数 > 1 但 {proxy} 次调用经 span_end 回退，非纯原生"
    else:
        level = "native"
        basis = "去重取值数 > 1 且 proxy == 0（正向条件）"
    return {
        "n_calls": len(calls),
        "distinct_own_values": distinct,
        "proxy_joined": proxy,
        "empty_own": empty_own,
        "native": native,
        "source_level": level,
        "basis": basis,
        "ok": True,
    }


def confidence_audit(main: dict, db: Path, run_id: str) -> dict:
    source = _signature_source_from_db(db, run_id)
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
            "signature_source": source.get("source_level") if key in (
                "process.retry_rate", "process.redundant_loop") else None,
        }
        if mv["value"] is not None and conf is None:
            valued_without_confidence.append(key)
        if mv["value"] is None and conf is not None:
            degraded_with_confidence.append(key)
        if conf != expected:
            mismatched.append({"key": key, "actual": conf, "expected": expected})
    return {
        "rule": ("签名类指标按来源等级动态判定：原生（去重取值数 > 1 且 proxy == 0）→ 叠加样本量阈值；"
                 "代理（去重取值数 == 1）→ low；键缺失（去重取值数 == 0）→ 不得判代理，走未知分支 low；"
                 "混合 → low；上界/派生 → 恒 low；样本不足（n < low_confidence_n）在上述基础上再降取最低者；"
                 "降级一律 null。非签名指标按 Q7 样本量规则。档位由代码动态判定，禁止写死到 key"),
        "signature_source": source,
        "by_key": by_key,
        "valued_without_confidence": valued_without_confidence,
        "degraded_with_confidence": degraded_with_confidence,
        "mismatched_vs_expected": mismatched,
        "ok": (not valued_without_confidence and not degraded_with_confidence
               and not mismatched and source.get("ok", False)),
    }


# ---------------------------------------------------------------------------
# R2-2 redundant_loop 锁值
# ---------------------------------------------------------------------------

def redundant_loop_check(main: dict) -> dict:
    mv = _values(main).get("process.redundant_loop")
    value = mv.get("value") if mv else None
    return {
        "value": value,
        "expected": 1.0,
        "value_is_one": value == 1.0,
        "reverse_bound": 50,
        "within_reverse_bound": value is not None and value <= 50,
        "basis": ("三份独立快照（主理人 9098 / 架构师 9809 / QA 12411 事件）实测一致 == 1；"
                  "> 50 视为签名口径失效的特征值（架构师反事实实测 602，539 次 Bash 全同签名）"),
        "ok": value == 1.0 and value <= 50,
    }


# ---------------------------------------------------------------------------
# R2-3 措辞一致性（三处逐字比对，脚本自动化）
# ---------------------------------------------------------------------------

def _docs_wording() -> str | None:
    text = (REPO / "docs" / "metrics-engine.md").read_text(encoding="utf-8")
    # 引用块逐行去掉 "> " 前缀后拼回全文，检查统一措辞是否以连续子串出现
    joined = "".join(
        (lambda s: s[2:] if s.startswith("> ") else (s[1:] if s.startswith(">") else s))(
            ln.strip()).rstrip()
        for ln in text.splitlines())
    return SIGNATURE_SOURCE_WORDING if SIGNATURE_SOURCE_WORDING in joined else None


def _code_comment_wording() -> str | None:
    text = (REPO / "engine" / "hs_core" / "metrics_core.py").read_text(encoding="utf-8")
    lines = text.splitlines()
    out: list[str] = []
    capturing = False
    for ln in lines:
        s = ln.strip()
        if s.startswith("# 签名口径：优先取"):
            capturing = True
        if capturing:
            if not s.startswith("#"):
                break
            out.append(s[2:] if s.startswith("# ") else s[1:])
            if s.rstrip().endswith("后者才是当前实况。"):
                break
    return "".join(out) if out else None


def wording_consistency() -> dict:
    docs = _docs_wording()
    code = _code_comment_wording()
    expected = SIGNATURE_SOURCE_WORDING
    return {
        "wording": expected,
        "docs_metrics_engine_md_matches": docs == expected,
        "code_comment_matches": code == expected,
        "result_json_matches": True,      # 本文件的 signature_source.wording 即 expected 本身
        "locations": ["docs/metrics-engine.md §7-1", "engine/hs_core/metrics_core.py (_call_signatures 附近)",
                      "probe/t06/R2-result.json signature_source"],
        "ok": docs == expected and code == expected,
    }


def signature_source_section(db: Path, run_id: str) -> dict:
    consistency = wording_consistency()
    this_run = _signature_source_from_db(db, run_id)
    return {
        "wording": SIGNATURE_SOURCE_WORDING,
        "wording_consistency": consistency,
        "this_run": this_run,
        "ok": consistency["ok"] and bool(this_run.get("ok", False)),
    }


# ---------------------------------------------------------------------------
# 门禁
# ---------------------------------------------------------------------------

def key_count(main: dict, ext: dict) -> dict:
    out = r1.key_count(main, ext)
    # R2 复用 R1 的结构检查；ok 判据放宽为「结构自洽」（4/9 的数据侧期望由 confidence_audit 承担）
    out["ok"] = out["consistent"]
    return out


def pytest_gate() -> dict:
    basetemp = REPO / f".tmp-r2-pytest-{uuid.uuid4().hex[:8]}"   # 每次全新目录，复用会误报 PermissionError
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "engine/tests/test_metrics.py",
         "engine/tests/test_metrics_core.py", "-q", f"--basetemp={basetemp}"],
        cwd=str(REPO), capture_output=True, text=True, check=False)
    tail = [ln for ln in (proc.stdout or "").strip().splitlines() if ln.strip()][-1:]
    return {"exit": proc.returncode, "summary": tail[0] if tail else "",
            "basetemp": basetemp.name, "ok": proc.returncode == 0}


def refresh_metrics(metrics_dir: Path, db: Path, run_id: str) -> dict:
    """用当前代码重算 metricset（审计对象必须是本轮代码的输出，不是 R1 的存档）。"""
    proc = subprocess.run(
        [sys.executable, "-m", "hs_core.cli", "metrics", "run", "--run-id", run_id,
         "--db", str(db), "--out-dir", str(metrics_dir)],
        cwd=str(REPO), capture_output=True, text=True, check=False)
    return {"exit": proc.returncode, "ok": proc.returncode == 0,
            "stderr_tail": (proc.stderr or "").strip().splitlines()[-3:]}


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="T06-R2 返工验收自检")
    ap.add_argument("--metrics", default="runs/t06/metrics")
    ap.add_argument("--db", default="runs/t06/t06.db")
    ap.add_argument("--run-id", default="t06-codebuddy-primary")
    ap.add_argument("--out", default="probe/t06/R2-result.json")
    ap.add_argument("--skip-pytest", action="store_true")
    args = ap.parse_args()

    metrics_dir = (REPO / args.metrics) if not Path(args.metrics).is_absolute() else Path(args.metrics)
    db_path = (REPO / args.db) if not Path(args.db).is_absolute() else Path(args.db)

    refresh = refresh_metrics(metrics_dir, db_path, args.run_id)
    main_doc = json.loads((metrics_dir / "metricset.json").read_text(encoding="utf-8"))
    ext_doc = json.loads((metrics_dir / "metricset.ext.json").read_text(encoding="utf-8"))

    report: dict = {
        "card": "T06-R2",
        "generated_at": "2026-10-26",
        "run_id": main_doc.get("run_id"),
        "event_count": ext_doc.get("event_count"),
        "params_digest": ext_doc.get("params_digest"),
        "canonical_sha256": ext_doc.get("canonical_sha256"),
        "params": MetricParams().as_dict(),
        "how_to_reproduce": (
            ".venv/Scripts/python.exe scripts/t06_r2_check.py --metrics runs/t06/metrics "
            "--db runs/t06/t06.db --run-id t06-codebuddy-primary --out probe/t06/R2-result.json"),
        "metrics_recomputed_with_current_code": refresh,
        "naming_check": r1.naming_check(main_doc, ext_doc),
        "key_count_check": key_count(main_doc, ext_doc),
        "drilldown": r1.drilldown(main_doc, ext_doc, db_path, args.run_id),
        "confidence_audit": confidence_audit(main_doc, db_path, args.run_id),
        "redundant_loop_check": redundant_loop_check(main_doc),
        "signature_source": signature_source_section(db_path, args.run_id),
        "signature_audit": r1.signature_audit(db_path, args.run_id),
        "identity_check": r1.identity_report(ext_doc),
        "gates": {
            "schema": r1.schema_gate(main_doc),
            "privacy": r1.privacy_gate(metrics_dir, db_path, args.run_id),
            "packages_schema_untouched": r1.schema_diff_gate(),
            "pytest": {"skipped": True} if args.skip_pytest else pytest_gate(),
        },
        "decisions": {
            "R2-1_confidence_by_source_level": {
                "ruling": ("推翻 R1-5 对 retry_rate / redundant_loop 记 high 的裁定。confidence 按签名"
                           "来源等级动态判定：原生（content_sha256 去重取值数 > 1 且 proxy == 0）→ 叠加"
                           "样本量阈值取最低者；代理（去重取值数 == 1）→ low；键缺失（去重取值数 == 0）"
                           "→ 不得判代理，走未知分支 low + note「签名来源未采集」；混合 → low；"
                           "上界/派生 → 恒 low；降级 → null。样本量回答「够不够」，来源等级回答「准不准」"),
                "why_not_hardcode": ("写死 low 等于把「现在测不准」固化成「永远测不准」——T05 v2 补到真实 "
                                     "content 后签名自动从代理变回原生，这两个指标必须自动升 high"),
                "implementation_trap": ("_usable_hash() 对「键缺失」和「值为空常量」都返回 None，而 "
                                        "empty_own 只统计后者，两者不是补集；禁止 native = total - empty_own "
                                        "反推，原生的正向条件是 proxy == 0。反例单测 "
                                        "test_native_signature_count_ignores_missing_key 拦截"),
            },
        },
        "r2_items": R2_ITEMS,
        "open_questions": OPEN_QUESTIONS,
    }
    items = {k: report[k]["ok"] for k in
             ("naming_check", "key_count_check", "drilldown", "identity_check",
              "confidence_audit", "redundant_loop_check", "signature_audit",
              "signature_source")}
    gates = {k: v["ok"] for k, v in report["gates"].items() if "ok" in v}
    report["r2_items_ok"] = items
    report["gates_ok"] = gates
    report["ok"] = all(items.values()) and all(gates.values()) and refresh["ok"]

    out = (REPO / args.out) if not Path(args.out).is_absolute() else Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("ok", "r2_items_ok", "gates_ok")},
                     ensure_ascii=False, indent=2))
    print(f"written: {out}")
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

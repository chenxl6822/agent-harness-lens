"""QA 独立复核脚本：G2 / G5 / G6 / G10 / G13 + CLI 退出码 + ext 命名合规性。

对真实产物做事后复核（不信任实现方自报值：canonical_sha256 按 docs/metrics-engine.md §3
的定义自己独立实现一遍再比对）。
运行：.venv/Scripts/python.exe docs/evidence/T06/qa/qa_blackbox.py
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
assert (ROOT / "engine" / "hs_core").is_dir(), f"ROOT 推导错误: {ROOT}"
PY = ROOT / ".venv" / "Scripts" / "python.exe"
TMP = ROOT / ".tmp-qa"
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def my_canonical_sha256(path: Path) -> str:
    """按 docs/metrics-engine.md §3 独立实现一遍（不调用 hs_core 的任何函数）。"""
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop("computed_at", None)
    payload["groups"] = {k: sorted(v, key=lambda m: m["key"])
                         for k, v in payload["groups"].items()}
    payload["degraded_metrics"] = sorted(payload["degraded_metrics"])
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def g6_reproducibility(db: Path, run_id: str) -> None:
    print("\n== G6 可复现（双向断言）==")
    outs = []
    for i in (1, 2):
        out = TMP / f"repro{i}"
        proc = subprocess.run(
            [str(PY), "-m", "hs_core.cli", "metrics", "run", "--run-id", run_id,
             "--db", str(db), "--out-dir", str(out)],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        outs.append((out, proc))
    (o1, p1), (o2, p2) = outs
    if p1.returncode or p2.returncode:
        check("G6-0 CLI 两次都成功", False, f"exit {p1.returncode}/{p2.returncode}")
        return
    f1, f2 = o1 / "metricset.json", o2 / "metricset.json"
    e1 = json.loads((o1 / "metricset.ext.json").read_text(encoding="utf-8"))
    e2 = json.loads((o2 / "metricset.ext.json").read_text(encoding="utf-8"))
    engine1, engine2 = e1["canonical_sha256"], e2["canonical_sha256"]
    mine1, mine2 = my_canonical_sha256(f1), my_canonical_sha256(f2)
    b1, b2 = file_sha256(f1), file_sha256(f2)
    print(f"    引擎报 canonical : {engine1}")
    print(f"    QA 独立复算      : {mine1}")
    check("G6-① 两次 canonical_sha256 相同", engine1 == engine2, f"{engine1[:24]}… == {engine2[:24]}…")
    check("G6-①b QA 独立实现复算与引擎一致", mine1 == engine1 and mine2 == engine2,
          "按 docs §3 定义独立实现，逐 bit 一致")
    check("G6-② 文件字节 sha256 必须不同", b1 != b2,
          f"文件1 {b1[:24]}… vs 文件2 {b2[:24]}…（证明 computed_at 确实在变）")
    at1 = json.loads(f1.read_text(encoding="utf-8"))["computed_at"]
    at2 = json.loads(f2.read_text(encoding="utf-8"))["computed_at"]
    check("G6-②b 差异确实来自 computed_at", at1 != at2, f"{at1} != {at2}")
    raw = f1.read_text(encoding="utf-8") + (o1 / "metricset.ext.json").read_text(encoding="utf-8")
    nan_hits = len(re.findall(r"\b(NaN|-?Infinity)\b", raw))
    check("G6-③ NaN/Infinity 命中 0", nan_hits == 0, f"命中 {nan_hits} 次")
    # 负向对照：把整个对象（含 computed_at）塞进 canonical 会得到不同值 → 证明 canonical 确实排除了它
    whole = json.dumps(json.loads(f1.read_text(encoding="utf-8")),
                       ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    whole_sha = "sha256:" + hashlib.sha256(whole.encode("utf-8")).hexdigest()
    check("G6-④ 含 computed_at 的整体摘要 ≠ canonical_sha256", whole_sha != engine1,
          "证明 canonical 确实排除了 computed_at（不是碰巧相同）")


def g5_drilldown(db: Path, metricset: Path, run_id: str) -> None:
    print("\n== G5 可下钻（每条 event_id 必须带 run_id 命中库）==")
    doc = json.loads(metricset.read_text(encoding="utf-8"))
    con = sqlite3.connect(str(db))
    cur = con.cursor()
    total = miss = 0
    non_null = 0
    for group in doc["groups"].values():
        for mv in group:
            if mv.get("value") is None:
                if mv["evidence"]:
                    check("G5-0 降级项 evidence 必须为空", False, f"{mv['key']} evidence 非空")
                continue
            non_null += 1
            if not mv["evidence"]:
                check(f"G5-1 {mv['key']} 非空值必须有证据", False, "evidence=[]")
                continue
            for ref in mv["evidence"]:
                if not ref.get("event_ids"):
                    check(f"G5-2 {mv['key']} EvidenceRef.event_ids 非空", False, "空 event_ids")
                for eid in ref["event_ids"]:
                    total += 1
                    row = cur.execute(
                        "SELECT 1 FROM events WHERE run_id=? AND event_id=?",
                        (run_id, eid)).fetchone()
                    if row is None:
                        miss += 1
    con.close()
    print(f"    非空指标 {non_null} 个，event_id 合计 {total} 条，未命中 {miss} 条")
    check("G5-3 全部 event_id 命中库", miss == 0, f"未命中 {miss}/{total}")

    # 反向不能丢：同一 event_id 不带 run_id 在其他 run 也存在 → 证明必须带 run_id
    con = sqlite3.connect(str(db))
    dup = con.execute(
        "SELECT (SELECT COUNT(DISTINCT run_id) FROM events e2 WHERE e2.event_id = e.event_id) AS n "
        "FROM events e WHERE e.run_id=? LIMIT 1", (run_id,)).fetchone()
    con.close()
    print(f"    （同一 db 内该 run 的事件跨 run 复用计数上限={dup[0] if dup else 'n/a'}）")


def g2_schema_conformance(metricset: Path) -> None:
    print("\n== G2 schema 合规 ==")
    proc = subprocess.run(
        [str(PY), "-m", "hs_core.cli", "schema", "check", "--kind", "metric-set",
         str(metricset)], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    check("G2-1 hs schema check --kind metric-set exit 0", proc.returncode == 0,
          (proc.stdout + proc.stderr).strip()[:120])
    dif = subprocess.run(["git", "diff", "--stat", "packages/schema/"], cwd=ROOT,
                         capture_output=True, text=True, encoding="utf-8")
    check("G2-2 git diff packages/schema/ 为空", dif.stdout.strip() == "",
          f"diff={dif.stdout.strip()!r}")


def g10_privacy(*paths: Path) -> None:
    print("\n== G10 隐私红线 ==")
    user = getpass.getuser()
    home = str(Path.home()).replace("\\", "/")
    home_win = str(Path.home())
    patterns = {
        "本机用户名": user,
        "Windows home 绝对路径": home_win,
        "POSIX home 绝对路径": home,
        "raw_ref 标记词": "raw_ref",
        "traces 路径片段": "/traces/",
    }
    for p in paths:
        text = p.read_text(encoding="utf-8")
        hits = {k: len(re.findall(re.escape(v), text, flags=re.I)) for k, v in patterns.items()}
        bad = {k: v for k, v in hits.items() if v > 0}
        check(f"G10 {p.name} 命中 0", not bad,
              (f"命中 {bad}" if bad else f"扫描 {len(text)} 字符，0 命中"))


def ext_naming(ext: dict) -> None:
    print("\n== ext 命名合规（架构师/主理人裁定：ext 侧必须叫 degraded_reasons）==")
    has_dm = "degraded_metrics" in ext
    has_dr = "degraded_reasons" in ext
    dm_type = type(ext.get("degraded_metrics")).__name__
    dr_type = type(ext.get("degraded_reasons")).__name__
    print(f"    ext['degraded_metrics'] 存在={has_dm} 类型={dm_type}")
    print(f"    ext['degraded_reasons'] 存在={has_dr} 类型={dr_type}")
    check("命名裁定：ext 不得出现名为 degraded_metrics 的映射",
          not (has_dm and isinstance(ext.get("degraded_metrics"), dict)),
          f"实际 ext['degraded_metrics'] 是 {dm_type}"
          f"（裁定要求 ext 侧因字典一律用 degraded_reasons）")
    check("命名裁定：ext['degraded_reasons'] 必须是 dict", has_dr and dr_type == "dict",
          f"实际 {dr_type}")


def g13_degraded_honesty(ext: dict) -> None:
    print("\n== G13 降级诚实（9 项，reason 与 PRD §4 对齐）==")
    expected = {
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
    reasons = ext.get("degraded_reasons") or ext.get("degraded_metrics") or {}
    check("G13-0 降级项数 = 9", len(reasons) == 9, f"实际 {len(reasons)} 项：{sorted(reasons)}")
    for key, want in expected.items():
        got = reasons.get(key, {}).get("reason")
        check(f"G13-1 {key}", got == want, f"期望 {want}，实测 {got}")
    missing_detail = [k for k, v in reasons.items() if not (v or {}).get("detail")]
    check("G13-2 每项都有非空 detail", not missing_detail, f"缺 detail：{missing_detail}")


def cli_exit_codes(db: Path, run_id: str) -> None:
    print("\n== CLI 退出码约定 ==")
    ok_proc = subprocess.run(
        [str(PY), "-m", "hs_core.cli", "metrics", "run", "--run-id", run_id, "--db", str(db),
         "--out-dir", str(TMP / "exit0")], cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8")
    check("primary_only → exit 0", ok_proc.returncode == 0, f"exit={ok_proc.returncode}")
    merge_db = TMP / "qa-g3.db"
    if merge_db.exists():
        m = subprocess.run(
            [str(PY), "-m", "hs_core.cli", "metrics", "run", "--run-id", "qa-merge-run",
             "--db", str(merge_db), "--out-dir", str(TMP / "exit2")],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        check("merge run → exit 2", m.returncode == 2, f"exit={m.returncode}")
    nf = subprocess.run(
        [str(PY), "-m", "hs_core.cli", "metrics", "run", "--run-id", "__no_such_run__",
         "--db", str(db), "--out-dir", str(TMP / "exitnf")],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    check("不存在的 run → 非 0 且有明确报错", nf.returncode != 0,
          f"exit={nf.returncode} stderr={(nf.stderr or nf.stdout).strip()[:80]}")
    lst = subprocess.run([str(PY), "-m", "hs_core.cli", "metrics", "list"], cwd=ROOT,
                         capture_output=True, text=True, encoding="utf-8")
    check("hs metrics list exit 0 且列 13 项", lst.returncode == 0
          and lst.stdout.count("cost.") + lst.stdout.count("process.")
          + lst.stdout.count("result.") == 13,
          f"exit={lst.returncode}，输出行数 {len(lst.stdout.splitlines())}")


def g1_registry_pluggable(db: Path, run_id: str) -> None:
    print("\n== G1 注册器可插拔（第三方模块 --extra-module）==")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent) + os.pathsep + env.get(
        "PYTHONPATH", "")
    lst = subprocess.run(
        [str(PY), "-m", "hs_core.cli", "metrics", "list"], cwd=ROOT, capture_output=True,
        text=True, encoding="utf-8", env=env)
    probe_in_default = "process.qa_probe_metric" in lst.stdout
    check("G1-0 未加载时 list 不含探针（证明不是硬编码进注册表）", not probe_in_default,
          f"默认 list 命中={probe_in_default}")
    out = TMP / "g1probe"
    res = subprocess.run(
        [str(PY), "-m", "hs_core.cli", "metrics", "run", "--run-id", run_id, "--db", str(db),
         "--out-dir", str(out), "--extra-module", "qa_ext_probe"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", env=env)
    check("G1-1 --extra-module 计算成功", res.returncode == 0,
          f"exit={res.returncode} {(res.stderr or '')[:120]}")
    if res.returncode == 0:
        summary = json.loads(res.stdout.strip().splitlines()[-1])
        check("G1-2 第三方指标被装配并出值",
              "process.qa_probe_metric" in summary.get("computed_keys", []),
              f"computed_keys={summary.get('computed_keys')}")
        doc = json.loads((out / "metricset.json").read_text(encoding="utf-8"))
        hit = [mv for g in doc["groups"].values() for mv in g
               if mv["key"] == "process.qa_probe_metric"]
        check("G1-3 第三方指标落进 schemas 合规主文件",
              bool(hit) and hit[0]["label_zh"] == "QA 探针（第三方注册）",
              f"命中={bool(hit)}")


def main() -> int:
    TMP.mkdir(parents=True, exist_ok=True)
    db = TMP / "qa-t06.db"
    run_id = "qa-t06-primary"
    if not db.exists():
        print(f"缺少 QA 自摄入库 {db}，请先跑 ingest")
        return 2
    print("QA T06 黑盒复核（真实数据产物）")
    print(f"  db={db.relative_to(ROOT)} run_id={run_id}")
    mdir = TMP / "final"
    proc = subprocess.run(
        [str(PY), "-m", "hs_core.cli", "metrics", "run", "--run-id", run_id, "--db", str(db),
         "--out-dir", str(mdir)], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    if proc.returncode:
        print(proc.stdout, proc.stderr)
        return 2
    metricset, ext_path = mdir / "metricset.json", mdir / "metricset.ext.json"
    ext = json.loads(ext_path.read_text(encoding="utf-8"))
    g2_schema_conformance(metricset)
    g5_drilldown(db, metricset, run_id)
    g6_reproducibility(db, run_id)
    g10_privacy(metricset, ext_path)
    ext_naming(ext)
    g13_degraded_honesty(ext)
    g1_registry_pluggable(db, run_id)
    cli_exit_codes(db, run_id)
    print("\n" + "=" * 60)
    print(json.dumps({"total_failures": len(FAILURES), "failures": FAILURES},
                     ensure_ascii=False, indent=2))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())

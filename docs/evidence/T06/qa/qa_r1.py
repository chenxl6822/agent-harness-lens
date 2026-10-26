"""QA 补验：对照 `taskcards/T06-R1-返工单.md` 的 R1-1 / R1-2 / R1-3 / R1-5 逐条跑一遍。

R1-1 step_count 口径必须 == count(type=='step.start')，且不得被其它事件类型干扰
R1-2 13 key = 4 可算 + 9 降级，且无 turn_count；null 数 == degraded_metrics 条数
R1-3 ext 不得出现 degraded_metrics；主文件不得出现 degraded_reasons
R1-5 反向断言：任何 value != null 且 confidence == null 即失败
R1-6 签名来源（native/proxy）计数：不得用 total - empty_own 反推 native
运行：.venv/Scripts/python.exe docs/evidence/T06/qa/qa_r1.py
"""

from __future__ import annotations

import hashlib
import json
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


def r1_1_step_count(db: Path, run_id: str, mdir: Path) -> None:
    print("\n== R1-1 step_count 口径锁定 ==")
    con = sqlite3.connect(str(db))
    n_start = con.execute(
        "SELECT COUNT(*) FROM events WHERE run_id=? AND type='step.start'", (run_id,)).fetchone()[0]
    n_end = con.execute(
        "SELECT COUNT(*) FROM events WHERE run_id=? AND type='step.end'", (run_id,)).fetchone()[0]
    n_sess = con.execute(
        "SELECT COUNT(*) FROM events WHERE run_id=? AND type='session.start'", (run_id,)).fetchone()[0]
    con.close()
    doc = json.loads((mdir / "metricset.json").read_text(encoding="utf-8"))
    got = [m["value"] for g in doc["groups"].values() for m in g
           if m["key"] == "process.step_count"][0]
    print(f"    库内 step.start={n_start} step.end={n_end} session.start={n_sess}；产物={got}")
    check("R1-1 step_count == count(step.start)", got == n_start, f"{got} == {n_start}")
    check("R1-1b 未被 session.start 误用", got != n_sess,
          f"session.start={n_sess}（若相等说明口径被污染）")
    note = json.loads((mdir / "metricset.ext.json").read_text(encoding="utf-8"))[
        "notes"]["process.step_count"]
    check("R1-1c note 写明不读 payload", "step.start" in note and "不读 payload" in note,
          f"note={note[:80]}…")


def r1_2_key_self_consistency(mdir: Path) -> None:
    print("\n== R1-2 降级清单口径自洽 ==")
    doc = json.loads((mdir / "metricset.json").read_text(encoding="utf-8"))
    ext = json.loads((mdir / "metricset.ext.json").read_text(encoding="utf-8"))
    all_mv = [m for g in doc["groups"].values() for m in g]
    computed = sorted(m["key"] for m in all_mv if m["value"] is not None)
    degraded = sorted(m["key"] for m in all_mv if m["value"] is None)
    dlist = doc["degraded_metrics"]
    print(f"    总 key {len(all_mv)}；可算 {len(computed)}；降级 {len(degraded)}")
    check("R1-2 key 总数 == 13", len(all_mv) == 13, f"实际 {len(all_mv)}")
    check("R1-2 可算 == 4", len(computed) == 4, f"{computed}")
    check("R1-2 降级 == 9", len(degraded) == 9, f"{degraded}")
    check("R1-2 degraded_metrics 条数 == null 指标数", sorted(dlist) == degraded,
          f"{len(dlist)} vs {len(degraded)}")
    check("R1-2b 无 turn_count 幽灵键", "process.turn_count" not in computed + degraded,
          "零命中")
    check("R1-2c 三合一不变量：null ⟺ 进降级清单 ⟺ evidence==[]",
          all((m["value"] is None) == (m["key"] in dlist) == (m["evidence"] == [])
              for m in all_mv), "13/13 满足")
    reasons = ext.get("degraded_reasons") or {}
    check("R1-2d 降级清单与原因字典 key 集合一致",
          sorted(reasons) == degraded, f"{len(reasons)} vs {len(degraded)}")


def r1_3_naming(mdir: Path) -> None:
    print("\n== R1-3 双文件键名逐字检查 ==")
    doc = json.loads((mdir / "metricset.json").read_text(encoding="utf-8"))
    ext = json.loads((mdir / "metricset.ext.json").read_text(encoding="utf-8"))
    check("主文件 degraded_metrics 是 list[str]",
          isinstance(doc.get("degraded_metrics"), list)
          and all(isinstance(x, str) for x in doc["degraded_metrics"]),
          f"type={type(doc.get('degraded_metrics')).__name__}")
    check("主文件不得出现 degraded_reasons", "degraded_reasons" not in doc, "零命中")
    dr = ext.get("degraded_reasons")
    check("ext.degraded_reasons 是 dict 且含 reason/detail",
          isinstance(dr, dict) and all(
              isinstance(v, dict) and "reason" in v and "detail" in v for v in dr.values()),
          f"type={type(dr).__name__} n={len(dr) if isinstance(dr, dict) else '-'}")
    check("★ ext 不得再出现 degraded_metrics（R1-3 当前未满足）",
          "degraded_metrics" not in ext,
          f"ext 键 = {sorted(ext)}；存在映射型 degraded_metrics → 判 FAIL")


def r1_5_confidence(mdir: Path, run_id: str) -> None:
    print("\n== R1-5 confidence 反向断言 ==")
    doc = json.loads((mdir / "metricset.json").read_text(encoding="utf-8"))
    all_mv = [m for g in doc["groups"].values() for m in g]
    want = {
        "process.step_count": "high",        # 2567/3352 直接观测，非派生
        "process.retry_rate": "high",        # 分母 = tool.call 数 ≥ 20
        "process.redundant_loop": "high",    # 参与判定调用数 ≥ 20
        "process.tool_correctness": "low",   # deny 上界口径 → 恒 low
    }
    bad = []
    for m in all_mv:
        if m["value"] is None:
            if m.get("confidence") is not None:
                bad.append((m["key"], "降级项却填了 confidence", m.get("confidence")))
            continue
        checked_conf = m.get("confidence")
        expect = want.get(m["key"])
        if checked_conf is None:
            bad.append((m["key"], "value 非 null 却 confidence=null", None))
        elif checked_conf != expect:
            bad.append((m["key"], f"应为 {expect}", checked_conf))
    for row in bad:
        print(f"    违规：{row}")
    check("★ R1-5 反向断言：0 命中 value!=null && confidence==null", not bad,
          f"违规 {len(bad)} 项" + ("（当前实现未改，符合预期失败）" if bad else ""))
    for key, expect in want.items():
        got = [m.get("confidence") for m in all_mv if m["key"] == key][0]
        ok = got == expect
        check(f"R1-5 {key}", ok, f"应 {expect!r} / 实测 {got!r}")


def _count_native(calls: list[dict]) -> int:
    """正确的 native 计数（= 调用自身携带真实入参哈希的条数）。

    `_usable_hash()` 对「键缺失」和「== 空常量哈希」都返回 None，而 `empty_own`
    只统计后者 ⇒ `total - empty_own` **不等于** native。此函数即是正确写法。
    """
    from hs_core.metrics_core import _EMPTY_CONTENT_SHA
    return sum(1 for c in calls
               if isinstance(c["payload"].get("content_sha256"), str)
               and c["payload"]["content_sha256"] not in ("", _EMPTY_CONTENT_SHA))


def r1_6_signature_provenance() -> None:
    """R1-6 预置探针：native 计数必须处理「键缺失」这一支。

    三种形态各造一条 tool.call：
      A 键缺失 + 有 span_end 兄弟哈希   → 代理（naive 公式会误判原生）
      B 键存在但为空常量 + 有兄弟哈希   → 代理
      C 键存在且为真哈希               → 原生
    """
    print("\n== R1-6 签名来源计数（native 不得用 total - empty_own 反推）==")
    from hs_core.metrics_core import _EMPTY_CONTENT_SHA, _call_signatures

    real = hashlib.sha256(b"qa-real-arg-bytes").hexdigest()
    sib = hashlib.sha256(b"qa-sibling-result-bytes").hexdigest()
    calls = [
        {"event_id": "cA", "seq": 1, "type": "tool.call",
         "payload": {"tool_name": "Read", "span_id": "SA"}},                       # A 键缺失
        {"event_id": "cB", "seq": 2, "type": "tool.call",
         "payload": {"tool_name": "Read", "span_id": "SB",
                     "content_sha256": _EMPTY_CONTENT_SHA}},                        # B 空常量
        {"event_id": "cC", "seq": 3, "type": "tool.call",
         "payload": {"tool_name": "Read", "span_id": "SC",
                     "content_sha256": real}},                                      # C 真哈希
    ]
    events = calls + [
        {"event_id": "rA", "seq": 4, "type": "tool.result",
         "payload": {"span_id": "SA", "content_sha256": sib}},
        {"event_id": "rB", "seq": 5, "type": "tool.result",
         "payload": {"span_id": "SB", "content_sha256": sib}},
    ]
    sigs, joined, empty_own = _call_signatures(calls, events)
    total = len(calls)
    usable = sum(1 for s in sigs if s is not None)
    native_correct = _count_native(calls)
    native_naive = total - empty_own          # ← 实现方容易这么写（错的）
    proxy = usable - native_correct

    print(f"    A 键缺失(代理) / B 空常量(代理) / C 真哈希(原生)")
    print(f"    joined(补齐)={joined}  empty_own={empty_own}  可用签名={usable}/{total}")
    print(f"    正确 native = {native_correct}（期望 1：只有 C）")
    print(f"    朴素 native = total - empty_own = {native_naive}（期望 1 才是巧合）")
    check("R1-6a 正确算法 native == 1", native_correct == 1, f"实测 {native_correct}")
    check("R1-6b 朴素算法在本用例上确实算错（证明不是理论问题）",
          native_naive != native_correct,
          f"朴素={native_naive} vs 正确={native_correct}"
          f"（键缺失那条被误判为原生）")
    check("R1-6c proxy = 可用签名 - native", proxy == 2, f"实测 proxy={proxy}（期望 2）")
    check("R1-6d 判定须用正向条件 proxy==0（本用例 proxy>0 ⇒ 恒 low）",
          proxy > 0, f"proxy={proxy} > 0 ⇒ confidence 应为 low")

    # 为什么当前快照看不到这个 bug：数据齐时 empty_own + native == total 碰巧成立
    con = sqlite3.connect(str(TMP / "qa-t06.db"))
    rows = con.execute(
        "SELECT payload FROM events WHERE run_id='qa-t06-primary' AND type='tool.call'").fetchall()
    con.close()
    pl = [json.loads(p) for (p,) in rows]
    has_key = sum(1 for r in pl if "content_sha256" in r)
    n_empty = sum(1 for r in pl if r.get("content_sha256") == _EMPTY_CONTENT_SHA)
    n_native = sum(1 for r in pl if isinstance(r.get("content_sha256"), str)
                   and r["content_sha256"] not in ("", _EMPTY_CONTENT_SHA))
    missing = len(pl) - has_key
    print(f"    【当前 QA 快照】tool.call {len(pl)} 条：有键 {has_key} / 键缺失 {missing}"
          f" / 空常量 {n_empty} / 原生 {n_native}")
    check("R1-6e 当前快照键缺失为 0（故两算法等价，bug 潜伏）",
          missing == 0 and n_native == 0,
          f"missing={missing} native={n_native} → empty_own+native==total 碰巧成立，"
          "实跑无法暴露，必须靠单测")


def main() -> int:
    TMP.mkdir(parents=True, exist_ok=True)
    print("QA 补验：T06-R1 返工单对照")
    for label, db, run_id in (("QA 自摄快照", TMP / "qa-t06.db", "qa-t06-primary"),
                              ("实现方快照", ROOT / "runs" / "t06" / "t06.db",
                               "t06-codebuddy-primary")):
        if not db.exists():
            print(f"\n### 跳过 {label}（{db.name} 不存在）")
            continue
        print(f"\n### {label}：run_id={run_id}")
        mdir = TMP / f"r1_{run_id}"
        proc = subprocess.run(
            [str(PY), "-m", "hs_core.cli", "metrics", "run", "--run-id", run_id,
             "--db", str(db), "--out-dir", str(mdir)],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        if proc.returncode:
            print(f"  metrics run 失败 exit={proc.returncode}")
            FAILURES.append(f"{label}: metrics run exit {proc.returncode}")
            continue
        r1_1_step_count(db, run_id, mdir)
        r1_2_key_self_consistency(mdir)
        r1_3_naming(mdir)
        r1_5_confidence(mdir, run_id)
        r1_6_signature_provenance()
    print("\n" + "=" * 60)
    print(json.dumps({"total_failures": len(FAILURES), "failures": FAILURES},
                     ensure_ascii=False, indent=2))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())

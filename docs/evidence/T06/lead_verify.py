"""验收方独立复核脚本（T06-R2 收口）。

与执行方自带的 `scripts/t06_r2_check.py` **无关**：那份是执行方写的，按项目铁律
「验收方不采信执行方自报数字」，必须另写一份，且要包含**反事实对照**——只断言
「现在是 low」证明不了「判定是动态的」，必须证明换个输入会升档。

跑法：
    env -u NODE_OPTIONS .venv/Scripts/python.exe docs/evidence/T06/lead_verify.py
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "engine"))

from hs_core import metrics_core as mc  # noqa: E402
from hs_core.metrics import MetricParams, canonical_sha256, load_builtin_metrics  # noqa: E402

OUT: dict = {}
EMPTY = hashlib.sha256(b"").hexdigest()


_SEQ = {"n": 0}


def call(name: str, sha: str | None, span: str | None = None) -> dict:
    _SEQ["n"] += 1
    payload = {"tool_name": name}
    if sha is not None:
        payload["content_sha256"] = sha
    if span is not None:
        payload["span_id"] = span
    return {
        "type": "tool.call", "seq": _SEQ["n"], "event_id": f"c{_SEQ['n']}",
        "ts": f"2026-10-26T00:00:{_SEQ['n'] % 60:02d}Z", "payload": payload,
    }


def result(span: str, sha: str) -> dict:
    _SEQ["n"] += 1
    return {
        "type": "tool.result", "seq": _SEQ["n"], "event_id": f"r{_SEQ['n']}",
        "ts": f"2026-10-26T00:01:{_SEQ['n'] % 60:02d}Z",
        "payload": {"span_id": span, "content_sha256": sha},
    }


def confidence_case() -> dict:
    """四个输入形态 → 档位。原生必须能升到 high，代理必须 low 并注明来源。"""
    params = MetricParams()
    cases = {}

    native = [call("Read", f"{i:064x}") for i in range(1, 26)]
    sigs, proxy, empty_own, nat = mc._call_signatures(native, native)
    conf, note = mc._signature_confidence(native, proxy, nat, params)
    cases["native_distinct"] = {
        "n": len(native), "distinct": mc._signature_distinct(native),
        "proxy": proxy, "native": nat, "confidence": conf, "note": note[:60],
        "expect": "high",
    }

    proxy_calls = [call("Read", EMPTY, span=f"s{i}") for i in range(25)]
    sigs, proxy, empty_own, nat = mc._call_signatures(proxy_calls, proxy_calls)
    conf, note = mc._signature_confidence(proxy_calls, proxy, nat, params)
    cases["proxy_all_empty"] = {
        "n": len(proxy_calls), "distinct": mc._signature_distinct(proxy_calls),
        "proxy": proxy, "native": nat, "confidence": conf, "note": note[:60],
        "expect": "low",
    }

    missing = [call("Read", None, span=f"s{i}") for i in range(25)]
    sigs, proxy, empty_own, nat = mc._call_signatures(missing, missing)
    conf, note = mc._signature_confidence(missing, proxy, nat, params)
    cases["key_missing"] = {
        "n": len(missing), "distinct": mc._signature_distinct(missing),
        "proxy": proxy, "native": nat, "confidence": conf, "note": note,
        "expect": "low",
        # 「不得据此判为代理」是正确措辞，判据要看它**有没有被判成代理**，而不是字面出现「代理」二字
        "judged_as_proxy": "来源=代理" in note,
        "says_unobserved": "未采集" in note,
    }

    mixed = [call("Read", f"{i:064x}") for i in range(1, 13)] + [
        call("Read", EMPTY, span=f"m{i}") for i in range(13)
    ]
    events = mixed + [result(f"m{i}", f"{i+999:064x}") for i in range(13)]
    sigs, proxy, empty_own, nat = mc._call_signatures(mixed, events)
    conf, note = mc._signature_confidence(mixed, proxy, nat, params)
    cases["mixed"] = {
        "n": len(mixed), "distinct": mc._signature_distinct(mixed),
        "proxy": proxy, "native": nat, "confidence": conf, "note": note[:60],
        "expect": "low",
    }
    return cases


def loop_counterfactual() -> dict:
    """反事实：把签名打塌成 (tool_name, "") 后 redundant_loop 是否爆炸。

    如果 ≤50 这条反向断言在两种签名下都成立，那它就等于没写。
    """
    params = MetricParams()
    calls = []
    events = []
    for i in range(120):  # 120 次调用，两两不同结果哈希但工具名相同
        calls.append(call("Read", EMPTY, span=f"x{i}"))
        events.append(result(f"x{i}", f"{i+1:064x}"))
    all_ev = calls + events

    real = mc.redundant_loop(all_ev, params, None) if _arity3() else mc.redundant_loop(all_ev, params)
    real_val = real.value

    orig = mc._call_signatures

    def collapsed(c, e):
        sigs, proxy, empty_own, nat = orig(c, e)
        return [("Read", "") if s is not None else None for s in sigs], proxy, empty_own, nat

    mc._call_signatures = collapsed
    try:
        fake = mc.redundant_loop(all_ev, params, None) if _arity3() else mc.redundant_loop(all_ev, params)
    finally:
        mc._call_signatures = orig
    return {"real_value": real_val, "collapsed_value": fake.value, "collapsed_note": (fake.note or "")[:120]}


def _arity3() -> bool:
    import inspect

    return len(inspect.signature(mc.redundant_loop).parameters) >= 3


def live_metricset() -> dict:
    """跑一次真实 run，检查档位、note、硬不变量。"""
    out_dir = REPO / ".tmp-lead-metrics"
    cmd = [
        str(REPO / ".venv" / "Scripts" / "python.exe"),
        "-m", "hs_core.cli", "metrics", "run",
        "--run-id", "t06-codebuddy-primary",
        "--db", str(REPO / "runs" / "t06" / "t06.db"),
        "--out-dir", str(out_dir),
    ]
    # 子进程的 rich 输出在 cp936 控制台下会吐 GBK 字节，`text=True` 的 reader 线程会抛
    # UnicodeDecodeError（本次真踩到）。摘要本来就落 md 文件，这里只要退出码，别去解 stdout。
    r = subprocess.run(
        cmd, cwd=str(REPO / "engine"),
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
        env={"SYSTEMROOT": "C:\\Windows", "PYTHONIOENCODING": "utf-8",
             "PYTHONPATH": str(REPO / "engine")},
    )
    if r.returncode != 0:
        return {"error": r.returncode, "stderr": (r.stderr or "")[-800:]}

    ms = json.loads((out_dir / "metricset.json").read_text(encoding="utf-8"))
    ext = json.loads((out_dir / "metricset.ext.json").read_text(encoding="utf-8"))

    # 教训：指标在 `groups.{result,process,collaboration,cost}` 四个 list 下，
    # 不在顶层。上一轮按顶层 `ms["metrics"]` 查，拿到「0 条」的假通过。
    values: dict[str, dict] = {}
    for group_name, items in (ms.get("groups") or {}).items():
        assert isinstance(items, list), f"groups.{group_name} 不是 list"
        for it in items:
            values[it["key"]] = it

    degraded = {k: True for k in (ms.get("degraded_metrics") or [])}
    # 硬不变量：value=null ⟺ in degraded ⟺ evidence=[]
    viol = []
    for key, it in values.items():
        is_null = it.get("value") is None
        in_deg = key in degraded
        no_ev = not (it.get("evidence") or [])
        if not (is_null == in_deg == no_ev):
            viol.append({"key": key, "null": is_null, "degraded": in_deg, "no_evidence": no_ev})

    # note 不在 MetricValue 里（schema `additionalProperties:false`），落在 ext.notes[key]。
    # 第二次踩同类坑：查错位置会得到「note 为空」的假阴性。
    notes = ext.get("notes") or {}
    sig_keys = ["process.retry_rate", "process.redundant_loop"]
    sig_report = {
        k: {
            "value": values[k].get("value"),
            "confidence": values[k].get("confidence"),
            "note_has_proxy": "来源=代理" in (notes.get(k) or ""),
            "note_has_span_end_result_hash": "span_end" in (notes.get(k) or "")
            and "结果内容哈希" in (notes.get(k) or ""),
            "note": (notes.get(k) or "")[:120],
        }
        for k in sig_keys
        if k in values
    }

    # G6 双向：文件字节 sha256 与 canonical 序列化 sha256 不是同一个东西
    return {
        "count": len(values),
        "degraded_n": len(degraded),
        "invariant_violations": viol,
        "signature_metrics": sig_report,
        "ext_keys": sorted(ext.keys()),
        "canonical_sha256_reported": ext.get("canonical_sha256"),
        "data_quality_flags": ext.get("data_quality_flags"),
        "channel_policy": ext.get("channel_policy"),
    }


def wording_check() -> dict:
    doc = (REPO / "docs" / "metrics-engine.md").read_text(encoding="utf-8")
    code_note = mc.SIGNATURE_SOURCE_NOTE
    tokens = ["span_id", "span_end", "结果内容哈希"]
    anchors = {
        "docs_5_6": "| 签名 |",
        "docs_5_7": "| 签名来源 |",
        "code_constant": code_note,
    }
    return {
        "code_constant": code_note,
        "code_constant_tokens": {t: t in code_note for t in tokens},
        "docs_has_span_id_span_end_result_hash": all(t in doc for t in tokens),
        "docs_occurrences": {t: doc.count(t) for t in tokens},
        "anchors": list(anchors),
    }


def main() -> int:
    load_builtin_metrics()
    OUT["confidence_counterfactual"] = confidence_case()
    OUT["loop_counterfactual"] = loop_counterfactual()
    OUT["live"] = live_metricset()
    OUT["wording"] = wording_check()

    cf = OUT["confidence_counterfactual"]
    km = cf["key_missing"]
    ok_conf = (
        all(v["confidence"] == v["expect"] for v in cf.values())
        and not km["judged_as_proxy"]      # 键缺失不得被判成代理
        and km["says_unobserved"]          # 且必须说清「未采集」
    )
    # 判别力要求：签名塌缩后必须**突破** ≤50 这条反向断言的边界，否则该断言等于没写
    ok_loop = OUT["loop_counterfactual"]["collapsed_value"] > 50
    live = OUT["live"]
    ok_live = "error" not in live and not live["invariant_violations"] and all(
        v["confidence"] == "low" and v["note_has_proxy"] and v["note_has_span_end_result_hash"]
        for v in live["signature_metrics"].values()
    )
    OUT["verdict"] = {
        "confidence_is_dynamic": ok_conf,
        "loop_bound_is_meaningful": ok_loop,
        "live_signature_metrics_low_with_reason": ok_live,
        "wording_consistent": OUT["wording"]["docs_has_span_id_span_end_result_hash"],
        "all_ok": ok_conf and ok_loop and ok_live,
    }
    print(json.dumps(OUT, ensure_ascii=False, indent=2))
    # 同时落盘：控制台重定向在本机会被 shim 干扰，证据以文件为准
    (REPO / "docs" / "evidence" / "T06" / "lead-independent-verify.json").write_text(
        json.dumps(OUT, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0 if OUT["verdict"]["all_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

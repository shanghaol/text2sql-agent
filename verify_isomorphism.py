"""W4 §6.2-1 同构闸门对照小脚本：三道固定题（真实检索）vs W3 明细逐元素全等。

对照基准：W3 明细 `eval/reports/detail_baseline_full_v2_20261010_161633.json`
（run_idx=1）；固定题 EV-003 / EV-017 / EV-029 覆盖陷阱 / 多表 / 口径。
单轮评测线程恒新 → retrieval_query == question（W3 §2.3 [2] 既定语义），
injected_chunks 依赖检索全链路确定性（W3 §6 风险 6：同 query 必同注入）——
不等即回归。

运行（在仓库根目录，需 Chroma 索引 + fastembed 本地模型，不连 MySQL 不耗 token）：
    .venv/Scripts/python.exe verify_isomorphism.py
"""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
W3_BASELINE = REPO_ROOT / "eval" / "reports" / "detail_baseline_full_v2_20261010_161633.json"
DATASET = REPO_ROOT / "eval" / "dataset" / "eval_v1.jsonl"
CASE_IDS = ("EV-003", "EV-017", "EV-029")


def main() -> None:
    detail = json.loads(W3_BASELINE.read_text(encoding="utf-8"))
    baseline = {c["id"]: c for c in detail["cases"] if c["id"] in CASE_IDS and c["run_idx"] == 1}
    dataset = {}
    for line in DATASET.read_text(encoding="utf-8").splitlines():
        case = json.loads(line)
        dataset[case["id"]] = case

    from agent.core.turn import prepare_turn

    all_ok = True
    for case_id in CASE_IDS:
        question = dataset[case_id]["question"]
        pt = prepare_turn(question, f"iso-gate-{case_id}")
        w3_chunks = baseline[case_id]["injected_chunks"]
        mine = [c.id for c in pt.injected_chunks]
        ok_query = pt.retrieval_query == question
        ok_chunks = mine == w3_chunks
        all_ok = all_ok and ok_query and ok_chunks
        print(f"[{case_id}] question={question}")
        print(f"  retrieval_query == question : {'OK' if ok_query else f'MISMATCH: {pt.retrieval_query!r}'}")
        print(f"  injected_chunks vs W3       : {'OK（逐元素相等）' if ok_chunks else 'MISMATCH'}")
        print(f"    W4  prepare_turn: {mine}")
        print(f"    W3  baseline    : {w3_chunks}")
    print("RESULT:", "PASS" if all_ok else "FAIL")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()

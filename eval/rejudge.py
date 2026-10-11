"""W6-T1 离线复判脚本（W6 设计 §1.3）：用判定器 v2.1 复判 W3 旧 detail JSON。

动机：v2.0 → v2.1 修了无序比较排序键的假阴性（judge.py 模块 docstring），W3 已
引用的基线数字（41.1% / 64.4%）是否被误判，必须复判后才知道——"评测器自己也要
被审计"。被否备选"重跑 W3 全量 LLM"：LLM 运行方差（W3 三 run 极差 20pp）会与
判定修复效应混叠，无法归因；离线复判把判定器这个单一变量干净隔离，且零 LLM 成本。

可行性（§8-4）：detail JSON 逐题存了 sql_calls（全序列）、sql_used、
final_answer_text（最后一条 AIMessage 全文）；ordered / standard_wrong / gold_sql
从数据集按 id join 回来。判定是"金标重放 + 模型 SQL 重放 + 文本"的确定性函数，
不需要 LLM 就能完整复现。

运行方式（仓库根目录，直接路径运行——与 verify_gold 同款 sys.path 处理）：
    .venv/Scripts/python.exe eval/rejudge.py eval/reports/detail_baseline_full_v2_20261010_161633.json
    .venv/Scripts/python.exe eval/rejudge.py eval/reports/detail_*.json   # 多份一并复判

产物：控制台前后对照 + 翻转清单；结果追加写 docs/rejudge_v2.1.md（翻转表 + 前后
汇总数字 + 成因点评）。退出码 0 = 跑完（是否有翻转都算成功——翻转是发现，不是故障）。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# 直接路径运行时 sys.path[0] 是 eval/ 目录，手动插入仓库根才能 import agent.*
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval.judge import judge_case, load_dataset, load_multiturn_dataset, pick_sql
from eval.run_eval import (
    Replayer,
    _fmt_pct,
    _pass_via_counts,
    _split_accuracy,
    _text_matched_str,
)

REPORTS_DIR = REPO_ROOT / "eval" / "reports"
DEFAULT_DATASET = REPO_ROOT / "eval" / "dataset" / "eval_v1.jsonl"
DEFAULT_MT_DATASET = REPO_ROOT / "eval" / "dataset" / "eval_v1_multiturn.jsonl"
REPORT_PATH = REPO_ROOT / "docs" / "rejudge_v2.1.md"

JUDGE_VERSION = "v2.1（无序比较排序键数值化分桶）"


# ---------------------------------------------------------------------------
# 复判核心：一条旧明细 → v2.1 新判定
# ---------------------------------------------------------------------------


def rejudge_record(
    record: dict,
    case: dict,
    replayer,
) -> dict:
    """对一条旧 detail 记录用 v2.1 判定器重判（与 run_eval._finalize_record 的判定
    路径同构：金标重放 → pick_sql 三级规则 → judge_case）。

    输入 record 消费三个字段：sql_calls（全序列，含被三级规则淘汰的失败调用）、
    final_answer_text、error_msg（invoke_exception 的原始子码回填，避免复判把
    "调用异常"误写成 no_usable_sql 的子码漂移）。返回 {"judge", "pass_via",
    "failure_mode", "error_msg", "text_matched", "sql_used", "sql_source"}。
    """
    gold_sql = case["final_gold_sql"] if "turns" in case else case["gold_sql"]
    gold_rows = replayer(gold_sql)
    if gold_rows is None:
        return {
            "judge": "error", "pass_via": None, "failure_mode": None,
            "error_msg": "replay_error", "text_matched": None,
            "sql_used": None, "sql_source": None,
        }

    used_sql, sql_source = pick_sql(record.get("sql_calls") or [], replayer)
    if used_sql is None:
        verdict = judge_case(
            gold_rows, None, error_msg="no_usable_sql",
            final_answer_text=record.get("final_answer_text"),
        )
    else:
        model_rows = replayer(used_sql)  # pick_sql 已保证非 None
        verdict = judge_case(
            gold_rows, model_rows,
            ordered=case["ordered"],
            standard_wrong=case["standard_wrong"],
            final_answer_text=record.get("final_answer_text"),
        )
    error_msg = verdict["error_msg"]
    if verdict["judge"] == "error" and record.get("error_msg") == "invoke_exception":
        # 原始调用异常的记录复判仍是 error，子码保留原始事实（复判不重跑 LLM）
        error_msg = "invoke_exception"
    return {
        "judge": verdict["judge"],
        "pass_via": verdict["pass_via"],
        "failure_mode": verdict["failure_mode"],
        "error_msg": error_msg,
        "text_matched": _text_matched_str(verdict["text_matched"]),
        "sql_used": used_sql,
        "sql_source": sql_source,
    }


def is_flip(old_record: dict, new_verdict: dict) -> bool:
    """翻转 = 影响汇总数字的判定变化：judge 档位或 pass_via 途径不同。
    failure_mode 的归类变化不动数字（fail 仍是 fail），不算翻转。"""
    return (
        old_record["judge"] != new_verdict["judge"]
        or old_record.get("pass_via") != new_verdict["pass_via"]
    )


def classify_flip_cause(old_record: dict, new_verdict: dict, case: dict) -> str:
    """翻转成因归类（复判报告的核心信息——归因错了数字就用错了）：

    - "replay_env"：三级规则取用了不同的 SQL（旧最后调用今次重放失败回退更早调用，
      或反之）——重放环境差异（如 W5 上线的 max_execution_time 超时防线），**不是
      判定器 v2.1 的效应**，不应计入判定器修复的归因；
    - "sort_key_fix"：取用同一 SQL、无序比较（ordered=False）从 fail 翻 pass——
      v2.1 排序键数值化修复的 v2.0 假阴性（设计 §1.1 的目标翻转）；
    - "text_fallback"：其余途径变化（文本回退命中/未命中的差异）。
    """
    if old_record.get("sql_used") != new_verdict.get("sql_used"):
        return "replay_env"
    if (
        case.get("ordered") is False
        and old_record["judge"] == "fail"
        and new_verdict["judge"] == "pass"
    ):
        return "sort_key_fix"
    return "text_fallback"


def _rows_preview(replayer, sql: str | None, max_rows: int = 4) -> str:
    """翻转表里的结果集预览：给人看的错位成因证据（str 排序 vs 数值序）。"""
    if sql is None:
        return "-"
    rows = replayer(sql)
    if rows is None:
        return "<重放失败>"
    def _cell(v) -> str:
        return str(v)
    parts = ["|".join(_cell(v) for v in row) for row in rows[:max_rows]]
    if len(rows) > max_rows:
        parts.append(f"…（共 {len(rows)} 行）")
    return "; ".join(parts)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def _summary(records: list[dict]) -> dict:
    """一列表 old-or-new 记录的汇总数字：与 run_eval 报告同口径（双层指标）。
    旧记录缺 pass_via 字段时 _pass_via_counts 按 SQL 级兜底（v1 兼容路径）。"""
    passed, failed, errored, answer_acc = _split_accuracy(records)
    sql_pass, text_pass = _pass_via_counts(records)
    return {
        "total": len(records),
        "sql_pass": sql_pass,
        "text_pass": text_pass,
        "fail": failed,
        "error": errored,
        "sql_acc": sql_pass / len(records) if records else 0.0,
        "answer_acc": answer_acc,
        "passed": passed,
    }


def _fmt_summary(label: str, s: dict) -> str:
    return (
        f"- {label}：SQL 级 {_fmt_pct(s['sql_acc'])}（{s['sql_pass']}/{s['total']}）｜ "
        f"答案级 {_fmt_pct(s['answer_acc'])}（pass {s['passed']} = sql {s['sql_pass']} + text "
        f"{s['text_pass']}｜fail {s['fail']}｜error {s['error']}）"
    )


def rejudge_file(detail_path: Path, cases_by_id: dict[str, dict], replayer) -> dict:
    """复判一份 detail JSON：返回 {summary_old, summary_new, flips, skipped}。"""
    data = json.loads(detail_path.read_text(encoding="utf-8"))
    old_records = data.get("cases", [])
    new_records: list[dict] = []
    flips: list[dict] = []
    skipped: list[str] = []
    for record in old_records:
        case = cases_by_id.get(record["id"])
        if case is None:
            skipped.append(record["id"])
            new_records.append(record)  # 数据集中已无此题（不应发生），原样保留不参与对比
            continue
        verdict = rejudge_record(record, case, replayer)
        new_records.append({**record, **verdict})
        if is_flip(record, verdict):
            cause = classify_flip_cause(record, verdict, case)
            flips.append(
                {
                    "id": record["id"],
                    "run_idx": record["run_idx"],
                    "old_judge": record["judge"],
                    "old_pass_via": record.get("pass_via"),
                    "new_judge": verdict["judge"],
                    "new_pass_via": verdict["pass_via"],
                    "ordered": case["ordered"],
                    "cause": cause,
                    "old_sql_used": record.get("sql_used"),
                    "new_sql_used": verdict["sql_used"],
                    "gold_rows": _rows_preview(replayer, case.get("gold_sql") or case.get("final_gold_sql")),
                    "model_rows": _rows_preview(replayer, verdict["sql_used"]),
                }
            )
    return {
        "path": detail_path,
        "tag": data.get("tag", detail_path.stem),
        "n": len(old_records),
        "old": _summary(old_records),
        "new": _summary(new_records),
        "flips": flips,
        "skipped": skipped,
    }


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("用法：python eval/rejudge.py <detail.json> [detail2.json ...]")
        return 1
    detail_paths = [Path(a) for a in args]
    for p in detail_paths:
        if not p.exists():
            print(f"[fatal] 明细文件不存在：{p}")
            return 1

    # 数据集按 id join（ordered / standard_wrong / gold_sql 的真源）；新旧数据集
    # 均可——旧 id 的金标不动，扩题只增不改
    singles = load_dataset(DEFAULT_DATASET)
    multis = load_multiturn_dataset(DEFAULT_MT_DATASET)
    cases_by_id = {c["id"]: c for c in singles + multis}
    print(f"[0] 数据集 join 面：单轮 {len(singles)} + 多轮 {len(multis)} 条")

    replayer = Replayer()
    results = [rejudge_file(p, cases_by_id, replayer) for p in detail_paths]

    # —— 控制台输出 ——
    report_lines: list[str] = []
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    report_lines.append(f"## 复判轮次：{stamp}（判定器 {JUDGE_VERSION}）\n")
    total_flips = 0
    sort_key_flips = 0
    for r in results:
        print(f"\n[{r['path'].name}] tag={r['tag']}，{r['n']} 次运行")
        print(f"  {_fmt_summary('旧判 v2.0', r['old'])}")
        print(f"  {_fmt_summary('新判 v2.1', r['new'])}")
        delta_sql = (r["new"]["sql_acc"] - r["old"]["sql_acc"]) * 100
        delta_ans = (r["new"]["answer_acc"] - r["old"]["answer_acc"]) * 100
        print(f"  Δ：SQL 级 {delta_sql:+.1f}pp ｜ 答案级 {delta_ans:+.1f}pp ｜ 翻转 {len(r['flips'])} 次")
        for f in r["flips"]:
            arrow = f"{f['old_judge']}{'/' + f['old_pass_via'] if f['old_pass_via'] else ''} → " \
                    f"{f['new_judge']}{'/' + f['new_pass_via'] if f['new_pass_via'] else ''}"
            print(f"    翻转 {f['id']} r{f['run_idx']}：{arrow}（成因：{f['cause']}）")
        if r["skipped"]:
            print(f"  ⚠ 数据集中找不到的 id（原样保留）：{r['skipped']}")

        report_lines.append(f"### {r['path'].name}（tag={r['tag']}，{r['n']} 次运行）\n")
        report_lines.append(_fmt_summary("旧判（v2.0，detail 原始判定）", r["old"]))
        report_lines.append(_fmt_summary("新判（v2.1，本次离线复判）", r["new"]))
        report_lines.append(
            f"- Δ：SQL 级 {delta_sql:+.1f}pp ｜ 答案级 {delta_ans:+.1f}pp ｜ 翻转 {len(r['flips'])} 次"
        )
        if r["flips"]:
            report_lines.append("")
            report_lines.append("| id | run | 旧判 | 新判 | ordered | 成因 | 金标结果集 | 模型结果集 |")
            report_lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
            for f in r["flips"]:
                report_lines.append(
                    f"| {f['id']} | r{f['run_idx']} | {f['old_judge']}"
                    f"{'（' + f['old_pass_via'] + '）' if f['old_pass_via'] else ''}"
                    f" | {f['new_judge']}"
                    f"{'（' + f['new_pass_via'] + '）' if f['new_pass_via'] else ''}"
                    f" | {f['ordered']} | {f['cause']} | {f['gold_rows']} | {f['model_rows']} |"
                )
        report_lines.append("")
        total_flips += len(r["flips"])
        sort_key_flips += sum(1 for f in r["flips"] if f["cause"] == "sort_key_fix")

    total_runs = sum(r["n"] for r in results)
    if total_flips == 0:
        verdict_line = (
            f"复判 {total_runs} 次运行零翻转——W3 判定数字在 v2.1 下稳健（零翻转本身是加分声明："
            "排序键修复未动摇既有结论，此前引用的基线数字无需改口）。"
        )
    elif sort_key_flips == 0:
        verdict_line = (
            f"复判 {total_runs} 次运行，翻转 {total_flips} 次，其中判定器 v2.1 修复效应（sort_key_fix）"
            f"为 {sort_key_flips} 次——全部翻转源于重放环境差异（replay_env：三级规则取用了不同 SQL），"
            "W3 判定数字在 v2.1 下依然稳健；环境差异逐条列于上表，成因点评见下。"
        )
    else:
        verdict_line = (
            f"复判 {total_runs} 次运行，翻转 {total_flips} 次（判定器修复效应 sort_key_fix "
            f"{sort_key_flips} 次、重放环境差异 replay_env {total_flips - sort_key_flips} 次）——"
            "W3 基线中被 v2.0 假阴性误判的题按复判结果诚实更新，experiment-log 与简历引用"
            "同步改为 v2.1 口径。"
        )
    print(f"\n[结论] {verdict_line}")
    report_lines.append(f"**结论**：{verdict_line}")

    # —— 追加写报告（留痕文档，每轮一个时间戳分节）——
    if not REPORT_PATH.exists():
        header = (
            "# W3 基线的判定器 v2.1 离线复判记录（rejudge_v2.1）\n\n"
            "> 本文件由 `eval/rejudge.py` 自动追加生成。复判不重跑 LLM：旧 detail 的 "
            "sql_calls/sql_used/final_answer_text + 数据集 join 回来的金标与 ordered，"
            "用 v2.1 判定器确定性重放（W6 设计 §1.3）。\n"
        )
        with open(REPORT_PATH, "w", encoding="utf-8") as fh:
            fh.write(header)
    with open(REPORT_PATH, "a", encoding="utf-8") as fh:
        fh.write("\n" + "\n".join(report_lines) + "\n")
    print(f"报告已追加：{REPORT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

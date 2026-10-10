"""W3-T1 金标核对脚本：评测集（单轮 30 + 多轮 4）的质量闸门（设计 §4.1/§4.2）。

职责：
1. schema 校验——缺字段 / question_type 非法枚举 / expected_chunks 含不存在的 chunk id
   （真实 id 集合从 chromadb collection.get() 拉取，不硬编码）/ 类型分布 6/7/7/5/5；
2. 金标逐条在 mart 库执行（sql_tools.get_engine()，text() 包裹，行数保护上限 5000），
   断言执行通过且结果非空，并核对命题预期的行数与关键值锚点；
3. TOP-N 有序题（EV-002/010/013/015/019/021）边界平局检查：
   LIMIT N 的第 N 名与第 N+1 名排序键相等 → 报警（有序判定会随并列顺序抖动，必须改题）；
4. 陷阱题标准错答核对：standard_wrong 的 value 与 qa_verification.md 已知值比对打勾；
   新实测项（EV-009 quantity 版、EV-029 orders 口径冲减）真库执行错答 SQL 后比对；
5. 追加写 docs/eval_gold_verification.md（仿 qa_verification.md：每条一行 ✓ 记录）。

运行命令（仓库根目录下，直接路径运行——脚本自行把仓库根加入 sys.path，
eval/ 无需 __init__.py，因此不用 -m 方式）：
    python eval/verify_gold.py

注意：数据是 --seed 42 固定种子的静态快照，核对一次终身有效；
**重导数据后必须重跑本脚本**（金标数值锚点会随数据失效）。
退出码：0 = 全部通过；1 = 存在问题行（详见控制台与核对记录）。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

# 直接路径运行时 sys.path[0] 是 eval/ 目录，手动插入仓库根才能 import agent.*
# （第三方与本项目 import 因此必须放在 sys.path 处理之后）
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import text

from agent.rag.chroma_store import get_collection
from agent.tools.sql_tools import get_engine

DATASET_PATH = REPO_ROOT / "eval" / "dataset" / "eval_v1.jsonl"
DATASET_MT_PATH = REPO_ROOT / "eval" / "dataset" / "eval_v1_multiturn.jsonl"
REPORT_PATH = REPO_ROOT / "docs" / "eval_gold_verification.md"

# 行数保护上限（设计 §2.4）：判定看逻辑结果集，但超大结果只留前 5000 行并标记 oversized
MAX_ROWS = 5000

# ---------------------------------------------------------------------------
# schema 常量（设计 §1.2 / §1.7）
# ---------------------------------------------------------------------------

VALID_QUESTION_TYPES = {
    "single_table",
    "multi_join",
    "aggregation",
    "time_window",
    "glossary_trap",
}
REQUIRED_FIELDS = [
    "id", "question", "question_type", "gold_sql",
    "ordered", "expected_chunks", "standard_wrong", "notes",
]
REQUIRED_FIELDS_MT = [
    "id", "turns", "final_gold_sql",
    "ordered", "expected_chunks", "standard_wrong", "notes",
]
# §1.1 的五类分配（T1 验收：类型分布恰为 6/7/7/5/5）
TYPE_DISTRIBUTION = {
    "single_table": 6, "multi_join": 7, "aggregation": 7,
    "time_window": 5, "glossary_trap": 5,
}

# ---------------------------------------------------------------------------
# 命题预期（来源：设计 §1.4 的预期量级 + docs/qa_verification.md 的 W2 实测锚点
# + 本 T1 于 2026-10-09 的真库实测回填）。核对不符 = 命题带病，列为问题。
# ---------------------------------------------------------------------------

# 金标行数预期：int = 精确行数；(min, max) = 区间（预期 ≈20 行的"全城市"类题用区间）
EXPECTED_ROW_COUNTS: dict[str, int | tuple[int, int]] = {
    "EV-001": 15, "EV-002": 5, "EV-003": 1, "EV-004": 1, "EV-005": 1,
    "EV-006": 1, "EV-007": 7, "EV-008": 1, "EV-009": 1, "EV-010": 3,
    "EV-011": 3, "EV-012": 1, "EV-013": 3, "EV-014": 6, "EV-015": 5,
    "EV-016": 20, "EV-017": 8, "EV-018": 20, "EV-019": 5,
    "EV-020": 1, "EV-021": 5, "EV-022": 3, "EV-023": 1, "EV-024": 20,
    "EV-025": 1, "EV-026": 7, "EV-027": 12, "EV-028": 1, "EV-029": 1, "EV-030": 1,
}

# 金标关键值锚点（仅单行结果题）：id -> (期望值字符串, 说明)。
# 带"T1 实测"标注的是本次核对实测回填值（qa_verification.md 里没有的）。
GOLD_VALUE_CHECKS: dict[str, tuple[str, str]] = {
    "EV-003": ("16029955.34", "2026-09 GMV（qa_verification 实测）"),
    "EV-004": ("0.0775", "退款率·金额口径（qa_verification 实测）"),
    "EV-005": ("4086", "数码类目女性用户数（qa_verification 实测）"),
    "EV-006": ("5639", "从未成功支付用户数（qa_verification 实测）"),
    "EV-008": ("2765.34", "2026-06 客单价（qa_verification 实测）"),
    "EV-009": ("0.6925", "2026-H1 复购率（qa_verification 实测）"),
    "EV-012": ("6053", "2025-H1 注册用户数（T1 实测；设计预估 ≈6,000）"),
    "EV-020": ("762553.89", "2026-04 美妆销售额（T1 实测，非空单行）"),
    "EV-028": ("5901", ("近 30 天支付笔数（T1 实测；设计预估 ≈7,200 偏高——"
                        "按真实支付转化率 9 月支付笔数即 5,901，单行命题不受影响）")),
    "EV-023": ("73.96", "下单用户占比（T1 实测：14,792/20,000，与 ≈74% 预期一致）"),
    "EV-025": ("77.50", "2025-11 环比（T1 实测；设计预估 ≈+73% 为量级参考）"),
    "EV-029": ("14460561.20", ("净销售额（T1 实测 = 16,029,955.34 − 1,569,394.14；"
                               "设计文档原写 14,787,925.19 系误引 QA-004 分子，见核对记录修正说明）")),
    "EV-030": ("0.0886", "退款率·订单口径（qa_verification + experiment-log 双背书）"),
    "MT-02": ("2749.39", "2026-07 客单价（T1 实测回填）"),
    "MT-03": ("0.0886", "订单口径退款率（同 EV-030）"),
    "MT-04": ("9.96", "银联占比（T1 实测回填：8,785/88,244，精确 9.9554）"),
}

# TOP-N 有序题边界平局检查（§4.1）：id -> (第 N 与第 N+1 名扩展 SQL, 排序键列下标, N)。
# 扩展 SQL = 金标去掉 LIMIT N、换成 LIMIT 2 OFFSET N-1，取回第 N/N+1 两行比排序键。
TIE_CHECKS: dict[str, tuple[str, int, int]] = {
    "EV-002": (
        ("SELECT u.city, SUM(p.amount) AS sales_amount FROM payments p "
         "JOIN orders o ON o.id = p.order_id JOIN users u ON u.id = o.user_id "
         "WHERE p.paid_at >= '2025-10-01' AND p.paid_at < '2026-10-01' "
         "GROUP BY u.city ORDER BY sales_amount DESC LIMIT 2 OFFSET 4"), 1, 5),
    "EV-010": (
        ("SELECT order_id, amount FROM refunds "
         "ORDER BY amount DESC LIMIT 2 OFFSET 2"), 1, 3),
    "EV-013": (
        ("SELECT name, price FROM products WHERE category = '图书' "
         "ORDER BY price DESC LIMIT 2 OFFSET 2"), 1, 3),
    "EV-015": (
        ("SELECT order_no, order_amount FROM orders "
         "WHERE created_at >= '2026-08-01' AND created_at < '2026-09-01' "
         "ORDER BY order_amount DESC LIMIT 2 OFFSET 4"), 1, 5),
    "EV-019": (
        ("SELECT o.user_id, u.name, SUM(r.amount) AS refund_total FROM refunds r "
         "JOIN orders o ON o.id = r.order_id JOIN users u ON u.id = o.user_id "
         "GROUP BY o.user_id, u.name ORDER BY refund_total DESC LIMIT 2 OFFSET 4"), 2, 5),
    # EV-021 无 LIMIT：金标本身返回全部 5 档，检查"计数严格降序"（等价于无边界平局）
    "EV-021": (None, 1, 5),
}

# qa_verification.md 已实测登记的标准错答值：(case_id, trap_id) -> value 字符串。
# 核对方式：与 jsonl 登记值直接数值比对，一致打勾。
KNOWN_WRONG_VALUES: dict[tuple[str, str], str] = {
    ("EV-003", "gmv_order_amount"): "20280681.72",
    ("EV-003", "month_shift_aug"): "16164482.43",
    ("EV-004", "refund_status_basis"): "0.0565",
    ("EV-004", "valid_alt_gloss"): "0.0886",
    ("EV-008", "arpu_confusion"): "1489.13",
    ("EV-008", "aov_unpaid_denom"): "2463.41",
    ("EV-029", "no_refund_deduction"): "16029955.34",
    ("EV-030", "refund_status_basis"): "0.0565",
    ("EV-030", "valid_alt_gloss"): "0.0775",
    ("MT-01", "month_shift_aug"): "16164482.43",
    ("MT-03", "valid_alt_gloss"): "0.0775",
    ("MT-03", "refund_status_basis"): "0.0565",
}

# T1 新实测的标准错答（qa_verification.md 里没有、需真库执行错答 SQL，§4.2）：
# (case_id, trap_id) -> (错答 SQL, 说明)
MEASURED_WRONG_SQL: dict[tuple[str, str], tuple[str, str]] = {
    ("EV-009", "repurchase_by_qty"): (
        ("SELECT ROUND(COUNT(*) / (SELECT COUNT(DISTINCT user_id) FROM orders "
         "WHERE created_at >= '2026-01-01' AND created_at < '2026-07-01'), 4) "
         "FROM (SELECT o.user_id FROM orders o "
         "JOIN order_items oi ON oi.order_id = o.id "
         "WHERE o.created_at >= '2026-01-01' AND o.created_at < '2026-07-01' "
         "GROUP BY o.user_id HAVING SUM(oi.quantity) >= 2) t"),
        "'买 ≥2 件'当复购（SUM(quantity)>=2）版错答",
    ),
    ("EV-029", "orders_amount_basis"): (
        ("SELECT ROUND(SUM(o.order_amount) - (SELECT SUM(r.amount) FROM refunds r "
         "WHERE r.refunded_at >= '2026-09-01' AND r.refunded_at < '2026-10-01'), 2) "
         "FROM orders o "
         "WHERE o.created_at >= '2026-09-01' AND o.created_at < '2026-10-01'"),
        ("orders.order_amount 口径冲减（设计预估 19,038,651.57 与实测不符，"
         "以本实测为准回填 jsonl）"),
    ),
}


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------

def load_jsonl(path: Path) -> list[dict]:
    """逐行加载 JSONL；JSON 解析失败直接抛（文件级错误，无法定位到字段）。"""
    cases: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                cases.append(json.loads(stripped))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"[ fatal ] {path.name} 第 {line_no} 行不是合法 JSON：{exc}")
    return cases


def validate_cases(cases: list[dict], required: list[str], real_chunk_ids: set[str],
                   is_multiturn: bool) -> list[str]:
    """schema 校验：返回问题清单（空 = 通过）。校验项见模块 docstring 第 1 条。"""
    problems: list[str] = []
    seen_ids: set[str] = set()
    type_counts: dict[str, int] = {}

    for i, case in enumerate(cases, start=1):
        cid = case.get("id", f"<第{i}行缺id>")
        # 1) 必填字段：缺任何一个都记问题（设计原则：漏填报错而不是静默跳过）
        for field in required:
            if field not in case:
                problems.append(f"{cid}: 缺必填字段 {field}")
        # 2) id 唯一
        if cid in seen_ids:
            problems.append(f"{cid}: id 重复")
        seen_ids.add(cid)
        # 3) 类型与取值
        if not isinstance(case.get("ordered"), bool):
            problems.append(f"{cid}: ordered 必须是布尔值")
        if is_multiturn:
            turns = case.get("turns")
            if not (isinstance(turns, list) and len(turns) >= 2
                    and all(isinstance(t, str) and t for t in turns)):
                problems.append(f"{cid}: turns 必须是 ≥2 条非空字符串的列表")
        else:
            qtype = case.get("question_type")
            if qtype not in VALID_QUESTION_TYPES:
                problems.append(f"{cid}: question_type 非法（{qtype!r} 不在五类枚举内）")
            type_counts[qtype] = type_counts.get(qtype, 0) + 1
        # 4) expected_chunks：非空列表，且每个 id 都在真实 28 片集合内
        chunks = case.get("expected_chunks")
        if not (isinstance(chunks, list) and chunks
                and all(isinstance(c, str) for c in chunks)):
            problems.append(f"{cid}: expected_chunks 必须是非空字符串列表")
        else:
            unknown = [c for c in chunks if c not in real_chunk_ids]
            if unknown:
                problems.append(f"{cid}: expected_chunks 含不存在的 chunk id：{unknown}")
        # 5) standard_wrong：列表，元素含 trap_id/description/value 三个字段
        wrong = case.get("standard_wrong")
        if not isinstance(wrong, list):
            problems.append(f"{cid}: standard_wrong 必须是列表（无错答登记时为 []）")
        else:
            for entry in wrong:
                if not (isinstance(entry, dict)
                        and {"trap_id", "description", "value"} <= set(entry)):
                    problems.append(f"{cid}: standard_wrong 元素缺 trap_id/description/value：{entry}")

    # 6) 规模与类型分布（仅单轮）
    if not is_multiturn:
        if len(cases) != 30:
            problems.append(f"单轮评测集应为 30 条，实际 {len(cases)} 条")
        if type_counts != TYPE_DISTRIBUTION:
            problems.append(f"类型分布应为 6/7/7/5/5，实际 {type_counts}")
    elif len(cases) != 4:
        problems.append(f"多轮评测集应为 4 条，实际 {len(cases)} 条")
    return problems


def replay(sql: str) -> tuple[list[tuple], bool]:
    """金标/错答 SQL 重放：返回 (行列表, 是否被 5000 行上限截断)。

    用与评测器相同的路径（get_engine + text），行数超上限时截断并标记，
    保证核对脚本本身不会被失控查询拖死。
    """
    with get_engine().connect() as conn:
        result = conn.execute(text(sql))
        rows = result.fetchmany(MAX_ROWS + 1)
    return rows[:MAX_ROWS], len(rows) > MAX_ROWS


def fmt_cell(v) -> str:
    """单元格转可读字符串：Decimal/datetime 等直接 str()，bytes 解码。"""
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="replace")
    return str(v)


def preview(rows: list[tuple], max_rows: int = 3) -> str:
    """前 N 行预览：cell 用 | 连接、行用 ; 连接，给人看的数值说明。"""
    head = rows[:max_rows]
    text = "; ".join("|".join(fmt_cell(v) for v in row) for row in head)
    if len(rows) > max_rows:
        text += f"; …（共 {len(rows)} 行）"
    return text


def dec_equal(actual, expected: str) -> bool:
    """数值等值比较：两边都转 Decimal（Decimal 数值比较对尾部零不敏感）。"""
    try:
        return Decimal(str(actual)) == Decimal(expected)
    except Exception:  # noqa: BLE001 —— 值不是数字时按不等处理，交给问题清单
        return False


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    problems: list[str] = []          # 致命问题（影响退出码）
    report_lines: list[str] = []      # 追加进 md 的本轮记录

    # —— 第 0 步：从 Chroma 拉真实 chunk id 集合（不硬编码 28 个 id，防语料改版失配）——
    collection = get_collection()
    real_chunk_ids = set(collection.get()["ids"])
    print(f"[0] Chroma 真实 chunk id 集合：{len(real_chunk_ids)} 片")
    report_lines.append(f"Chroma 索引 {len(real_chunk_ids)} 片，expected_chunks 全部对照实拉 id 集合校验。")

    # —— 第 1 步：加载 + schema 校验 ——
    singles = load_jsonl(DATASET_PATH)
    multis = load_jsonl(DATASET_MT_PATH)
    problems += validate_cases(singles, REQUIRED_FIELDS, real_chunk_ids, is_multiturn=False)
    problems += validate_cases(multis, REQUIRED_FIELDS_MT, real_chunk_ids, is_multiturn=True)
    print(f"[1] schema 校验：单轮 {len(singles)} 条 + 多轮 {len(multis)} 条，"
          f"{'通过' if not problems else f'{len(problems)} 个问题'}")
    for p in problems:
        print(f"    ✗ {p}")
    if any("缺必填字段" in p or "question_type 非法" in p or "chunk id" in p for p in problems):
        print("[abort] schema 校验存在致命问题，跳过金标执行（先修数据集再核对）。")
        write_report(report_lines, problems, fatal=True)
        return 1

    # —— 第 2 步：单轮金标逐条执行 + 行数/关键值核对 ——
    print("\n[2] 单轮金标真库执行")
    for case in singles:
        cid, sql = case["id"], case["gold_sql"]
        try:
            rows, oversized = replay(sql)
        except Exception as exc:  # noqa: BLE001 —— 单题失败不中断整场，收集后统一汇报
            problems.append(f"{cid}: 金标执行报错：{exc}")
            report_lines.append(f"- ✗ {cid}：执行报错：{exc}")
            print(f"    ✗ {cid} 执行报错：{exc}")
            continue
        desc_line = f"- ✓ {cid}：执行通过；{len(rows)} 行"
        if oversized:
            problems.append(f"{cid}: 结果超过 {MAX_ROWS} 行保护上限，已截断（oversized）")
            desc_line += "（超上限截断！）"
        if not rows:
            problems.append(f"{cid}: 金标结果为空（命题不成立）")
            report_lines.append(f"- ✗ {cid}：执行通过但结果为空（命题不成立）")
            print(f"    ✗ {cid} 结果为空")
            continue

        # 行数命题预期核对（剧情类检查：如 EV-014 六状态非零 / EV-017 八类目全覆盖）
        expected = EXPECTED_ROW_COUNTS.get(cid)
        row_note = ""
        if expected is not None:
            lo, hi = (expected, expected) if isinstance(expected, int) else expected
            if not (lo <= len(rows) <= hi):
                problems.append(f"{cid}: 行数 {len(rows)} 超出命题预期 [{lo}, {hi}]")
                row_note = f"，⚠ 行数偏离命题预期 [{lo},{hi}]"
        desc_line += f"；{preview(rows)}{row_note}"

        # 关键值锚点核对（单行题：qa_verification 已知值 / T1 实测回填值）
        check = GOLD_VALUE_CHECKS.get(cid)
        if check is not None and len(rows) == 1:
            expected_str, note = check
            if dec_equal(rows[0][0], expected_str):
                desc_line += f"；值 {fmt_cell(rows[0][0])} = 锚点 {expected_str}（{note}）"
            else:
                problems.append(
                    f"{cid}: 金标值 {fmt_cell(rows[0][0])} ≠ 锚点 {expected_str}（{note}）")
                desc_line += f"；⚠ 值 {fmt_cell(rows[0][0])} ≠ 锚点 {expected_str}"

        # EV-011 附加：pct 列合计应为 100.00（占比工艺的命题自检）
        if cid == "EV-011":
            pct_sum = sum(Decimal(str(r[2])) for r in rows)
            if abs(pct_sum - Decimal("100.00")) > Decimal("0.005"):
                problems.append(f"EV-011: pct 合计 {pct_sum} ≠ 100.00")
            desc_line += f"；pct 合计 {pct_sum}"

        report_lines.append(desc_line)
        print(f"    ✓ {cid}: {len(rows)} 行 | {preview(rows, 2)}")

    # —— 第 3 步：多轮金标执行（判定只看最后一轮，金标即 final_gold_sql）——
    print("\n[3] 多轮金标真库执行")
    for case in multis:
        cid, sql = case["id"], case["final_gold_sql"]
        try:
            rows, oversized = replay(sql)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{cid}: 金标执行报错：{exc}")
            report_lines.append(f"- ✗ {cid}：执行报错：{exc}")
            print(f"    ✗ {cid} 执行报错：{exc}")
            continue
        desc_line = f"- ✓ {cid}：执行通过；{len(rows)} 行；{preview(rows)}"
        if oversized or not rows:
            problems.append(f"{cid}: 结果{'超上限' if oversized else '为空'}（命题不成立）")
            report_lines.append(f"- ✗ {cid}：结果{'超上限' if oversized else '为空'}")
            print(f"    ✗ {cid} 结果{'超上限' if oversized else '为空'}")
            continue
        # MT-01 金标是有序两行（8 月→9 月），值锚点单独核对
        if cid == "MT-01":
            expected_pairs = [("2026-08", "16164482.43"), ("2026-09", "16029955.34")]
            actual_pairs = [(fmt_cell(r[0]), r[1]) for r in rows]
            ok = len(actual_pairs) == 2 and all(
                a[0] == e[0] and dec_equal(a[1], e[1])
                for a, e in zip(actual_pairs, expected_pairs))
            if ok:
                desc_line += "；两月 GMV = 锚点 16164482.43/16029955.34（有序一致）"
            else:
                problems.append(f"MT-01: 两月 GMV {actual_pairs} ≠ 锚点 {expected_pairs}")
        report_lines.append(desc_line)
        print(f"    ✓ {cid}: {len(rows)} 行 | {preview(rows, 2)}")

    # —— 第 4 步：TOP-N 有序题边界平局检查（§4.1，命题风险的兜底）——
    print("\n[4] TOP-N 边界平局检查")
    tie_lines: list[str] = []
    for cid, (sql, key_idx, n) in TIE_CHECKS.items():
        if sql is None:  # EV-021：检查 5 档计数严格降序（等价于无边界平局）
            case = next(c for c in singles if c["id"] == cid)
            rows, _ = replay(case["gold_sql"])
            counts = [r[key_idx] for r in rows]
            strictly = all(counts[i] > counts[i + 1] for i in range(len(counts) - 1))
            if strictly:
                tie_lines.append(f"- ✓ {cid}：{n} 档计数 {counts} 严格降序，无平局")
                print(f"    ✓ {cid} 严格降序 {counts}")
            else:
                problems.append(f"{cid}: 计数 {counts} 非严格降序，ordered 判定会抖动，须改题")
                tie_lines.append(f"- ✗ {cid}：计数 {counts} 非严格降序")
            continue
        rows, _ = replay(sql)
        if len(rows) < 2:
            problems.append(f"{cid}: 平局检查取不到第 {n}/{n + 1} 名（结果不足 {n + 1} 行）")
            tie_lines.append(f"- ✗ {cid}：结果不足 {n + 1} 行，无法检查")
            continue
        key_n, key_n1 = rows[0][key_idx], rows[1][key_idx]
        if key_n == key_n1:
            problems.append(f"{cid}: 第 {n}/{n + 1} 名排序键相等（{key_n}），"
                            "ordered 判定会随并列顺序抖动，必须改题")
            tie_lines.append(f"- ✗ {cid}：第 {n}/{n + 1} 名排序键平局（{key_n}）")
            print(f"    ✗ {cid} 第 {n}/{n + 1} 名平局：{key_n}")
        else:
            tie_lines.append(f"- ✓ {cid}：第 {n}/{n + 1} 名排序键 {key_n} / {key_n1}，无平局")
            print(f"    ✓ {cid} 第 {n}/{n + 1} 名：{key_n} vs {key_n1}")
    report_lines.extend(tie_lines)

    # —— 第 5 步：陷阱题标准错答核对（§4.2）——
    print("\n[5] 陷阱题标准错答核对")
    wrong_lines: list[str] = []
    all_cases = singles + multis
    for case in all_cases:
        for entry in case.get("standard_wrong", []):
            key = (case["id"], entry["trap_id"])
            registered = entry["value"]
            if key in KNOWN_WRONG_VALUES:
                known = KNOWN_WRONG_VALUES[key]
                if dec_equal(registered, known):
                    wrong_lines.append(
                        f"- ✓ {case['id']}/{entry['trap_id']}：登记 {registered} "
                        f"= qa_verification.md 已知值")
                    print(f"    ✓ {case['id']}/{entry['trap_id']} = {known}")
                else:
                    problems.append(
                        f"{case['id']}/{entry['trap_id']}: 登记值 {registered} "
                        f"≠ qa_verification.md 已知值 {known}")
                    wrong_lines.append(
                        f"- ✗ {case['id']}/{entry['trap_id']}：登记 {registered} ≠ 已知 {known}")
            elif key in MEASURED_WRONG_SQL:
                sql, note = MEASURED_WRONG_SQL[key]
                rows, _ = replay(sql)
                measured = rows[0][0]
                if dec_equal(registered, measured):
                    wrong_lines.append(
                        f"- ★ {case['id']}/{entry['trap_id']}：新实测 {fmt_cell(measured)}"
                        f"（{note}），与登记一致")
                    print(f"    ★ {case['id']}/{entry['trap_id']} 实测 = {measured}（一致）")
                else:
                    problems.append(
                        f"{case['id']}/{entry['trap_id']}: 登记值 {registered} "
                        f"≠ 实测 {fmt_cell(measured)}（{note}）")
                    wrong_lines.append(
                        f"- ✗ {case['id']}/{entry['trap_id']}：登记 {registered} ≠ 实测 {measured}")
            else:
                # 陷阱值既无已知锚点也无实测 SQL → 登记值来源不明，必须补登记
                problems.append(
                    f"{case['id']}/{entry['trap_id']}: 登记值 {registered} "
                    "无核对来源（不在 qa_verification.md 锚点表，也无实测 SQL）")
                wrong_lines.append(
                    f"- ? {case['id']}/{entry['trap_id']}：登记 {registered} 无核对来源")
    report_lines.extend(wrong_lines)

    # —— 第 6 步：汇总 + 追加写核对记录 ——
    summary = (f"单轮 {len(singles)} 条 + 多轮 {len(multis)} 条；"
               f"问题 {len(problems)} 个（退出码 {0 if not problems else 1}）")
    print(f"\n[6] 汇总：{summary}")
    for p in problems:
        print(f"    ✗ {p}")

    write_report(report_lines, problems)
    print(f"    核对记录已追加：{REPORT_PATH}")
    return 0 if not problems else 1


def write_report(report_lines: list[str], problems: list[str], fatal: bool = False) -> None:
    """把本轮核对结果按时间戳分节追加到 docs/eval_gold_verification.md。

    追加而非覆盖：核对记录是留痕文档（仿 qa_verification.md 的台账性质），
    每轮核对（含数据重导后的复跑）都保留历史。
    """
    # 时间戳用 UTC：核对记录是留痕文档，跨机器复跑时不受本地时区歧义影响
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    header = ""
    if not REPORT_PATH.exists():
        header = (
            "# W3 评测集金标核对记录（eval_gold_verification）\n\n"
            "> 本文件由 `eval/verify_gold.py` 自动追加生成（每轮核对一个时间戳分节）。\n"
            "> 数据为 `--seed 42` 固定种子静态快照，核对一次终身有效；"
            "**重导数据后必须重跑核对**（`python eval/verify_gold.py`）。\n"
            "> 金标值与错答值来源：`docs/qa_verification.md`（W2 实测）+ T1 真库执行实测。\n"
        )
    section = [f"\n## 核对轮次：{stamp}\n"]
    section += [f"{line}" for line in report_lines]
    if problems:
        section.append("\n### 问题清单（须修数后复跑）")
        section += [f"- ✗ {p}" for p in problems]
    else:
        section.append("\n### 结论")
        section.append("- 全部金标执行通过且结果非空；schema / 平局 / 错答登记核对全部通过。")
    if fatal:
        section.append("- schema 校验未通过，本轮未执行金标。")

    with open(REPORT_PATH, "a", encoding="utf-8") as f:
        if header:
            f.write(header)
        f.write("\n".join(section) + "\n")


if __name__ == "__main__":
    sys.exit(main())

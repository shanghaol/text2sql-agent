"""W3→W6 金标核对脚本：评测集（单轮 50 + 多轮 8）的质量闸门（W3 §4.1/§4.2 + W6 §1.2/§2）。

职责：
1. schema 校验——缺字段 / question_type 非法枚举 / expected_chunks 含不存在的 chunk id
   （真实 id 集合从 chromadb collection.get() 拉取，不硬编码）/ 类型分布 9/14/11/8/8
   / generalize_layer 枚举与覆盖（W6 §2.3：EV-012+ 39+6=45 条全覆盖，EV-001~011 恒 hit）；
2. 金标逐条在 mart 库执行（sql_tools.get_engine()，text() 包裹，行数保护上限 5000），
   断言执行通过且结果非空、行数 ≤ 20（W6 纪律 3：答案行数超 20 会触发工具截断带偏模型），
   并核对命题预期的行数与关键值锚点；
3. TOP-N 有序题边界平局检查（EV-002/010/013/015/019/021/033/035/040 + EV-044 组内第一并列）：
   LIMIT N 的第 N 名与第 N+1 名排序键相等 → 报警（有序判定会随并列顺序抖动，必须改题）；
4. 陷阱题标准错答核对：standard_wrong 的 value 与 qa_verification.md 已知值比对打勾；
   实测项真库执行错答 SQL 后比对（W3 §4.2 流程；W6 新增 EV-048~050/MT-07/08 全走实测）；
5. 年份窗口纪律（W6 §1.2）：单行单列纯整数金标 ∈ [1900,2100] 直接报错、∈ [1,31] 警告
   （答案级文本回退会误吸回答文本里的日期碎数——命题侧规避，judge 不动）；
6. 追加写 docs/eval_gold_verification.md（仿 qa_verification.md：每条一行 ✓ 记录）。

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
# schema 常量（设计 §1.2 / §1.7 / W6 §2.1 / §2.3）
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
# W6 §2.1 的五类分配（T1 验收：类型分布恰为 9/14/11/8/8）
TYPE_DISTRIBUTION = {
    "single_table": 9, "multi_join": 14, "aggregation": 11,
    "time_window": 8, "glossary_trap": 8,
}
SINGLE_SIZE = 50
MT_SIZE = 8
# W6 §2.3 分层字段：EV-001~011 恒 hit（QA 原题）；EV-012+ 必须显式标注 near/far
VALID_LAYERS = {"hit", "near", "far"}
FEWSHOT_LAYER_LAST_ID = "EV-011"

# 年份窗口纪律（W6 §1.2）：单行单列纯整数金标落入窗口 = 文本回退巧合误判面
YEAR_WINDOW_HARD = (1900, 2100)   # 落入 → 报错（日期碎数撞金标）
YEAR_WINDOW_WARN = (1, 31)        # 落入 → 警告（月/日碎数，须人工确认答案文本不同值）

# 行数纪律（W6 §2.4-3）：答案行数 ≤ 20（工具截断上限，EV-001 教训）
MAX_ANSWER_ROWS = 20

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
    # W6 新增 20 条（T1 2026-10-11 真库实测回填）
    "EV-031": 8, "EV-032": 1, "EV-033": 1, "EV-034": 8, "EV-035": 3,
    "EV-036": 3, "EV-037": 1, "EV-038": 20, "EV-039": 1, "EV-040": 5,
    "EV-041": 2, "EV-042": 3, "EV-043": 3, "EV-044": 8, "EV-045": 1,
    "EV-046": 7, "EV-047": 4, "EV-048": 1, "EV-049": 1, "EV-050": 1,
}

# 多轮金标行数预期（W6 MT-05~08 新增；判定只看最后一轮）
EXPECTED_ROW_COUNTS_MT: dict[str, int] = {
    "MT-01": 2, "MT-02": 1, "MT-03": 1, "MT-04": 1,
    "MT-05": 1, "MT-06": 1, "MT-07": 1, "MT-08": 1,
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
    # W6 新增（T1 2026-10-11 真库实测回填；EV-033 双列与多行题不适用标量锚点，走 notes 互证）
    "EV-032": ("47.90", "高价商品占比（T1 实测：957/2,000）"),
    "EV-037": ("7717", "微信×图书双条件用户数（T1 实测）"),
    "EV-039": ("4.00", "美妆占全站比例（T1 实测；类目权重倾斜：同期数码 51.09% 居首）"),
    "EV-045": ("17.91", "Q2 订单量环比增幅（T1 实测）"),
    "EV-048": ("77.09", "9 月支付转化率（T1 实测）"),
    "EV-049": ("98.55", "Q3 动销率（T1 实测：1,971/2,000）"),
    "EV-050": ("5.22", "9 月新客占比（T1 实测）"),
    "MT-05": ("2.83", "6 月连带率（T1 实测，与 EV-043 的 2026-06 行互证）"),
    "MT-06": ("14460561.20", "9 月净销售额（复用 EV-029 已验证：16,029,955.34 − 1,569,394.14）"),
    "MT-07": ("1559768.99", "1 月上海支付金额（T1 实测，与 EV-016 上海行互证）"),
    "MT-08": ("11705", "未买过数码用户数（T1 实测 = 20,000 − 8,295 勾稽自证）"),
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
    # W6 新增有序题（§2.4-4：TOP-N 边界平局全查）
    "EV-033": (
        ("SELECT DATE_FORMAT(refunded_at, '%Y-%m-%d'), amount FROM refunds "
         "ORDER BY amount DESC LIMIT 2 OFFSET 0"), 1, 1),
    "EV-035": (
        ("SELECT oc.category, SUM(r.amount) AS refund_amount FROM refunds r "
         "JOIN orders o ON o.id = r.order_id "
         "JOIN (SELECT DISTINCT oi.order_id, p.category FROM order_items oi "
         "JOIN products p ON p.id = oi.product_id) oc ON oc.order_id = o.id "
         "GROUP BY oc.category ORDER BY refund_amount DESC LIMIT 2 OFFSET 2"), 1, 3),
    "EV-040": (
        ("SELECT u.id, SUM(p.amount) AS total_paid FROM users u "
         "JOIN orders o ON o.user_id = u.id JOIN payments p ON p.order_id = o.id "
         "WHERE NOT EXISTS (SELECT 1 FROM refunds r JOIN orders o2 ON o2.id = r.order_id "
         "WHERE o2.user_id = u.id) "
         "GROUP BY u.id ORDER BY total_paid DESC LIMIT 2 OFFSET 4"), 1, 5),
}

# EV-044 组内第一并列检查（特殊形态：窗口函数取各类目第一，两商品并列最大销量时
# ROW_NUMBER 与相关子查询两等价形态结果不同 → 判定抖动）。SQL 返回并列于各类目
# 最大销量的商品数，任一 >1 即命题不成立。
EV044_TIE_SQL = (
    "SELECT t.category, t.qty, COUNT(*) AS tied FROM ("
    "  SELECT p.category, p.id, SUM(oi.quantity) AS qty "
    "  FROM order_items oi JOIN products p ON p.id = oi.product_id "
    "  GROUP BY p.category, p.id) t "
    "JOIN (SELECT category AS cat, MAX(g.qty) AS mx FROM ("
    "  SELECT p2.category, SUM(oi2.quantity) AS qty "
    "  FROM order_items oi2 JOIN products p2 ON p2.id = oi2.product_id "
    "  GROUP BY p2.category, p2.id) g GROUP BY category) m "
    "ON m.cat = t.category AND m.mx = t.qty "
    "GROUP BY t.category, t.qty ORDER BY t.category"
)

# 已实测登记的标准错答值：(case_id, trap_id) -> value 字符串。
# 来源有两处（核对方式相同：与 jsonl 登记值直接数值比对，一致打勾）：
# - 多数出自 docs/qa_verification.md（T2 收尾时的口径陷阱实测）
# - month_shift_aug 的 16164482.43 出自 docs/experiment-log.md（实验二重跑/基线对照实测）
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
    # W6 新增：复用已验证锚点（W6 §2.5 命题纪律——金标/错答复用已验证数字优先）
    ("MT-05", "metric_not_switched"): "2765.34",   # = EV-008 已验证 6 月客单价
    ("MT-06", "gmv_no_deduction"): "16029955.34",  # = EV-003 已验证 9 月 GMV
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
    # W6 新增陷阱/多轮错答（T1 2026-10-11 真库实测，每轮核对自动重放）
    ("EV-048", "denominator_missing_unpaid"): (
        ("SELECT ROUND((SELECT COUNT(*) FROM orders o WHERE o.created_at >= '2026-09-01' "
         "AND o.created_at < '2026-10-01' AND o.status IN ('paid','shipped','completed',"
         "'refunded')) * 100.0 / (SELECT COUNT(*) FROM orders o WHERE o.created_at >= "
         "'2026-09-01' AND o.created_at < '2026-10-01' AND o.status IN ('paid','shipped',"
         "'completed','refunded')), 2)"),
        "支付转化率分母漏未支付（分子分母同源 → 100%）",
    ),
    ("EV-048", "denominator_by_payments"): (
        ("SELECT ROUND((SELECT COUNT(DISTINCT p.order_id) FROM payments p JOIN orders o "
         "ON o.id = p.order_id WHERE o.created_at >= '2026-09-01' AND o.created_at < "
         "'2026-10-01') * 100.0 / (SELECT COUNT(*) FROM payments p JOIN orders o ON "
         "o.id = p.order_id WHERE o.created_at >= '2026-09-01' AND o.created_at < "
         "'2026-10-01'), 2)"),
        "支付转化率分母误用 payments 计数（对 payments 计数即分子 → 100%）",
    ),
    ("EV-049", "denominator_in_period"): (
        ("SELECT ROUND((SELECT COUNT(DISTINCT oi.product_id) FROM order_items oi "
         "JOIN orders o ON o.id = oi.order_id WHERE o.created_at >= '2026-07-01' AND "
         "o.created_at < '2026-10-01') * 100.0 / (SELECT COUNT(DISTINCT oi.product_id) "
         "FROM order_items oi JOIN orders o ON o.id = oi.order_id WHERE o.created_at >= "
         "'2026-07-01' AND o.created_at < '2026-10-01'), 2)"),
        "动销率分母误用期内有售商品数（分子分母同源 → 100%）",
    ),
    ("EV-049", "numerator_by_orders"): (
        ("SELECT ROUND((SELECT COUNT(DISTINCT o.id) FROM orders o WHERE o.created_at >= "
         "'2026-07-01' AND o.created_at < '2026-10-01') * 100.0 / (SELECT COUNT(*) FROM "
         "products), 2)"),
        "动销率分子误按订单数计（订单维度 ≠ 商品维度）",
    ),
    ("EV-050", "first_order_in_period_only"): (
        ("SELECT ROUND((SELECT COUNT(*) FROM (SELECT user_id FROM orders WHERE created_at "
         ">= '2026-09-01' AND created_at < '2026-10-01' GROUP BY user_id HAVING "
         "MIN(created_at) >= '2026-09-01') t) * 100.0 / (SELECT COUNT(DISTINCT user_id) "
         "FROM orders WHERE created_at >= '2026-09-01' AND created_at < '2026-10-01'), 2)"),
        "'首次'只在期内取 MIN（期内下单者'期内首次'恒真 → 100%，glossary 陷阱条目原文）",
    ),
    ("EV-050", "registration_cohort"): (
        ("SELECT ROUND((SELECT COUNT(*) FROM users WHERE registered_at >= '2026-09-01' "
         "AND registered_at < '2026-10-01') * 100.0 / (SELECT COUNT(DISTINCT user_id) "
         "FROM orders WHERE created_at >= '2026-09-01' AND created_at < '2026-10-01'), 2)"),
        "新客与'新增注册用户'混淆（注册止于 2025-08-31 → 0%）",
    ),
    ("MT-07", "city_filter_missing"): (
        ("SELECT SUM(p.amount) FROM payments p JOIN orders o ON o.id = p.order_id "
         "JOIN users u ON u.id = o.user_id WHERE p.paid_at >= '2026-01-01' AND "
         "p.paid_at < '2026-02-01'"),
        "范围收窄失败：忘加城市过滤的全城合计",
    ),
    ("MT-08", "negation_missing"): (
        ("SELECT COUNT(DISTINCT o.user_id) FROM orders o JOIN order_items oi ON "
         "oi.order_id = o.id JOIN products p ON p.id = oi.product_id WHERE "
         "p.category = '数码'"),
        "否定式失败：未取补集，仍答数码购买者数",
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
            # 3b) generalize_layer（W6 §2.3）：EV-001~011 恒 hit；EV-012+ 必填且
            #     notes 须记近/远依据（G 行消融的 Δ 归因依赖静态分层）
            layer = case.get("generalize_layer")
            if layer is not None and layer not in VALID_LAYERS:
                problems.append(f"{cid}: generalize_layer 非法枚举（{layer!r}，合法 hit/near/far）")
            if cid <= FEWSHOT_LAYER_LAST_ID:
                if layer != "hit":
                    problems.append(f"{cid}: 示例层题 generalize_layer 必须是 hit，实际 {layer!r}")
            elif layer not in ("near", "far"):
                problems.append(f"{cid}: EV-012+ 泛化题缺 generalize_layer（near/far）")
            if cid > FEWSHOT_LAYER_LAST_ID and "分层" not in case.get("notes", ""):
                problems.append(f"{cid}: notes 未记近/远分层依据（W6 §2.3）")
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
        if len(cases) != SINGLE_SIZE:
            problems.append(f"单轮评测集应为 {SINGLE_SIZE} 条，实际 {len(cases)} 条")
        if type_counts != TYPE_DISTRIBUTION:
            problems.append(
                f"类型分布应为 {TYPE_DISTRIBUTION}，实际 {type_counts}")
    elif len(cases) != MT_SIZE:
        problems.append(f"多轮评测集应为 {MT_SIZE} 条，实际 {len(cases)} 条")
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


def is_integral_number(value) -> bool:
    """纯整数判定：int（bool 除外）/ 值为整数的 Decimal（如 MySQL COUNT(*) 的 int、
    SUM 溢出转 Decimal 但值仍为整数的情形）。"""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, Decimal):
        return value.is_finite() and value == value.to_integral_value()
    return False


def check_year_window(cid: str, rows: list[tuple]) -> tuple[str | None, str | None]:
    """年份窗口纪律（W6 §1.2）：单行单列纯整数金标落窗检测。

    返回 (problem, warning)：problem = 落入 [1900,2100]（日期碎数撞金标，文本回退
    巧合误判面，直接拦下）；warning = 落入 [1,31]（月/日碎数窗口，存在合法答案但须
    人工确认答案文本不会同时出现同值环境数字）。多行/多列/非整数金标天然免疫。
    """
    if len(rows) != 1 or len(rows[0]) != 1:
        return None, None
    value = rows[0][0]
    if not is_integral_number(value):
        return None, None
    num = int(value)
    lo, hi = YEAR_WINDOW_HARD
    if lo <= num <= hi:
        return (f"{cid}: 单值整数金标 {num} 落入年份窗口 [{lo},{hi}]——"
                "答案级文本回退会误吸回答里的日期碎数，按 §1.2 命题纪律改题（带小数/"
                "改多行/换大数值），judge 不动"), None
    lo, hi = YEAR_WINDOW_WARN
    if lo <= num <= hi:
        return None, (f"{cid}: 单值整数金标 {num} 落入月/日碎数窗口 [{lo},{hi}]——"
                      "人工确认该题答案文本不会同时出现同值环境数字（§1.2 软警告）")
    return None, None


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    problems: list[str] = []          # 致命问题（影响退出码）
    warnings: list[str] = []          # 软警告（不影响退出码，人工确认项）
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
    layer_counts: dict[str, int] = {}
    for case in singles:
        layer = case.get("generalize_layer")
        layer_counts[layer] = layer_counts.get(layer, 0) + 1
    print(f"[1] schema 校验：单轮 {len(singles)} 条 + 多轮 {len(multis)} 条，"
          f"{'通过' if not problems else f'{len(problems)} 个问题'}；"
          f"分层 hit/near/far = {layer_counts.get('hit', 0)}/{layer_counts.get('near', 0)}/"
          f"{layer_counts.get('far', 0)}")
    report_lines.append(
        f"分层分布（W6 §2.3）：hit {layer_counts.get('hit', 0)} / near "
        f"{layer_counts.get('near', 0)} / far {layer_counts.get('far', 0)}。")
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

        # W6 §2.4-3 行数纪律：答案行数 ≤ 20（工具截断上限，超限会带偏模型改查子区间）
        if len(rows) > MAX_ANSWER_ROWS:
            problems.append(
                f"{cid}: 结果 {len(rows)} 行超过答案行数上限 {MAX_ANSWER_ROWS}"
                "（工具截断会带偏模型，EV-001 教训）")

        # W6 §1.2 年份窗口纪律：单值整数金标落窗检测（硬错误/软警告）
        year_problem, year_warning = check_year_window(cid, rows)
        if year_problem:
            problems.append(year_problem)
        if year_warning:
            warnings.append(year_warning)

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
        # W6：MT 行数预期 + 行数上限 + 年份窗口（与单轮同款纪律）
        expected = EXPECTED_ROW_COUNTS_MT.get(cid)
        if expected is not None and len(rows) != expected:
            problems.append(f"{cid}: 行数 {len(rows)} ≠ 命题预期 {expected}")
        if len(rows) > MAX_ANSWER_ROWS:
            problems.append(f"{cid}: 结果 {len(rows)} 行超过答案行数上限 {MAX_ANSWER_ROWS}")
        year_problem, year_warning = check_year_window(cid, rows)
        if year_problem:
            problems.append(year_problem)
        if year_warning:
            warnings.append(year_warning)
        # 关键值锚点（MT-02~08 单行题；W6 起新增条目与单轮同表 GOLD_VALUE_CHECKS）
        check = GOLD_VALUE_CHECKS.get(cid)
        if check is not None and len(rows) == 1:
            expected_str, note = check
            if dec_equal(rows[0][0], expected_str):
                desc_line += f"；值 {fmt_cell(rows[0][0])} = 锚点 {expected_str}（{note}）"
            else:
                problems.append(
                    f"{cid}: 金标值 {fmt_cell(rows[0][0])} ≠ 锚点 {expected_str}（{note}）")
                desc_line += f"；⚠ 值 {fmt_cell(rows[0][0])} ≠ 锚点 {expected_str}"
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

    # EV-044（W6 §2.2）：组内第一并列检查——窗口函数与相关子查询两等价形态在
    # "最大销量并列"时会给出不同结果（ROW_NUMBER 任选其一 / 子查询并列全给），
    # 任一类目并列于最大销量即命题抖动，必须改题
    rows, _ = replay(EV044_TIE_SQL)
    tied = [(r[0], int(r[1]), int(r[2])) for r in rows if int(r[2]) > 1]
    if tied:
        problems.append(f"EV-044: 类目最大销量存在并列 {tied}，两等价形态结果不同，须改题")
        tie_lines.append(f"- ✗ EV-044：组内第一并列 {tied}")
        print(f"    ✗ EV-044 组内并列：{tied}")
    else:
        tie_lines.append("- ✓ EV-044：8 类目最大销量并列数全为 1，两等价形态结果一致")
        print(f"    ✓ EV-044 组内第一无并列（{len(rows)} 类目）")
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
                        f"= 已实测登记值（见 KNOWN_WRONG_VALUES 注释的来源）")
                    print(f"    ✓ {case['id']}/{entry['trap_id']} = {known}")
                else:
                    problems.append(
                        f"{case['id']}/{entry['trap_id']}: 登记值 {registered} "
                        f"≠ 已实测登记值 {known}")
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
               f"问题 {len(problems)} 个、软警告 {len(warnings)} 个"
               f"（退出码 {0 if not problems else 1}）")
    print(f"\n[6] 汇总：{summary}")
    for p in problems:
        print(f"    ✗ {p}")
    for w in warnings:
        print(f"    ⚠ {w}")

    write_report(report_lines, problems, warnings=warnings)
    print(f"    核对记录已追加：{REPORT_PATH}")
    return 0 if not problems else 1


def write_report(report_lines: list[str], problems: list[str],
                 fatal: bool = False, warnings: list[str] | None = None) -> None:
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
            "# W3→W6 评测集金标核对记录（eval_gold_verification）\n\n"
            "> 本文件由 `eval/verify_gold.py` 自动追加生成（每轮核对一个时间戳分节）。\n"
            "> 数据为 `--seed 42` 固定种子静态快照，核对一次终身有效；"
            "**重导数据后必须重跑核对**（`python eval/verify_gold.py`）。\n"
            "> 金标值与错答值来源：`docs/qa_verification.md`（W2 实测）+ T1 真库执行实测。\n"
        )
    section = [f"\n## 核对轮次：{stamp}\n"]
    section += [f"{line}" for line in report_lines]
    if warnings:
        section.append("\n### 软警告（不影响退出码，人工确认项）")
        section += [f"- ⚠ {w}" for w in warnings]
    if problems:
        section.append("\n### 问题清单（须修数后复跑）")
        section += [f"- ✗ {p}" for p in problems]
    else:
        section.append("\n### 结论")
        section.append("- 全部金标执行通过且结果非空；schema / 平局 / 错答登记 / "
                       "年份窗口 / 行数上限核对全部通过。")
    if fatal:
        section.append("- schema 校验未通过，本轮未执行金标。")

    with open(REPORT_PATH, "a", encoding="utf-8") as f:
        if header:
            f.write(header)
        f.write("\n".join(section) + "\n")


if __name__ == "__main__":
    sys.exit(main())

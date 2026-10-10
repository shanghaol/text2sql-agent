"""W3 评测管线的纯函数层：结果集判定、SQL 提取、评测集加载、指标小工具。

模块纪律（W3 设计 §2.1 / §2.4）：本模块 **不 import 任何 agent 包**，因此不触发
MySQL / Chroma / embedding / API key 的任何加载——tests/test_eval_judge.py 在
无 MySQL、无 key 的裸机上全绿（延续 W1/W2 的默认套件原则）。

内容分五段：
1. 结果集规范化与判定（§2.4 / §4.3）：normalize_rows / rows_equal / classify_failure /
   judge_case——金标与模型 SQL 都由评测器重放后比较两个结果集，不存死值金标；
2. 答案级回退（§2.4 判定规则 v2，2026-10-10 修订）：extract_number_candidates（文本抽数字
   候选）/ text_contains_gold（金标标量按容差比对）/ extract_final_answer_text（取最后一条
   AIMessage 文本）/ classify_failure_from_text（standard_wrong 的文本候选比对）——
   SQL 级 fail 或无可用 SQL 时，单行单列数值金标可由最终回答文本的计算结果兜底判过
   （pass_via="text"），与 SQL 级（pass_via="sql"）构成双层指标；
3. SQL 提取（§2.2）：extract_execute_calls（从消息链收集 execute_readonly_sql 结构化调用）
   与 pick_sql（三级取用规则：最后一条重放成功 → 回退最后一条成功 → error）；
4. 评测集加载与 schema 校验（§1.2 / §1.7）：load_dataset / load_multiturn_dataset
   ——全字段必填、无默认值，漏填在加载时报错而不是静默跳过；
5. 指标小工具（§2.3 / §2.6 / §2.7）：join_retrieval_query（检索 query 拼接，与
   main.py 逐字符同款）、sum_usage（token 累加）、p95（P95 分位数口径）。

容差规则（设计 §2.4 拍板，不开放调参）：两数值相等 ⇔
|a−b| ≤ max(0.005, 1e-3 × max(|a|, |b|))，全程 Decimal 运算避免 float 噪声。
相对容差 1e-3 能吸收 4 位小数 ROUND 的差异，而陷阱值与金标的差异都在 20% 量级
（月份偏移差 100%）——容差比陷阱间距小三个数量级，不会把陷阱错答放行为 pass。
"""

from __future__ import annotations

import json
import re
import statistics
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from pathlib import Path

# ---------------------------------------------------------------------------
# 1. 结果集规范化与判定（设计 §2.4 / §4.3）
# ---------------------------------------------------------------------------

# 数值容差常量（§2.4）：|a-b| <= max(_ABS_TOL, _REL_TOL * max(|a|, |b|))
_ABS_TOL = Decimal("0.005")
_REL_TOL = Decimal("0.001")


def _normalize_cell(value):
    """单元格规范化（§2.4 规则表）。bool 是 int 的子类，必须先于数值分支处理
    （Decimal(str(True)) 会抛 InvalidOperation）。"""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (Decimal, int, float)):
        # str() 消除 float 尾数噪声（repr 是最短往返表示），Decimal 也接受科学计数法字符串
        return Decimal(str(value))
    if isinstance(value, datetime):
        return str(value)  # '2026-03-15 14:22:01'
    if isinstance(value, date):  # datetime 是 date 的子类，已在上面的分支被截住
        return str(value)  # '2026-03-15'
    if isinstance(value, time):
        return str(value)  # '14:22:01'
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value  # None / str 原样


def normalize_rows(rows: list[tuple]) -> list[list]:
    """把一整个结果集按 §2.4 规则表规范化；金标行与模型行走同一函数。"""
    return [[_normalize_cell(v) for v in row] for row in rows]


def _cells_equal(a, b) -> bool:
    """两单元格相等：数值走容差公式（Decimal 运算），其余类型恒等比较。

    列名（AS 别名）在重放比较里天然不存在（fetchall 只有值）；字符串单元格
    （城市名/类目名/DATE_FORMAT 产物）必须完全一致，容差只作用于数值。
    """
    if isinstance(a, Decimal) and isinstance(b, Decimal):
        if not (a.is_finite() and b.is_finite()):
            # NaN/Infinity 的兜底：进容差运算会抛 InvalidOperation（实测确认）
            return a == b
        diff = abs(a - b)
        tol = max(_ABS_TOL, _REL_TOL * max(abs(a), abs(b)))
        return diff <= tol
    return a == b


def _row_sort_key(row: list) -> tuple:
    """无序比较的排序键：给混合类型（Decimal/str/None）一个确定性全序。

    等值不同格式（如 Decimal('1.0') vs Decimal('1.00')）的字符串形式不同、
    排序位置可能错开，但逐位比较走 _cells_equal 的容差，等值仍会判等——
    多重集合相等的判定不受排序键形态影响。类型标签分桶避免 Decimal 与 str
    之间的 TypeError（同一列金标是 Decimal、模型是 str 时仍能排序）。
    """

    def key(cell):
        if isinstance(cell, Decimal):
            return (0, str(cell))
        if isinstance(cell, str):
            return (1, cell)
        return (2, "" if cell is None else str(cell))

    return tuple(key(c) for c in row)


def rows_equal(gold_rows: list[tuple], model_rows: list[tuple], ordered: bool = False) -> bool:
    """判定规则（§2.4，从上到下短路）：列数 → 行数 → 有序逐位 / 无序多重集合。

    - ordered=True（人工标注金标有序）：按位置逐行逐格比较；
    - ordered=False：两边各自排序后逐位比较（等价多重集合比较，正确处理重复行）。
    """
    gold = normalize_rows(gold_rows)
    model = normalize_rows(model_rows)

    gold_cols = len(gold[0]) if gold else 0
    model_cols = len(model[0]) if model else 0
    if gold_cols != model_cols:  # 投影数不对是实质错误
        return False
    if len(gold) != len(model):
        return False

    if ordered:
        gold_sorted, model_sorted = gold, model
    else:
        gold_sorted = sorted(gold, key=_row_sort_key)
        model_sorted = sorted(model, key=_row_sort_key)
    for gold_row, model_row in zip(gold_sorted, model_sorted):
        for g, m in zip(gold_row, model_row):
            if not _cells_equal(g, m):
                return False
    return True


def _registered_value(raw) -> Decimal | str:
    """standard_wrong.value 的规范化：登记的是字符串形式，比较时数值化，
    与结果集单元格走同一条 _cells_equal（§4.3）。非数字（日期串等）保留字符串。"""
    text = str(raw).strip()
    try:
        return Decimal(text)
    except InvalidOperation:
        return text


def classify_failure(model_rows: list[tuple], standard_wrong: list[dict]) -> str | None:
    """错答模式分类（§4.3）：模型重放结果匹配某条登记值 → trap:<trap_id>；
    该条 trap_id 标注为合法口径（"valid_alt_gloss"）→ valid_alt_gloss；
    都不匹配 → None（调用方记 other，人工看明细归类）。

    登记表里的陷阱值全是单值标量，因此只对"单行单列"的模型结果做比对；
    多行/多列结果返回 None。
    """
    if not standard_wrong:
        return None
    normalized = normalize_rows(model_rows)
    if len(normalized) != 1 or len(normalized[0]) != 1:
        return None
    cell = normalized[0][0]
    for entry in standard_wrong:
        trap_id = str(entry.get("trap_id", ""))
        if _cells_equal(cell, _registered_value(entry.get("value", ""))):
            if trap_id == "valid_alt_gloss":
                return "valid_alt_gloss"
            return f"trap:{trap_id}"
    return None


# ---------------------------------------------------------------------------
# 2. 答案级回退（§2.4 判定规则 v2）：文本数字候选与金标标量比对
# ---------------------------------------------------------------------------

# 文本数字候选的两条形态（§2.4 v2）：千分位形态（要求至少一组 ,ddd）+ 普通形态兜底。
# 设计给出的千分位正则写作 (?:,\d{3})*，但 * 允许零组逗号，会把普通整数
# 16029955.34 掐成 "160"——两遍扫描方案下收紧为 + 才不互相误伤；
# 与普通形态的并集与设计原意（两条正则都要匹配）等价。
_THOUSANDS_NUM_RE = re.compile(r"-?\d{1,3}(?:,\d{3})+(?:\.\d+)?%?")
_PLAIN_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?%?")


def extract_number_candidates(text: str) -> list[Decimal]:
    """从文本抽取数字候选（§2.4 v2 规则 3），按出现位置保序：

    - 先用千分位形态扫一遍（要求至少一组 ,ddd），把命中段挖掉后再用普通形态
      扫剩余文本——两遍归并，普通整数与千分位数字互不误伤；
    - 去千分位逗号后 Decimal 化；带 % 后缀的产生两个候选：原值与原值/100
      （"7.75%" → 7.75 和 0.0775——模型文本里的百分数与金标的小数存法都要能对上）；
    - 去重保序（同一数值多次出现只留第一个位置上的）。
    """
    hits: list[tuple[int, int, str]] = [
        (match.start(), match.end(), match.group()) for match in _THOUSANDS_NUM_RE.finditer(text)
    ]
    masked = list(text)
    for start, end, _ in hits:
        for index in range(start, end):
            masked[index] = " "
    for match in _PLAIN_NUM_RE.finditer("".join(masked)):
        hits.append((match.start(), match.end(), match.group()))

    candidates: list[Decimal] = []
    for _, _, raw in sorted(hits, key=lambda hit: hit[0]):
        has_percent = raw.endswith("%")
        try:
            value = Decimal(raw[:-1].replace(",", "") if has_percent else raw.replace(",", ""))
        except InvalidOperation:
            continue
        candidates.append(value)
        if has_percent:
            candidates.append(value / Decimal(100))
    unique: list[Decimal] = []
    seen: set[Decimal] = set()
    for value in candidates:
        if value not in seen:
            seen.add(value)
            unique.append(value)
    return unique


def text_contains_gold(text: str | None, gold_scalar) -> Decimal | None:
    """答案级回退的命中判定（§2.4 v2 规则 3）：文本数字候选逐一与金标标量按同一
    容差公式（max(0.005, 1e-3·max(|a|,|b|))，Decimal 运算）比对。

    - 金标非数值（str / None / bool 的规范化产物）→ 不适用，返回 None——
      DATE_FORMAT 输出的月份字符串等字符串金标不做文本回退（设计明文）；
    - 文本无任何数字候选 → None；
    - 命中 → 返回命中的候选值（调用方记入明细 text_matched），否则 None。
    """
    if not isinstance(gold_scalar, Decimal):
        return None
    for candidate in extract_number_candidates(text or ""):
        if _cells_equal(gold_scalar, candidate):
            return candidate
    return None


def _single_scalar(gold_rows: list[tuple]) -> Decimal | str | None:
    """金标结果集为单行单列时取其规范化值，否则 None（多行/多列不走文本回退，§2.4 v2）。"""
    normalized = normalize_rows(gold_rows)
    if len(normalized) == 1 and len(normalized[0]) == 1:
        return normalized[0][0]
    return None


def classify_failure_from_text(text: str | None, standard_wrong: list[dict]) -> str | None:
    """standard_wrong 的文本候选比对（§2.4 v2 规则 5 / §4.3）：最终回答文本中的数字
    候选命中某条登记值 → trap:<trap_id> / valid_alt_gloss（登记顺序优先）。

    只做数值比对（字符串登记值如日期串不参与文本回退）；都不命中 → None。
    """
    if not standard_wrong or not text:
        return None
    candidates = extract_number_candidates(text)
    if not candidates:
        return None
    for entry in standard_wrong:
        value = _registered_value(entry.get("value", ""))
        if not isinstance(value, Decimal):
            continue
        if any(_cells_equal(value, candidate) for candidate in candidates):
            trap_id = str(entry.get("trap_id", ""))
            if trap_id == "valid_alt_gloss":
                return "valid_alt_gloss"
            return f"trap:{trap_id}"
    return None


def extract_final_answer_text(messages: list) -> str:
    """最后一条 AIMessage 的文本内容（§2.4 v2 答案级回退的取材面；明细落盘审计用）。

    - 只看最后一条 AIMessage（type=="ai"）的 content：链路里更早的数字是中间量，
      不扫全链路（§2.4 v2：只看最终回答）；
    - content 为 str 原样返回；为多模态块 list 时逐块抽取 text 字段、其余块 str() 化
      （ChatDeepSeek 正常返回 str，list 分支是防御）；其他类型一律 str() 化；
    - 没有 AIMessage → 空串。
    """
    for msg in reversed(messages):
        if getattr(msg, "type", None) != "ai":
            continue
        content = getattr(msg, "content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    parts.append(block["text"])
                else:
                    parts.append(str(block))
            return "\n".join(parts)
        return str(content)
    return ""


def judge_case(
    gold_rows: list[tuple] | None,
    model_rows: list[tuple] | None,
    ordered: bool = False,
    standard_wrong: list[dict] | None = None,
    error_msg: str | None = None,
    final_answer_text: str | None = None,
) -> dict:
    """单题判定主入口（§2.4 判定规则 v2：SQL 级判定 + 答案级回退的双层指标）。

    返回 {"judge", "failure_mode", "error_msg", "pass_via", "text_matched"}：
    - pass_via="sql"：SQL 重放结果集与金标一致（严格口径，简历主数字）；
    - pass_via="text"：SQL 级 fail 或无可用 SQL，但金标为单行单列数值标量、最后一条
      AIMessage 文本中的数字候选按同一容差命中金标（文本计算兜底，答案级口径；
      text_matched 记录命中的候选值，明细留痕）；
    - model_rows 为 None 且文本回退不适用 → error（error_msg 由调用方给：
      no_usable_sql / replay_error / invoke_exception 三子码之一；未给时按 no_usable_sql）；
    - 结果集不一致且文本回退不适用或未命中 → fail，failure_mode ∈
      {trap:<id>, valid_alt_gloss, other}（§4.3；standard_wrong 比对同样享受文本候选）；
    - 多行/多列金标与 str 型金标（DATE_FORMAT 月份串）不走文本回退（设计明文）；
      调用方未提供 final_answer_text 时行为与 v1 完全一致。
    """
    wrong = standard_wrong or []
    if model_rows is None:
        # 规则 1：无可用 SQL / 重放报错 → 先试答案级回退，不适用才 error（§2.4 v2）
        text_matched = text_contains_gold(final_answer_text, _single_scalar(gold_rows or []))
        if text_matched is not None:
            return {
                "judge": "pass",
                "failure_mode": None,
                "error_msg": None,
                "pass_via": "text",
                "text_matched": text_matched,
            }
        return {
            "judge": "error",
            "failure_mode": None,
            "error_msg": error_msg or "no_usable_sql",
            "pass_via": None,
            "text_matched": None,
        }
    if rows_equal(gold_rows, model_rows, ordered=ordered):
        return {
            "judge": "pass",
            "failure_mode": None,
            "error_msg": None,
            "pass_via": "sql",
            "text_matched": None,
        }
    # 规则 3：SQL 级 fail → 答案级回退（仅单行单列数值金标；规则 4：多行/多列不回退）
    text_matched = text_contains_gold(final_answer_text, _single_scalar(gold_rows))
    if text_matched is not None:
        return {
            "judge": "pass",
            "failure_mode": None,
            "error_msg": None,
            "pass_via": "text",
            "text_matched": text_matched,
        }
    # 规则 5：failure_mode 的 standard_wrong 比对享受文本候选（§4.3）
    mode = classify_failure(model_rows, wrong) or classify_failure_from_text(
        final_answer_text, wrong
    )
    return {
        "judge": "fail",
        "failure_mode": mode or "other",
        "error_msg": None,
        "pass_via": None,
        "text_matched": None,
    }


# ---------------------------------------------------------------------------
# 3. SQL 提取与三级取用规则（设计 §2.2）
# ---------------------------------------------------------------------------


def extract_execute_calls(messages: list) -> list[dict]:
    """按发生顺序收集 execute_readonly_sql 调用：[{"seq": 1, "sql": "..."}, ...]。

    只认 AIMessage.tool_calls 里的结构化调用（inspect_chain.py 实证：tool_calls 是
    LangChain 已解析的 [{name, args}] 列表，args["sql"] 就是实际发起执行的 SQL）；
    content 正文里的 SQL 不算——W1 实验 4 观察到正文 SQL 与实际执行的 SQL 可能不同。
    list_tables 等其他工具跳过。
    """
    calls: list[dict] = []
    for msg in messages:
        for tool_call in getattr(msg, "tool_calls", None) or []:
            if tool_call.get("name") != "execute_readonly_sql":
                continue
            # 防御畸形调用：args 缺 sql 参数的调用不进序列（设计代码片段的 .get 加固版）
            sql = (tool_call.get("args") or {}).get("sql")
            if isinstance(sql, str):
                calls.append({"seq": len(calls) + 1, "sql": sql})
    return calls


def pick_sql(calls: list[dict], replay) -> tuple[str | None, str]:
    """三级取用规则（§2.2）：返回 (sql, sql_source)。

    1. 最后一条重放成功 → 用它判定（sql_source="last"，绝大多数题一发命中）；
    2. 最后一条重放报错 → 回退最后一条成功的更早调用（"fallback_to_earlier"，
       "以失败调用收尾"本身是模型自我修正失败的信号，调用方据此留痕统计）；
    3. 全部失败，或一条都没调（拒答/纯文字回答）→ (None, "no_usable_sql")。
       不解析 content 语义去救——拒答本来就该算失败。

    replay: sql -> 行列表 | None（None = 重放失败）。同一 SQL 会被试探多次，
    重复打库由调用方的重放器缓存兜住，本函数保持无状态。
    """
    if not calls:
        return None, "no_usable_sql"
    if replay(calls[-1]["sql"]) is not None:
        return calls[-1]["sql"], "last"
    for call in reversed(calls[:-1]):
        if replay(call["sql"]) is not None:
            return call["sql"], "fallback_to_earlier"
    return None, "no_usable_sql"


# ---------------------------------------------------------------------------
# 4. 评测集加载与 schema 校验（设计 §1.2 单轮 / §1.7 多轮）
# ---------------------------------------------------------------------------

_SINGLE_REQUIRED = (
    "id",
    "question",
    "question_type",
    "gold_sql",
    "ordered",
    "expected_chunks",
    "standard_wrong",
    "notes",
)
_MULTITURN_REQUIRED = (
    "id",
    "turns",
    "final_gold_sql",
    "ordered",
    "expected_chunks",
    "standard_wrong",
    "notes",
)
_QUESTION_TYPES = frozenset(
    {"single_table", "multi_join", "aggregation", "time_window", "glossary_trap"}
)
_SINGLE_ID_RE = re.compile(r"^EV-\d{3}$")
_MULTITURN_ID_RE = re.compile(r"^MT-\d{2}$")


class DatasetSchemaError(ValueError):
    """评测集 schema 校验失败（§1.2：全字段必填、无默认值，漏填在加载时报错）。

    用 ValueError 子类而非裸 ValueError：语义上是"数据文件内容不合法"，
    调用方（runner / T1 verify_gold）可按类型精确捕获；pytest.raises(ValueError)
    同样命中。
    """


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(f"评测集文件不存在：{path}")
    cases: list[dict] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DatasetSchemaError(f"评测集第 {lineno} 行不是合法 JSON：{exc}") from exc
        if not isinstance(obj, dict):
            raise DatasetSchemaError(f"评测集第 {lineno} 行不是 JSON 对象")
        cases.append(obj)
    if not cases:
        raise DatasetSchemaError(f"评测集为空：{path}")
    return cases


def _validate_common_fields(case: dict, where: str, required: tuple[str, ...]) -> None:
    """两类评测集共享的校验：必填字段 / ordered 布尔 / expected_chunks / standard_wrong。"""
    missing = [field for field in required if field not in case]
    if missing:
        raise DatasetSchemaError(
            f"{where} 缺少必填字段：{', '.join(missing)}（§1.2 schema 全字段必填、无默认值）"
        )

    if not isinstance(case["ordered"], bool):
        raise DatasetSchemaError(f"{where} 的 ordered 必须是布尔值 true/false（人工标注的结果集有序标志）")

    expected = case["expected_chunks"]
    if not isinstance(expected, list) or not expected:
        raise DatasetSchemaError(f"{where} 的 expected_chunks 必须是非空字符串列表")
    for chunk_id in expected:
        if not isinstance(chunk_id, str) or not chunk_id.strip():
            raise DatasetSchemaError(f"{where} 的 expected_chunks 含非字符串或空元素")

    wrong = case["standard_wrong"]
    if not isinstance(wrong, list):
        raise DatasetSchemaError(f"{where} 的 standard_wrong 必须是列表（非陷阱题为 []）")
    for entry in wrong:
        if not isinstance(entry, dict):
            raise DatasetSchemaError(f"{where} 的 standard_wrong 条目必须是对象")
        trap_id = entry.get("trap_id")
        if not isinstance(trap_id, str) or not trap_id.strip():
            raise DatasetSchemaError(f"{where} 的 standard_wrong 条目缺少非空 trap_id")
        if not isinstance(entry.get("description"), str):
            raise DatasetSchemaError(f"{where} 的 standard_wrong 条目（{trap_id}）缺少 description")
        # value 存字符串形式（§1.2），比较时才数值化——这里强制 str 防手写成数字
        if not isinstance(entry.get("value"), str) or not entry["value"].strip():
            raise DatasetSchemaError(f"{where} 的 standard_wrong 条目（{trap_id}）的 value 必须是非空字符串")

    if not isinstance(case["notes"], str):
        raise DatasetSchemaError(f"{where} 的 notes 必须是字符串（可以留空串）")


def _validate_chunk_ids(case: dict, where: str, valid_chunk_ids: set[str] | None) -> None:
    if valid_chunk_ids is None:
        return
    unknown = [c for c in case["expected_chunks"] if c not in valid_chunk_ids]
    if unknown:
        raise DatasetSchemaError(
            f"{where} 的 expected_chunks 含语料中不存在的 chunk id：{', '.join(unknown)}"
        )


def load_dataset(path: str | Path, valid_chunk_ids: set[str] | None = None) -> list[dict]:
    """加载单轮评测集（§1.2 schema），逐条校验，任何问题抛 DatasetSchemaError /
    FileNotFoundError。

    valid_chunk_ids：语料的真实 chunk id 集合（runner 从 retriever.ids 拿）；
    传入时校验 expected_chunks 逐条存在（§8-7），None 跳过该检查（裸机单测用）。
    """
    cases = _load_jsonl(Path(path))
    seen_ids: set[str] = set()
    for index, case in enumerate(cases, 1):
        where = f"评测集第 {index} 条（id={case.get('id', '?')}）"
        _validate_common_fields(case, where, _SINGLE_REQUIRED)

        case_id = case["id"]
        if not isinstance(case_id, str) or not _SINGLE_ID_RE.match(case_id):
            raise DatasetSchemaError(f"{where} 的 id 必须形如 EV-001 的三位序号，实际：{case_id!r}")
        if case_id in seen_ids:
            raise DatasetSchemaError(f"{where} 的 id 重复（报告/明细/复跑都按 id 寻址，必须唯一）")
        seen_ids.add(case_id)

        if not isinstance(case["question"], str) or not case["question"].strip():
            raise DatasetSchemaError(f"{where} 的 question 必须是非空字符串")
        if case["question_type"] not in _QUESTION_TYPES:
            legal = " / ".join(sorted(_QUESTION_TYPES))
            raise DatasetSchemaError(
                f"{where} 的 question_type 非法枚举：{case['question_type']!r}（合法值：{legal}）"
            )
        if not isinstance(case["gold_sql"], str) or not case["gold_sql"].strip():
            raise DatasetSchemaError(f"{where} 的 gold_sql 必须是非空字符串（MySQL 8 方言）")
        _validate_chunk_ids(case, where, valid_chunk_ids)
    return cases


def load_multiturn_dataset(path: str | Path, valid_chunk_ids: set[str] | None = None) -> list[dict]:
    """加载多轮评测集（§1.7 schema：turns + final_gold_sql），校验规则同单轮。"""
    cases = _load_jsonl(Path(path))
    seen_ids: set[str] = set()
    for index, case in enumerate(cases, 1):
        where = f"多轮评测集第 {index} 条（id={case.get('id', '?')}）"
        _validate_common_fields(case, where, _MULTITURN_REQUIRED)

        case_id = case["id"]
        if not isinstance(case_id, str) or not _MULTITURN_ID_RE.match(case_id):
            raise DatasetSchemaError(f"{where} 的 id 必须形如 MT-01 的两位序号，实际：{case_id!r}")
        if case_id in seen_ids:
            raise DatasetSchemaError(f"{where} 的 id 重复")
        seen_ids.add(case_id)

        turns = case["turns"]
        if not isinstance(turns, list) or len(turns) < 2:
            raise DatasetSchemaError(f"{where} 的 turns 必须是至少 2 轮的字符串列表（多轮题的定义）")
        if any(not isinstance(t, str) or not t.strip() for t in turns):
            raise DatasetSchemaError(f"{where} 的 turns 含非字符串或空轮次")
        if not isinstance(case["final_gold_sql"], str) or not case["final_gold_sql"].strip():
            raise DatasetSchemaError(f"{where} 的 final_gold_sql 必须是非空字符串（只判最后一轮）")
        _validate_chunk_ids(case, where, valid_chunk_ids)
    return cases


# ---------------------------------------------------------------------------
# 5. 指标小工具（设计 §2.3 / §2.6 / §2.7）
# ---------------------------------------------------------------------------


def join_retrieval_query(previous: str, current: str) -> str:
    """检索 query 拼接：与 main.py 的表达式逐字符同款（§2.3 / §2.7）。

    main.py：
        retrieval_query = f"{previous_question}\\n{user_input}".strip() if previous_question else user_input
    评测 runner 一律经本函数拼接，同构性由构造保证。
    """
    return f"{previous}\n{current}".strip() if previous else current


def sum_usage(messages: list) -> dict:
    """累加消息链里全部 AIMessage 的 usage_metadata（§2.6 token 采集口径）。

    实测结论（2026-10-09，langchain-deepseek 1.1.1 + langchain 1.4.3，inspect_chain 多次实证）：
    AIMessage.usage_metadata 存在且每条 AIMessage 各带一份，形如
        {'input_tokens': 1400, 'output_tokens': 136, 'total_tokens': 1536,
         'input_token_details': {'cache_read': 512}, 'output_token_details': {'reasoning': 56}}
    累加全部 AIMessage 即整题成本（Agent 多轮工具调用 = 多次 LLM 请求，每轮重发全部历史，
    累加才是这次查询的真实计费口径）；cache_read 已含在 input_tokens 里，单独记录不重复计。
    """
    totals = {"input": 0, "output": 0, "total": 0, "cache_read": 0}
    for msg in messages:
        usage = getattr(msg, "usage_metadata", None)
        if not usage:
            continue  # HumanMessage / ToolMessage / 老版本无 usage 的消息
        totals["input"] += int(usage.get("input_tokens") or 0)
        totals["output"] += int(usage.get("output_tokens") or 0)
        totals["total"] += int(usage.get("total_tokens") or 0)
        details = usage.get("input_token_details") or {}
        totals["cache_read"] += int(details.get("cache_read") or 0)
    return totals


def p95(values: list[float]) -> float:
    """P95 分位数（§2.6 / §8-5 拍板口径）：
    statistics.quantiles(data, n=20, method='inclusive')[18]。

    method='inclusive' 为线性插值（等价 Excel PERCENTILE.INC / numpy 默认 'linear'），
    编码时已用固定数组自测（tests/test_eval_judge.py::test_p95_fixed_array，
    30 样本下落在第 28.55 位，符合"第 29 位附近"的直觉）。报告需注明此口径。
    样本 < 2 时 quantiles 抛 StatisticsError，退化为样本自身。
    """
    if len(values) < 2:
        return float(values[0]) if values else 0.0
    return float(statistics.quantiles(values, n=20, method="inclusive")[18])

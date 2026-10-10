"""eval/judge.py 纯函数层的默认套件测试。

零依赖原则（W3 设计 §2.1）：judge.py 不 import 任何 agent 包，本文件也只 import
eval.judge——无 MySQL、无 Chroma、无 API key、不加载模型的裸机全绿。
评测集数据文件由 T1 工单产出，本文件全部用自造 fixture，不依赖真实数据集。

运行方式（在仓库根目录）：
    .venv/Scripts/python.exe -m pytest tests/test_eval_judge.py -q
"""

import json
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

import pytest

from eval.judge import (
    classify_failure,
    extract_execute_calls,
    join_retrieval_query,
    judge_case,
    load_dataset,
    load_multiturn_dataset,
    normalize_rows,
    p95,
    pick_sql,
    rows_equal,
    sum_usage,
)

# ---------------------------------------------------------------------------
# ① normalize_rows：规范化规则表（§2.4）
# ---------------------------------------------------------------------------


def test_normalize_numeric_types_via_decimal_str():
    """DECIMAL / int / float 统一走 Decimal(str(v))。"""
    rows = normalize_rows([(Decimal("1.50"), 42, 0.1)])
    assert rows == [[Decimal("1.50"), Decimal(42), Decimal("0.1")]]


def test_normalize_float_tail_noise_is_absorbed_by_str():
    """str() 消除 float 尾数：0.1 规范化为精确的 Decimal('0.1')，
    且 0.1+0.2 这类算术噪声在容差内与 0.3 判等。"""
    assert normalize_rows([(0.1,)]) == [[Decimal("0.1")]]
    assert rows_equal([[0.1 + 0.2]], [[Decimal("0.3")]])


def test_normalize_datetime_date_time_to_str():
    """datetime/date/time 规范化为字符串（DATE_FORMAT 产物本来就是 str）。
    fixture 用 naive datetime 是刻意的：MySQL 返回的就是 naive datetime。"""
    rows = normalize_rows(
        [
            (
                datetime(2026, 3, 15, 14, 22, 1),  # noqa: DTZ001 - 被测对象是 naive datetime 的 str()
                date(2026, 3, 15),
                time(14, 22, 1),
                "2026-03-15",
            )
        ]
    )
    assert rows == [["2026-03-15 14:22:01", "2026-03-15", "14:22:01", "2026-03-15"]]


def test_normalize_bytes_decode_utf8():
    assert normalize_rows([("武汉".encode(),)]) == [["武汉"]]


def test_normalize_none_and_str_untouched():
    assert normalize_rows([(None, "武汉", "")]) == [[None, "武汉", ""]]


def test_normalize_bool_does_not_crash_decimal_branch():
    """回归锁：bool 是 int 子类，必须先于数值分支处理
    （Decimal(str(True)) 会抛 InvalidOperation，实测确认）。"""
    assert normalize_rows([(True, False)]) == [["True", "False"]]


# ---------------------------------------------------------------------------
# ② rows_equal：判定规则（§2.4：列数 → 行数 → 有序逐位 / 无序多重集合）
# ---------------------------------------------------------------------------


def test_column_count_mismatch_fails():
    """投影数不一致是实质错误：金标 2 列 vs 模型 1 列 → fail。"""
    assert not rows_equal([(1, "a")], [(1,)], ordered=False)
    verdict = judge_case([(1, "a")], [(1,)])
    assert verdict["judge"] == "fail"
    assert verdict["failure_mode"] == "other"


def test_row_count_mismatch_fails():
    assert not rows_equal([(1,), (2,)], [(1,)], ordered=False)
    assert not rows_equal([(1,)], [(1,), (2,)], ordered=True)


def test_multiset_equality_with_duplicate_rows():
    """无序 = 多重集合比较：重复行各按次数对齐（§2.4 规则 5）。"""
    gold = [("a", 1), ("a", 1), ("b", 2)]
    model = [("b", 2), ("a", 1), ("a", 1)]
    assert rows_equal(gold, model, ordered=False)


def test_multiset_detects_duplicate_count_mismatch():
    """同为多重集合，次数不对必须判不等：{a×2, b×1} ≠ {a×1, b×2}。"""
    assert not rows_equal([("a",), ("a",), ("b",)], [("a",), ("b",), ("b",)], ordered=False)


def test_ordered_compare_is_positional():
    """ordered=true：同多重集但顺序不同 → 判不等（§2.4 规则 4）。"""
    gold = [(1, "a"), (2, "b")]
    model = [(2, "b"), (1, "a")]
    assert rows_equal(gold, model, ordered=False)
    assert not rows_equal(gold, model, ordered=True)


def test_ordered_identical_order_passes():
    gold = [(1, "a"), (2, "b")]
    assert rows_equal(gold, [(1, "a"), (2, "b")], ordered=True)


def test_unordered_mode_still_requires_column_order():
    """无序比较只忽略行序，不忽略列序：(None,'x') ≠ ('x',None)。"""
    assert not rows_equal([(None, "x")], [("x", None)], ordered=False)


def test_both_empty_result_sets_equal():
    """两边都空 = 判等（题面条件筛空的合法结果）；一边空一边非空 = 行数不等。"""
    assert rows_equal([], [], ordered=False)
    assert not rows_equal([], [(1,)], ordered=False)


def test_mixed_type_rows_can_be_sorted_and_compared():
    """同列金标是数值、模型是字符串的脏情形：排序不炸（类型分桶），逐位判不等。"""
    assert not rows_equal([(1,)], [("1",)], ordered=False)


# ---------------------------------------------------------------------------
# ③ 数值容差（§2.4 公式）与"容差不放行陷阱值"回归（W3 验收点名用例）
# ---------------------------------------------------------------------------


def test_tolerance_absolute_boundaries():
    """绝对下限 0.005：|a-b| 恰在界内判等、界外判不等。"""
    assert rows_equal([(Decimal("1.000"),)], [(Decimal("1.004"),)])
    assert not rows_equal([(Decimal("1.000"),)], [(Decimal("1.006"),)])


def test_tolerance_relative_boundaries():
    """相对容差 1e-3：大数值按比例放宽（吸收 ROUND 位数差异）。"""
    assert rows_equal([(Decimal(10000),)], [(Decimal(10010),)])  # 差 10 ≤ 10.01
    assert not rows_equal([(Decimal(10000),)], [(Decimal(10011),)])  # 差 11 > 10.011


@pytest.mark.parametrize(
    ("gold", "model"),
    [
        # EV-030 订单口径金标 vs 金额口径（valid_alt_gloss 值）
        ("0.0886", "0.0775"),
        # EV-004 金额口径金标 vs status 口径陷阱值
        ("0.0775", "0.0565"),
        # EV-003 GMV 金标 vs order_amount 口径陷阱值（20% 量级差异）
        ("16029955.34", "20280681.72"),
    ],
)
def test_tolerance_never_passes_registered_trap_values(gold: str, model: str):
    """验收点名回归：容差不得把任何登记陷阱值放行为 pass（间距 20% 量级 >> 容差）。"""
    assert not rows_equal([(Decimal(gold),)], [(Decimal(model),)])
    assert not rows_equal([(Decimal(model),)], [(Decimal(gold),)])


def test_string_cells_require_exact_match():
    """字符串单元格（城市/类目/DATE_FORMAT 产物）不做容差。"""
    assert rows_equal([("武汉",)], [("武汉",)])
    assert not rows_equal([("武汉",)], [("汉口",)])


def test_none_cells_equal_each_other():
    assert rows_equal([(None, 1)], [(None, 1)], ordered=False)


# ---------------------------------------------------------------------------
# ④ classify_failure / judge_case：错答模式三档 + error（§4.3 / §2.2）
# ---------------------------------------------------------------------------

_EV029_WRONG = [
    {"trap_id": "no_refund_deduction", "description": "答成 GMV（未冲减退款）", "value": "16029955.34"},
    {"trap_id": "orders_amount_basis", "description": "orders 口径冲减", "value": "19038651.57"},
]


def test_classify_failure_returns_trap_id_on_match():
    """档一：命中登记值 → trap:<trap_id>。"""
    mode = classify_failure([(Decimal("16029955.34"),)], _EV029_WRONG)
    assert mode == "trap:no_refund_deduction"


def test_classify_failure_valid_alt_gloss_entry():
    """档二：命中但 trap_id 标注为合法口径 → valid_alt_gloss（§4.3 单独统计）。"""
    wrong = [{"trap_id": "valid_alt_gloss", "description": "订单口径（合法）但答非所问", "value": "0.0886"}]
    assert classify_failure([(Decimal("0.0886"),)], wrong) == "valid_alt_gloss"


def test_classify_failure_no_match_returns_none():
    """档三：都不匹配 → None（调用方记 other）。"""
    assert classify_failure([(Decimal("12345.67"),)], _EV029_WRONG) is None


def test_classify_failure_non_scalar_rows_return_none():
    """登记值全是标量：多行/多列结果不参与标量比对。"""
    assert classify_failure([(Decimal("16029955.34"),), (Decimal(1),)], _EV029_WRONG) is None
    assert classify_failure([(Decimal("16029955.34"), "extra")], _EV029_WRONG) is None


def test_classify_failure_matches_within_tolerance():
    """登记值匹配走与结果集相同的规范化与容差（§4.3）：ROUND 位数差异不另立新模式。"""
    wrong = [{"trap_id": "month_shift_aug", "description": "答成 8 月", "value": "16164482.43"}]
    assert classify_failure([(Decimal("16164482.435"),)], wrong) == "trap:month_shift_aug"


def test_judge_case_pass():
    verdict = judge_case([(1, "a")], [(1, "a")], ordered=True)
    assert verdict == {"judge": "pass", "failure_mode": None, "error_msg": None}


def test_judge_case_fail_with_trap_mode():
    verdict = judge_case(
        [(Decimal("14787925.19"),)],
        [(Decimal("16029955.34"),)],
        ordered=False,
        standard_wrong=_EV029_WRONG,
    )
    assert verdict["judge"] == "fail"
    assert verdict["failure_mode"] == "trap:no_refund_deduction"


def test_judge_case_fail_other_when_unregistered():
    verdict = judge_case([(Decimal(1),)], [(Decimal(2),)], standard_wrong=_EV029_WRONG)
    assert verdict["judge"] == "fail"
    assert verdict["failure_mode"] == "other"


def test_judge_case_error_subcodes():
    """error 三子码由调用方传入（no_usable_sql / replay_error / invoke_exception）。"""
    for code in ("no_usable_sql", "replay_error", "invoke_exception"):
        verdict = judge_case([(1,)], None, error_msg=code)
        assert verdict == {"judge": "error", "failure_mode": None, "error_msg": code}


def test_judge_case_error_defaults_to_no_usable_sql():
    verdict = judge_case([(1,)], None)
    assert verdict["error_msg"] == "no_usable_sql"


# ---------------------------------------------------------------------------
# ⑤ SQL 提取与三级取用规则（§2.2）
# ---------------------------------------------------------------------------


class _FakeMessage:
    """duck-typed 消息：只暴露 tool_calls / usage_metadata 两个被消费的属性。"""

    def __init__(self, tool_calls=None, usage_metadata=None):
        self.tool_calls = tool_calls
        self.usage_metadata = usage_metadata


def test_extract_execute_calls_filters_and_numbers():
    """只收 execute_readonly_sql；list_tables 跳过；seq 按发生顺序编号。"""
    messages = [
        _FakeMessage(tool_calls=[{"name": "list_tables", "args": {}}]),
        _FakeMessage(tool_calls=[{"name": "execute_readonly_sql", "args": {"sql": "SELECT 1"}}]),
        _FakeMessage(tool_calls=None),  # ToolMessage 无 tool_calls
        _FakeMessage(tool_calls=[{"name": "execute_readonly_sql", "args": {"sql": "SELECT 2"}}]),
        _FakeMessage(),  # HumanMessage 连属性都没有
    ]
    assert extract_execute_calls(messages) == [
        {"seq": 1, "sql": "SELECT 1"},
        {"seq": 2, "sql": "SELECT 2"},
    ]


def test_extract_execute_calls_skips_malformed_call_without_sql_arg():
    messages = [_FakeMessage(tool_calls=[{"name": "execute_readonly_sql", "args": {}}])]
    assert extract_execute_calls(messages) == []


def test_pick_sql_last_replay_ok():
    """规则一：最后一条重放成功 → 直接判定。"""
    calls = [{"seq": 1, "sql": "SELECT A"}, {"seq": 2, "sql": "SELECT B"}]
    sql, source = pick_sql(calls, lambda s: [("x",)] if s == "SELECT B" else None)
    assert (sql, source) == ("SELECT B", "last")


def test_pick_sql_falls_back_to_latest_earlier_success():
    """规则二：最后一条失败 → 回退到最后一条成功的更早调用。"""
    calls = [
        {"seq": 1, "sql": "SELECT A"},
        {"seq": 2, "sql": "SELECT B"},
        {"seq": 3, "sql": "SELECT C"},
    ]

    def replay(sql: str):
        return [("x",)] if sql == "SELECT A" else None  # B、C 都失败

    assert pick_sql(calls, replay) == ("SELECT A", "fallback_to_earlier")


def test_pick_sql_all_failed_is_no_usable_sql():
    calls = [{"seq": 1, "sql": "SELECT A"}, {"seq": 2, "sql": "SELECT B"}]
    assert pick_sql(calls, lambda _s: None) == (None, "no_usable_sql")


def test_pick_sql_no_calls_at_all_is_no_usable_sql():
    """拒答/纯文字回答：一条都没调 → error（不解析 content 去救，§2.2）。"""
    assert pick_sql([], lambda _s: [("x",)]) == (None, "no_usable_sql")


# ---------------------------------------------------------------------------
# ⑥ 多轮 query 拼接（§2.3 / §2.7，与 main.py 逐字符同款）
# ---------------------------------------------------------------------------


def test_join_retrieval_query_first_turn_is_bare_input():
    """首轮 previous_human_message 为空串 → query 就是当前输入。"""
    assert join_retrieval_query("", "2026 年 9 月的退款率是多少？") == "2026 年 9 月的退款率是多少？"


def test_join_retrieval_query_follow_up_joins_with_newline():
    """多轮：query = 上一条用户消息 + 换行 + 当前输入（指代式短问的消歧拼接）。"""
    assert join_retrieval_query("上个月 GMV 多少？", "那环比呢") == "上个月 GMV 多少？\n那环比呢"


# ---------------------------------------------------------------------------
# ⑦ 评测集加载与 schema 校验（§1.2 单轮 / §1.7 多轮）
# ---------------------------------------------------------------------------


def _valid_case(**overrides) -> dict:
    case = {
        "id": "EV-001",
        "question": "2026 年 3 月每天的订单量是多少？",
        "question_type": "time_window",
        "gold_sql": "SELECT DATE_FORMAT(created_at, '%Y-%m-%d') AS d, COUNT(*) FROM orders GROUP BY d ORDER BY d",
        "ordered": True,
        "expected_chunks": ["schema:orders"],
        "standard_wrong": [],
        "notes": "左闭右开 + DATE_FORMAT 按天",
    }
    case.update(overrides)
    return case


def _valid_mt_case(**overrides) -> dict:
    case = {
        "id": "MT-01",
        "turns": ["上个月 GMV 多少？", "那环比呢"],
        "final_gold_sql": "SELECT DATE_FORMAT(paid_at, '%Y-%m') AS m, ROUND(SUM(amount), 2) AS gmv"
        " FROM payments GROUP BY m ORDER BY m",
        "ordered": True,
        "expected_chunks": ["glossary:yoy_mom", "qa_pair:qa_003"],
        "standard_wrong": [
            {"trap_id": "month_shift_aug", "description": "把'上个月'答成 8 月", "value": "16164482.43"}
        ],
        "notes": "复刻 W2 验收两连问",
    }
    case.update(overrides)
    return case


def _write_jsonl(path: Path, cases: list[dict]) -> Path:
    path.write_text(
        "\n".join(json.dumps(case, ensure_ascii=False) for case in cases) + "\n",
        encoding="utf-8",
    )
    return path


def test_load_dataset_ok_preserves_order_and_fields(tmp_path: Path):
    path = _write_jsonl(tmp_path / "eval_v1.jsonl", [_valid_case(), _valid_case(id="EV-002", ordered=False)])
    cases = load_dataset(path)
    assert [c["id"] for c in cases] == ["EV-001", "EV-002"]
    assert cases[1]["ordered"] is False


def test_load_dataset_missing_field_raises_with_location(tmp_path: Path):
    bad = _valid_case()
    del bad["notes"]  # 全字段必填、无默认值（§1.2）
    path = _write_jsonl(tmp_path / "d.jsonl", [bad])
    with pytest.raises(ValueError) as excinfo:
        load_dataset(path)
    assert "notes" in str(excinfo.value)


def test_load_dataset_bad_enum_raises(tmp_path: Path):
    path = _write_jsonl(tmp_path / "d.jsonl", [_valid_case(question_type="join")])
    with pytest.raises(ValueError) as excinfo:
        load_dataset(path)
    assert "question_type" in str(excinfo.value)
    assert "join" in str(excinfo.value)


def test_load_dataset_bad_id_format_raises(tmp_path: Path):
    path = _write_jsonl(tmp_path / "d.jsonl", [_valid_case(id="EV1")])
    with pytest.raises(ValueError, match="EV-001"):
        load_dataset(path)


def test_load_dataset_duplicate_id_raises(tmp_path: Path):
    path = _write_jsonl(tmp_path / "d.jsonl", [_valid_case(), _valid_case()])
    with pytest.raises(ValueError, match="重复"):
        load_dataset(path)


def test_load_dataset_ordered_must_be_bool(tmp_path: Path):
    path = _write_jsonl(tmp_path / "d.jsonl", [_valid_case(ordered="false")])
    with pytest.raises(ValueError, match="ordered"):
        load_dataset(path)


def test_load_dataset_unknown_chunk_id_raises_only_when_ids_given(tmp_path: Path):
    """§8-7：valid_chunk_ids 传入时校验 expected_chunks 逐条存在；None 跳过（裸机单测）。"""
    path = _write_jsonl(tmp_path / "d.jsonl", [_valid_case(expected_chunks=["schema:userz"])])
    with pytest.raises(ValueError, match="schema:userz"):
        load_dataset(path, valid_chunk_ids={"schema:orders", "glossary:gmv"})
    assert load_dataset(path, valid_chunk_ids=None)[0]["id"] == "EV-001"


def test_load_dataset_standard_wrong_entry_missing_value_raises(tmp_path: Path):
    bad_entry = {"trap_id": "x", "description": "y"}  # 缺 value
    path = _write_jsonl(tmp_path / "d.jsonl", [_valid_case(standard_wrong=[bad_entry])])
    with pytest.raises(ValueError, match="value"):
        load_dataset(path)


def test_load_dataset_standard_wrong_value_must_be_string(tmp_path: Path):
    """value 存字符串形式（§1.2，比较时才数值化）——手写成数字在加载时就拦下。"""
    bad_entry = {"trap_id": "x", "description": "y", "value": 0.0775}
    path = _write_jsonl(tmp_path / "d.jsonl", [_valid_case(standard_wrong=[bad_entry])])
    with pytest.raises(ValueError, match="value"):
        load_dataset(path)


def test_load_dataset_empty_file_raises(tmp_path: Path):
    path = tmp_path / "d.jsonl"
    path.write_text("\n\n", encoding="utf-8")
    with pytest.raises(ValueError, match="为空"):
        load_dataset(path)


def test_load_dataset_missing_file_raises_filenotfound(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_dataset(tmp_path / "nope.jsonl")


def test_load_multiturn_dataset_ok(tmp_path: Path):
    path = _write_jsonl(tmp_path / "mt.jsonl", [_valid_mt_case()])
    cases = load_multiturn_dataset(path)
    assert cases[0]["id"] == "MT-01"
    assert cases[0]["turns"] == ["上个月 GMV 多少？", "那环比呢"]


def test_load_multiturn_requires_at_least_two_turns(tmp_path: Path):
    path = _write_jsonl(tmp_path / "mt.jsonl", [_valid_mt_case(turns=["只有一个问题"])])
    with pytest.raises(ValueError, match="turns"):
        load_multiturn_dataset(path)


def test_load_multiturn_missing_final_gold_sql_raises(tmp_path: Path):
    bad = _valid_mt_case()
    del bad["final_gold_sql"]
    path = _write_jsonl(tmp_path / "mt.jsonl", [bad])
    with pytest.raises(ValueError, match="final_gold_sql"):
        load_multiturn_dataset(path)


def test_load_multiturn_rejects_single_turn_loader_on_single_file(tmp_path: Path):
    """单轮加载器不吃多轮 schema（缺 question 字段），多轮加载器不吃单轮 schema。"""
    single_path = _write_jsonl(tmp_path / "s.jsonl", [_valid_case()])
    with pytest.raises(ValueError):
        load_multiturn_dataset(single_path)
    mt_path = _write_jsonl(tmp_path / "m.jsonl", [_valid_mt_case()])
    with pytest.raises(ValueError):
        load_dataset(mt_path)


# ---------------------------------------------------------------------------
# ⑧ 指标小工具：sum_usage（§2.6 实测口径）/ p95（§8-5 拍板口径）
# ---------------------------------------------------------------------------


def test_sum_usage_accumulates_every_ai_message_with_cache_read_separate():
    """实测形态逐字段回放：每条 AIMessage 各带一份 usage；cache_read 单列不重复计。"""
    messages = [
        _FakeMessage(
            usage_metadata={
                "input_tokens": 1400,
                "output_tokens": 136,
                "total_tokens": 1536,
                "input_token_details": {"cache_read": 512},
                "output_token_details": {"reasoning": 56},
            }
        ),
        _FakeMessage(
            usage_metadata={
                "input_tokens": 1500,
                "output_tokens": 100,
                "total_tokens": 1600,
                "input_token_details": {"cache_read": 600},
            }
        ),
        _FakeMessage(),  # HumanMessage / ToolMessage：无 usage，跳过
    ]
    assert sum_usage(messages) == {
        "input": 2900,
        "output": 236,
        "total": 3136,
        "cache_read": 1112,
    }


def test_sum_usage_empty_messages():
    assert sum_usage([]) == {"input": 0, "output": 0, "total": 0, "cache_read": 0}


def test_p95_fixed_array_matches_linear_interpolation():
    """§8-5 自测锁：quantiles(n=20, method='inclusive')[18] = 线性插值口径。
    30 样本 1..30 的 P95 落在第 27.55 位（0 起）→ 28 + 0.55 × 1 = 28.55，
    即"第 29 位附近"的直觉（报告需注明此口径）。"""
    assert p95(list(range(1, 31))) == pytest.approx(28.55)
    assert p95([11.2, 23.4, 15.1, 92.3, 44.7, 30.5, 61.8, 8.9]) == pytest.approx(81.625)


def test_p95_small_samples_do_not_crash():
    """样本 <2 时 quantiles 会抛 StatisticsError，退化为样本自身。"""
    assert p95([7.5]) == 7.5
    assert p95([]) == 0.0

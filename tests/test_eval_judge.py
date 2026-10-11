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
    classify_failure_from_text,
    extract_execute_calls,
    extract_final_answer_text,
    extract_number_candidates,
    join_retrieval_query,
    judge_case,
    load_dataset,
    load_multiturn_dataset,
    normalize_rows,
    p95,
    pick_sql,
    rows_equal,
    sum_usage,
    text_contains_gold,
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
# ③' 判定器 v2.1 回归（W6 设计 §1.1：无序比较排序键数值化分桶）
# ---------------------------------------------------------------------------


def test_v21_sort_key_numeric_order_fixes_false_negative():
    """设计 §1.1 可复现假阴性用例（W3 审查 P2-1 的实锤）：

    金标 {9.996, 11} vs 模型 {10, 11}（9.996 与 10 满足容差 max(0.005, 1e-3×10)=0.01）。
    v2.0 两侧 str 排序 → 金标 [11, 9.996] vs 模型 [10, 11]，首对 (11 vs 10) 错配 →
    误判 fail；v2.1 数值序为主键 → [9.996, 11] vs [10, 11] 逐位容差内相等 → pass。
    """
    gold = [(Decimal("9.996"),), (Decimal(11),)]
    model = [(Decimal(10),), (Decimal(11),)]
    assert rows_equal(gold, model, ordered=False)
    verdict = judge_case(gold, model, ordered=False)
    assert verdict["judge"] == "pass"
    assert verdict["pass_via"] == "sql"


def test_v21_sort_key_is_numeric_not_lexicographic():
    """排序键形态自检：数值桶内 (0, Decimal, str)——数值序为主、str 仅在数值相等时
    决胜（Decimal('1.0') 与 Decimal('1.00') 数值相等，str 决胜保证顺序确定）。
    _row_sort_key 返回逐 cell 键组成的外层元组（一行一元组）。"""
    from eval.judge import _row_sort_key

    assert _row_sort_key([Decimal("9.996")]) == ((0, Decimal("9.996"), "9.996"),)
    assert _row_sort_key([Decimal("1.0")])[0] < _row_sort_key([Decimal("1.00")])[0]  # str 决胜
    assert _row_sort_key([Decimal(11)])[0] > _row_sort_key([Decimal("9.996")])[0]  # 数值序而非字典序
    assert _row_sort_key(["武汉"]) == ((1, "武汉"),)
    assert _row_sort_key([None]) == ((2, ""),)


def test_v21_sort_key_nonfinite_does_not_raise():
    """NaN/Infinity 不进数值序（Decimal 排序会抛 InvalidOperation），按 str 决胜——
    含非有限值的多行结果集排序与无序比较不抛错（W6 §8-3 验证项）。"""
    from eval.judge import _row_sort_key

    rows = [[Decimal("NaN")], [Decimal(5)], [Decimal("-Infinity")], [Decimal("Infinity")]]
    sorted_rows = sorted(rows, key=_row_sort_key)  # 不抛 InvalidOperation
    assert len(sorted_rows) == 4
    # 无序比较含 NaN 行：NaN 走恒等比较（_cells_equal 的非有限分支），判不等不炸
    assert not rows_equal(
        [(Decimal("NaN"),), (Decimal(1),)], [(Decimal(1),), (Decimal(2),)], ordered=False
    )


def test_v21_tolerance_rule_itself_unchanged():
    """修复只动排序键，逐位比较仍走 _cells_equal 的容差公式（设计 §1.1 红线）：
    0.0775 vs 0.0886（陷阱间距）依然判不等，容差不放行陷阱值。"""
    assert not rows_equal([(Decimal("0.0775"),)], [(Decimal("0.0886"),)], ordered=False)
    assert not rows_equal(
        [(Decimal("0.0886"),), (Decimal(1),)], [(Decimal("0.0775"),), (Decimal(2),)], ordered=False
    )


def test_v21_multiset_with_mixed_formats_passes():
    """等值不同格式的多行无序集合（v2.0 假阴性的泛化形态）：金标带小数尾、
    模型整数值（容差内相等）+ 共同行，修复后判 pass。"""
    gold = [(Decimal("100.004"),), (Decimal(7),), (Decimal("-2.5"),)]
    model = [(Decimal("-2.5"),), (Decimal(100),), (Decimal("7.003"),)]
    assert rows_equal(gold, model, ordered=False)


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
    assert verdict == {
        "judge": "pass",
        "failure_mode": None,
        "error_msg": None,
        "pass_via": "sql",
        "text_matched": None,
    }


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
        assert verdict == {
            "judge": "error",
            "failure_mode": None,
            "error_msg": code,
            "pass_via": None,
            "text_matched": None,
        }


def test_judge_case_error_defaults_to_no_usable_sql():
    verdict = judge_case([(1,)], None)
    assert verdict["error_msg"] == "no_usable_sql"


# ---------------------------------------------------------------------------
# ④' 答案级回退（§2.4 判定规则 v2）：文本数字候选 / 金标比对 / 主流程集成
# ---------------------------------------------------------------------------


def test_extract_number_candidates_thousands_commas():
    """千分位去逗号："16,029,955.34" → 16029955.34。"""
    assert extract_number_candidates("GMV 为 16,029,955.34 元") == [Decimal("16029955.34")]


def test_extract_number_candidates_plain_number_not_truncated():
    """回归锁：千分位形态不得把普通整数掐头去尾（16029955.34 ≠ 160）。"""
    assert extract_number_candidates("16029955.34") == [Decimal("16029955.34")]
    assert extract_number_candidates("-1234 与 5678") == [Decimal(-1234), Decimal(5678)]


def test_extract_number_candidates_percent_produces_dual_candidates():
    """带 % 后缀产生两个候选（原值与 /100）："7.75%" → [7.75, 0.0775]。"""
    assert extract_number_candidates("退款率 7.75%") == [Decimal("7.75"), Decimal("0.0775")]


def test_extract_number_candidates_negative_and_order():
    """负号由文本携带；候选按出现位置保序。"""
    assert extract_number_candidates("环比 -77.50，基期 100") == [
        Decimal("-77.50"),
        Decimal(100),
    ]


def test_extract_number_candidates_dedupe_preserves_first_occurrence():
    assert extract_number_candidates("7.75% 或 0.0775 或 7.75%") == [
        Decimal("7.75"),
        Decimal("0.0775"),
    ]


def test_extract_number_candidates_no_numbers_returns_empty():
    assert extract_number_candidates("抱歉，无法回答这个问题") == []
    assert extract_number_candidates("") == []


def test_text_contains_gold_percent_fraction_hit_via_div100_candidate():
    """金标 0.0775 vs 文本 "7.75%"：经 /100 候选命中，返回命中的候选值。"""
    assert text_contains_gold("9 月退款率约为 7.75%", Decimal("0.0775")) == Decimal("0.0775")


def test_text_contains_gold_negative_hit():
    """负数金标：负号由文本携带（"-0.83%" → [-0.83, -0.0083]）。

    注：任务样例写的 "下降 0.83%" 无法按规格命中——"下降"是语义负号，
    §2.4 v2 的抽取规则只认正则的 -? 前缀，不做词语级符号推断。
    """
    assert text_contains_gold("环比 -0.83%", Decimal("-0.83")) == Decimal("-0.83")
    assert text_contains_gold("下降 0.83%", Decimal("-0.83")) is None


def test_text_contains_gold_thousands_hit():
    assert text_contains_gold("净销售额为 16,029,955.34 元", Decimal("16029955.34")) == Decimal(
        "16029955.34"
    )


def test_text_contains_gold_miss_when_only_other_value_present():
    """金标 0.0886 vs 只有 "7.75%" 的文本：/100 候选 0.0775 也不命中。"""
    assert text_contains_gold("退款率为 7.75%", Decimal("0.0886")) is None


def test_text_contains_gold_out_of_tolerance_miss():
    """容差外不命中：10000 vs 10011（相对容差 1e-3 → 10.01 < 11）。"""
    assert text_contains_gold("结果为 10011", Decimal(10000)) is None


def test_text_contains_gold_non_numeric_text_or_gold():
    """文本无数字候选、或金标非数值（str 月份串 / None）→ None（设计明文不回退）。"""
    assert text_contains_gold("抱歉无法计算", Decimal("0.0775")) is None
    assert text_contains_gold("2026-09 的答案是…", "2026-09") is None
    assert text_contains_gold("答案是 123", None) is None
    assert text_contains_gold(None, Decimal("0.0775")) is None


def test_judge_sql_fail_text_hit_passes_via_text():
    """EV-025 实测场景：最后一次 SQL 返回两个月数据行（行数不符 SQL 级 fail），
    最终文本算出环比 77.50% 与金标一致 → pass 且 pass_via="text"。"""
    gold = [(Decimal("77.50"),)]
    model = [("2025-10", 62647), ("2025-11", 111168)]  # 金标 1 行 vs 模型 2 行
    verdict = judge_case(
        gold,
        model,
        ordered=False,
        final_answer_text="2025 年 11 月订单量环比 10 月增长 77.50%。",
    )
    assert verdict["judge"] == "pass"
    assert verdict["pass_via"] == "text"
    assert verdict["failure_mode"] is None
    assert verdict["text_matched"] == Decimal("77.50")


def test_judge_no_usable_sql_text_hit_passes_via_text():
    """规则一：无可用 SQL → 先试答案级回退，命中即 pass_via="text"（不判 error）。"""
    verdict = judge_case(
        [(Decimal("0.0775"),)],
        None,
        error_msg="no_usable_sql",
        final_answer_text="9 月退款率约 7.75%",
    )
    assert verdict == {
        "judge": "pass",
        "failure_mode": None,
        "error_msg": None,
        "pass_via": "text",
        "text_matched": Decimal("0.0775"),
    }


def test_judge_no_usable_sql_text_miss_stays_error():
    verdict = judge_case(
        [(Decimal("0.0775"),)], None, error_msg="no_usable_sql", final_answer_text="抱歉，无法回答"
    )
    assert verdict["judge"] == "error"
    assert verdict["error_msg"] == "no_usable_sql"
    assert verdict["pass_via"] is None


def test_judge_sql_fail_text_miss_but_text_hits_standard_wrong_trap():
    """SQL 结果与文本都不命中金标，但文本报出了登记的 GMV 陷阱值 → trap 模式（规则五）。"""
    verdict = judge_case(
        [(Decimal("14787925.19"),)],
        [(Decimal(100),)],  # SQL 结果既非金标也非任何登记值
        standard_wrong=_EV029_WRONG,
        final_answer_text="净销售额为 16,029,955.34 元",
    )
    assert verdict["judge"] == "fail"
    assert verdict["failure_mode"] == "trap:no_refund_deduction"


def test_judge_sql_result_trap_match_takes_priority_over_text():
    """failure_mode 优先级：模型 SQL 结果命中登记值（v1 路径）优先于文本候选。"""
    wrong = [
        {"trap_id": "refund_status_basis", "description": "状态口径陷阱", "value": "0.0565"},
        {"trap_id": "valid_alt_gloss", "description": "金额口径（合法）但答非所问", "value": "0.0775"},
    ]
    verdict = judge_case(
        [(Decimal("0.0886"),)],
        [(Decimal("0.0565"),)],  # SQL 结果命中档一 trap；文本命中的是 valid_alt_gloss
        standard_wrong=wrong,
        final_answer_text="退款率约为 7.75%",
    )
    assert verdict["judge"] == "fail"
    assert verdict["failure_mode"] == "trap:refund_status_basis"


def test_judge_text_hit_valid_alt_gloss():
    """文本命中"合法他口径"登记值 → valid_alt_gloss（§4.3 与 trap 分开统计）。"""
    wrong = [{"trap_id": "valid_alt_gloss", "description": "金额口径（合法）但答非所问", "value": "0.0775"}]
    verdict = judge_case(
        [(Decimal("0.0886"),)],
        [(Decimal("0.0565"),)],
        standard_wrong=wrong,
        final_answer_text="退款率约为 7.75%",
    )
    assert verdict["failure_mode"] == "valid_alt_gloss"


def test_judge_multicolumn_gold_never_falls_back_to_text():
    """规则四：多行/多列金标不走文本回退——即使文本含金标数字也维持 fail。

    （模型第二个月的值须偏离出相对容差 1e-3，SQL 级才是真 fail：111168 vs 120000。）"""
    gold = [("2025-10", Decimal(62647)), ("2025-11", Decimal(111168))]
    verdict = judge_case(
        gold,
        [("2025-10", Decimal(62647)), ("2025-11", Decimal(120000))],
        ordered=True,
        final_answer_text="两个月订单量分别是 62,647 和 111,168",
    )
    assert verdict["judge"] == "fail"
    assert verdict["pass_via"] is None
    assert verdict["failure_mode"] == "other"


def test_judge_str_gold_not_eligible_for_text_fallback():
    """str 金标（DATE_FORMAT 输出的月份字符串）不适用文本回退。"""
    verdict = judge_case([("2026-09",)], [("2026-08",)], final_answer_text="答案是 2026-09")
    assert verdict["judge"] == "fail"
    assert verdict["pass_via"] is None


def test_judge_sql_level_pass_reports_pass_via_sql():
    """SQL 级判定优先：结果集一致即 pass_via="sql"，不看文本。"""
    verdict = judge_case([(Decimal("0.0775"),)], [(Decimal("0.0775"),)], final_answer_text="7.75%")
    assert verdict == {
        "judge": "pass",
        "failure_mode": None,
        "error_msg": None,
        "pass_via": "sql",
        "text_matched": None,
    }


def test_judge_without_final_answer_text_keeps_v1_semantics():
    """调用方未提供 final_answer_text（旧调用方/存量用例）→ 与 v1 行为逐字段一致。"""
    verdict = judge_case([(Decimal("14787925.19"),)], [(Decimal(2),)], standard_wrong=_EV029_WRONG)
    assert verdict == {"judge": "fail", "failure_mode": "other", "error_msg": None,
                       "pass_via": None, "text_matched": None}


def test_classify_failure_from_text_matches_registered_value():
    wrong = [{"trap_id": "month_shift_aug", "description": "把'上个月'答成 8 月", "value": "16164482.43"}]
    assert classify_failure_from_text("8 月 GMV 为 16,164,482.43 元", wrong) == "trap:month_shift_aug"
    assert classify_failure_from_text("GMV 是 20,280,681.72 元", wrong) is None
    assert classify_failure_from_text("无法回答", wrong) is None
    assert classify_failure_from_text("16,164,482.43", []) is None


class _TypedMessage:
    """duck-typed 消息：带 langchain 风格 type 属性（extract_final_answer_text 消费）。"""

    def __init__(self, content, mtype="ai"):
        self.content = content
        self.type = mtype


def test_extract_final_answer_text_takes_last_ai_message_only():
    """只看最后一条 AIMessage（链路里更早的数字是中间量，§2.4 v2）。"""
    messages = [
        _TypedMessage("中间思考：先算基期 62647"),
        _TypedMessage("工具结果", mtype="tool"),
        _TypedMessage("最终答案：环比 77.50%"),
    ]
    assert extract_final_answer_text(messages) == "最终答案：环比 77.50%"


def test_extract_final_answer_text_content_list_blocks_strified():
    """content 为多模态块 list：text 块抽取、其余块 str() 化（防御分支）。"""
    msg = _TypedMessage([{"type": "text", "text": "净销售额 "}, {"type": "image_url"}, "raw"])
    assert extract_final_answer_text([msg]) == "净销售额 \n{'type': 'image_url'}\nraw"


def test_extract_final_answer_text_empty_without_ai_message():
    assert extract_final_answer_text([_TypedMessage("hi", mtype="human")]) == ""
    assert extract_final_answer_text([]) == ""


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


# ---------------------------------------------------------------------------
# ⑦' generalize_layer 可选字段的兼容与枚举校验（W6 §2.3 / §8-10）
# ---------------------------------------------------------------------------


def test_load_dataset_without_generalize_layer_still_loads(tmp_path: Path):
    """旧格式文件（无该字段）加载不报错——向后兼容，缺失 = 不细分（旧行为）。"""
    path = _write_jsonl(tmp_path / "d.jsonl", [_valid_case()])
    assert "generalize_layer" not in load_dataset(path)[0]


def test_load_dataset_generalize_layer_valid_enum_passes(tmp_path: Path):
    path = _write_jsonl(
        tmp_path / "d.jsonl",
        [
            _valid_case(generalize_layer="hit"),
            _valid_case(id="EV-002", ordered=False, generalize_layer="near"),
            _valid_case(id="EV-003", ordered=False, generalize_layer="far"),
        ],
    )
    cases = load_dataset(path)
    assert [c["generalize_layer"] for c in cases] == ["hit", "near", "far"]


def test_load_dataset_generalize_layer_bad_enum_raises(tmp_path: Path):
    path = _write_jsonl(tmp_path / "d.jsonl", [_valid_case(generalize_layer="middle")])
    with pytest.raises(ValueError, match="generalize_layer"):
        load_dataset(path)


def test_load_multiturn_dataset_ignores_generalize_layer(tmp_path: Path):
    """多轮数据集不带该字段（W6 §2.3 只约束单轮 EV 题）：无字段加载不受影响；
    万一写了也按同一枚举校验（共享校验路径）。"""
    path = _write_jsonl(tmp_path / "mt.jsonl", [_valid_mt_case()])
    assert "generalize_layer" not in load_multiturn_dataset(path)[0]
    bad = _write_jsonl(
        tmp_path / "mt2.jsonl", [_valid_mt_case(generalize_layer="layer-x")]
    )
    with pytest.raises(ValueError, match="generalize_layer"):
        load_multiturn_dataset(bad)


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

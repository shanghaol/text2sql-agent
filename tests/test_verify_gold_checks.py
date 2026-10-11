"""eval/verify_gold.py 纯函数检查器的默认套件测试（W6 §1.2 年份窗口纪律）。

verify_gold 主体连 MySQL/Chroma（人工闸门脚本，不在裸机套件内）；本文件只测
不碰环境的两个纯函数：is_integral_number / check_year_window。
导入 eval.verify_gold 会连带 agent 包（chromadb/sqlalchemy 模块级 import），
但不触发任何连接——与 test_eval_runner 导入 run_eval（langchain）同级的妥协。
"""

from decimal import Decimal

from eval.verify_gold import check_year_window, is_integral_number


def test_is_integral_number():
    assert is_integral_number(7717)
    assert is_integral_number(Decimal(7717))
    assert is_integral_number(Decimal("4.00"))  # 值为整数的 Decimal（EV-039 软警告场景）
    assert not is_integral_number(Decimal("47.90"))
    assert not is_integral_number(Decimal("0.0886"))
    assert not is_integral_number("2026")  # 字符串金标（DATE_FORMAT 产物）不参与窗口
    assert not is_integral_number(None)
    assert not is_integral_number(True)  # bool 是 int 子类，显式排除
    assert not is_integral_number(Decimal("NaN"))


def test_check_year_window_hard_error_for_year_range():
    """单值整数金标 ∈ [1900,2100] → 硬错误（日期碎数撞金标的误判面，拦下改题）。"""
    for value in (2026, 1999, 2100, 1900):
        problem, warning = check_year_window("EV-x", [(value,)])
        assert problem is not None and f"{value}" in problem
        assert warning is None


def test_check_year_window_soft_warning_for_day_month_range():
    """单值整数金标 ∈ [1,31] → 软警告（存在合法答案，人工确认答案文本）。"""
    for value in (1, 8, 31):
        problem, warning = check_year_window("EV-x", [(value,)])
        assert problem is None
        assert warning is not None and f"{value}" in warning


def test_check_year_window_safe_values_pass_cleanly():
    """远离窗口的整数（如 5,639 / 7,717 / 11,705）与带小数值（47.90）双清。"""
    for value in (5639, 7717, 11705, 4086):
        assert check_year_window("EV-x", [(value,)]) == (None, None)
    assert check_year_window("EV-x", [(Decimal("47.90"),)]) == (None, None)
    assert check_year_window("EV-x", [(Decimal("98.55"),)]) == (None, None)


def test_check_year_window_immune_shapes():
    """多行/多列/字符串金标天然免疫（规则 4：多行不走文本回退）。"""
    assert check_year_window("EV-x", [(4,), (5,)]) == (None, None)          # 多行
    assert check_year_window("EV-x", [("2026-05-27", Decimal("48931.52"))]) == (None, None)  # 多列
    assert check_year_window("EV-x", [("2026-05",)]) == (None, None)         # 字符串金标

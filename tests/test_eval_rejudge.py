"""eval/rejudge.py 的默认套件测试（裸机：假重放器注入，无 MySQL / LLM）。

覆盖：复判核心的判定同构（rejudge_record）、翻转判定（is_flip）、
v2.0 假阴性用例经复判翻转为 pass 的端到端形态、invoke_exception 子码保留。
"""

from decimal import Decimal

from eval import rejudge


class FakeReplayer:
    """sql -> 行列表 | None 的字典兜底假重放器（与 Replayer 同契约）。"""

    def __init__(self, results: dict[str, list[tuple] | None]):
        self.results = results

    def __call__(self, sql: str) -> list[tuple] | None:
        return self.results.get(sql)


_CASE = {
    "id": "EV-100",
    "question_type": "aggregation",
    "gold_sql": "SELECT g",
    "ordered": False,
    "expected_chunks": ["schema:orders"],
    "standard_wrong": [],
    "notes": "",
}


def _record(**overrides) -> dict:
    record = {
        "id": "EV-100",
        "run_idx": 1,
        "sql_calls": [{"seq": 1, "sql": "SELECT m"}],
        "sql_used": "SELECT m",
        "sql_source": "last",
        "judge": "fail",
        "pass_via": None,
        "text_matched": None,
        "final_answer_text": None,
        "failure_mode": "other",
        "error_msg": None,
    }
    record.update(overrides)
    return record


def test_rejudge_record_reproduces_sql_pass_path():
    replayer = FakeReplayer({"SELECT g": [(1,)], "SELECT m": [(1,)]})
    verdict = rejudge.rejudge_record(_record(judge="fail"), _CASE, replayer)
    assert verdict["judge"] == "pass"
    assert verdict["pass_via"] == "sql"
    assert verdict["sql_used"] == "SELECT m"


def test_rejudge_record_text_fallback_uses_stored_final_answer_text():
    """复判消费 detail 存的 final_answer_text：SQL fail 但文本命中 → pass_via=text。"""
    replayer = FakeReplayer({"SELECT g": [(Decimal("14787925.19"),)], "SELECT m": [(Decimal(2),)]})
    verdict = rejudge.rejudge_record(
        _record(final_answer_text="净销售额为 14,787,925.19 元"), _CASE, replayer
    )
    assert verdict["judge"] == "pass"
    assert verdict["pass_via"] == "text"
    assert verdict["text_matched"] == "14787925.19"


def test_rejudge_record_v20_false_negative_flips_to_pass():
    """设计 §1.1 假阴性用例的复判端到端：金标 {9.996, 11} vs 模型 {10, 11}
    （无序）——旧 v2.0 判 fail 的记录，v2.1 复判翻转为 pass_via=sql。"""
    case = dict(_CASE, gold_sql="SELECT g", ordered=False)
    replayer = FakeReplayer(
        {
            "SELECT g": [(Decimal("9.996"),), (Decimal(11),)],
            "SELECT m": [(Decimal(10),), (Decimal(11),)],
        }
    )
    old = _record(judge="fail", failure_mode="other")
    verdict = rejudge.rejudge_record(old, case, replayer)
    assert verdict["judge"] == "pass"
    assert verdict["pass_via"] == "sql"
    assert rejudge.is_flip(old, verdict)


def test_rejudge_record_invoke_exception_subcode_preserved():
    """调用异常的旧记录复判仍是 error，子码保留原始事实（不漂移成 no_usable_sql）。"""
    replayer = FakeReplayer({"SELECT g": [(1,)]})
    old = _record(sql_calls=[], sql_used=None, judge="error", error_msg="invoke_exception")
    verdict = rejudge.rejudge_record(old, _CASE, replayer)
    assert verdict["judge"] == "error"
    assert verdict["error_msg"] == "invoke_exception"


def test_is_flip_semantics():
    """翻转 = judge 档位或 pass_via 变化；failure_mode 变化不动数字不算翻转。"""
    old = {"judge": "fail", "pass_via": None}
    assert rejudge.is_flip(old, {"judge": "pass", "pass_via": "sql"})
    assert rejudge.is_flip(old, {"judge": "pass", "pass_via": "text"})
    assert not rejudge.is_flip(old, {"judge": "fail", "pass_via": None})
    # fail→fail 但 mode 从 other 变 trap：数字不动，不是翻转
    assert not rejudge.is_flip(old, {"judge": "fail", "pass_via": None, "failure_mode": "trap:x"})
    # 同为 pass 但途径变化（sql→text）：双层指标分列变化，算翻转
    assert rejudge.is_flip({"judge": "pass", "pass_via": "sql"}, {"judge": "pass", "pass_via": "text"})


def test_classify_flip_cause_three_categories():
    """成因归类：replay_env（取用不同 SQL）/ sort_key_fix（同 SQL 无序 fail→pass）/
    text_fallback（其余途径变化）——归因错了数字就用错（EV-006 r3 实锤的教训）。"""
    case_unordered = {"ordered": False}
    # replay_env：三级规则今次取用了不同的 SQL（旧最后调用重放失败回退更早调用）
    assert (
        rejudge.classify_flip_cause(
            {"judge": "pass", "pass_via": "text", "sql_used": "SELECT heavy"},
            {"judge": "pass", "pass_via": "sql", "sql_used": "SELECT earlier"},
            case_unordered,
        )
        == "replay_env"
    )
    # sort_key_fix：同一 SQL、无序比较、fail→pass——v2.0 假阴性被 v2.1 修复
    assert (
        rejudge.classify_flip_cause(
            {"judge": "fail", "pass_via": None, "sql_used": "SELECT m"},
            {"judge": "pass", "pass_via": "sql", "sql_used": "SELECT m"},
            case_unordered,
        )
        == "sort_key_fix"
    )
    # text_fallback：同一 SQL、其余途径变化（如文本回退命中差异）
    assert (
        rejudge.classify_flip_cause(
            {"judge": "fail", "pass_via": None, "sql_used": "SELECT m"},
            {"judge": "pass", "pass_via": "text", "sql_used": "SELECT m"},
            {"ordered": True},
        )
        == "text_fallback"
    )


def test_rejudge_file_flips_and_summary(tmp_path):
    """整文件复判：翻转清单 + 新旧汇总数字；数据集中找不到的 id 原样保留进 skipped。"""
    import json

    case_near = dict(_CASE, id="EV-101", gold_sql="SELECT g2")
    cases_by_id = {"EV-100": _CASE, "EV-101": case_near}
    replayer = FakeReplayer(
        {
            "SELECT g": [(Decimal("9.996"),), (Decimal(11),)],
            "SELECT g2": [(1,)],
            "SELECT m": [(Decimal(10),), (Decimal(11),)],
            "SELECT m2": [(1,)],
        }
    )
    detail = {
        "tag": "ut",
        "cases": [
            _record(),  # v2.0 假阴性：复判翻转为 pass
            _record(id="EV-101", sql_calls=[{"seq": 1, "sql": "SELECT m2"}],
                    sql_used="SELECT m2", judge="pass", pass_via="sql"),  # 维持 pass
            _record(id="EV-999", sql_calls=[], sql_used=None, judge="error",
                    error_msg="invoke_exception"),  # 数据集无此 id
        ],
    }
    path = tmp_path / "detail_ut.json"
    path.write_text(json.dumps(detail, ensure_ascii=False), encoding="utf-8")

    result = rejudge.rejudge_file(path, cases_by_id, replayer)
    assert result["old"]["sql_pass"] == 1  # 旧：1 sql pass + 1 fail + 1 error
    assert result["old"]["fail"] == 1
    assert result["new"]["sql_pass"] == 2  # 新：假阴性翻正
    assert len(result["flips"]) == 1
    assert result["flips"][0]["id"] == "EV-100"
    assert result["skipped"] == ["EV-999"]

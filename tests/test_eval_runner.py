"""eval/run_eval.py 纯逻辑层的默认套件测试（runner 里可裸机测的部分）。

范围界定：runner 的环境依赖面（preflight 连库、检索器、agent.invoke）不在本文件
覆盖内——那是 T3 端到端冒烟（--limit 2 --tag smoke）与实跑的职责。这里用注入的
假重放器 / 假 engine / 假 records，验证：
- _finalize_record 的明细记录组装（§3.3 字段 + 三类判定 + error 子码）；
- Replayer 的重放缓存与行数保护（§2.4）；
- 报告渲染的 §3.3 固定结构（单轮 / 多轮两分支，声明段四条写死）。

导入 run_eval 会连带 agent.core.agent（langchain）——不触发 MySQL / 模型加载，
默认套件零环境依赖原则不破。
"""

import argparse
import json
from pathlib import Path

import pytest

from agent.tools import sql_tools
from eval import run_eval
from eval.judge import judge_case

# ---------------------------------------------------------------------------
# fixture：假重放器（与 Replayer 同契约：sql -> 行列表 | None）
# ---------------------------------------------------------------------------


class FakeReplayer:
    """字典兜底的假重放器，记录每次真实执行以便断言缓存行为。"""

    def __init__(self, results: dict[str, list[tuple] | None]):
        self.results = results
        self.executed: list[str] = []
        self.oversized: set[str] = set()

    def __call__(self, sql: str) -> list[tuple] | None:
        self.executed.append(sql)
        return self.results.get(sql)


_CASE = {
    "id": "EV-029",
    "question": "2026 年 9 月的净销售额是多少？",
    "question_type": "glossary_trap",
    "gold_sql": "SELECT ROUND(SUM(p.amount) - (...), 2) AS net_sales FROM payments p",
    "ordered": False,
    "expected_chunks": ["schema:payments", "schema:refunds", "glossary:net_sales"],
    "standard_wrong": [
        {"trap_id": "no_refund_deduction", "description": "答成 GMV", "value": "16029955.34"}
    ],
    "notes": "口径 2",
}
_GOLD_ROWS = [(14787925.19,)]


# ---------------------------------------------------------------------------
# ① _finalize_record：§3.3 明细组装与判定路径
# ---------------------------------------------------------------------------


def test_finalize_record_pass_path():
    replayer = FakeReplayer({_CASE["gold_sql"]: _GOLD_ROWS, "SELECT 1": _GOLD_ROWS})
    record = run_eval._finalize_record(
        case=_CASE,
        run_idx=2,
        replayer=replayer,
        sql_calls=[{"seq": 1, "sql": "SELECT 1"}],
        injected_ids=["schema:payments", "schema:refunds"],
        latency_s=23.4,
        tokens={"input": 100, "output": 10, "total": 110, "cache_read": 50},
    )
    assert record["id"] == "EV-029"
    assert record["run_idx"] == 2
    assert record["judge"] == "pass"
    assert record["failure_mode"] is None
    assert record["error_msg"] is None
    assert record["sql_used"] == "SELECT 1"
    assert record["sql_source"] == "last"
    assert record["gold_row_count"] == 1
    assert record["model_row_count"] == 1
    assert record["recall_hits"] == ["schema:payments", "schema:refunds"]
    assert record["ended_with_failed_call"] is False
    assert record["oversized"] is False
    assert "turns" not in record  # 单轮记录没有 turns 节


def test_finalize_record_fail_classifies_trap():
    replayer = FakeReplayer({_CASE["gold_sql"]: _GOLD_ROWS, "SELECT GMV": [(16029955.34,)]})
    record = run_eval._finalize_record(
        case=_CASE,
        run_idx=1,
        replayer=replayer,
        sql_calls=[{"seq": 1, "sql": "SELECT GMV"}],
        injected_ids=[],
        latency_s=1.0,
        tokens={"input": 0, "output": 0, "total": 0, "cache_read": 0},
    )
    assert record["judge"] == "fail"
    assert record["failure_mode"] == "trap:no_refund_deduction"


def test_finalize_record_no_usable_sql_error():
    """模型一条 SQL 都没调（或全部重放失败）、文本回退也不适用 → error/no_usable_sql。"""
    replayer = FakeReplayer({_CASE["gold_sql"]: _GOLD_ROWS})
    record = run_eval._finalize_record(
        case=_CASE,
        run_idx=1,
        replayer=replayer,
        sql_calls=[],
        injected_ids=[],
        latency_s=1.0,
        tokens={"input": 0, "output": 0, "total": 0, "cache_read": 0},
    )
    assert record["judge"] == "error"
    assert record["error_msg"] == "no_usable_sql"
    assert record["pass_via"] is None


def test_finalize_record_sql_pass_records_pass_via_sql():
    """SQL 级一致 → pass_via="sql"、text_matched 空（§2.4 v2 双层指标字段落盘）。"""
    replayer = FakeReplayer({_CASE["gold_sql"]: _GOLD_ROWS, "SELECT 1": _GOLD_ROWS})
    record = run_eval._finalize_record(
        case=_CASE,
        run_idx=1,
        replayer=replayer,
        sql_calls=[{"seq": 1, "sql": "SELECT 1"}],
        injected_ids=[],
        latency_s=1.0,
        tokens={"input": 0, "output": 0, "total": 0, "cache_read": 0},
        final_answer_text="净销售额 14,787,925.19 元",
    )
    assert record["judge"] == "pass"
    assert record["pass_via"] == "sql"
    assert record["text_matched"] is None
    assert record["final_answer_text"] == "净销售额 14,787,925.19 元"


def test_finalize_record_text_fallback_passes_and_persists_detail_fields():
    """§2.4 v2 答案级回退：SQL 级 fail（SQL 返回 GMV 陷阱值）+ 最终文本算出金标
    → pass 且 pass_via="text"；final_answer_text / pass_via / text_matched 落盘。"""
    replayer = FakeReplayer({_CASE["gold_sql"]: _GOLD_ROWS, "SELECT GMV": [(16029955.34,)]})
    record = run_eval._finalize_record(
        case=_CASE,
        run_idx=2,
        replayer=replayer,
        sql_calls=[{"seq": 1, "sql": "SELECT GMV"}],
        injected_ids=[],
        latency_s=1.0,
        tokens={"input": 0, "output": 0, "total": 0, "cache_read": 0},
        final_answer_text="净销售额为 14,787,925.19 元",
    )
    assert record["judge"] == "pass"
    assert record["pass_via"] == "text"
    assert record["text_matched"] == "14787925.19"  # Decimal 候选值 str 化，JSON 可序列化
    assert record["failure_mode"] is None
    assert record["error_msg"] is None
    assert record["final_answer_text"] == "净销售额为 14,787,925.19 元"


def test_finalize_record_no_usable_sql_tries_text_fallback_first():
    """§2.4 v2 规则一：无可用 SQL 先试文本回退，命中即 pass（不判 error）。"""
    replayer = FakeReplayer({_CASE["gold_sql"]: _GOLD_ROWS})
    record = run_eval._finalize_record(
        case=_CASE,
        run_idx=1,
        replayer=replayer,
        sql_calls=[],
        injected_ids=[],
        latency_s=1.0,
        tokens={"input": 0, "output": 0, "total": 0, "cache_read": 0},
        final_answer_text="净销售额约 14,787,925.19 元",
    )
    assert record["judge"] == "pass"
    assert record["pass_via"] == "text"
    assert record["error_msg"] is None
    assert record["gold_row_count"] == 1


def test_finalize_record_gold_replay_error():
    """金标 SQL 都重放不了 → error/replay_error（本题无法判定）。"""
    replayer = FakeReplayer({})  # 金标也失败
    record = run_eval._finalize_record(
        case=_CASE,
        run_idx=1,
        replayer=replayer,
        sql_calls=[{"seq": 1, "sql": "SELECT x"}],
        injected_ids=[],
        latency_s=1.0,
        tokens={"input": 0, "output": 0, "total": 0, "cache_read": 0},
    )
    assert record["judge"] == "error"
    assert record["error_msg"] == "replay_error"


def test_finalize_record_multiturn_attaches_turns_and_uses_final_gold_sql():
    """多轮 case（§2.7）：金标取 final_gold_sql，turns 全量明细挂进记录。"""
    mt_case = {
        "id": "MT-01",
        "turns": ["上个月 GMV 多少？", "那环比呢"],
        "final_gold_sql": "SELECT DATE_FORMAT(...)",
        "ordered": True,
        "expected_chunks": ["glossary:yoy_mom"],
        "standard_wrong": [],
        "notes": "",
    }
    replayer = FakeReplayer({"SELECT DATE_FORMAT(...)": [("2026-08", 1.0), ("2026-09", 2.0)]})
    turns = [{"turn_idx": 1}, {"turn_idx": 2}]
    record = run_eval._finalize_record(
        case=mt_case,
        run_idx=1,
        replayer=replayer,
        sql_calls=[{"seq": 1, "sql": "SELECT DATE_FORMAT(...)"}],
        injected_ids=["glossary:yoy_mom"],
        latency_s=30.0,
        tokens={"input": 1, "output": 1, "total": 2, "cache_read": 0},
        turn_records=turns,
    )
    assert record["question_type"] is None  # MT schema 无 question_type
    assert record["turns"] == turns
    # 金标 2 行有序 vs 模型 2 行有序 → pass（判定器吃的就是最后一轮的 SQL 重放结果）
    assert record["judge"] == "pass"


def test_invoke_exception_record_shape():
    record = run_eval._invoke_exception_record(_CASE, run_idx=3, exc=RuntimeError(" boom"))
    assert record["judge"] == "error"
    assert record["error_msg"] == "invoke_exception"
    assert "boom" in record["error_detail"]
    assert record["run_idx"] == 3
    assert record["tokens"] == {"input": 0, "output": 0, "total": 0, "cache_read": 0}


# ---------------------------------------------------------------------------
# ② Replayer：重放缓存 + 行数保护（§2.4）
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, rows: list[tuple], fail_on: str | None = None):
        self._rows = rows
        self._fail_on = fail_on

    def fetchmany(self, size: int) -> list[tuple]:
        return self._rows[:size]


class _FakeConnection:
    """duck-typed 连接：execute(sql) 按登记表返回游标或抛错。"""

    def __init__(self, rows_by_sql: dict[str, list[tuple]], failing: set[str]):
        self.rows_by_sql = rows_by_sql
        self.failing = failing
        self.execute_count = 0

    def connect(self) -> "_FakeConnection":  # Replayer 用 engine.connect() 取连接
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def dispose(self, close: bool = True) -> None:  # set_engine 释放旧池时调用
        return None

    def execute(self, sql):
        key = str(sql)  # Replayer 传进来的是 sqlalchemy TextClause，转回 SQL 文本再查表
        self.execute_count += 1
        if key in self.failing:
            raise RuntimeError("bad sql")
        return _FakeCursor(self.rows_by_sql.get(key, []))


@pytest.fixture()
def fake_engine():
    """把假 engine 注入 sql_tools 单例，结束后恢复（延续 set_engine 注入模式）。"""
    holder: dict = {}

    def install(rows_by_sql: dict[str, list[tuple]], failing: set[str]) -> _FakeConnection:
        conn = _FakeConnection(rows_by_sql, failing)
        sql_tools.set_engine(conn)
        holder["conn"] = conn
        return conn

    yield install
    sql_tools.set_engine(None)  # 恢复懒加载，不影响其他测试


def test_replayer_returns_native_rows_and_caches(fake_engine):
    conn = fake_engine({"SELECT 1": [(1, "a"), (2, "b")]}, failing=set())
    replayer = run_eval.Replayer()
    assert replayer("SELECT 1") == [(1, "a"), (2, "b")]
    assert replayer("SELECT 1") == [(1, "a"), (2, "b")]
    assert conn.execute_count == 1  # 同一 SQL 第二次走缓存，不再打库


def test_replayer_failure_returns_none(fake_engine):
    fake_engine({}, failing={"SELECT bad"})
    replayer = run_eval.Replayer()
    assert replayer("SELECT bad") is None


def test_replayer_row_cap_marks_oversized(fake_engine, monkeypatch: pytest.MonkeyPatch):
    """行数保护：> REPLAY_MAX_ROWS 截断并标记 oversized（§2.4）。"""
    monkeypatch.setattr(run_eval, "REPLAY_MAX_ROWS", 2)
    big_rows = [(i,) for i in range(5)]
    conn = fake_engine({"SELECT big": big_rows}, failing=set())
    replayer = run_eval.Replayer()
    rows = replayer("SELECT big")
    assert len(rows) == 2
    assert "SELECT big" in replayer.oversized
    assert conn.execute_count == 1


# ---------------------------------------------------------------------------
# ③ 报告渲染：§3.3 固定结构（单轮 / 多轮分支，声明段四条写死）
# ---------------------------------------------------------------------------


def _make_args(multiturn: bool) -> argparse.Namespace:
    return argparse.Namespace(
        tag="ut", dataset=None, top_n=8, runs=1, limit=None, rerun=None,
        multiturn=multiturn, compare=None,
        # W6 消融开关（§3.4；默认全关 = 线上默认路径）
        rrf_k=None, no_bm25=False, no_dense=False, no_fewshot=False, rerank=False,
        # W7 配额开关（设计 §1.5；默认关）
        quota=False,
    )


def _single_records() -> list[dict]:
    replayer = FakeReplayer(
        {
            "gold_pass": [(1, "a")],
            "sel_pass": [(1, "a")],
            "gold_fail": [(1, "a")],
            "sel_fail": [(2, "b")],
            "gold_err": [(1,)],
        }
    )
    case_pass = dict(_CASE, id="EV-001", gold_sql="gold_pass")
    case_fail = dict(
        _CASE,
        id="EV-002",
        question_type="aggregation",
        gold_sql="gold_fail",
        standard_wrong=[],
    )
    case_err = dict(_CASE, id="EV-003", gold_sql="gold_err")

    def run(case, sql):
        return run_eval._finalize_record(
            case=case, run_idx=1, replayer=replayer,
            sql_calls=[{"seq": 1, "sql": sql}] if sql else [],
            injected_ids=[], latency_s=1.5,
            tokens={"input": 10, "output": 2, "total": 12, "cache_read": 4},
        )

    return [run(case_pass, "sel_pass"), run(case_fail, "sel_fail"), run(case_err, None)]


def _multiturn_records() -> list[dict]:
    replayer = FakeReplayer({"final_gold": [("2026-08", 1.0), ("2026-09", 2.0)]})
    case = {
        "id": "MT-01", "turns": ["上个月 GMV 多少？", "那环比呢"],
        "final_gold_sql": "final_gold", "ordered": True,
        "expected_chunks": ["glossary:yoy_mom"], "standard_wrong": [], "notes": "",
    }
    return [
        run_eval._finalize_record(
            case=case, run_idx=1, replayer=replayer,
            sql_calls=[{"seq": 1, "sql": "final_gold"}],
            injected_ids=["glossary:yoy_mom"], latency_s=30.0,
            tokens={"input": 10, "output": 2, "total": 12, "cache_read": 0},
            turn_records=[
                {
                    "turn_idx": 1, "question": "上个月 GMV 多少？",
                    "retrieval_query": "上个月 GMV 多少？",
                    "injected_chunks": ["qa_pair:qa_003"],
                    "sql_calls": [], "latency_s": 10.0,
                    "tokens": {"input": 5, "output": 1, "total": 6, "cache_read": 0},
                },
                {
                    "turn_idx": 2, "question": "那环比呢",
                    "retrieval_query": "上个月 GMV 多少？\n那环比呢",
                    "injected_chunks": ["glossary:yoy_mom", "qa_pair:qa_003"],
                    "sql_calls": [{"seq": 1, "sql": "final_gold"}], "latency_s": 20.0,
                    "tokens": {"input": 5, "output": 1, "total": 6, "cache_read": 0},
                },
            ],
        )
    ]


def test_render_report_single_turn_fixed_structure():
    report = run_eval._render_report(
        _make_args(multiturn=False), Path("eval/dataset/eval_v1.jsonl"),
        _single_records(), started_at="2026-10-09 12:00:00",
    )
    for section in (
        "# W3 基线评测报告：ut",
        "## 运行配置",
        "## 四指标汇总",
        "### 执行准确率",
        "### 回答准确率（答案级，含文本计算）",
        "### Recall@8",
        "### 延迟",
        "### token 成本",
        "## 对照行对比表",
        "## 错答模式统计",
        "## flaky 题记录",
        "## 多轮评测节",
        "## 逐题结果",
    ):
        assert section in report, f"缺少固定结构节：{section}"
    # 声明段五条写死（§3.3，⑤ 为判定规则 v2 增补）
    assert "## 声明（固定五条，W3 设计 §3.3）" in report
    assert "few-shot 自举" in report
    assert "服务端非确定性" in report
    assert "网络波动" in report
    assert "cache_read" in report
    assert "双层指标" in report
    # P95 口径声明（§8-5）
    assert "method='inclusive'" in report
    # 分层表（W6 §2.3 三分：示例层/近示例/远示例）与答案级列（§3.3 v2）
    assert "示例层（hit：EV-001~011 有同款示例，§1.3）" in report
    assert "SQL 级准确率" in report and "答案级准确率" in report
    # 双层指标 overall 行（SQL 级 + 答案级 + 差值说明）
    assert "（SQL 判过 1 ｜ 文本判过 0" in report
    assert "（pass_via：sql 1 + text 0，共 3 次）" in report
    assert "差值说明" in report
    # 错答模式统计可见 other（EV-002 无登记值）
    assert "| other | 1 |" in report
    # error 子码可见
    assert "error:no_usable_sql" in report


def test_render_report_multiturn_section():
    report = run_eval._render_report(
        _make_args(multiturn=True), Path("eval/dataset/eval_v1_multiturn.jsonl"),
        _multiturn_records(), started_at="2026-10-09 12:00:00",
    )
    assert "多轮准确率（只判最后一轮）" in report
    assert "query 拼接行为观察" in report
    assert "上个月 GMV 多少？ / 那环比呢" in report  # query 拼接展示（换行渲染为 /）
    assert "MT-01" in report
    # 轮级注入明细可见（§2.7 每轮全量记录）
    assert "轮 1" in report and "轮 2" in report
    # MT 报告不出题型/分层表
    assert "示例层（hit" not in report


def test_render_report_multiturn_with_invoke_exception_record_does_not_crash():
    """回归锁：多轮 case 级异常（无 turns 键）时报告渲染不能崩——
    否则会"跑完全场却死在写报告"（§6 风险 4 的兜底记录也要能进报告）。"""
    records = _multiturn_records()
    records.append(run_eval._invoke_exception_record({"id": "MT-02", "expected_chunks": []}, 1, RuntimeError("x")))
    report = run_eval._render_report(
        _make_args(multiturn=True), Path("eval/dataset/eval_v1_multiturn.jsonl"),
        records, started_at="2026-10-09 12:00:00",
    )
    assert "error:invoke_exception" in report
    assert "无轮级明细" in report
    assert "1/1 个 case 的最后一轮注入集合与首轮不同" in report  # 观察分母只数有数据的 case


def test_render_report_top_n_zero_recalls_note_instead_of_zeros():
    """对照行 top_n=0：Recall 节只给说明，不列误导性的 0%（§2.5）。"""
    args = _make_args(multiturn=False)
    args.top_n = 0
    report = run_eval._render_report(
        args, Path("eval/dataset/eval_v1.jsonl"), _single_records(),
        started_at="2026-10-09 12:00:00",
    )
    assert "注入恒空" in report
    assert "整体逐片 Recall" not in report


def test_render_report_compare_table(tmp_path: Path):
    """--compare 指向对照行 detail JSON 时生成差值对比表（§3.3）。"""
    baseline = {"tag": "ut", "cases": _single_records()}
    baseline_path = tmp_path / "detail_ut.json"
    baseline_path.write_text(json.dumps(baseline, ensure_ascii=False), encoding="utf-8")

    args = _make_args(multiturn=False)
    args.compare = str(baseline_path)
    report = run_eval._render_report(
        args, Path("eval/dataset/eval_v1.jsonl"), _single_records(),
        started_at="2026-10-09 12:00:00",
    )
    assert "对照（ut）" in report
    # 双层 overall 行（§3.3 v2：答案级 + SQL 级）
    assert "| overall（答案级） |" in report
    assert "| overall（SQL 级） |" in report


def test_render_report_answer_level_section_counts_text_passes():
    """答案级节（§3.3 v2）：pass_via 构成、差值说明、逐题表 pass_via 列可见；
    SQL 级 overall 行把文本判过单列、不计入自身。"""
    replayer = FakeReplayer({_CASE["gold_sql"]: _GOLD_ROWS, "SELECT GMV": [(16029955.34,)]})
    record = run_eval._finalize_record(
        case=_CASE,
        run_idx=1,
        replayer=replayer,
        sql_calls=[{"seq": 1, "sql": "SELECT GMV"}],
        injected_ids=[],
        latency_s=1.0,
        tokens={"input": 0, "output": 0, "total": 0, "cache_read": 0},
        final_answer_text="净销售额为 14,787,925.19 元",
    )
    report = run_eval._render_report(
        _make_args(multiturn=False),
        Path("eval/dataset/eval_v1.jsonl"),
        [record],
        started_at="2026-10-09 12:00:00",
    )
    assert (
        "- overall：**0.0%**（SQL 判过 0 ｜ 文本判过 1（计入答案级，见下节）｜ fail 0 ｜ error 0，共 1 次）"
        in report
    )
    assert "- overall：**100.0%**（pass_via：sql 0 + text 1，共 1 次）" in report
    assert "差值说明：答案级 − SQL 级 = 100.0%" in report
    # 逐题结果表带 pass_via 列
    assert "| r1 | EV-029 | pass | text |" in report


def test_pass_via_counts_legacy_records_default_to_sql():
    """--compare 读入的 v1 旧明细没有 pass_via 字段：judge=pass 一律按 SQL 级计数（不误判成 text）。"""
    legacy = [{"judge": "pass"}, {"judge": "pass", "pass_via": "text"}, {"judge": "fail"}]
    assert run_eval._pass_via_counts(legacy) == (1, 1)
    assert run_eval._sql_acc(legacy) == pytest.approx(1 / 3)
    assert run_eval._sql_acc([]) == 0.0


# ---------------------------------------------------------------------------
# ④ preflight：缺 API key 时中文指引并退出（环境依赖面的唯一可裸机路径）
# ---------------------------------------------------------------------------


def test_preflight_exits_with_guidance_without_api_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    from agent import config

    monkeypatch.setattr(config, "DEEPSEEK_API_KEY", "", raising=False)
    with pytest.raises(SystemExit) as excinfo:
        run_eval.preflight()
    assert excinfo.value.code == 1
    assert "DEEPSEEK_API_KEY" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# ⑤ 判定入口与 judge_case 的一致性（runner 侧只经 judge_case 判定，无第二套规则）
# ---------------------------------------------------------------------------


def test_runner_uses_judge_module_verdict_object():
    """防漂移锁：_finalize_record 的 verdict 字段与 judge_case 返回逐字段一致。"""
    replayer = FakeReplayer({"g": [(1,)], "m": [(2,)]})
    case = dict(_CASE, gold_sql="g", standard_wrong=[])
    record = run_eval._finalize_record(
        case=case, run_idx=1, replayer=replayer,
        sql_calls=[{"seq": 1, "sql": "m"}],
        injected_ids=[], latency_s=0.0,
        tokens={"input": 0, "output": 0, "total": 0, "cache_read": 0},
    )
    expected = judge_case([(1,)], [(2,)], ordered=case["ordered"], standard_wrong=[])
    assert (record["judge"], record["failure_mode"], record["error_msg"]) == (
        expected["judge"],
        expected["failure_mode"],
        expected["error_msg"],
    )


# ---------------------------------------------------------------------------
# ⑥ W5 自举语料两行（§4.7，additive：不传/无命中时旧报告逐字节不变）
# ---------------------------------------------------------------------------


class _FakeRetriever:
    """corpus_type_counts 只消费 rebuild() 缓存的 metas（不打 Chroma）。"""

    def __init__(self, metas: list[dict]):
        self.metas = metas


def test_corpus_type_counts_from_retriever_metas():
    retriever = _FakeRetriever(
        [
            {"doc_type": "schema"},
            {"doc_type": "schema"},
            {"doc_type": "glossary"},
            {"doc_type": "qa_pair"},
            {"doc_type": "qa_pair"},
            {"doc_type": "qa_pair"},
        ]
    )
    assert run_eval.corpus_type_counts(retriever) == {"schema": 2, "glossary": 1, "qa_pair": 3}


def test_render_report_corpus_counts_line():
    """运行配置节的语料片数行（W5 §4.7-2）：任意两次报告可对齐语料版本。"""
    report = run_eval._render_report(
        _make_args(multiturn=False),
        Path("eval/dataset/eval_v1.jsonl"),
        _single_records(),
        started_at="2026-10-11 00:00:00",
        corpus_counts={"schema": 6, "glossary": 11, "qa_pair": 11},
    )
    assert "- 语料：schema 6 / glossary 11 / qa_pair 11（共 28 片" in report


def test_render_report_without_corpus_counts_unchanged():
    """兼容锁：不传 corpus_counts（旧调用/直调）不输出语料行，其余结构不变。"""
    report = run_eval._render_report(
        _make_args(multiturn=False),
        Path("eval/dataset/eval_v1.jsonl"),
        _single_records(),
        started_at="2026-10-11 00:00:00",
    )
    assert "- 语料：" not in report


def test_is_absorbed_qa_chunk_boundaries():
    """口径 = qa_pair:qa_NNN 且 NNN ≥ 012（三位数字，001~011 是人工语料）。"""
    assert run_eval._is_absorbed_qa_chunk("qa_pair:qa_012")
    assert run_eval._is_absorbed_qa_chunk("qa_pair:qa_999")
    assert not run_eval._is_absorbed_qa_chunk("qa_pair:qa_011")
    assert not run_eval._is_absorbed_qa_chunk("qa_pair:qa_000")
    assert not run_eval._is_absorbed_qa_chunk("qa_pair:qa_12")  # 非三位，防错位编号
    assert not run_eval._is_absorbed_qa_chunk("schema:qa_012")
    assert not run_eval._is_absorbed_qa_chunk("qa_pair:orders")


def test_absorbed_chunk_hits_only_counts_absorbed_injections():
    records = _single_records()
    records[0]["injected_chunks"] = ["qa_pair:qa_012", "schema:payments"]
    records[1]["injected_chunks"] = ["qa_pair:qa_011"]  # 人工语料不算自举
    records[2]["injected_chunks"] = []
    hits = run_eval.absorbed_chunk_hits(records)
    assert hits == {"EV-001": ["qa_pair:qa_012"]}


def test_render_report_absorbed_section_only_when_nonempty():
    records = _single_records()
    baseline = run_eval._render_report(
        _make_args(multiturn=False), Path("eval/dataset/eval_v1.jsonl"),
        records, started_at="2026-10-11 00:00:00",
    )
    assert "自举语料命中" not in baseline  # 无命中 → 该节整体缺席（W3 报告可复现）

    records[0]["injected_chunks"] = ["qa_pair:qa_012", "qa_pair:qa_015"]
    with_hit = run_eval._render_report(
        _make_args(multiturn=False), Path("eval/dataset/eval_v1.jsonl"),
        records, started_at="2026-10-11 00:00:00",
    )
    assert "## 自举语料命中" in with_hit
    assert "命中自举语料的题：EV-001（注入 qa_012+ 片：qa_pair:qa_012、qa_pair:qa_015）" in with_hit
    assert "NNN ≥ 012" in with_hit


# ---------------------------------------------------------------------------
# ⑦ W6 消融 CLI（§3.4）：参数解析 / opts 构建 / 实际生效参数记录 / Recall 动态标题
# ---------------------------------------------------------------------------


def test_parse_args_w6_ablation_flags(monkeypatch: pytest.MonkeyPatch):
    """新开关全部可解析且默认关闭（默认路径与线上逐字节等价的前提）。"""
    import sys

    monkeypatch.setattr(
        sys, "argv",
        ["eval.run_eval", "--tag", "ablation_row",
         "--rrf-k", "10", "--no-bm25", "--no-dense", "--no-fewshot", "--rerank"],
    )
    args = run_eval._parse_args()
    assert args.rrf_k == 10
    assert args.no_bm25 is True
    assert args.no_dense is True
    assert args.no_fewshot is True
    assert args.rerank is True

    monkeypatch.setattr(sys, "argv", ["eval.run_eval", "--tag", "plain"])
    args = run_eval._parse_args()
    assert args.rrf_k is None
    assert args.no_bm25 is False
    assert args.no_dense is False
    assert args.no_fewshot is False
    assert args.rerank is False
    assert run_eval._build_retrieval_opts(args) is None  # 无开关 → None（同构红线）


def test_effective_retrieval_config_default_matches_online():
    """无开关：实际生效参数 = 线上默认（W3 同构口径的延续）。"""
    cfg = run_eval.effective_retrieval_config(_make_args(multiturn=False))
    assert cfg == {
        "top_k_each": 20,
        "rrf_k": 60,
        "enable_bm25": True,
        "enable_dense": True,
        "doc_types": None,
        "rerank": False,
        "rerank_pool": 0,
        "rerank_model": None,
        "quota": False,
    }


def test_effective_retrieval_config_reflects_ablation_flags():
    """修硬编码谎报（§3.4）：--rrf-k 10 行的实际生效 rrf_k=10，不再是常量 60。"""
    args = _make_args(multiturn=False)
    args.rrf_k = 10
    args.no_bm25 = True
    args.no_fewshot = True
    args.rerank = True
    args.quota = True
    cfg = run_eval.effective_retrieval_config(args)
    assert cfg["rrf_k"] == 10
    assert cfg["enable_bm25"] is False
    assert cfg["enable_dense"] is True
    assert cfg["doc_types"] == ["schema", "glossary"]
    assert cfg["rerank"] is True
    assert cfg["rerank_pool"] == 16  # RERANK_POOL = 2×top_n 起点（设计 §4.2）
    assert cfg["rerank_model"] == "BAAI/bge-reranker-v2-m3"
    assert cfg["quota"] is True  # W7：实际参数如实记录（§1.5）


def test_build_retrieval_opts_maps_flags(monkeypatch: pytest.MonkeyPatch):
    """开关 → RetrievalOptions 映射；--rerank 构建真实 reranker 实例 + pool=16。"""
    from agent.rag import rerank as rerank_mod

    monkeypatch.setattr(rerank_mod, "SILICONFLOW_API_KEY", "sk-ut-key")
    args = _make_args(multiturn=False)
    args.rrf_k = 10
    args.no_fewshot = True
    args.rerank = True
    args.quota = True
    opts = run_eval._build_retrieval_opts(args)
    assert opts is not None
    assert opts.rrf_k == 10
    assert opts.doc_types == ["schema", "glossary"]
    assert opts.enable_bm25 is True
    assert opts.enable_dense is True
    assert opts.rerank_pool == 16
    assert isinstance(opts.reranker, rerank_mod.SiliconFlowReranker)
    assert opts.quota is True  # W7：--quota → RetrievalOptions.quota 透传
    opts.reranker.close()


def test_build_retrieval_opts_rerank_without_key_raises(monkeypatch: pytest.MonkeyPatch):
    """--rerank 无 key：启动期抛 ValueError（中文指引），不跑一半才在第一题上炸。"""
    from agent.rag import rerank as rerank_mod

    monkeypatch.setattr(rerank_mod, "SILICONFLOW_API_KEY", "")
    args = _make_args(multiturn=False)
    args.rerank = True
    with pytest.raises(ValueError, match="SILICONFLOW"):
        run_eval._build_retrieval_opts(args)


def test_build_retrieval_opts_none_when_no_flags():
    """同构红线：无任一开关 → opts=None → prepare_turn 走线上默认分支。"""
    assert run_eval._build_retrieval_opts(_make_args(multiturn=False)) is None


def test_render_report_recall_title_is_dynamic():
    """Recall 标题动态化（§3.4）：D2 行（top_n=12）的指标名必须是 Recall@12。"""
    args = _make_args(multiturn=False)
    args.top_n = 12
    report = run_eval._render_report(
        args, Path("eval/dataset/eval_v1.jsonl"), _single_records(),
        started_at="2026-10-11 00:00:00",
    )
    assert "### Recall@12" in report
    assert "### Recall@8" not in report


def test_render_report_retrieval_line_uses_effective_rrf_k():
    """修谎报：--rrf-k 10 行的报告检索参数行如实写 rrf_k=10（不再硬编码 60）。"""
    args = _make_args(multiturn=False)
    args.rrf_k = 10
    report = run_eval._render_report(
        args, Path("eval/dataset/eval_v1.jsonl"), _single_records(),
        started_at="2026-10-11 00:00:00",
    )
    assert "- 检索参数：rrf_k=10" in report
    assert "RRF k=60（与线上 hybrid_retriever 默认一致" not in report  # 谎报行不再出现

    # 默认行（无开关）文案与 W3 逐字节一致
    default_report = run_eval._render_report(
        _make_args(multiturn=False), Path("eval/dataset/eval_v1.jsonl"), _single_records(),
        started_at="2026-10-11 00:00:00",
    )
    assert (
        "- 检索参数：top_k_each=20，RRF k=60（与线上 hybrid_retriever 默认一致，§2.3 同构）"
        in default_report
    )


def test_corpus_commit_is_short_hash_or_unknown():
    """git HEAD 短哈希（§3.5 冻结证据链）；git 不可用降级 "unknown" 不炸。"""
    commit = run_eval._corpus_commit()
    assert commit == "unknown" or (len(commit) >= 7 and all(c in "0123456789abcdef" for c in commit))


def test_build_detail_config_records_effective_retrieval_and_corpus():
    """detail config 落盘面（§3.4/§3.5）：实际生效检索参数 + corpus_commit + 语料片数——
    summarize_ablation 冻结断言的消费面。"""
    args = _make_args(multiturn=False)
    args.rrf_k = 10
    config = run_eval._build_detail_config(
        args,
        Path("eval/dataset/eval_v1.jsonl"),
        started_at="2026-10-11 00:00:00",
        retrieval_cfg=run_eval.effective_retrieval_config(args),
        corpus_counts={"schema": 6, "glossary": 11, "qa_pair": 11},
    )
    assert config["retrieval"]["rrf_k"] == 10  # 如实记录，不谎报 60
    assert config["retrieval"]["rerank"] is False
    assert config["corpus"]["counts"] == {"schema": 6, "glossary": 11, "qa_pair": 11}
    assert config["corpus"]["total"] == 28
    assert config["corpus"]["commit"]  # 非空（短哈希或 "unknown"）
    assert config["replay_max_rows"] == run_eval.REPLAY_MAX_ROWS


# ---------------------------------------------------------------------------
# ⑦ W6 §2.3 三分层表（hit/near/far）：记录字段、归层规则、旧明细兼容
# ---------------------------------------------------------------------------


def test_finalize_record_persists_generalize_layer():
    """W6 §2.3 additive：分层字段随记录落盘，detail JSON 与报告消费。"""
    replayer = FakeReplayer({_CASE["gold_sql"]: _GOLD_ROWS, "SELECT 1": _GOLD_ROWS})
    case = dict(_CASE, generalize_layer="far")
    record = run_eval._finalize_record(
        case=case, run_idx=1, replayer=replayer,
        sql_calls=[{"seq": 1, "sql": "SELECT 1"}],
        injected_ids=[], latency_s=1.0,
        tokens={"input": 0, "output": 0, "total": 0, "cache_read": 0},
    )
    assert record["generalize_layer"] == "far"


def test_layer_of_hit_by_id_rule_near_far_by_field():
    """hit 由 id 前缀规则判定（EV-001~011，旧明细无字段也归层）；
    near/far 只认 generalize_layer 字段；EV-012+ 缺字段 → None（未分层）。"""
    assert run_eval._layer_of({"id": "EV-001"}) == "hit"
    assert run_eval._layer_of({"id": "EV-011"}) == "hit"
    assert run_eval._layer_of({"id": "EV-001", "generalize_layer": "far"}) == "hit"  # id 规则优先
    assert run_eval._layer_of({"id": "EV-012", "generalize_layer": "near"}) == "near"
    assert run_eval._layer_of({"id": "EV-050", "generalize_layer": "far"}) == "far"
    assert run_eval._layer_of({"id": "EV-012"}) is None  # 旧 30 条明细（v2.1 前）无字段
    assert run_eval._layer_of({"id": "MT-01"}) is None  # 多轮不分层


def test_layer_groups_three_rows_plus_legacy_fallback():
    records = [
        {"id": "EV-001", "generalize_layer": "hit"},
        {"id": "EV-012", "generalize_layer": "near"},
        {"id": "EV-013", "generalize_layer": "near"},
        {"id": "EV-029", "generalize_layer": "far"},
    ]
    groups = dict(run_eval._layer_groups(records))
    assert {r["id"] for r in groups["示例层（hit：EV-001~011 有同款示例，§1.3）"]} == {"EV-001"}
    near_label = "近示例（near：泛化题，有同口径/同构 QA 可改窗口改维度套用，W6 §2.3）"
    far_label = "远示例（far：泛化题，须 schema+glossary 组合推理，W6 §2.3）"
    assert {r["id"] for r in groups[near_label]} == {"EV-012", "EV-013"}
    assert {r["id"] for r in groups[far_label]} == {"EV-029"}
    assert len(groups) == 3  # 全部记录都有字段 → 无兜底行

    legacy = run_eval._layer_groups(records + [{"id": "EV-015"}])
    assert any("未分层" in label for label, _ in legacy)


def test_render_report_three_layer_table():
    """W6 §2.3 分层表三分渲染：hit/near/far 三行都出现（additive，旧二分表退役）。"""
    replayer = FakeReplayer({_CASE["gold_sql"]: _GOLD_ROWS, "SELECT 1": _GOLD_ROWS})
    records = []
    for cid, layer in (("EV-001", "hit"), ("EV-012", "near"), ("EV-029", "far")):
        records.append(
            run_eval._finalize_record(
                case=dict(_CASE, id=cid, generalize_layer=layer),
                run_idx=1, replayer=replayer,
                sql_calls=[{"seq": 1, "sql": "SELECT 1"}],
                injected_ids=[], latency_s=1.0,
                tokens={"input": 0, "output": 0, "total": 0, "cache_read": 0},
            )
        )
    report = run_eval._render_report(
        _make_args(multiturn=False), Path("eval/dataset/eval_v1.jsonl"),
        records, started_at="2026-10-11 00:00:00",
    )
    assert "示例层（hit：EV-001~011 有同款示例，§1.3）" in report
    assert "近示例（near" in report
    assert "远示例（far" in report

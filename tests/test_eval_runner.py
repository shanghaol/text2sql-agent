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
    """模型一条 SQL 都没调（或全部重放失败）→ error/no_usable_sql（§2.2 规则三）。"""
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
    # 声明段四条写死（§3.3）
    assert "## 声明（固定四条，W3 设计 §3.3）" in report
    assert "few-shot 自举" in report
    assert "服务端非确定性" in report
    assert "网络波动" in report
    assert "cache_read" in report
    # P95 口径声明（§8-5）
    assert "method='inclusive'" in report
    # 分层表（§1.3）
    assert "有同款示例（EV-001~011，§1.3）" in report
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
    assert "有同款示例（EV-001~011，§1.3）" not in report


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
    assert "| overall |" in report


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

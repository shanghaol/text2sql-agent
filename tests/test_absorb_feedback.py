"""agent/rag/absorb_feedback.py 的默认套件测试（W5 设计 §4.3-§4.6）。

零依赖原则：不连 Java、不连 MySQL、不加载 fastembed/Chroma——
- 六道门槛用直接构造的 Sample 与注入的假 SemanticDeduper（duck-typing）验证；
- G4 的两种形态都锁：T1 未完成（sql_guard 不可导入）→ 跳过不崩；guard 在位 → 真校验
  （用 sys.modules 注入假模块，T1 是否合码都不影响本文件红绿）；
- 语料文本用内联 fixture，不读仓库里的 qa_pairs.md（T4 --apply 后该文件会增长，
  断言具体条数会让测试随语料漂移）。

真实链路（Java 接口 + 真嵌入 + 真索引）由 T3 联调手测覆盖，不在默认套件内。
"""

import argparse
import sys
import types

import pytest

from agent.rag import absorb_feedback
from agent.rag.absorb_feedback import (
    Sample,
    append_entries,
    check_gates,
    cosine_similarity,
    format_qa_entry,
    next_qa_number,
    normalize_question,
    parse_corpus_questions,
    parse_max_qa_number,
)

# ---------------------------------------------------------------------------
# fixture：语料文本 / 假语义判重器
# ---------------------------------------------------------------------------


def _qa_entry(no: int, question: str, sql: str = "SELECT 1;") -> str:
    return (
        f"### QA-{no:03d}\n"
        f"**问**：{question}\n"
        "**金标 SQL**：\n"
        "```sql\n"
        f"{sql}\n"
        "```\n"
        "**说明**：测试条目"
    )


CORPUS_TEXT = (
    "# few-shot 金标问答对（测试夹具）\n\n"
    + _qa_entry(1, "2026 年 3 月每天的订单量是多少？")
    + "\n\n"
    + _qa_entry(3, "上个月的 GMV 是多少？", sql="SELECT SUM(amount) FROM payments;")
    + "\n"
)

CLEAN_SAMPLE = Sample(
    history_id=101,
    question="各支付方式的订单占比是多少？",
    generated_sql="SELECT method, COUNT(*) FROM payments GROUP BY method;",
    comment="口径按支付时间",
    created_at="2026-10-11 10:00:00",
)


class FakeDeduper:
    """duck-typing 假判重器：只实现 check_gates 用到的判重与诊断接口。

    search_with_embedding 是判重口径（新问题 vs 现有条目「问」字段，top1 直接给
    QA 编号），retrieval_top1 是诊断展示口径（整片索引检索，给 chunk id）——
    真实实现里两者口径不同。
    """

    def __init__(self, top1_id="QA-003", top1_sim=0.87, qa_count=11, embeddings=None,
                 retrieval_id="qa_pair:qa_003", retrieval_sim=0.67):
        self.top1_id = top1_id
        self.top1_sim = top1_sim
        self._qa_count = qa_count
        self.embeddings = embeddings or {}
        self.retrieval_id = retrieval_id
        self.retrieval_sim = retrieval_sim

    def embedding_of(self, question: str) -> list[float]:
        return self.embeddings.get(question, [1.0, 0.0])

    def search_with_embedding(self, question: str):
        embedding = self.embedding_of(question)
        if self._qa_count == 0:
            return None, None, embedding
        return self.top1_id, self.top1_sim, embedding

    def retrieval_top1(self, question: str):
        if self.retrieval_id is None:
            return None, None
        return self.retrieval_id, self.retrieval_sim


def _outcome(result, gate: str):
    return next(o for o in result.outcomes if o.gate == gate)


# ---------------------------------------------------------------------------
# ① 六道门槛：G1-G5 各一条违反样本
# ---------------------------------------------------------------------------


def test_all_gates_pass_on_clean_sample():
    result = check_gates(CLEAN_SAMPLE, parse_corpus_questions(CORPUS_TEXT), [], FakeDeduper())
    assert [o.gate for o in result.outcomes] == ["G1", "G2", "G3", "G4", "G5", "G6"]
    assert result.kept
    assert result.failed_gates == []


def test_g1_rejects_non_like_rating():
    sample = Sample(history_id=1, question="各城市销量？", generated_sql="SELECT 1;", rating=0)
    result = check_gates(sample, [], [], None)
    assert _outcome(result, "G1").ok is False
    assert not result.kept
    assert result.failed_gates == ["G1"]


def test_g2_rejects_failed_execution():
    sample = Sample(history_id=1, question="各城市销量？", generated_sql="SELECT 1;", status=2)
    result = check_gates(sample, [], [], None)
    assert _outcome(result, "G2").ok is False
    assert result.failed_gates == ["G2"]


def test_g3_rejects_empty_sql():
    sample = Sample(history_id=1, question="今天心情怎么样？", generated_sql="   ")
    result = check_gates(sample, [], [], None)
    assert _outcome(result, "G3").ok is False
    assert "为空" in _outcome(result, "G3").detail
    assert "G3" in result.failed_gates  # 空 SQL 通常同时过不了 G4，不锁全集


def test_g4_rejects_unsafe_sql_with_fake_guard(monkeypatch: pytest.MonkeyPatch):
    """G4 与自家安全规则自洽（§4.3）：假 guard 判拒绝 → 样本落选于 G4。

    用 sys.modules 注入假 sql_guard：无论 T1 是否已合码，本用例行为一致。
    """
    fake_guard = types.ModuleType("agent.tools.sql_guard")
    fake_guard.validate_readonly_sql = lambda sql: (False, "拒绝执行：只允许 SELECT 查询语句。")
    monkeypatch.setitem(sys.modules, "agent.tools.sql_guard", fake_guard)
    sample = Sample(history_id=1, question="清空用户表", generated_sql="DROP TABLE users;")
    result = check_gates(sample, [], [], None)
    outcome = _outcome(result, "G4")
    assert outcome.ok is False
    assert "拒绝执行" in outcome.detail
    assert result.failed_gates == ["G4"]


def test_g4_skips_gracefully_when_t1_not_done(monkeypatch: pytest.MonkeyPatch):
    """G4 容错（工单硬性要求）：sql_guard 导入失败 → 输出"G4 跳过（T1 未完成）"且不崩。

    sys.modules 里放 None 会让 import 抛 ImportError——确定性地模拟 T1 未合码。
    """
    monkeypatch.setitem(sys.modules, "agent.tools.sql_guard", None)
    result = check_gates(CLEAN_SAMPLE, [], [], None)
    outcome = _outcome(result, "G4")
    assert outcome.ok is None  # 跳过不是失败
    assert "G4 跳过（T1 未完成）" in outcome.detail
    assert result.kept  # 其余门槛全过 → 样本仍保留
    assert result.failed_gates == []


def test_g4_with_real_guard_if_available():
    """真实 sql_guard 在位时的集成锁（T1 合码后自动生效；未合码则跳过）。"""
    pytest.importorskip("agent.tools.sql_guard")
    from agent.tools.sql_guard import validate_readonly_sql

    ok, _reason = validate_readonly_sql("SELECT 1;")
    assert ok  # 干净 SQL 通过 G4
    ok, reason = validate_readonly_sql("DROP TABLE users;")
    assert not ok and "拒绝执行" in reason


def test_g5_rejects_overlong_question():
    sample = Sample(history_id=1, question="问" * 101, generated_sql="SELECT 1;")
    result = check_gates(sample, [], [], None)
    assert _outcome(result, "G5").ok is False
    assert "超长" in _outcome(result, "G5").detail or "101" in _outcome(result, "G5").detail


def test_g5_boundary_values_pass():
    """边界：问题恰好 100 字、SQL 恰好 2000 字符 → 通过（§4.3 是 1~100 与 ≤2000）。"""
    sample = Sample(history_id=1, question="问" * 100, generated_sql="S" * 2000)
    result = check_gates(sample, [], [], None)
    assert _outcome(result, "G5").ok is True


def test_g5_rejects_overlong_sql():
    sample = Sample(history_id=1, question="销量？", generated_sql="S" * 2001)
    result = check_gates(sample, [], [], None)
    assert _outcome(result, "G5").ok is False


def test_g5_rejects_empty_question():
    sample = Sample(history_id=1, question="", generated_sql="SELECT 1;")
    result = check_gates(sample, [], [], None)
    assert _outcome(result, "G5").ok is False


# ---------------------------------------------------------------------------
# ② 双层去重（§4.4）：文本归一层 + 语义阈值层 + 本批次内
# ---------------------------------------------------------------------------


def test_normalize_question_ignores_whitespace_and_case():
    assert normalize_question("上个月的 GMV 是多少？") == normalize_question("上个月的GMV是多少？")
    assert normalize_question("  ABC  ") == "abc"
    assert normalize_question("a\tb\nc") == "abc"


def test_g6_text_layer_blocks_normalized_duplicate():
    corpus = parse_corpus_questions(CORPUS_TEXT)
    sample = Sample(history_id=2, question="上个月的GMV是多少？", generated_sql="SELECT 1;")
    result = check_gates(sample, corpus, [], FakeDeduper(top1_sim=0.80))
    outcome = _outcome(result, "G6")
    assert outcome.ok is False
    assert "与 QA-003 完全相同" in outcome.detail


def test_g6_text_layer_unique_question_survives():
    corpus = parse_corpus_questions(CORPUS_TEXT)
    result = check_gates(CLEAN_SAMPLE, corpus, [], FakeDeduper(top1_sim=0.80))
    outcome = _outcome(result, "G6")
    assert outcome.ok is True
    assert "语义 top1：QA-003 相似度 0.8000" in outcome.detail


def test_g6_semantic_layer_blocks_near_duplicate():
    sample = Sample(history_id=2, question="上月成交总额是多少？", generated_sql="SELECT 1;")
    result = check_gates(sample, [], [], FakeDeduper(top1_sim=0.92))
    assert _outcome(result, "G6").ok is False
    assert "语义相似度 0.9200" in _outcome(result, "G6").detail


def test_g6_semantic_layer_threshold_is_exclusive():
    """恰好等于阈值判重（≥0.92）；略低则保留——设计口径"相似度 ≥0.92 视为重复"。"""
    below = check_gates(CLEAN_SAMPLE, [], [], FakeDeduper(top1_sim=0.9199))
    assert _outcome(below, "G6").ok is True
    at = check_gates(CLEAN_SAMPLE, [], [], FakeDeduper(top1_sim=0.92))
    assert _outcome(at, "G6").ok is False


def test_g6_batch_text_duplicate_blocked():
    corpus = parse_corpus_questions(CORPUS_TEXT)
    first = Sample(history_id=10, question="近7天每天的支付金额", generated_sql="SELECT 1;")
    second = Sample(history_id=11, question="近 7 天 每天的支付金额", generated_sql="SELECT 2;")
    batch = []
    result_first = check_gates(first, corpus, batch, FakeDeduper(top1_sim=0.5))
    assert result_first.kept
    batch.append(
        absorb_feedback.BatchItem(
            first.history_id, normalize_question(first.question), result_first.embedding
        )
    )
    result_second = check_gates(second, corpus, batch, FakeDeduper(top1_sim=0.5))
    outcome = _outcome(result_second, "G6")
    assert outcome.ok is False
    assert "与本批次样本 historyId=10 相同" in outcome.detail


def test_g6_batch_semantic_duplicate_blocked():
    """§4.4"本批次内先两两判重"：同批两条语义近似（文本层拦不住）也被拦。"""
    vectors = {"问题A": [1.0, 0.0], "问题B": [1.0, 0.0]}  # 完全同向 → cosine 1.0
    deduper = FakeDeduper(top1_sim=0.5, embeddings=vectors)
    first = Sample(history_id=10, question="问题A", generated_sql="SELECT 1;")
    second = Sample(history_id=11, question="问题B", generated_sql="SELECT 2;")
    batch = []
    result_first = check_gates(first, [], batch, deduper)
    assert result_first.kept
    batch.append(
        absorb_feedback.BatchItem(
            first.history_id, normalize_question(first.question), result_first.embedding
        )
    )
    result_second = check_gates(second, [], batch, deduper)
    outcome = _outcome(result_second, "G6")
    assert outcome.ok is False
    assert "与本批次样本 historyId=10 语义相似度 1.0000" in outcome.detail


def test_g6_semantic_skipped_when_index_has_no_qa_pairs():
    result = check_gates(CLEAN_SAMPLE, [], [], FakeDeduper(qa_count=0))
    outcome = _outcome(result, "G6")
    assert outcome.ok is True
    assert "语义层跳过" in outcome.detail


def test_g6_without_deduper_only_text_layer():
    """语义栈未加载（降级/单测路径）：文本层仍生效，语义部分注明跳过。"""
    corpus = parse_corpus_questions(CORPUS_TEXT)
    dup = check_gates(
        Sample(history_id=2, question="上个月的GMV是多少？", generated_sql="SELECT 1;"), corpus, [], None
    )
    assert _outcome(dup, "G6").ok is False
    fresh = check_gates(CLEAN_SAMPLE, corpus, [], None)
    outcome = _outcome(fresh, "G6")
    assert outcome.ok is True
    assert "语义层未加载" in outcome.detail


def test_cosine_similarity_pure_function():
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)
    assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0  # 零向量不崩


# ---------------------------------------------------------------------------
# ③ QA 编号解析与顺延（§4.6）
# ---------------------------------------------------------------------------


def test_parse_max_qa_number_and_increment():
    assert parse_max_qa_number(CORPUS_TEXT) == 3
    # 夹具最大号 3，但自举编号有 12 的下限（001~011 留给人工语料）→ 仍取 12
    assert next_qa_number(CORPUS_TEXT) == 12


def test_next_qa_number_from_real_corpus_shape():
    text = "\n\n".join(_qa_entry(i, f"问题{i}") for i in range(1, 12)) + "\n"
    assert parse_max_qa_number(text) == 11
    assert next_qa_number(text) == 12  # 设计 §4.6：从 QA-012 起顺延


def test_next_qa_number_floor_is_12_even_when_corpus_empty():
    """下限防御：001~011 段位留给人工语料，自举编号永不挤进该区间。"""
    assert next_qa_number("") == 12
    assert next_qa_number("只有文件头没有条目") == 12
    assert next_qa_number(_qa_entry(15, "已有 015")) == 16


def test_parse_corpus_questions_extracts_pairs_and_skips_tail_section():
    corpus = CORPUS_TEXT + "\n## 待真库核对清单\n\n- [ ] QA-001 已核对\n"
    pairs = parse_corpus_questions(corpus)
    assert pairs == [
        ("QA-001", "2026 年 3 月每天的订单量是多少？"),
        ("QA-003", "上个月的 GMV 是多少？"),
    ]


# ---------------------------------------------------------------------------
# ④ 条目格式（§4.6 模板）与追加位置
# ---------------------------------------------------------------------------


def test_format_qa_entry_snapshot_without_comment():
    entry = format_qa_entry(12, "上个月的GMV是多少？", "SELECT 1;", "", "2026-10-11")
    assert entry == (
        "### QA-012\n"
        "**问**：上个月的GMV是多少？\n"
        "**金标 SQL**：\n"
        "```sql\n"
        "SELECT 1;\n"
        "```\n"
        "**说明**：（自举收录 2026-10-11，来源：用户点赞问答）"
    )


def test_format_qa_entry_appends_comment_and_normalizes_whitespace():
    entry = format_qa_entry(
        13, "  上个月\n的GMV  是多少？ ", "SELECT 2;", "口径按支付时间", "2026-10-11"
    )
    assert "**问**：上个月 的GMV 是多少？" in entry  # 展示空白规范化
    assert "**说明**：（自举收录 2026-10-11，来源：用户点赞问答；用户补充：口径按支付时间）" in entry


def test_append_entries_at_file_end():
    new_text = append_entries(CORPUS_TEXT, [format_qa_entry(4, "新问题", "SELECT 9;", "", "2026-10-11")])
    assert "### QA-003" in new_text and "### QA-004" in new_text
    assert "\n\n### QA-004" in new_text  # 与上一条之间恰一个空行
    assert new_text.endswith("**说明**：（自举收录 2026-10-11，来源：用户点赞问答）\n")


def test_append_entries_inserts_before_trailing_section():
    """防御分支（§4.6）：尾部有非 QA 的 `## ` 章节时插到它前面，防止尾注粘进语料片。"""
    corpus = CORPUS_TEXT + "\n## 待真库核对清单\n\n- [ ] QA-001 已核对\n"
    new_text = append_entries(corpus, [format_qa_entry(4, "新问题", "SELECT 9;", "", "2026-10-11")])
    assert new_text.index("### QA-004") < new_text.index("## 待真库核对清单")
    assert "- [ ] QA-001 已核对" in new_text  # 尾注内容原样保留


# ---------------------------------------------------------------------------
# ⑤ --apply 的文件写入（tmp_path 隔离；网络与模型全部注入假件）
# ---------------------------------------------------------------------------


def _patch_run_collect(monkeypatch: pytest.MonkeyPatch, qa_path, samples, deduper):
    monkeypatch.setattr(absorb_feedback, "QA_PAIRS_PATH", qa_path)
    monkeypatch.setattr(absorb_feedback, "fetch_samples", lambda server: samples)
    monkeypatch.setattr(absorb_feedback, "load_deduper", lambda corpus_questions: deduper)


def test_apply_writes_numbered_entries_to_corpus_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
):
    qa_path = tmp_path / "qa_pairs.md"
    qa_path.write_text(CORPUS_TEXT, encoding="utf-8")
    disliked = Sample(history_id=1, question="随便问问", generated_sql="SELECT 1;", rating=0)
    _patch_run_collect(monkeypatch, qa_path, [CLEAN_SAMPLE, disliked], None)
    args = argparse.Namespace(apply=True, server="http://ut", calibrate=False)
    absorb_feedback.run_collect(args)
    written = qa_path.read_text(encoding="utf-8")
    assert "### QA-012" in written  # 夹具最大号 3 → 自举编号取下限 12
    assert "**说明**：（自举收录" in written and "来源：用户点赞问答" in written
    assert CLEAN_SAMPLE.question in written
    assert "随便问问" not in written  # G1 落选样本不进语料
    output = capsys.readouterr().out
    assert "已追加 1 条" in output
    assert "G1: 1" in output
    assert "请运行 python -m agent.rag.build_index 重建索引" in output


def test_apply_inserts_before_tail_section(monkeypatch: pytest.MonkeyPatch, tmp_path):
    qa_path = tmp_path / "qa_pairs.md"
    qa_path.write_text(CORPUS_TEXT + "\n## 待真库核对清单\n\n- [ ] x\n", encoding="utf-8")
    _patch_run_collect(monkeypatch, qa_path, [CLEAN_SAMPLE], None)
    absorb_feedback.run_collect(argparse.Namespace(apply=True, server="http://ut", calibrate=False))
    written = qa_path.read_text(encoding="utf-8")
    assert written.index("### QA-012") < written.index("## 待真库核对清单")


def test_apply_with_no_kept_sample_leaves_file_untouched(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
):
    qa_path = tmp_path / "qa_pairs.md"
    qa_path.write_text(CORPUS_TEXT, encoding="utf-8")
    bad = Sample(history_id=1, question="上个月的GMV是多少？", generated_sql="SELECT 1;")
    _patch_run_collect(monkeypatch, qa_path, [bad], None)
    absorb_feedback.run_collect(argparse.Namespace(apply=True, server="http://ut", calibrate=False))
    assert qa_path.read_text(encoding="utf-8") == CORPUS_TEXT  # 逐字节未动
    assert "语料文件未改动" in capsys.readouterr().out


def test_dry_run_does_not_write(monkeypatch: pytest.MonkeyPatch, tmp_path, capsys):
    qa_path = tmp_path / "qa_pairs.md"
    qa_path.write_text(CORPUS_TEXT, encoding="utf-8")
    _patch_run_collect(monkeypatch, qa_path, [CLEAN_SAMPLE], FakeDeduper())
    absorb_feedback.run_collect(argparse.Namespace(apply=False, server="http://ut", calibrate=False))
    assert qa_path.read_text(encoding="utf-8") == CORPUS_TEXT
    output = capsys.readouterr().out
    assert "--dry-run 未写文件" in output and "拟编号 QA-012" in output


def test_run_collect_empty_samples_never_loads_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
):
    """完成标准：空样本集不崩，且不白付几秒的 embedding 模型加载。"""
    qa_path = tmp_path / "qa_pairs.md"
    qa_path.write_text(CORPUS_TEXT, encoding="utf-8")

    def _boom(*_args):
        raise AssertionError("空样本集不应加载语义栈")

    monkeypatch.setattr(absorb_feedback, "QA_PAIRS_PATH", qa_path)
    monkeypatch.setattr(absorb_feedback, "fetch_samples", lambda server: [])
    monkeypatch.setattr(absorb_feedback, "load_deduper", _boom)
    absorb_feedback.run_collect(argparse.Namespace(apply=False, server="http://ut", calibrate=False))
    output = capsys.readouterr().out
    assert "样本 0 条" in output
    assert "语料未改动" in output


def test_run_collect_tolerates_missing_corpus_file(monkeypatch: pytest.MonkeyPatch, tmp_path, capsys):
    qa_path = tmp_path / "does_not_exist.md"
    _patch_run_collect(monkeypatch, qa_path, [CLEAN_SAMPLE], None)
    absorb_feedback.run_collect(argparse.Namespace(apply=True, server="http://ut", calibrate=False))
    written = qa_path.read_text(encoding="utf-8")
    assert "### QA-012" in written  # 空语料 → 自举编号从下限 12 起
    assert "警告：未找到语料文件" in capsys.readouterr().out

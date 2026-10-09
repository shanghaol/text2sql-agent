"""W2 系统提示词组装的默认套件测试。

零依赖原则：不连 MySQL、不需要 API key、不加载 embedding 模型——
build_system_prompt / format_context_block 都是纯函数；
导入 agent.core.agent 不会触发模型加载（BgeEmbedder 是懒加载的）。
"""

from agent.core.agent import build_system_prompt, format_context_block
from agent.rag.hybrid_retriever import RetrievedChunk

# 六张表名（数据表一览的静态兜底，一行一个）
_EXPECTED_TABLES = ("users", "products", "orders", "order_items", "payments", "refunds")


def _make_chunk(doc_id: str, doc_type: str, title: str, text: str) -> RetrievedChunk:
    return RetrievedChunk(id=doc_id, doc_type=doc_type, title=title, text=text, rrf_score=0.01)


# ---------------------------------------------------------------------------
# build_system_prompt：静态骨架快照式断言（设计 §3.5）
# ---------------------------------------------------------------------------


def test_prompt_contains_reference_section_marker():
    """【参考资料】动态块必须存在——这是 W2"静态骨架 + 动态注入"的结构标记。"""
    prompt = build_system_prompt("示例资料内容")
    assert "【参考资料】" in prompt
    assert "示例资料内容" in prompt


def test_prompt_contains_data_anchor():
    """数据锚点声明：静态快照期 + 相对时间以 2026-09-30 为'今天'。"""
    prompt = build_system_prompt("")
    assert "2025-10-01" in prompt
    assert "2026-09-30" in prompt
    assert "静态快照" in prompt


def test_prompt_contains_mysql_dialect_hints():
    """MySQL 方言提示：DATE_FORMAT/CURDATE/DATE_SUB/CONCAT 要点名，
    SQLite 的 strftime/date('now') 要点名禁止。"""
    prompt = build_system_prompt("")
    assert "DATE_FORMAT" in prompt
    assert "CURDATE" in prompt
    assert "DATE_SUB" in prompt
    assert "CONCAT" in prompt
    assert "strftime" in prompt
    assert "date('now')" in prompt


def test_prompt_lists_all_six_tables():
    """常驻表一览：6 行表名兜底（检索失灵时模型至少知道库里有什么）。"""
    prompt = build_system_prompt("")
    for table in _EXPECTED_TABLES:
        assert table in prompt


def test_prompt_contains_behavior_rules_and_list_tables_fallback():
    """行为规则第 1 条：先查参考资料，list_tables 降级为兜底。"""
    prompt = build_system_prompt("")
    assert "【行为规则】" in prompt
    assert "list_tables" in prompt
    assert "只读" in prompt


def test_prompt_empty_context_uses_fallback_guidance():
    """检索颗粒无收时：参考资料段换成兜底文案，引导模型走 list_tables。"""
    prompt = build_system_prompt("")
    assert "没有检索到相关资料" in prompt
    assert "list_tables" in prompt


# ---------------------------------------------------------------------------
# format_context_block：检索片注入格式（`--- [N] 类型 · 标题 ---`）
# ---------------------------------------------------------------------------


def test_format_context_block_headers_types_and_order():
    """每片一个标题行，类型名按 doc_type 映射，序号从 1 连续递增。"""
    chunks = [
        _make_chunk("schema:orders", "schema", "订单表 orders", "orders 的 DDL 全文"),
        _make_chunk("glossary:gmv", "glossary", "GMV（成交总额）", "GMV 的口径定义"),
        _make_chunk("qa_pair:qa_001", "qa_pair", "上个月 GMV 是多少", "### QA-001 全文"),
    ]
    block = format_context_block(chunks)
    assert "--- [1] 表结构 · 订单表 orders ---" in block
    assert "--- [2] 业务口径 · GMV（成交总额） ---" in block
    assert "--- [3] 示例问答 · 上个月 GMV 是多少 ---" in block
    assert "orders 的 DDL 全文" in block
    assert "GMV 的口径定义" in block
    # 注入的是 chunk 全文（qa_pair 片含问/SQL/说明整体）
    assert "### QA-001 全文" in block
    # 标题顺序与检索排序一致
    assert block.index("--- [1]") < block.index("--- [2]") < block.index("--- [3]")


def test_format_context_block_unknown_type_and_empty():
    """未知 doc_type 兜底为'资料'；空列表返回空字符串（触发 prompt 兜底文案）。"""
    single = format_context_block([_make_chunk("x:1", "mystery", "标题", "正文")])
    assert "--- [1] 资料 · 标题 ---" in single
    assert format_context_block([]) == ""

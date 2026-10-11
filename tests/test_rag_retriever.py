"""RAG 检索层的默认套件测试。

零依赖原则：不连 MySQL、不需要 API key、不加载真实 embedding 模型——
- 向量用 FakeEmbedder（按分词 hash 到 512 维的确定性假实现）；
- BgeEmbedder 只断言类常量，不实例化（实例化才惰性加载 fastembed）；
- Chroma 用 pytest 临时目录里的 PersistentClient。

jieba 的分词结果是进程级全局状态：导入 agent.rag.hybrid_retriever 时
已自动执行 setup_jieba()，所以本文件所有用例都在"自定义词典已生效"下运行。
"""

import hashlib
import math
from pathlib import Path

import chromadb
import jieba
import pytest

from agent.rag import build_index
from agent.rag.embedder import BgeEmbedder
from agent.rag.hybrid_retriever import HybridRetriever, RetrievedChunk, rrf_fuse
from agent.rag.jieba_dict import COLUMN_WORDS, METRIC_WORDS, TABLE_WORDS, setup_jieba

RRF_K = 60
EMBED_DIM = 512


# ---------------------------------------------------------------------------
# ① rrf_fuse 纯函数（面试"手撕 RRF"的可复现性断言）
# ---------------------------------------------------------------------------


def test_rrf_fuse_single_path_ranking_and_scores():
    """单路输入：第 rank 名得分 1/(k+rank)，按分数降序。"""
    fused = rrf_fuse([["a", "b", "c"]], k=RRF_K, top_n=8)
    assert fused[0] == ("a", 1.0 / (RRF_K + 1))
    assert fused[1] == ("b", 1.0 / (RRF_K + 2))
    assert fused[2] == ("c", 1.0 / (RRF_K + 3))


def test_rrf_fuse_stacks_same_doc_hit_in_both_paths():
    """两路同片叠加：得分相加，排到最前——"两路都认可"的设计意图。"""
    fused = rrf_fuse([["a", "b"], ["a"]], k=RRF_K, top_n=8)
    assert fused[0] == ("a", 2.0 / (RRF_K + 1))
    assert fused[1] == ("b", 1.0 / (RRF_K + 2))


def test_rrf_fuse_ties_break_by_id_stably():
    """平局按 id 升序，与输入路的出现顺序无关（可复现性核心断言）。"""
    first = rrf_fuse([["x"], ["y"]], k=RRF_K, top_n=8)
    second = rrf_fuse([["y"], ["x"]], k=RRF_K, top_n=8)
    assert [doc_id for doc_id, _ in first] == ["x", "y"]
    assert [doc_id for doc_id, _ in second] == ["x", "y"]


def test_rrf_fuse_truncates_to_top_n():
    fused = rrf_fuse([["a", "b", "c", "d"]], k=RRF_K, top_n=2)
    assert [doc_id for doc_id, _ in fused] == ["a", "b"]


def test_rrf_fuse_empty_input_does_not_crash():
    assert rrf_fuse([], k=RRF_K, top_n=8) == []
    assert rrf_fuse([[], []], k=RRF_K, top_n=8) == []


# ---------------------------------------------------------------------------
# ② jieba 自定义词典：逐词断言整词（jieba 升级行为漂移的回归锁）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("word", TABLE_WORDS + COLUMN_WORDS + METRIC_WORDS)
def test_jieba_keeps_every_domain_word_whole(word: str):
    """每个自定义词单独成句时必须整词切出——任何一词被切碎测试立刻红。"""
    setup_jieba()
    assert jieba.lcut(word) == [word]


def test_setup_jieba_is_idempotent_and_applies_in_sentences():
    """重复调用不报错；真实句子里的下划线词与指标词保持整词。"""
    setup_jieba()
    setup_jieba()
    assert jieba.lcut("order_items") == ["order_items"]
    tokens = jieba.lcut("上个月客单价和复购率是多少")
    assert "上个月" in tokens
    assert "客单价" in tokens
    assert "复购率" in tokens


# ---------------------------------------------------------------------------
# ③ parse_corpus 语料解析器（tmp_path 迷你语料）
# ---------------------------------------------------------------------------


def _write_mini_corpus(root: Path) -> None:
    """造一套三类各 2 片的迷你语料（结构与 T2 正式语料同构）。"""
    schema_dir = root / "schema_docs"
    schema_dir.mkdir()
    (schema_dir / "users.md").write_text(
        "# 表：users（用户表）\n\n## DDL\nCREATE TABLE users (...)\n",
        encoding="utf-8",
    )
    (schema_dir / "orders.md").write_text(
        "# 表：orders（订单表）\n\n## DDL\nCREATE TABLE orders (...)\n",
        encoding="utf-8",
    )
    (root / "glossary.md").write_text(
        "# 业务口径\n\n"
        "## 指标：成交总额（gmv）\n"
        "**定义**：支付成功订单金额总和。\n\n"
        "## 指标：客单价（avg_order_value）\n"
        "**定义**：GMV 除以支付订单数。\n",
        encoding="utf-8",
    )
    long_question = "2026 年 3 月每天的订单量是多少？" + "补充说明" * 20
    (root / "qa_pairs.md").write_text(
        "### QA-001\n"
        "**问**：上个月的 GMV 是多少？\n"
        "**金标 SQL**：\nSELECT SUM(payments.amount) FROM payments;\n\n"
        f"### QA-002\n**问**：{long_question}\n"
        "**金标 SQL**：\nSELECT 1;\n",
        encoding="utf-8",
    )


def test_parse_corpus_splits_three_types_with_correct_ids_and_metadata(tmp_path: Path):
    _write_mini_corpus(tmp_path)
    chunks = build_index.parse_corpus(tmp_path)

    assert {c["id"] for c in chunks} == {
        "schema:users",
        "schema:orders",
        "glossary:gmv",
        "glossary:avg_order_value",
        "qa_pair:qa_001",
        "qa_pair:qa_002",
    }
    by_type: dict[str, list[dict]] = {}
    for chunk in chunks:
        by_type.setdefault(chunk["doc_type"], []).append(chunk)
    assert {k: len(v) for k, v in by_type.items()} == {
        "schema": 2,
        "glossary": 2,
        "qa_pair": 2,
    }

    # 每片的字段恰为六项；metadata 四字段齐备且缺失处用 "" 不用 None
    orders = next(c for c in chunks if c["id"] == "schema:orders")
    assert set(orders) == {"id", "doc_type", "source_id", "title", "table_name", "text"}
    assert orders["title"] == "表：orders（订单表）"
    assert orders["table_name"] == "orders"

    gmv = next(c for c in chunks if c["id"] == "glossary:gmv")
    assert gmv["title"] == "成交总额（gmv）"
    assert gmv["table_name"] == ""
    assert "**定义**：支付成功订单金额总和。" in gmv["text"]

    qa1 = next(c for c in chunks if c["id"] == "qa_pair:qa_001")
    assert qa1["title"] == "上个月的 GMV 是多少？"
    assert qa1["table_name"] == ""
    assert qa1["text"].startswith("### QA-001")  # 条目全文含标题行

    qa2 = next(c for c in chunks if c["id"] == "qa_pair:qa_002")
    assert len(qa2["title"]) == 50  # 超长问句截断到 50 字


def test_parse_corpus_schema_title_falls_back_to_filename(tmp_path: Path):
    schema_dir = tmp_path / "schema_docs"
    schema_dir.mkdir()
    (schema_dir / "refunds.md").write_text("没有 H1 标题行的内容\n", encoding="utf-8")
    chunks = build_index.parse_corpus(tmp_path)
    assert len(chunks) == 1
    assert chunks[0]["id"] == "schema:refunds"
    assert chunks[0]["title"] == "refunds"  # 解析不到 "# 表：" 时用文件名


def test_parse_corpus_raises_listing_entries_without_english_id(tmp_path: Path):
    (tmp_path / "glossary.md").write_text(
        "## 指标：成交总额（gmv）\n定义 A。\n\n"
        "## 指标：动销率\n定义 B，但标题里没有全角括号英文 id。\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as excinfo:
        build_index.parse_corpus(tmp_path)
    message = str(excinfo.value)
    assert "动销率" in message  # 报错要列出问题行（条目名）
    assert "glossary.md" in message  # 以及所在文件与行号定位


def test_parse_corpus_skips_missing_files(tmp_path: Path):
    """只写 schema 也能解析（先写好 schema 先建索引的渐进体验）。"""
    schema_dir = tmp_path / "schema_docs"
    schema_dir.mkdir()
    (schema_dir / "users.md").write_text("# 表：users（用户表）\n", encoding="utf-8")
    chunks = build_index.parse_corpus(tmp_path)
    assert [c["id"] for c in chunks] == ["schema:users"]


def test_build_index_main_prints_guidance_when_corpus_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """语料目录缺失：打印中文指引并以退出码 1 结束，不抛裸异常。"""
    monkeypatch.setattr(build_index, "CORPUS_DIR", tmp_path / "rag_corpus")
    with pytest.raises(SystemExit) as excinfo:
        build_index.main()
    assert excinfo.value.code == 1
    captured = capsys.readouterr().out
    assert "rag_corpus" in captured
    assert "语料" in captured


# ---------------------------------------------------------------------------
# ④ HybridRetriever（假 embedder + 临时 Chroma collection）
# ---------------------------------------------------------------------------


class FakeEmbedder:
    """确定性假 embedder：每个分词 hash 到 512 维中的一格，计数后归一化。

    与 BM25 路共用 jieba 分词，因此"文本相同的片"距离为 0、
    "共享分词的片"距离更近——足以驱动混合检索的相关性排序。
    """

    def __init__(self, dim: int = EMBED_DIM) -> None:
        self.dim = dim

    def _vector(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for token in jieba.lcut(text):
            digest = hashlib.md5(token.encode("utf-8")).digest()
            vec[int.from_bytes(digest[:4], "big") % self.dim] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    def embed(self, docs: list[str]) -> list[list[float]]:
        return [self._vector(d) for d in docs]

    def embed_query(self, q: str) -> list[float]:
        return self._vector(q)


MINI_CORPUS = [
    (
        "schema:orders",
        {"doc_type": "schema", "source_id": "orders", "title": "表：orders（订单表）", "table_name": "orders"},
        "订单表 orders 的 DDL 与列说明，含 created_at order_amount user_id status",
    ),
    (
        "schema:users",
        {"doc_type": "schema", "source_id": "users", "title": "表：users（用户表）", "table_name": "users"},
        "用户表 users 的 DDL 与列说明，含 name city registered_at gender",
    ),
    (
        "glossary:avg_order_value",
        {"doc_type": "glossary", "source_id": "avg_order_value", "title": "客单价（avg_order_value）", "table_name": ""},
        "客单价口径：GMV 除以支付订单数，分母要 DISTINCT 去重",
    ),
    (
        "glossary:repurchase",
        {"doc_type": "glossary", "source_id": "repurchase", "title": "复购率（repurchase）", "table_name": ""},
        "复购率口径：期内下单不少于两次的用户占下单用户的比例",
    ),
    (
        "qa_pair:qa_001",
        {"doc_type": "qa_pair", "source_id": "qa_001", "title": "上个月的 GMV 是多少", "table_name": ""},
        "示例问答：上个月的 GMV 是多少，金标 SQL 使用 payments.amount",
    ),
    (
        "qa_pair:qa_002",
        {"doc_type": "qa_pair", "source_id": "qa_002", "title": "近 7 天每天的支付金额", "table_name": ""},
        "示例问答：近 7 天每天的支付金额，金标 SQL 使用 DATE_SUB 时间窗口",
    ),
]


@pytest.fixture()
def retriever(tmp_path: Path) -> HybridRetriever:
    """临时目录里的 Chroma collection + 假 embedder 组装的检索器。"""
    client = chromadb.PersistentClient(path=str(tmp_path / "chroma"))
    collection = client.get_or_create_collection(name="test_corpus")
    embedder = FakeEmbedder()
    collection.upsert(
        ids=[doc_id for doc_id, _, _ in MINI_CORPUS],
        embeddings=embedder.embed([text for _, _, text in MINI_CORPUS]),
        documents=[text for _, _, text in MINI_CORPUS],
        metadatas=[meta for _, meta, _ in MINI_CORPUS],
    )
    return HybridRetriever(collection, embedder)


def test_search_returns_sorted_retrieved_chunks(retriever: HybridRetriever):
    results = retriever.search("客单价怎么算")
    assert results  # 语料 6 片 < 默认 top_n=8，全部返回也合法
    assert all(isinstance(chunk, RetrievedChunk) for chunk in results)
    scores = [chunk.rrf_score for chunk in results]
    assert scores == sorted(scores, reverse=True)
    for chunk in results:
        assert chunk.id and chunk.doc_type and chunk.text
        assert isinstance(chunk.title, str)


def test_search_truncates_to_top_n(retriever: HybridRetriever):
    """融合排序取前 top_n 条（即使每路每类型取了更多候选）。"""
    results = retriever.search("订单 口径 示例", top_k_each=20, top_n=3)
    assert len(results) == 3


def test_search_semantic_hit_ranks_glossary_first(retriever: HybridRetriever):
    """与口径片共享分词的查询：该片两路（BM25+向量）双命中，得分叠加排第一。"""
    results = retriever.search("客单价怎么算")
    assert results[0].id == "glossary:avg_order_value"


def test_search_filters_by_doc_types(retriever: HybridRetriever):
    results = retriever.search("口径 分母", doc_types=["glossary"])
    assert results
    assert {chunk.doc_type for chunk in results} == {"glossary"}

    both = retriever.search("口径", doc_types=["schema", "glossary"])
    assert {chunk.doc_type for chunk in both} <= {"schema", "glossary"}


def test_search_without_types_searches_all(retriever: HybridRetriever):
    results = retriever.search("GMV 口径 示例")
    assert results
    assert {chunk.doc_type for chunk in results} <= {"schema", "glossary", "qa_pair"}


def test_search_top_k_each_falls_back_to_filtered_size(retriever: HybridRetriever):
    """top_k_each 大于过滤后条数：n_results 取 min 兜底，不抛 contiguous 错。"""
    results = retriever.search("订单", top_k_each=100)
    assert results

    filtered = retriever.search("订单", doc_types=["qa_pair"], top_k_each=100)
    assert {chunk.doc_type for chunk in filtered} == {"qa_pair"}


def test_search_unknown_doc_types_returns_empty(retriever: HybridRetriever):
    assert retriever.search("任意", doc_types=["nonexistent_type"]) == []


def test_search_on_empty_collection_returns_empty(tmp_path: Path):
    """空 collection：rebuild 不报错，search 返回空列表（先建索引的体验）。"""
    client = chromadb.PersistentClient(path=str(tmp_path / "empty_chroma"))
    collection = client.get_or_create_collection(name="empty_corpus")
    retriever = HybridRetriever(collection, FakeEmbedder())
    assert retriever.search("上个月 GMV") == []
    # 指定了 doc_types 也不能崩
    assert retriever.search("上个月 GMV", doc_types=["glossary"]) == []


def test_rebuild_picks_up_new_corpus(tmp_path: Path):
    """rebuild() 后新语料可被检索（rank-bm25 语料更新靠重建对象的语义）。"""
    client = chromadb.PersistentClient(path=str(tmp_path / "rebuild_chroma"))
    collection = client.get_or_create_collection(name="rebuild_corpus")
    embedder = FakeEmbedder()
    retriever = HybridRetriever(collection, embedder)
    assert retriever.search("退款率") == []

    collection.upsert(
        ids=["glossary:refund_rate"],
        embeddings=embedder.embed(["退款率口径：退款金额除以支付金额"]),
        documents=["退款率口径：退款金额除以支付金额"],
        metadatas=[{"doc_type": "glossary", "source_id": "refund_rate", "title": "退款率", "table_name": ""}],
    )
    retriever.rebuild()
    results = retriever.search("退款率")
    assert results[0].id == "glossary:refund_rate"


# ---------------------------------------------------------------------------
# ⑤ embedder 只断言契约常量（不实例化 → 不触发 fastembed 模型加载）
# ---------------------------------------------------------------------------


def test_bge_embedder_query_instruction_prefix():
    """查询侧指令前缀必须与设计一致（bge 检索口径，W6 消融不再动它）。"""
    assert BgeEmbedder.QUERY_INSTRUCTION_PREFIX == "为这个句子生成表示以检索相关文章："
    assert BgeEmbedder.DEFAULT_MODEL_NAME == "BAAI/bge-small-zh-v1.5"


def test_split_entries_last_entry_stops_at_h2_section():
    """回归（W2 审查 P1）：最后一条条目后面跟 `## ` 章节标题时必须截断，
    防止核对清单/金标答案等非语料内容被粘进最后一个语料片。"""
    from agent.rag.build_index import _split_entries

    text = (
        "### QA-001\n**问**：A\n内容A\n"
        "### QA-002\n**问**：B\n内容B\n"
        "## 待真库核对清单\n- [x] 金标答案 16,029,955.34\n"
    )
    entries = _split_entries(text, "### QA-")
    assert len(entries) == 2
    assert "内容B" in entries[1][2]
    assert "金标答案" not in entries[1][2]  # `## ` 章节被截掉


# ---------------------------------------------------------------------------
# ⑥ W6 消融入口（设计 §3.4/§4.2）：路开关 + rerank 钩子
#    同构红线：新参全部默认值路径与现行逐字节等价（首测即锁）
# ---------------------------------------------------------------------------


class CountingEmbedder:
    """包一层 FakeEmbedder 数 embed_query 调用次数——路开关的单路断言面。"""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.query_calls = 0

    def embed_query(self, q: str) -> list[float]:
        self.query_calls += 1
        return self.inner.embed_query(q)

    def embed(self, docs: list[str]) -> list[list[float]]:
        return self.inner.embed(docs)


class FakeReranker:
    """mock 重排器：记录调用；reverse=True 时倒序返回（模拟"救回池尾片"）。"""

    def __init__(self, reverse: bool = False, fail: bool = False) -> None:
        self.calls: list[dict] = []
        self.reverse = reverse
        self.fail = fail

    def rerank(
        self, query: str, documents: list[str], top_n: int | None = None
    ) -> list[tuple[int, float]]:
        self.calls.append({"query": query, "documents": list(documents), "top_n": top_n})
        if self.fail:
            raise RuntimeError("mock rerank down")
        order = list(range(len(documents)))
        if self.reverse:
            order.reverse()
        pairs = [(i, 1.0 - i * 0.01) for i in order]
        return pairs[:top_n] if top_n is not None else pairs


@pytest.fixture()
def spied_retriever(tmp_path: Path) -> tuple[HybridRetriever, CountingEmbedder]:
    """带 embed_query 计数的检索器 fixture（语料同 MINI_CORPUS 六片）。"""
    client = chromadb.PersistentClient(path=str(tmp_path / "chroma_spy"))
    collection = client.get_or_create_collection(name="test_corpus_spy")
    inner = FakeEmbedder()
    spy = CountingEmbedder(inner)
    collection.upsert(
        ids=[doc_id for doc_id, _, _ in MINI_CORPUS],
        embeddings=spy.embed([text for _, _, text in MINI_CORPUS]),
        documents=[text for _, _, text in MINI_CORPUS],
        metadatas=[meta for _, meta, _ in MINI_CORPUS],
    )
    return HybridRetriever(collection, spy), spy


def test_search_new_params_defaults_are_byte_equivalent(retriever: HybridRetriever):
    """同构红线锁：显式传全部默认新参 == 不传（id 与 rrf_score 逐位相等）。"""
    for query in ("客单价怎么算", "订单 口径 示例", "上个月 GMV"):
        plain = retriever.search(query)
        explicit = retriever.search(
            query, enable_bm25=True, enable_dense=True, reranker=None, rerank_pool=0
        )
        assert [(c.id, c.rrf_score) for c in plain] == [(c.id, c.rrf_score) for c in explicit]


def test_search_bm25_only_skips_embedding(spied_retriever):
    """纯 BM25（消融 B 行镜像：关向量路）：不调 embed_query，仍出结果。"""
    retriever, spy = spied_retriever
    results = retriever.search("客单价怎么算", enable_dense=False)
    assert spy.query_calls == 0
    assert results
    assert all(isinstance(c, RetrievedChunk) for c in results)
    # BM25 单路的相关性：与口径片共享分词的查询，该口径片排第一
    assert results[0].id == "glossary:avg_order_value"


def test_search_dense_only_skips_bm25(spied_retriever):
    """纯向量（消融 C 行镜像：关 BM25 路）：embed_query 恰好一次，仍出结果。"""
    retriever, spy = spied_retriever
    results = retriever.search("客单价怎么算", enable_bm25=False)
    assert spy.query_calls == 1
    assert results
    scores = [c.rrf_score for c in results]
    assert scores == sorted(scores, reverse=True)


def test_search_single_path_keeps_doc_types_filtering(spied_retriever):
    """单路模式下沿用既有过滤语义：BM25 路分组跳过 / 向量路 where 过滤。"""
    retriever, _ = spied_retriever
    bm25_only = retriever.search("口径 分母", doc_types=["glossary"], enable_dense=False)
    assert bm25_only and {c.doc_type for c in bm25_only} == {"glossary"}

    dense_only = retriever.search("订单", doc_types=["qa_pair"], enable_bm25=False)
    assert dense_only and {c.doc_type for c in dense_only} == {"qa_pair"}


def test_search_single_path_on_empty_collection_returns_empty(tmp_path: Path):
    client = chromadb.PersistentClient(path=str(tmp_path / "empty_single"))
    collection = client.get_or_create_collection(name="empty_single")
    retriever = HybridRetriever(collection, FakeEmbedder())
    assert retriever.search("任何", enable_dense=False) == []
    assert retriever.search("任何", enable_bm25=False) == []


def test_search_both_paths_disabled_raises(retriever: HybridRetriever):
    """双路全关=检索恒空，消融参数配错的信号——配置期就大声失败。"""
    with pytest.raises(ValueError, match="不能同时为 False"):
        retriever.search("任何", enable_bm25=False, enable_dense=False)


def test_search_rerank_hook_off_by_default(spied_retriever):
    """钩子默认关：reranker 存在但 rerank_pool=0 → 不重排，结果与普通 search 全等。"""
    retriever, _ = spied_retriever
    reranker = FakeReranker()
    with_hook_off = retriever.search("订单 口径", reranker=reranker, rerank_pool=0)
    plain = retriever.search("订单 口径")
    assert reranker.calls == []
    assert [(c.id, c.rrf_score) for c in with_hook_off] == [(c.id, c.rrf_score) for c in plain]


def test_search_rerank_hook_pool_size_and_top_n(spied_retriever):
    """钩子开启：候选池=rerank_pool 片、注入=top_n 片；重排分写入 rrf_score 位。"""
    retriever, _ = spied_retriever
    reranker = FakeReranker(reverse=True)
    results = retriever.search("订单 口径 示例", top_n=3, reranker=reranker, rerank_pool=4)

    assert len(reranker.calls) == 1
    call = reranker.calls[0]
    assert call["top_n"] == 3
    assert len(call["documents"]) == 4  # 池=4（2×top_n 起点，设计 §4.2）
    assert len(results) == 3
    # reverse 重排：池内第 4 片升到第 1，且重排分（≈1.0）替换了 RRF 分（<0.033）
    pool_ids = [c.id for c in retriever.search("订单 口径 示例", top_n=4)]
    assert [c.id for c in results] == [pool_ids[3], pool_ids[2], pool_ids[1]]
    assert [c.rrf_score for c in results] == [0.97, 0.98, 0.99]


def test_search_rerank_hook_rescues_low_ranked_chunk(tmp_path: Path):
    """rerank 的核心假设（发现 3）：RRF 排位低但语义相关的片被重排上位——
    池必须大于注入数（pool=top_n 时只能换序救不回第 9-16 名）。"""
    # 10 片同构语料：查询与全部共享"口径"，BM25 并列 → 稳定序即语料序
    corpus = [
        (f"glossary:g{i:02d}", {"doc_type": "glossary", "source_id": f"g{i}", "title": f"口径{i}", "table_name": ""},
         f"口径说明第{i}号：某指标的分子与分母定义")
        for i in range(10)
    ]
    client = chromadb.PersistentClient(path=str(tmp_path / "chroma_rescue"))
    collection = client.get_or_create_collection(name="rescue_corpus")
    embedder = FakeEmbedder()
    collection.upsert(
        ids=[d for d, _, _ in corpus],
        embeddings=embedder.embed([t for _, _, t in corpus]),
        documents=[t for _, _, t in corpus],
        metadatas=[m for _, m, _ in corpus],
    )
    retriever = HybridRetriever(collection, embedder)

    plain_top2 = [c.id for c in retriever.search("口径说明 某指标", top_n=2)]
    reranker = FakeReranker(reverse=True)
    reranked_top2 = [c.id for c in retriever.search("口径说明 某指标", top_n=2, reranker=reranker, rerank_pool=4)]

    assert len(reranker.calls[0]["documents"]) == 4
    assert set(plain_top2) != set(reranked_top2)  # 换序 + 救回池尾片
    assert reranked_top2[0] not in plain_top2  # 池内第 4 名（普通 top-2 之外）被救到第 1


def test_search_rerank_pool_larger_than_corpus(spied_retriever):
    """pool=16 > 语料 6 片：池=全语料，reranker 收到 6 片（rrf_fuse 自然截断）。"""
    retriever, _ = spied_retriever
    reranker = FakeReranker()
    retriever.search("订单", top_n=8, reranker=reranker, rerank_pool=16)
    assert len(reranker.calls[0]["documents"]) == 6


def test_search_rerank_failure_propagates_not_silent(spied_retriever):
    """fail-closed（设计 §4.1）：rerank 挂了向上抛，不静默回退原序——
    评测 runner 题级 try/except 落 error 记录，--rerun 补跑；静默降级会让
    消融 F 行部分题实际没 rerank，变量失控。"""
    retriever, _ = spied_retriever
    reranker = FakeReranker(fail=True)
    with pytest.raises(RuntimeError, match="mock rerank down"):
        retriever.search("订单", reranker=reranker, rerank_pool=4)


def test_search_reranker_none_with_pool_is_plain(spied_retriever):
    """reranker=None 时 rerank_pool 无效（钩子需要两者同时在场）。"""
    retriever, _ = spied_retriever
    with_pool = retriever.search("订单 口径", rerank_pool=16)
    plain = retriever.search("订单 口径")
    assert [(c.id, c.rrf_score) for c in with_pool] == [(c.id, c.rrf_score) for c in plain]

"""doc_type 覆盖配额（W7 设计 §1.3）的默认套件测试。

四个层次：
- ① 纯函数单测（裸机）：quota.py 用 duck-type 假片驱动——不 import
  RetrievedChunk/hybrid_retriever（后者拖 jieba），证明"纯函数零依赖可裸机测"；
- ② 50 题期望分布冲突检查：直接读 eval_v1.jsonl 统计 expected_chunks 的类型
  组合，断言设计 §1.3 的数学验证（floor 总和 2 ≤ top_n 8、schema 槽位下界
  6 ≥ 单题最大 schema 期望 5）——数据集/语料漂移导致分布变化时本测试红，
  提示配额剂量（floor=1）需重新标定；
- ③ search 集成测试（mock reranker + 临时 Chroma + 假 embedder）：
  quota=True 时注入构成变化（被挤出的类型被救回）、quota=False/缺省逐字节等价、
  配额不干预"重排已覆盖全部类型"的情形；
- ④ 参数配错早失败（quota 无重排钩子抛 ValueError）与 CLI 互斥校验。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import chromadb
import jieba
import pytest

from agent.rag.hybrid_retriever import HybridRetriever
from agent.rag.quota import apply_coverage_quota, coverage_types
from eval import run_eval

REPO_ROOT = Path(__file__).resolve().parents[1]
DATASET = REPO_ROOT / "eval" / "dataset" / "eval_v1.jsonl"

# 设计 §1.3 主案常量：top_n=8、每个覆盖类型 floor=1（glossary/qa 单题期望各 ≤1）
TOP_N = 8
FLOOR_TOTAL = 2  # glossary 1 + qa_pair 1（schema 无需保底剂量，设计 §1.4）


# ---------------------------------------------------------------------------
# ① 纯函数单测（裸机假片，零 chroma/jieba 依赖）
# ---------------------------------------------------------------------------


@dataclass
class QuotaChunk:
    """quota.py 的 duck-type 假片：只暴露 apply_coverage_quota 消费的三个属性。

    刻意不 import RetrievedChunk（它在 hybrid_retriever 里，import 即拖 jieba）——
    本假片证明纯函数对片对象只有 .id / .doc_type / .rrf_score 三个要求。
    rrf_score 在配额语境下存 rerank 分（与线上 search 的写入位置一致，设计 §1.3）。
    """

    id: str
    doc_type: str
    rrf_score: float


def _pool(rrf_order_types: list[tuple[str, str]], scores: dict[str, float]) -> tuple[list[QuotaChunk], list[str]]:
    """按 RRF 序构造 (池片, rrf_order)；rerank 全序按 (-分, 池内 index) 排出。"""
    rrf_order = [chunk_id for chunk_id, _ in rrf_order_types]
    chunks = [QuotaChunk(chunk_id, dtype, scores[chunk_id]) for chunk_id, dtype in rrf_order_types]
    reranked = sorted(chunks, key=lambda c: (-c.rrf_score, rrf_order.index(c.id)))
    return reranked, rrf_order


def test_coverage_types_from_rrf_top_ids():
    """覆盖集 = RRF top-8 内出现过的 doc_type（基线行为锚）。"""
    metas = {
        "schema:orders": {"doc_type": "schema"},
        "glossary:gmv": {"doc_type": "glossary"},
        "qa_pair:qa_003": {"doc_type": "qa_pair"},
    }
    coverage = coverage_types(["schema:orders", "glossary:gmv", "qa_pair:qa_003"], metas)
    assert coverage == {"schema", "glossary", "qa_pair"}

    # 重复类型去重；id 不在 metas / doc_type 为空的片不计入（空类型无法保底）
    partial = coverage_types(["schema:orders", "schema:orders", "ghost:id", "meta:empty"], {
        **metas,
        "meta:empty": {"doc_type": ""},
    })
    assert partial == {"schema"}


def test_coverage_types_empty_inputs():
    assert coverage_types([], {"schema:orders": {"doc_type": "schema"}}) == set()
    assert coverage_types(["schema:orders"], {}) == set()


def test_quota_reserve_fill_and_global_order():
    """设计 §1.3 两段式主场景：预留段保底被挤出的类型，填充段按 rerank 全序补满，
    输出按 rerank 分全局降序——重排分第 3 名的 schema 片被第 5 名的 glossary 片
    换下（类型覆盖 > 纯分数序，这正是配额的干预语义）。"""
    reranked, rrf_order = _pool(
        [("schema:s1", "schema"), ("schema:s2", "schema"), ("schema:s3", "schema"),
         ("qa_pair:q1", "qa_pair"), ("glossary:g1", "glossary"), ("glossary:g2", "glossary")],
        {"schema:s1": 0.90, "schema:s2": 0.80, "schema:s3": 0.70,
         "qa_pair:q1": 0.61, "glossary:g1": 0.55, "glossary:g2": 0.50},
    )
    out = apply_coverage_quota(reranked, rrf_order, {"schema", "glossary", "qa_pair"}, top_n=4)

    # 预留：glossary→g1（该类最高分）、qa_pair→q1、schema→s1；填充 1 槽→s2；
    # 输出全局降序：q1(.61)/g1(.55) 入选而 s3(.70) 落选
    assert [(c.id, c.rrf_score) for c in out] == [
        ("schema:s1", 0.90), ("schema:s2", 0.80), ("qa_pair:q1", 0.61), ("glossary:g1", 0.55),
    ]
    assert "schema:s3" not in {c.id for c in out}


def test_quota_reserve_picks_best_of_type():
    """预留段取 c 类 rerank 分最高的 1 片，不看出现在 rerank 序的第几位。"""
    reranked, rrf_order = _pool(
        [("glossary:g1", "glossary"), ("schema:s1", "schema"), ("glossary:g2", "glossary")],
        {"glossary:g1": 0.50, "schema:s1": 0.90, "glossary:g2": 0.95},
    )
    out = apply_coverage_quota(reranked, rrf_order, {"glossary", "schema"}, top_n=2)
    assert [(c.id, c.rrf_score) for c in out] == [("glossary:g2", 0.95), ("schema:s1", 0.90)]


def test_quota_tie_breaks_by_pool_index():
    """同 rerank 分按池内 index 升序决胜（与 rrf_fuse/rerank.py 哲学一致）：
    预留段在同类同分的两片里取池内靠前的一片。"""
    reranked, rrf_order = _pool(
        [("schema:s1", "schema"), ("glossary:g1", "glossary"), ("glossary:g2", "glossary")],
        {"schema:s1": 0.90, "glossary:g1": 0.50, "glossary:g2": 0.50},
    )
    out = apply_coverage_quota(reranked, rrf_order, {"glossary", "schema"}, top_n=2)
    assert [(c.id, c.rrf_score) for c in out] == [("schema:s1", 0.90), ("glossary:g1", 0.50)]
    assert "glossary:g2" not in {c.id for c in out}  # 同分同型，池内 index 大者落选


def test_quota_logs_and_skips_type_missing_from_pool(caplog: pytest.LogCaptureFixture):
    """池内无 c 类片：跳过并记日志（配额救不了池外的片，设计 §1.3 诚实边界），
    其余覆盖类型照常保底。"""
    reranked, rrf_order = _pool(
        [("schema:s1", "schema"), ("glossary:g1", "glossary")],
        {"schema:s1": 0.90, "glossary:g1": 0.50},
    )
    with caplog.at_level(logging.WARNING, logger="agent.rag.quota"):
        out = apply_coverage_quota(reranked, rrf_order, {"schema", "glossary", "qa_pair"}, top_n=2)
    assert [(c.id, c.rrf_score) for c in out] == [("schema:s1", 0.90), ("glossary:g1", 0.50)]
    assert "qa_pair" in caplog.text  # 日志点名缺失的类型


def test_quota_coverage_larger_than_top_n():
    """覆盖类型数 > top_n 的退化边界：输出截到 top_n，全局降序后分数最低的
    预留片被丢弃（确定性，不崩不溢）。"""
    reranked, rrf_order = _pool(
        [("schema:s1", "schema"), ("glossary:g1", "glossary"), ("qa_pair:q1", "qa_pair")],
        {"schema:s1": 0.90, "glossary:g1": 0.60, "qa_pair:q1": 0.30},
    )
    out = apply_coverage_quota(reranked, rrf_order, {"schema", "glossary", "qa_pair"}, top_n=2)
    assert [(c.id, c.rrf_score) for c in out] == [("schema:s1", 0.90), ("glossary:g1", 0.60)]


@pytest.mark.parametrize("reranked_n, top_n", [(0, 8), (3, 0)])
def test_quota_empty_pool_or_zero_top_n_returns_empty(reranked_n: int, top_n: int):
    """空池 / top_n=0（对照行）→ 空输出，不抛不崩。"""
    rrf_order_types = [("schema:s1", "schema"), ("glossary:g1", "glossary"), ("qa_pair:q1", "qa_pair")]
    reranked, rrf_order = _pool(rrf_order_types, {"schema:s1": 0.9, "glossary:g1": 0.5, "qa_pair:q1": 0.3})
    assert apply_coverage_quota(reranked[:reranked_n], rrf_order[:reranked_n], {"schema"}, top_n) == []


def test_quota_deterministic_on_repeat():
    """同样输入必然同样输出（确定性：RRF 序 + rerank 序 + index，无随机无阈值）。"""
    reranked, rrf_order = _pool(
        [("schema:s1", "schema"), ("glossary:g1", "glossary"), ("qa_pair:q1", "qa_pair"),
         ("schema:s2", "schema"), ("glossary:g2", "glossary")],
        {"schema:s1": 0.90, "glossary:g1": 0.55, "qa_pair:q1": 0.61, "schema:s2": 0.80, "glossary:g2": 0.50},
    )
    first = apply_coverage_quota(reranked, rrf_order, {"schema", "glossary", "qa_pair"}, TOP_N)
    for _ in range(3):
        assert apply_coverage_quota(reranked, rrf_order, {"schema", "glossary", "qa_pair"}, TOP_N) == first


def test_quota_chunk_outside_rrf_order_raises():
    """契约：reranked 里的片必须在 rrf_order（池成员资格由它定义）——
    组装错误直接 KeyError 大声失败，不猜不兜底。"""
    inside = QuotaChunk("schema:s1", "schema", 0.9)
    outside = QuotaChunk("schema:ghost", "schema", 0.8)
    with pytest.raises(KeyError):
        apply_coverage_quota([inside, outside], ["schema:s1"], {"schema"}, top_n=2)


# ---------------------------------------------------------------------------
# ② 50 题期望分布冲突检查（设计 §1.3 配额可行性的数学验证）
# ---------------------------------------------------------------------------


def test_eval50_expected_distribution_quota_feasibility():
    """从 eval_v1.jsonl 读 expected_chunks 统计类型组合，断言：
    - 组合分布与设计 §1.4 需求侧实测一致（schema-only 38 / glossary+schema 11 /
      glossary+qa 1）——分布漂移说明数据集/语料变了，配额标定需重做；
    - glossary/qa 单题期望各 ≤1（floor=1 精确匹配需求的前提）；
    - floor 总和 2 ≤ top_n 8；schema 槽位下界 8−2=6 ≥ 单题最大 schema 期望 5。
    """
    combos: Counter[str] = Counter()
    max_per_type = {"schema": 0, "glossary": 0, "qa_pair": 0}
    n_cases = 0
    for line in DATASET.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        case = json.loads(line)
        n_cases += 1
        per_type = Counter(chunk.split(":", 1)[0] for chunk in case["expected_chunks"])
        combos["+".join(sorted(per_type))] += 1
        for doc_type, current_max in max_per_type.items():
            max_per_type[doc_type] = max(current_max, per_type.get(doc_type, 0))

    assert n_cases == 50
    assert combos == {"schema": 38, "glossary+schema": 11, "glossary+qa_pair": 1}
    assert max_per_type["glossary"] <= 1
    assert max_per_type["qa_pair"] <= 1
    assert FLOOR_TOTAL <= TOP_N  # 冲突检查①：floor 总和不超过注入预算
    assert max_per_type["schema"] == 5
    assert max_per_type["schema"] <= TOP_N - FLOOR_TOTAL  # 冲突检查②：6 ≥ 5，恰余一槽


# ---------------------------------------------------------------------------
# ③ search 集成测试（mock reranker + 临时 Chroma + 假 embedder）
# ---------------------------------------------------------------------------


class QuotaFakeEmbedder:
    """确定性假 embedder（与 test_rag_retriever 同款分词 hash 方案）。"""

    DIM = 512

    def _vector(self, text: str) -> list[float]:
        vec = [0.0] * self.DIM
        for token in jieba.lcut(text):
            digest = hashlib.md5(token.encode("utf-8")).digest()
            vec[int.from_bytes(digest[:4], "big") % self.DIM] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    def embed(self, docs: list[str]) -> list[list[float]]:
        return [self._vector(d) for d in docs]

    def embed_query(self, q: str) -> list[float]:
        return self._vector(q)


# 迷你语料（12 片）：glossary/qa_pair 各 2 片只含查询词（两路检索都排最前），
# schema 8 片为长文本——RRF top-8 必然同时含三类（每类至少 1 片：glossary/qa
# 各有 1 片的 RRF 得分 1/61+1/61 以上，高于任何 schema 片的最优 1/61+1/65）。
QUERY = "口径"
_G_TEXTS = {"glossary:g1": "口径 定义一", "glossary:g2": "口径 定义二"}
_Q_TEXTS = {"qa_pair:q1": "口径 问法一", "qa_pair:q2": "口径 问法二"}
_S_TEXT = "口径 表结构说明：订单与支付字段的完整定义与备注说明"
QUOTA_CORPUS: list[tuple[str, dict, str]] = [
    (chunk_id, {"doc_type": "glossary", "source_id": chunk_id, "title": f"口径{chunk_id}", "table_name": ""}, text)
    for chunk_id, text in _G_TEXTS.items()
] + [
    (chunk_id, {"doc_type": "qa_pair", "source_id": chunk_id, "title": f"问法{chunk_id}", "table_name": ""}, text)
    for chunk_id, text in _Q_TEXTS.items()
] + [
    (f"schema:s{i}", {"doc_type": "schema", "source_id": f"s{i}", "title": f"表{i}", "table_name": f"t{i}"}, _S_TEXT)
    for i in range(1, 9)
]


@pytest.fixture()
def quota_retriever(tmp_path: Path) -> HybridRetriever:
    client = chromadb.PersistentClient(path=str(tmp_path / "chroma_quota"))
    collection = client.get_or_create_collection(name="quota_corpus")
    embedder = QuotaFakeEmbedder()
    collection.upsert(
        ids=[doc_id for doc_id, _, _ in QUOTA_CORPUS],
        embeddings=embedder.embed([text for _, _, text in QUOTA_CORPUS]),
        documents=[text for _, _, text in QUOTA_CORPUS],
        metadatas=[meta for _, meta, _ in QUOTA_CORPUS],
    )
    return HybridRetriever(collection, embedder)


class TypeDemoteReranker:
    """按文本打分的 mock 重排器：schema 片 0.90、glossary/qa 片递减低分——
    复现 W6 发现 3 的"类型挤出"：重排后 glossary/qa_pair 全部跌出 top_n。"""

    def __init__(self, score_by_text: dict[str, float]) -> None:
        self.score_by_text = score_by_text
        self.calls: list[dict] = []

    def rerank(self, query: str, documents: list[str], top_n: int | None = None) -> list[tuple[int, float]]:
        self.calls.append({"query": query, "documents": list(documents), "top_n": top_n})
        pairs = [(i, self.score_by_text[text]) for i, text in enumerate(documents)]
        pairs.sort(key=lambda pair: (-pair[1], pair[0]))  # 与 SiliconFlowReranker 同款决胜
        return pairs[:top_n] if top_n is not None else pairs


class PoolOrderReranker:
    """恒等重排：按候选池传入顺序给递减分（保序）——用于"配额不干预已覆盖"断言。"""

    def rerank(self, query: str, documents: list[str], top_n: int | None = None) -> list[tuple[int, float]]:
        pairs = [(i, 1.0 - i * 0.01) for i in range(len(documents))]
        return pairs[:top_n] if top_n is not None else pairs


def _pool_ids(retriever: HybridRetriever) -> list[str]:
    """RRF 池全序（无 rerank 的 plain search，池=语料 12 片全量）。"""
    return [chunk.id for chunk in retriever.search(QUERY, top_n=len(QUOTA_CORPUS))]


def test_search_quota_restores_type_coverage(quota_retriever: HybridRetriever):
    """主场景：mock rerank 把 glossary/qa 全部挤出 top_n（quota=False 构成塌成
    纯 schema）；quota=True 时覆盖集（RRF top-8 锚）里的两类各被保底 1 片救回，
    填充按 rerank 全序补满，输出仍按 rerank 分降序。"""
    pool_order = _pool_ids(quota_retriever)
    # 前置断言：本语料下 RRF top-8 三类齐备（假 embedder 行为漂移时在此红，指明是语料前提坏了）
    types_in_top8 = {chunk_id.split(":", 1)[0] for chunk_id in pool_order[:TOP_N]}
    assert types_in_top8 == {"schema", "glossary", "qa_pair"}

    scores = {text: 0.90 for text in {_S_TEXT}}
    scores.update({"口径 定义一": 0.10, "口径 定义二": 0.09, "口径 问法一": 0.05, "口径 问法二": 0.04})
    demoter = TypeDemoteReranker(scores)

    without_quota = quota_retriever.search(QUERY, top_n=TOP_N, reranker=demoter, rerank_pool=12)
    # quota=False：被挤出——注入构成塌成纯 schema（W6 负结果的迷你复现）
    assert {chunk.doc_type for chunk in without_quota} == {"schema"}

    with_quota = quota_retriever.search(QUERY, top_n=TOP_N, reranker=demoter, rerank_pool=12, quota=True)
    # 配额路径对整池取 rerank 全序（top_n=池长 12，而不是注入数 8——预留段要能救回池内片）
    assert demoter.calls[-1]["top_n"] == 12
    assert len(demoter.calls[-1]["documents"]) == 12

    counter = Counter(chunk.doc_type for chunk in with_quota)
    assert counter == {"schema": 6, "glossary": 1, "qa_pair": 1}  # 预留 2 + 填充 6（schema 保底 1 也在内）
    # 救回的是各类 rerank 分最高者：glossary:g1(0.10)、qa_pair:q1(0.05)
    by_id = {chunk.id: chunk for chunk in with_quota}
    assert by_id["glossary:g1"].rrf_score == 0.10
    assert by_id["qa_pair:q1"].rrf_score == 0.05
    # 输出按 rerank 分全局降序；schema 部分按池内 index 升序（同分决胜）
    scores_out = [chunk.rrf_score for chunk in with_quota]
    assert scores_out == sorted(scores_out, reverse=True)
    schema_part = [chunk.id for chunk in with_quota if chunk.doc_type == "schema"]
    assert schema_part == [chunk_id for chunk_id in pool_order if chunk_id.startswith("schema:")][:6]


def test_search_quota_noop_when_rerank_already_covers(quota_retriever: HybridRetriever):
    """恒等重排（rerank top_n = RRF top-8，覆盖集类型全在场）时配额是空操作：
    输出与非配额 rerank 逐字节相等——"最小干预"的正面断言。"""
    identity = PoolOrderReranker()
    plain = quota_retriever.search(QUERY, top_n=TOP_N, reranker=identity, rerank_pool=12)
    with_quota = quota_retriever.search(QUERY, top_n=TOP_N, reranker=identity, rerank_pool=12, quota=True)
    assert [(c.id, c.rrf_score) for c in with_quota] == [(c.id, c.rrf_score) for c in plain]


def test_search_quota_false_is_byte_equivalent_to_default(quota_retriever: HybridRetriever):
    """同构红线：quota 缺省（False 默认路径）与显式 quota=False 逐字节等价，
    且与"完全不认识 quota 参数"的 W6 现行行为等价（全默认 search）。"""
    demoter_default = TypeDemoteReranker({_S_TEXT: 0.90, "口径 定义一": 0.10, "口径 定义二": 0.09,
                                          "口径 问法一": 0.05, "口径 问法二": 0.04})
    demoter_explicit = TypeDemoteReranker({_S_TEXT: 0.90, "口径 定义一": 0.10, "口径 定义二": 0.09,
                                           "口径 问法一": 0.05, "口径 问法二": 0.04})
    plain = quota_retriever.search(QUERY, top_n=TOP_N, reranker=demoter_default, rerank_pool=12)
    explicit = quota_retriever.search(QUERY, top_n=TOP_N, reranker=demoter_explicit, rerank_pool=12, quota=False)
    assert [(c.id, c.rrf_score) for c in explicit] == [(c.id, c.rrf_score) for c in plain]


# ---------------------------------------------------------------------------
# ④ 参数配错早失败 + CLI 互斥校验
# ---------------------------------------------------------------------------


def test_search_quota_without_reranker_raises(quota_retriever: HybridRetriever):
    """quota=True 而 reranker=None → ValueError（配额无重排可调，参数配错早失败）。"""
    with pytest.raises(ValueError, match="quota"):
        quota_retriever.search(QUERY, quota=True)


def test_search_quota_with_zero_pool_raises(quota_retriever: HybridRetriever):
    """quota=True 而 rerank_pool=0（重排钩子关）同样拒绝：静默忽略会让实验行
    退化成无配额行而无人察觉——与本仓库"配置期大声失败"哲学一致。"""
    with pytest.raises(ValueError, match="rerank_pool"):
        quota_retriever.search(QUERY, quota=True, reranker=TypeDemoteReranker({}), rerank_pool=0)


def test_cli_quota_and_no_fewshot_mutually_exclusive(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    """--quota --no-fewshot 同给：argparse 启动期报错退出（码 2），文案指向语义矛盾。"""
    monkeypatch.setattr(sys, "argv", ["eval.run_eval", "--tag", "x", "--quota", "--no-fewshot"])
    with pytest.raises(SystemExit) as excinfo:
        run_eval._parse_args()
    assert excinfo.value.code == 2
    stderr = capsys.readouterr().err
    assert "--quota" in stderr
    assert "语义矛盾" in stderr  # few-shot 被过滤还保 qa 覆盖 = 语义矛盾


def test_cli_quota_flag_parses_and_off_by_default(monkeypatch: pytest.MonkeyPatch):
    """--quota 可解析且默认关闭；--quota 单独给出即计入消融开关（opts 非 None）。"""
    monkeypatch.setattr(sys, "argv", ["eval.run_eval", "--tag", "x", "--quota"])
    args = run_eval._parse_args()
    assert args.quota is True
    assert run_eval._ablation_flags_present(args) is True

    monkeypatch.setattr(sys, "argv", ["eval.run_eval", "--tag", "x"])
    args = run_eval._parse_args()
    assert args.quota is False
    assert run_eval._ablation_flags_present(args) is False
    assert run_eval._build_retrieval_opts(args) is None  # 无开关 → None（同构红线）


def test_cli_quota_with_rerank_maps_to_opts(monkeypatch: pytest.MonkeyPatch):
    """--rerank --quota → RetrievalOptions（quota=True + reranker + pool=16）。"""
    from agent.rag import rerank as rerank_mod

    monkeypatch.setattr(rerank_mod, "SILICONFLOW_API_KEY", "sk-ut-key")
    monkeypatch.setattr(sys, "argv", ["eval.run_eval", "--tag", "x", "--rerank", "--quota"])
    opts = run_eval._build_retrieval_opts(run_eval._parse_args())
    assert opts is not None
    assert opts.quota is True
    assert opts.reranker is not None
    assert opts.rerank_pool == 16
    opts.reranker.close()


def test_cli_detail_config_records_quota(monkeypatch: pytest.MonkeyPatch):
    """detail config 如实记录 quota 状态（W7 §1.5：实际参数落盘）。"""
    monkeypatch.setattr(sys, "argv", ["eval.run_eval", "--tag", "x", "--rerank", "--quota"])
    args = run_eval._parse_args()
    cfg = run_eval.effective_retrieval_config(args)
    assert cfg["quota"] is True
    assert run_eval._describe_retrieval(cfg).count("quota=开") == 1

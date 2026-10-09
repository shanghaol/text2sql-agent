"""混合检索器：BM25（关键词）+ Chroma（语义）+ RRF 融合。

实现基线：《Chroma实战与自实现混合检索》§4.2 代码骨架，
按 W2 设计 §3.4 细化为三处明确语义：
1. search 返回结构化 RetrievedChunk（W3 评测直接消费 id/doc_type）；
2. doc_types=None 时不过滤、全类型参与检索（Agent 主链路就是全类型）；
3. RRF 平局按 (-score, id) 排序保证可复现；BM25 按 doc_type 分组建索引、
   每个类型独立排名参与融合。

本模块被导入时自动执行一次 setup_jieba()（幂等），保证 BM25 分词质量。
"""

from dataclasses import dataclass

import jieba
from rank_bm25 import BM25Okapi

from agent.rag.jieba_dict import setup_jieba

# 导入即执行一次（setup_jieba 幂等，构造函数里再调一次是零成本的双保险）
setup_jieba()


@dataclass
class RetrievedChunk:
    """检索结果：W3 评测的 Recall@K 直接消费 id / doc_type 字段。"""

    id: str
    doc_type: str
    title: str
    text: str
    rrf_score: float


def rrf_fuse(
    ranked_ids: list[list[str]], k: int, top_n: int
) -> list[tuple[str, float]]:
    """Reciprocal Rank Fusion：把多路排名融合成一个可复现的排序。

    - 每路里第 rank 名贡献 1/(k+rank)，同一片在多路命中时得分叠加
      （这正是设计意图：两路都认可的片排前面）；
    - 只用排名不用原始分数——余弦相似度在 [-1,1]、BM25 分数无上界，量纲不可比；
    - 平局按 (-score, id) 升序，保证同分时顺序稳定可复现。

    独立成模块级纯函数：可脱离 Chroma/jieba 单测（面试"手撕 RRF"的展示位）。
    """
    scores: dict[str, float] = {}
    for ranked in ranked_ids:
        for rank, doc_id in enumerate(ranked, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    return ordered[:top_n]


class HybridRetriever:
    """BM25 + 向量双路检索，RRF 融合。语料更新后 rebuild() 一次。"""

    def __init__(self, collection, embedder, k: int = 60) -> None:
        """collection: chromadb Collection；embedder: 提供 embed_query 的可注入依赖
        （生产用 BgeEmbedder，测试注入确定性假实现）。"""
        self.collection = collection
        self.embedder = embedder
        self.k = k
        setup_jieba()
        self.rebuild()

    def rebuild(self) -> None:
        """全量拉语料重建 BM25 分组索引。

        rank-bm25 的语料更新只能重建对象（库的边界），28 片毫秒级完成。
        空 collection 时本方法正常返回（bm25s 为空字典），search 返回空列表
        ——支持"首次使用先建索引"的渐进体验。
        """
        data = self.collection.get(include=["documents", "metadatas"])
        self.ids: list[str] = list(data["ids"] or [])
        self.docs: list[str] = list(data["documents"] or [])
        self.metas: list[dict] = list(data["metadatas"] or [])

        # 按 doc_type 分组建索引：检索时只在对应组内搜（等价于向量路的 where 过滤）
        indices_by_type: dict[str, list[int]] = {}
        for i, meta in enumerate(self.metas):
            indices_by_type.setdefault(meta.get("doc_type", ""), []).append(i)

        self.bm25s: dict[str, tuple[list[int], BM25Okapi]] = {}
        for dtype, indices in indices_by_type.items():
            tokenized = [list(jieba.lcut(self.docs[i])) for i in indices]
            self.bm25s[dtype] = (indices, BM25Okapi(tokenized))

    def search(
        self,
        query: str,
        doc_types: list[str] | None = None,
        top_k_each: int = 20,
        top_n: int = 8,
    ) -> list[RetrievedChunk]:
        """混合检索一个查询。

        doc_types=None 不过滤（全类型）；指定时 BM25 路只查对应分组、
        向量路加 where 过滤。top_k_each 是每路每类型取的条数，
        top_n 是融合后最终返回条数。
        """
        wanted = set(doc_types) if doc_types is not None else None

        # —— BM25 路：每个类型组独立排名，各取前 top_k_each ——
        bm25_ranked: list[list[str]] = []
        for dtype in self.bm25s:
            if wanted is not None and dtype not in wanted:
                continue
            indices, bm25 = self.bm25s[dtype]
            scores = bm25.get_scores(list(jieba.lcut(query)))
            order = sorted(range(len(indices)), key=lambda j: scores[j], reverse=True)
            bm25_ranked.append([self.ids[indices[j]] for j in order[:top_k_each]])

        # —— 向量路：Chroma query + metadata 过滤 ——
        # n_results 用 min(top_k_each, 过滤后条数) 兜底：超过剩余条数会抛
        # "contiguous 2D array" 错（坑清单 #6）。
        if wanted is None:
            filtered_count = len(self.ids)
            where = None
        else:
            filtered_count = sum(1 for m in self.metas if m.get("doc_type") in wanted)
            where = {"doc_type": {"$in": list(doc_types)}}

        dense_ranked: list[list[str]] = []
        if filtered_count > 0:
            query_embedding = self.embedder.embed_query(query)
            query_kwargs: dict = {
                "query_embeddings": [query_embedding],
                "n_results": min(top_k_each, filtered_count),
            }
            if where is not None:
                query_kwargs["where"] = where
            result = self.collection.query(**query_kwargs)
            dense_ranked = [list(r) for r in result["ids"]]

        # —— RRF 融合，取前 top_n ——
        fused = rrf_fuse([*bm25_ranked, *dense_ranked], self.k, top_n)

        id_to_index = {doc_id: i for i, doc_id in enumerate(self.ids)}
        chunks: list[RetrievedChunk] = []
        for doc_id, score in fused:
            i = id_to_index[doc_id]
            meta = self.metas[i]
            chunks.append(
                RetrievedChunk(
                    id=doc_id,
                    doc_type=meta.get("doc_type", ""),
                    title=meta.get("title", ""),
                    text=self.docs[i],
                    rrf_score=score,
                )
            )
        return chunks

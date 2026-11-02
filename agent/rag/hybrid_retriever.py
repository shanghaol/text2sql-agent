"""混合检索器：BM25（关键词）+ Chroma（语义）+ RRF 融合。

实现基线：《Chroma实战与自实现混合检索》§4.2 代码骨架，
按 W2 设计 §3.4 细化为三处明确语义：
1. search 返回结构化 RetrievedChunk（W3 评测直接消费 id/doc_type）；
2. doc_types=None 时不过滤、全类型参与检索（Agent 主链路就是全类型）；
3. RRF 平局按 (-score, id) 排序保证可复现；BM25 按 doc_type 分组建索引、
   每个类型独立排名参与融合。

W6 改造（设计 §3.4 消融入口 + §4.2 rerank 钩子，全部默认关闭）：
- search() 新参 enable_bm25 / enable_dense（路开关）、reranker / rerank_pool
  （重排钩子）——**默认值路径与现行逐字节等价**（同构红线，W4 §6.2 验收覆盖）；
- rerank 钩子：融合候选池取 rerank_pool（=2×top_n，池=8 时 Recall 无提升空间），
  对池内片全文重排后取前 top_n；重排分写入 rrf_score 字段位置（明细可区分）。

W7 改造（设计 §1.3/§1.5 doc_type 覆盖配额，默认关闭）：
- search() 新参 quota：对 rerank 结果追加"类型覆盖保底"——配额是"重排允许改变
  顺序、不允许降低类型覆盖"的最小干预语义（相对基线 RRF top-8 而言）：覆盖集
  取 RRF top-8 出现过的 doc_type（基线行为锚），预留段给每个覆盖类型强制保底
  rerank 分最高的 1 片（救回被重排挤出 top_n 的片），填充段按 rerank 全序补满，
  输出仍按 rerank 分全局降序。规则实现在 agent/rag/quota.py（纯函数），
  与离线标定工具共用同一份代码；
- quota=True 但 rerank 钩子不在场（reranker=None 或 rerank_pool<=0）时抛
  ValueError——配额是对重排结果的修正，无重排可调说明参数配错，早失败
  （与"双路全关"同款哲学）。

本模块被导入时自动执行一次 setup_jieba()（幂等），保证 BM25 分词质量。
"""

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

import jieba
from rank_bm25 import BM25Okapi

from agent.rag.jieba_dict import setup_jieba
from agent.rag.quota import apply_cap_quota, apply_coverage_quota, coverage_types

if TYPE_CHECKING:  # 仅为类型标注，不引入运行时依赖
    from agent.rag.rerank import Reranker

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
        enable_bm25: bool = True,
        enable_dense: bool = True,
        reranker: "Reranker | None" = None,
        rerank_pool: int = 0,
        quota: bool = False,
        quota_mode: str = "coverage",
        rerank_scope: str = "all",
    ) -> list[RetrievedChunk]:
        """混合检索一个查询。

        doc_types=None 不过滤（全类型）；指定时 BM25 路只查对应分组、
        向量路加 where 过滤。top_k_each 是每路每类型取的条数，
        top_n 是融合后最终返回条数。

        W6 消融入口（设计 §3.4，全部默认关闭——默认参数路径与现行逐字节等价）：
        - enable_bm25 / enable_dense：路开关（消融 B/C 行：纯向量 / 纯 BM25）；
        - reranker + rerank_pool：RRF 融合后取前 rerank_pool 片为候选池，交
          reranker.rerank(query, 片全文列表, top_n) 重排，再取前 top_n 注入
          （设计 §4.2：池必须大于注入数，pool=top_n 时重排只能换序救不回排位低的片）。
          rerank 失败**不降级**（fail-closed，设计 §4.1）：异常向上抛，由评测
          runner 题级 try/except 落 error 记录——静默回退原序会让消融变量失控。
        - quota（W7 设计 §1.3/§1.2，需 rerank 钩子在场）：doc_type 配额——
          **重排允许改变顺序、不允许降低类型覆盖**的最小干预语义。覆盖集锚
          RRF top-8（基线无 rerank 时会注入的类型，不新增基线没有的注入策略），
          预留段为每个覆盖类型强制保底池内 rerank 分最高的 1 片（可救回被重排
          挤出 top_n 的片——配额的修复对象），填充段按 rerank 全序补满剩余
          槽位，输出仍按 rerank 分全局降序（选片规则在 agent/rag/quota.py 纯函数，
          确定性：RRF 序 + rerank 序 + 池内 index 决胜）。quota=True 而重排钩子
          不在场（reranker=None 或 rerank_pool<=0）时抛 ValueError——配额无重排
          可调说明参数配错，早失败（与"双路全关"同款哲学）。
        """
        if not enable_bm25 and not enable_dense:
            raise ValueError("enable_bm25 与 enable_dense 不能同时为 False（双路全关检索恒空，多半是消融参数配错）")
        if quota and (reranker is None or rerank_pool <= 0):
            raise ValueError(
                "quota=True 需要 rerank 钩子在场（reranker 且 rerank_pool>0）："
                "配额是对重排结果的类型修正，无重排可调说明参数配错（W7 设计 §1.5）"
            )
        if quota and quota_mode not in ("coverage", "cap"):
            raise ValueError(f"quota_mode 非法：{quota_mode!r}（可选 coverage | cap）")
        if rerank_scope not in ("all", "schema_only"):
            raise ValueError(f"rerank_scope 非法：{rerank_scope!r}（可选 all | schema_only）")

        wanted = set(doc_types) if doc_types is not None else None

        # —— BM25 路：每个类型组独立排名，各取前 top_k_each ——
        bm25_ranked: list[list[str]] = []
        if enable_bm25:
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
        dense_ranked: list[list[str]] = []
        if enable_dense:
            if wanted is None:
                filtered_count = len(self.ids)
                where = None
            else:
                filtered_count = sum(1 for m in self.metas if m.get("doc_type") in wanted)
                where = {"doc_type": {"$in": list(doc_types)}}

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

        # —— RRF 融合：默认取前 top_n；开 rerank 时取前 rerank_pool 做候选池 ——
        use_rerank = reranker is not None and rerank_pool > 0
        fused = rrf_fuse([*bm25_ranked, *dense_ranked], self.k, rerank_pool if use_rerank else top_n)

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

        # —— rerank 钩子（设计 §4.2）：池内片全文重排 → 取前 top_n 注入 ——
        # 重排分写入 rrf_score 字段位置（RetrievedChunk 不加新字段，明细可区分：
        # 默认路径的 rrf_score ≤ 2/(k+1) < 0.033，重排分是 cosine 级的 0~1）。
        # quota=True（W7 设计 §1.3）：对整池取 rerank 全序（预留段要能救回被重排
        # 挤出 top_n 的片，这正是配额的修复对象），再交覆盖配额选片。
        if use_rerank and rerank_scope == "schema_only":
            # C 行实验（W7 预注册）：只重排 schema 片——注入选集不变（成员=RRF top-8
            # 基线锚），仅 schema 片内部按 rerank 分优化排序。纯生成侧对照行。
            schema_chunks = [c for c in chunks if c.doc_type == "schema"]
            other_chunks = [c for c in chunks if c.doc_type != "schema"]
            # rerank 返回 [(schema_chunks 下标, score)]——映射回 chunk id
            ranked = reranker.rerank(query, [c.text for c in schema_chunks], top_n=len(schema_chunks))
            rerank_schema = sorted(
                (replace(schema_chunks[idx], rrf_score=float(score)) for idx, score in ranked),
                key=lambda c: -c.rrf_score,
            )
            # 非 schema 片保持 RRF 原序（rrf_score 未动），排在重排后的 schema 之后；
            # 输出截 top_n——与全量重排同款的"重排分全局降序"形态
            chunks = [*rerank_schema, *other_chunks]
            return chunks[:top_n]
        if use_rerank:
            pool_texts = [chunk.text for chunk in chunks]
            ranked = reranker.rerank(query, pool_texts, top_n=len(pool_texts) if quota else top_n)
            reranked: list[RetrievedChunk] = []
            for index, score in ranked:
                if not 0 <= index < len(chunks):
                    raise ValueError(f"reranker 返回的 index 越界（{index}，候选池 {len(chunks)} 片）")
                reranked.append(replace(chunks[index], rrf_score=float(score)))
            if quota:
                if quota_mode == "cap":
                    # cap_only（离线标定胜者）：schema 封顶，非 schema 不设限——
                    # 判别"schema 挤出"与"示例稀释"两通道谁是 W6 负结果主因。
                    # 遍历停在 top_n：被封顶的类型片跳过不占槽，非 schema 片
                    # 因此得以进入注入（这正是配额的修复机制）。
                    chunks = apply_cap_quota(reranked, [doc_id for doc_id, _ in fused], top_n=top_n)
                else:
                    # 覆盖保底（W7 设计 §1.3 原主案，离线预演 J1 不过保留作对照实现）：
                    # 预留段保证覆盖类型各 1 片必在注入内。
                    coverage = coverage_types(
                        [doc_id for doc_id, _ in fused[:top_n]],
                        {doc_id: self.metas[id_to_index[doc_id]] for doc_id, _ in fused},
                    )
                    chunks = apply_coverage_quota(
                        reranked, [doc_id for doc_id, _ in fused], coverage, top_n=top_n
                    )
            else:
                chunks = reranked[:top_n]
        return chunks

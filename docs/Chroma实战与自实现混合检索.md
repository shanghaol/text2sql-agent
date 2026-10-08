# Chroma 实战与自实现混合检索 —— 项目知识补充（第 2 期）

> **定位**：教学材料，配合需求文档 v1.2 §5 技术约束与 W2 里程碑阅读。写于 2026-10-08，全部结论经 Chroma 官方文档/PyPI/GitHub 源码交叉验证（来源见文末）。
> **前置**：先读《向量数据库与Milvus知识补充》第 1-2 章（向量库通用概念，COSINE/HNSW/ANN 那些知识两边通用，这里不再重复）。
> **学完你能回答**：为什么最终选 Chroma？Chroma 怎么持久化、怎么过滤？`where_document` 能当 BM25 用吗？自实现 RRF 的代码和坑？什么规模才值得升级 Milvus？

---

## 0. 为什么项目用 Chroma：一个真实的选型故事

决策链：需求需要混合检索 → 首选调研了 Milvus（它 2.5+ 内置 BM25 混合检索，功能最匹配）→ 发现 Milvus Standalone 官方最低要求 **4核8G 内存**，而我们部署服务器只有 **2核2G** → 强行上会让整个服务 OOM → 回退到**嵌入式** Chroma：`pip install` 进 Python 进程、零额外服务、持久化只是一个本地目录。

**这就是面试里最值钱的那种选型故事**——不是"听说 Milvus 强所以用"，而是：评估过更强方案 → 量化过资源约束（8G vs 2G）→ 做出取舍 → 说得清"什么时候该升级回去"（第 5 节）。面试官问"为什么不用 Milvus"，你就赢了大多数背答案的候选人。

**代价也要诚实**（面试主动说更显工程成熟度）：Chroma 没有内置 BM25，混合检索要自己搭（第 4 章——这反而把"RRF 手撕原题"从能讲升级成写过）；它没有权限/审计体系；它是单机嵌入式，天生不适合多人共享。

---

## 1. Chroma 核心概念（5 分钟版）

- **嵌入式向量库**：不像 MySQL/Milvus Standalone 那样是独立进程，而是嵌在你的 Python 进程里的一个库（类比：SQLite 之于 MySQL）。数据持久化到本地目录（底层是 SQLite + 向量索引文件）。
- **当前版本**：chromadb **1.5.x**（2026-05 的 1.5.9），2025 年用 Rust 重写内核后已稳定一年半；要求 **Python ≥3.9**（本项目钉 3.10+），Windows 11 `pip install chromadb` 即装即用。
- **Client**：`chromadb.PersistentClient(path=...)`——持久化模式，重启不丢数据。⚠️ path 用**绝对路径**；一个进程只建一次（Chroma 按 path 缓存单实例，多实例混用会出诡异冲突）。
- **Collection** ≈ 表；**metadata** 是每条数据的标量字段（值只能是 str/int/float/bool），检索时可作过滤条件——我们的 `doc_type`（`schema` / `glossary` / `qa_pair`）就放这里。
- 底层就是 **HNSW 索引**（第 1 期材料 2.3 节的原理直接适用），支持 cosine / l2 / ip 三种距离。

## 2. 最小可跑代码（W2 对照用，Windows 直接能跑）

```python
# pip install chromadb fastembed numpy   （版本锁：chromadb>=1.5.5,<2，1.5.4 已被官方撤回）
import chromadb
from fastembed import TextEmbedding

# ① 持久化 client + collection，指定 cosine（注意：1.x 用 configuration 嵌套写法，
#    旧的 metadata={"hnsw:space": ...} 冒号写法已弃用，网上旧教程别照抄）
client = chromadb.PersistentClient(path=r"C:\data\text2sql\chroma_db")
collection = client.get_or_create_collection(
    name="rag_corpus",
    configuration={"hnsw": {"space": "cosine", "ef_construction": 200}},
)

# ② 插入：fastembed 自己算向量、显式传入（官方标准用法，别让它触发内置默认模型）
model = TextEmbedding(model_name="BAAI/bge-small-zh-v1.5")   # 512 维
docs  = ["CREATE TABLE orders (...)", "GMV = 支付成功订单金额总和（不含退款）", "怎么查近7天订单量？"]
metas = [{"doc_type": "schema"}, {"doc_type": "glossary"}, {"doc_type": "qa_pair"}]
vecs  = [e.tolist() for e in model.embed(docs)]

collection.upsert(ids=["d1", "d2", "d3"], embeddings=vecs,
                  documents=docs, metadatas=metas)   # 幂等重建一律用 upsert；add 撞 id 直接抛错

# ③ 按类型过滤的语义检索
q_vec = [e.tolist() for e in model.embed(["订单表的字段有哪些"])][0]
res = collection.query(query_embeddings=[q_vec], n_results=5,
                       where={"doc_type": {"$in": ["schema", "glossary"]}})
# 返回列式结构：res["ids"]/["documents"]/["metadatas"]/["distances"]（各是按查询分组的列表）
```

**三个理解点**：
1. cosine 模式下 `distances` 是**余弦距离 = 1 − 余弦相似度**，越小越相似——RRF 融合只用排名不用分数，所以不需要换算。
2. **始终显式传 `embeddings=` / `query_embeddings=`**。不传的话 Chroma 会加载内置默认 Embedding 模型（384 维英文 all-MiniLM-L6-v2）——维度对不上直接报错。
3. 复杂结构（如列清单）不能塞 metadata（只允许标量），序列化成 JSON 字符串放进 document 正文或单独字段。

## 3. HNSW 参数在 Chroma 里怎么调

| 参数 | 默认 | 说明 |
|---|---|---|
| `space` | **l2**（⚠️ 不改就是 l2！） | 只能建库时定，建完不可改；文本场景必须显式设 `cosine` |
| `ef_construction` | 100 | 建图质量，只能建库时定，建议 200 |
| `ef_search` | 100 | **运行时可调**：`collection.modify(configuration={"hnsw": {"ef_search": N}})`，调大召回↑延迟↑ |
| `max_neighbors`（=M） | 16 | 建库时定 |

⚠️ 两个坑：`get_or_create_collection` 对**已存在**的 collection 会原样返回并**忽略**新传的 configuration——想改 `space` 必须删掉数据目录重建；默认 space 是 `l2`，忘了设 cosine 是新手最常见错误。

## 4. 自实现混合检索（本项目 F3 的核心代码骨架）

### 4.1 架构

```
用户问题
   ├─ jieba 分词 ──────────► BM25 路（rank-bm25，按 doc_type 分组索引）──┐
   └─ fastembed 编码 ──────► 语义路（Chroma query + where 过滤）────────┤
                                                                    RRF 融合（自写）
                                                                    ─► top-N 进 prompt
```

为什么两路分数不能直接加权平均：余弦相似度在 [-1,1]、BM25 分数无上界，**量纲不可比**——这就是用 RRF（基于排名）而不是线性加权的原因（第 1 期 4.4 节的手算例子在这里原样适用）。

### 4.2 代码骨架

```python
import jieba
from rank_bm25 import BM25Okapi

class HybridRetriever:
    """BM25(关键词) + Chroma(语义) + RRF 融合。语料更新时 rebuild() 一次。"""

    def __init__(self, collection, model, k=60):
        self.collection, self.model, self.k = collection, model, k
        self.rebuild()

    def rebuild(self):
        # 全量拉语料（万级毫秒完成，问答对入库后调用即可）
        data = self.collection.get(include=["documents", "metadatas"])
        self.ids, self.docs, self.metas = data["ids"], data["documents"], data["metadatas"]
        # 按 doc_type 分组建 BM25，检索时只在对应类型内搜（等价于向量路的 where 过滤）
        self.bm25s = {}
        for dtype in {m["doc_type"] for m in self.metas}:
            idx = [i for i, m in enumerate(self.metas) if m["doc_type"] == dtype]
            tokenized = [list(jieba.lcut(self.docs[i])) for i in idx]
            self.bm25s[dtype] = (idx, BM25Okapi(tokenized))

    def search(self, query: str, doc_types: list[str], top_k_each=20, top_n=10):
        tokenized_q = list(jieba.lcut(query))

        # —— BM25 路：每个类型各取 top ——
        bm25_ranked = []
        for dtype in doc_types:
            idx, bm25 = self.bm25s[dtype]
            scores = bm25.get_scores(tokenized_q)
            order = sorted(range(len(idx)), key=lambda j: scores[j], reverse=True)[:top_k_each]
            bm25_ranked += [[self.ids[idx[j]] for j in order]]

        # —— 语义路：Chroma + metadata 过滤 ——
        q_vec = [e.tolist() for e in self.model.embed([query])][0]
        res = self.collection.query(query_embeddings=[q_vec], n_results=top_k_each,
                                    where={"doc_type": {"$in": doc_types}})
        dense_ranked = res["ids"]

        # —— 自实现 RRF 融合 ——
        scores = {}
        for ranked in [*bm25_ranked, *dense_ranked]:
            for rank, doc_id in enumerate(ranked, start=1):
                scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (self.k + rank)
        top_ids = sorted(scores, key=scores.get, reverse=True)[:top_n]
        id2doc = dict(zip(self.ids, self.docs))
        return [{"id": i, "text": id2doc[i], "rrf_score": scores[i]} for i in top_ids]
```

### 4.3 这段代码里藏着的面试细节（每一句都能被追问）

1. **为什么 RRF 用排名不用分数**——量纲不可比（见 4.1）；k=60 平滑排名差距。
2. **BM25 路怎么"过滤" doc_type**——按类型分组建索引（每类一个小 BM25），检索时只查对应组；等价于向量路的 `where` 过滤。数据量大时可换 Lucene/ES，但万级没必要。
3. **rank-bm25 语料更新只能重建对象**——问答对 👍 入库后调一次 `rebuild()`；万级重建毫秒级，成本可忽略。这是"知道工具边界"的加分点。
4. **两路各取 top 20 再融合**——K 取太小融合不出"两路都认可"的效果，取太大浪费计算；20 是常见的起点，W6 消融实验里可以测。
5. **jieba 分词质量直接影响 BM25 路**——表名 `order_amount` 这类英文 token 要保证不被切碎（可给 jieba 加自定义词表 `jieba.add_word`）。
6. **`n_results` 大于过滤后剩余条数会抛 "contiguous 2D array" 错**——代码里要 `min(top_k_each, 类型内条数)` 兜底。

## 5. 规模边界：什么时候才值得升级 Milvus（面试话术）

官方性能包络：单机最大向量数 ≈ **内存(GB) × 25 万条**（1024 维口径）。我们 2G 服务器理论上限约 50 万条，实际语料万级 512 维（约 20MB 向量数据）——**占用不到 5%，非常安全**。查询毫秒级。

升级触发条件（背下来，面试说"我留了明确的升级判断标准"）：
1. 语料超过 10 万条 / 多个服务需要共享同一个向量库（嵌入式是进程内的，没法共享）
2. 需要 BM25+向量的**数据库级**混合检索、标量索引、权限审计这些工程能力
3. 多人/多环境需要统一运维的检索服务

到时候迁移路径也说得清：Milvus 内置 BM25 Function + hybrid_search + RRFRanker（第 1 期材料第 4 章），我们的 `HybridRetriever` 接口不变、换底层实现即可——**这就是把检索层抽象成接口的回报**。

## 6. 面试 Q&A

1. **"为什么不用 Milvus？"** → 完整讲第 0 节的选型故事：功能上 Milvus 最匹配（内置 BM25 混合检索）→ 但 Standalone 官方最低 4核8G，服务器 2核2G → 量化过 Chroma 包络（2G≈25万条）我们只用 5% → 选 Chroma + 自实现混合检索 → 留有升级触发条件。**先夸再弃、有数据、有后路。**
2. **"Chroma、Milvus、FAISS 三者怎么选？"** → FAISS 是算法库（无持久化/CRUD/过滤）；Chroma 是嵌入式原型库（零运维、单进程、十万级以下）；Milvus 是生产级独立服务（亿级、混合检索、分布式）。选型 = 数据规模 × 是否需要独立服务 × 运维预算。
3. **"Chroma 的全文搜索能当 BM25 用吗？"**（细节题，答对显功底）→ 不能。`where_document` 只是**子串/正则过滤器**，命中即保留、不打相关性分，无法参与排序；所以 BM25 必须外挂 rank-bm25。
4. **"你的 RRF 有什么坑？"** → 两路 K 值选择、分数不可比所以只用排名、同一文档两路都命中时得分叠加（这正是设计意图）、平局时按 id 稳定排序保证可复现。
5. **"rank-bm25 这个库 2022 年就不更新了，敢用吗？"** → BM25 是 1994 年的成熟算法，库小而稳定、无依赖；且我们把它封装在 `HybridRetriever` 内部，未来换实现（ES/自写）不动上层接口。

## 7. 坑清单（开发前过一遍）

1. `PersistentClient` 的 path 用**绝对路径**，一个进程只建一次；相对路径换目录启动就"数据丢失"
2. `space` 默认是 **l2**，建库必须显式 `cosine`；建完不可改，改配置 = 删目录重建
3. `get_or_create_collection` 已存在时忽略新配置——别指望它顺带升级参数
4. 重建语料一律 `upsert`（`add` 撞 id 抛 ValueError）
5. metadata 值只能是 str/int/float/bool（新版支持同质数组）——None/dict 不行
6. `n_results` 超过过滤后剩余条数会抛 contiguous 2D array 错——记得 min 兜底
7. Python 的 SQLite 要 ≥3.35（3.11+ 的 Python 一般没问题；Docker 里用 Debian bookworm+）
8. 依赖锁 `chromadb>=1.5.5,<2`（1.5.4 已被撤回）；langchain-chroma 要 1.x 线才配 chromadb 1.x
9. bge 系模型做检索时，**查询侧**建议加指令前缀（"为这个句子生成表示以检索相关文章："），**文档侧**不加——W6 评测时两组要统一口径
10. Chroma 跑在 Python 服务进程内 → FastAPI 多 worker 部署时注意每 worker 各持一份索引的内存开销，2G 服务器 worker 数设 1

## 8. 自检清单

- [ ] 能完整讲出"为什么用 Chroma 不用 Milvus"的选型故事（含数字）？
- [ ] Chroma 的持久化机制和"嵌入式"意味着什么？和 SQLite 的类比说得出口吗？
- [ ] `where_document` 和 metadata `where` 的区别？为什么前者当不了 BM25？
- [ ] HNSW 的 space/ef_search 在 Chroma 里怎么设？哪个建后可改？
- [ ] RRF 的代码能默写吗？为什么不用两路分数直接加权？
- [ ] BM25 路的 doc_type 过滤是怎么实现的？语料更新后要做什么？
- [ ] 什么信号出现时该升级 Milvus？迁移路径是什么？
- [ ] 2核2G 上我们的服务内存预算大概是多少？哪个组件最吃内存？

---

## 附：来源

- [chromadb (PyPI)](https://pypi.org/project/chromadb/) ｜ [v1.0 迁移日志](https://docs.trychroma.com/docs/overview/migration.md)
- [PersistentClient](https://docs.trychroma.com/docs/run-chroma/clients.md) ｜ [Collection 配置/HNSW 参数](https://docs.trychroma.com/docs/collections/configure.md) ｜ [metadata 过滤](https://docs.trychroma.com/docs/querying-collections/metadata-filtering.md)
- [全文搜索（where_document 语义）](https://docs.trychroma.com/docs/querying-collections/full-text-search.md) ｜ [Collection API](https://docs.trychroma.com/reference/python/collection.md)
- [单机性能包络](https://docs.trychroma.com/guides/performance/single-node.md) ｜ [故障排查](https://docs.trychroma.com/docs/overview/troubleshooting.md)
- [rank-bm25 (PyPI)](https://pypi.org/project/rank-bm25/) ｜ [langchain-chroma (PyPI)](https://pypi.org/project/langchain-chroma/)

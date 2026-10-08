# 向量数据库与 Milvus —— 项目知识补充（第 1 期）

> ⚠️ **v2 变更说明（2026-10-08）**：项目最终确认服务器为 2核2G，达不到 Milvus Standalone 官方最低 8G 内存要求，**向量库已回退为嵌入式 Chroma**（见《Chroma 实战与自实现混合检索》）。本材料**不作废**，角色调整为：
> - **第 1、2 章（为什么需要向量数据库、距离度量/HNSW/索引/标量过滤）**：向量数据库通用知识，不依赖具体产品，**仍然必学**，面试考的就是这些
> - **第 3-4 章（Milvus 部署与代码）**：转为面试储备——你是"评估过 Milvus 的人"，能说出 Standalone 最低 8G 内存、Lite 不支持 Windows、内置 BM25 混合检索这些细节，比只会说"我用了 Chroma"的人高一档
> - **第 5-6 章的对比表和 Q&A**：通用面试素材，"为什么不用 Milvus"的标准答法更新为第 5 节末尾 + 《Chroma 实战》第 6 节的**资源约束叙事**
>
> **定位**：教学材料，配合需求文档 §5 技术约束阅读。写于 2026-10-08，基于 Milvus 官方文档与社区调研（来源见文末）。
> **学完你能回答**：为什么需要向量数据库？HNSW 是什么原理？COSINE/IP/L2 怎么选？我们项目为什么用 Milvus 不用 Chroma/FAISS？混合检索和 RRF 怎么做的？——这些都是调研到的面试原题。

---

## 0. 怎么用这份材料

按顺序读第 1→2→3 章建立概念（约 30 分钟），第 4 章是我们的落地设计（W2 动手时对照），第 5→6 章是面试话术，第 8 章自检清单**全部答得上来才算过关**。标 ⚠️ 的是会直接影响你开发环境的事。

---

## 1. 从我们的项目出发：为什么需要向量数据库

回顾 RAG 的检索环节在干什么：把文本用 Embedding 模型编码成**向量**（比如 512 个浮点数），语义相近的文本向量在空间里距离也近。用户问"销售额怎么算"，语料库里"GMV 统计口径"这条文档的向量离它最近——检索 = **找最近的 K 个向量（KNN）**。

那为什么不用最朴素的方法？

| 方案 | 是什么 | 缺了什么 |
|---|---|---|
| NumPy 暴力比对 | 全部向量算一遍距离，O(N·d) | 数据一大就慢；每次重启重新加载；没有增删改 |
| FAISS | Meta 出的 **ANN 算法库** | 快，但它只是个**库**：没有持久化服务、没有增删改查接口、没有标量过滤、没有高可用——数据一变就得重建索引 |
| **向量数据库（Milvus）** | 存储 + ANN 索引 + 标量过滤 + 增删改 + 容错的一体化**服务** | 多一个常驻服务要运维（这是代价，见第 5 节） |

**面试一句话版**：向量数据库 = 在 FAISS 这类 ANN 算法之上补齐了数据库该有的东西（持久化、CRUD、标量过滤、分布式与高可用），FAISS 是算法库，Milvus 是数据库。

**我们项目为什么真的需要**（而不是跟风）：三路语料要**持续更新**（优质问答对会不断入库）、要**按类型过滤**（这次查询只搜"口径文档"）、上线后是**常驻服务**——这三点恰好都踩在"需要数据库"而不是"需要算法库"的线上。

---

## 2. 核心概念速成（面试直接考的部分）

### 2.1 距离度量：COSINE / IP / L2 怎么选

| 度量 | 含义 | 取值 | 什么时候用 |
|---|---|---|---|
| **COSINE** 余弦相似度 | 只看向量**方向**，忽略长度 | [-1, 1]，越大越相似 | **文本 Embedding 默认选它**（BGE/OpenAI 等模型官方推荐） |
| **IP** 内积 | 点积 | 越大越相似 | 向量已归一化时与 COSINE 数学等价；模型明确要求内积时用 |
| **L2** 欧氏距离 | 空间直线距离 | 越小越相似 | 图像特征、传统 ML 特征多见；文本场景少用 |

**口诀**：文本用 COSINE，不确定就看 Embedding 模型文档的推荐。BM25 稀疏字段固定用 `metric_type="BM25"`。

### 2.2 ANN：用一点点召回换数量级的速度

精确 KNN 要和所有向量比一遍，数据量大了不可行。**ANN（Approximate Nearest Neighbor，近似最近邻）** 的思路：建一种索引结构，只探索"可能相近"的一小部分向量，**牺牲少量召回率换取百倍千倍加速**。衡量指标：recall@K（找回来的比例）、QPS、P99 延迟。调优抓手就是下面这些索引的参数。

### 2.3 索引类型：重点吃透 HNSW

| 索引 | 一句话原理 | 选型建议 |
|---|---|---|
| **FLAT** | 暴力全量精确比对 | <10 万向量、要 100% 召回时 |
| **HNSW** | 分层"可导航小世界"图（下详） | **默认首选**：高召回 + 低延迟 |
| **IVF_FLAT / IVF_SQ8 / IVF_PQ** | 先聚类分桶，查询只搜最近的 nprobe 个桶；SQ8/PQ 额外做向量压缩 | 亿级数据省内存方案，召回略低于 HNSW |
| **DiskANN** | 图索引大部分放磁盘 | 内存装不下、能接受稍高延迟 |

**HNSW 原理（面试必讲清）**：把向量组织成多层图。**上层稀疏、边长**——像高速公路，几跳就能跨越整个空间逼近目标区域；**下层稠密、边短**——像街道，精细搜索。查询从最上层出发，每层贪心走"离目标最近的邻居"，逐层下降，直到最底层。三个关键参数：

- `M`：每个节点的连边数（越大图越密、召回越高、内存越大），常用 16
- `efConstruction`：建图时候选队列长度（影响图质量），常用 100-200
- `ef`：**查询时候选队列长度（运行时可调）**——调大它召回上升、延迟上升，是性能调优的第一抓手

**代价要会说**：内存约为原始向量的 1.5-2 倍；建图慢；删除节点效率低。

### 2.4 Milvus 数据模型与标量过滤

- **Collection** ≈ MySQL 的"表"；**Schema** 定义字段，注意：VARCHAR 必须给 `max_length`，向量字段必须给 `dim`（维度必须和你的 Embedding 模型输出一致，插错直接报错）
- **Partition**：表内物理分区，检索可指定只扫某几个分区。什么时候才需要？——数据量大到"按业务裁剪能明显提速"时。我们项目语料在万~十万级，**先用标量字段过滤就够了**，别为了用而用（面试时"我没有过度设计"是加分答法）
- **标量过滤（filter）**：检索时传表达式字符串，先过滤后向量检索：

```python
client.search(
    "text2sql_rag", data=[q_vec], anns_field="dense",
    filter='doc_type == "glossary"',   # 只搜"业务口径"这类语料
    limit=5, output_fields=["text", "doc_type"],
)
```

  我们用 `doc_type` 区分三类语料（`table_schema` / `glossary` / `qa_pair`），这个字段建上标量索引。
- **动态字段**：Schema 外的杂散字段进隐藏 JSON。灵活但没法建高效索引——核心过滤字段一定要显式声明。

---

## 3. Milvus 全景与部署选型

### 3.1 版本现状（2026-10 查证）

- 最新稳定版 **v3.0.2**（2026-09-20）；2.6 线仍在维护（v2.6.25）
- **pymilvus SDK 与 server 大版本要配套**（3.x 配 3.x，2.6 配 2.6），langchain-milvus 0.4.0 要求 pymilvus ≥3.0 且 **Python ≥3.10** → 本项目 Python 版本钉在 3.10+
- 关键特性时间线：**2.5 引入内置 BM25 全文检索**（文本→稀疏向量由服务端 Function 自动完成）→ 2.6 中文分词器完善 → 3.0 新式 rerank Function 写法

### 3.2 四种形态，我们选 Standalone

| 形态 | 定位 | 资源 | 关键限制 |
|---|---|---|---|
| Milvus Lite | pip 装的嵌入式库 | 极低 | 只有 FLAT 索引；不支持 Partition/别名；**不支持 Windows 原生**（⚠️ 见 3.3）；BM25 支持状态文档说法不一 |
| **Milvus Standalone** ✅ | 单机 all-in-one Docker 镜像 | **官方最低 4 核 CPU / 8GB 内存 / 50GB 磁盘** | 单点无高可用——个人项目完全够 |
| Milvus Distributed | K8s 编排的集群 | 集群级 | 个人服务器无必要 |
| Zilliz Cloud | 全托管 | 免运维 | 数据在云上；服务器内存不够时的逃生通道 |

### 3.3 ⚠️ Windows 开发机的两条路（直接影响你）

官方文档明确：Milvus Lite 只支持 Ubuntu ≥ 20.04 和 macOS ≥ 11，**Windows 不在列表里**（要在 Windows 上跑 Lite 得装 WSL）。所以你的开发机有两条路：

1. **推荐：Docker Desktop 里跑 Milvus Standalone**——一条 `docker compose up` 的事，和你最终部署到服务器的形态完全一致（开发即演练），W2 搭好后 W4 直接把同一份 compose 文件搬上服务器
2. 备选：本地不装，代码直连服务器上的 Standalone（`MilvusClient(uri="http://服务器IP:19530")`）——省本地资源，但开发依赖网络和服务器状态

**前置检查（需要你确认）**：① 你的开发机内存 ≥16GB（Docker Desktop + Milvus 8GB + IDE + 浏览器）；② **你的服务器内存是多少？低于 8GB 的话 Milvus 跑不动，需要换 Zilliz Cloud 免费版或减配方案**——这个答案决定部署章节怎么写。

---

## 4. 本项目的落地设计（W2 对照用，代码可直接改用）

### 4.1 设计决策：一个 Collection，不是三个

三类语料放**同一个 Collection**，用 `doc_type` 标量字段区分、检索时 filter。理由：三路语料量级都是万级以下，没必要三个 Collection 三套管理；单 Collection 让"跨类型联合检索"（比如问题先搜口径再搜问答对）变成改一个 filter 表达式。面试被问"为什么不用 Partition"：数据量不支持裁剪收益，引入 Partition 是复杂度没有收益——**按需设计**。

### 4.2 Schema：带 BM25 Function 的建表代码

```python
from pymilvus import MilvusClient, DataType, Function, FunctionType

client = MilvusClient(uri="http://localhost:19530")   # Docker Desktop 里的 Standalone

schema = client.create_schema()
schema.add_field("id", DataType.INT64, is_primary=True, auto_id=True)
schema.add_field("text", DataType.VARCHAR, max_length=4096,
                 enable_analyzer=True,
                 analyzer_params={"type": "chinese"})        # 中文语料必须配中文分词器
schema.add_field("doc_type", DataType.VARCHAR, max_length=64)  # table_schema / glossary / qa_pair
schema.add_field("sparse", DataType.SPARSE_FLOAT_VECTOR)
schema.add_field("dense", DataType.FLOAT_VECTOR, dim=512)      # bge-small-zh-v1.5 输出 512 维

# BM25 Function：服务端自动把 text 分词生成稀疏向量——插入时【不要】手传 sparse 字段
schema.add_function(Function(
    name="text_bm25",
    input_field_names=["text"],
    output_field_names=["sparse"],
    function_type=FunctionType.BM25,
    function_params={"bm25": {"k1": 1.2, "b": 0.75}},
))

index_params = client.prepare_index_params()
index_params.add_index(field_name="sparse",
                       index_type="SPARSE_INVERTED_INDEX", metric_type="BM25")
index_params.add_index(field_name="dense",
                       index_type="HNSW", metric_type="COSINE",
                       params={"M": 16, "efConstruction": 200})

client.create_collection("text2sql_rag", schema=schema, index_params=index_params)
```

### 4.3 混合检索：两代写法都给你（装好后实测用哪代）

```python
from pymilvus import AnnSearchRequest, RRFRanker

q = "上季度销售额怎么算"
dense_req = AnnSearchRequest(
    data=[emb_model.embed_query(q)], anns_field="dense",
    param={"metric_type": "COSINE", "params": {"ef": 64}}, limit=20)
sparse_req = AnnSearchRequest(
    data=[q], anns_field="sparse",                   # 原文直接传，服务端转稀疏
    param={"metric_type": "BM25"}, limit=20)

# 经典写法（2.5/2.6 教程资料最多；pymilvus 3.x 是否保留旧类，装好后 from pymilvus import RRFRanker 实测）
res = client.hybrid_search("text2sql_rag", reqs=[dense_req, sparse_req],
                           ranker=RRFRanker(60), limit=10,
                           output_fields=["text", "doc_type"])
# 3.0 新写法：ranker 换成 Function(FunctionType.RERANK, ...)，见官方 v3.0 文档
```

### 4.4 RRF 到底怎么算的（面试手撕点，必须会手算）

RRF（Recall-Oriented Reciprocal Rank Fusion，倒数排名融合）：每路检索各自给出排名，**每个文档的融合分 = Σ 1/(k + rank_i)**，k 默认 60。手算例子（k=60）：

| 文档 | 语义路排名 | BM25路排名 | 融合分 |
|---|---|---|---|
| A | 1 | 3 | 1/61 + 1/63 = 0.03226 |
| B | 2 | 1 | 1/62 + 1/61 = **0.03252** ← 胜出 |
| C | 3 | - | 1/63 = 0.01587 |

B 在两路都不错所以赢，C 只有一路垫底所以输——这就是 RRF 的直觉：**奖励"两路都认可"的文档**。为什么用排名不用分数？两路分数量纲完全不同（余弦 [-1,1] vs BM25 无界），排名天生可比。k=60 的作用是平滑排名差距，让前排优势不至于一家独大。

**项目场景化答法**（面试官问"你的 Text-to-SQL 为什么混合检索"）：表名字段名这种**精确关键词**靠 BM25 保命（"order_amount" 语义向量未必和"订单金额"贴得很近），自然语言问法靠语义向量泛化（"营收"≈"销售额"），RRF 融合两头兼顾。

### 4.5 LangChain 集成还是原生 SDK？

有官方包 `langchain-milvus`（0.4.0，与 LangChain v1 兼容），`as_retriever()` 三行接好，还支持 `BM25BuiltInFunction` 和 `ranker_type="rrf"`。**我们的策略：原生 pymilvus `MilvusClient` 直连 + 自己包一层 Retriever 喂给 Agent**。理由：BM25 Function + hybrid_search 的高级配置用原生 API 控制力最强，而且"自己包一层"本身就是面试可讲的工程决策；langchain-milvus 留作快速原型对照。

---

## 5. Milvus vs Chroma：对比与面试话术

| 维度 | Milvus | Chroma |
|---|---|---|
| 定位 | 独立向量数据库服务（云原生） | 嵌入式优先，pip 即用，原型定位 |
| 规模上限 | 十亿级 | 不面向大规模 |
| 混合检索 | **内置 BM25 + 稀疏向量 + RRF（2.5+）** | 稠密为主，混合检索弱 |
| 生产就绪 | 国内生产采用最广的开源向量库 | 无权限/审计/备份体系 |
| 代价 | 多一个常驻服务，官方最低 8G 内存 | 轻，进程内嵌 |

**"为什么选 Milvus 弃 Chroma"标准答法**：① 项目核心是 BM25+向量混合检索，Milvus 原生内置、Chroma 做不到开箱即用；② 需要 doc_type 标量过滤的工程能力；③ Milvus 是国内生产最主流选择，我想借项目把生产级向量库的运维也练了。

**"小数据用 Milvus 是不是过度设计？"**（大概率被挑战，提前备好）：诚实承认性能上 Chroma 完全够，选 Milvus 是为了**混合检索能力**和**生产叙事**，不是为了性能刚需——这个坦率的回答比硬撑加分。反过来说，这也是 Chroma 换成 Milvus 后新增的成本清单：你要多学 Schema/索引/部署这些 DB 概念（正是这份材料存在的意义）。

---

## 6. 面试 Q&A 精编（含美团真题）

1. **为什么需要向量数据库，NumPy/FAISS 不行吗？** → 第 1 章表格式答法：算法库 vs 数据库（持久化/CRUD/标量过滤/高可用）。
2. **HNSW 原理？** → 分层图 + 贪心逐层下降 + M/efConstruction/ef 三参数 + 内存代价（第 2.3 节）。
3. **ANN 是什么？召回和速度怎么权衡？** → 牺牲少量精度换数量级加速；recall@K / QPS / P99；调 ef、nprobe、量化方式。
4. **Milvus 索引怎么选？**（美团真题）→ FLAT 精确 / HNSW 默认 / IVF 系省内存 / DiskANN 落盘；我们项目万级语料选 HNSW+COSINE 并说明理由。
5. **百万级/亿级怎么办？** → 分区分片、量化压缩（SQ8/PQ/RaBitQ）、DiskANN、存算分离水平扩容——点到为止，说明知道路径即可。
6. **向量检索和标量过滤怎么结合？** → filter 表达式预过滤下推 + 标量索引；我们的 doc_type 过滤就是实例。
7. **稠密 vs 稀疏向量？混合检索为什么有效？** → 语义泛化 vs 精确关键词，用 Text-to-SQL 场景讲（4.4 末尾话术）。
8. **RRF 和加权融合的区别？** → RRF 基于排名、免归一化、对分数量纲不敏感；WeightedRanker 线性融合原始分数、可调"更信语义还是关键词"，但要求分数可比。
9. **Milvus 架构为什么适合生产？** → 日志先行（WAL）保证写入持久化、存算分离四层架构可独立扩容、Segment 组织 + 后台 compaction——能讲出这几个词并解释即可。

---

## 7. 常见坑清单（开发前过一遍）

1. Milvus Lite 在 Windows 跑不起来 → Docker Desktop 跑 Standalone（第 3.3 节）
2. `dense` 字段 dim 必须与 Embedding 模型输出一致（bge-small-zh-v1.5 = **512**），插错维度直接报错
3. 插入数据时**不要手动传 sparse 字段**——BM25 Function 服务端自动生成
4. VARCHAR 必须给 `max_length`；中文语料建 BM25 必须设 `analyzer_params={"type": "chinese"}`，否则按空格切中文等于没切
5. pymilvus 与 server 大版本要配套；langchain-milvus 0.4.0 要求 **Python ≥3.10**
6. pymilvus 3.x 下 `RRFRanker` 旧类是否保留未完全确认，装好后先 `from pymilvus import RRFRanker` 实测，不行就换 3.0 Function-RERANK 写法
7. `doc_type` 这类过滤字段建标量索引，否则大数据量下 filter 变慢
8. Docker Desktop 内存配额默认可能只有一半物理内存，Milvus 起不来先去设置里调大

---

## 8. 自检清单（全部答得出才算过关）

- [ ] 向量数据库和 FAISS 的本质区别是什么？
- [ ] COSINE/IP/L2 分别什么含义？文本场景选哪个？
- [ ] HNSW 的分层结构和查询过程能画出来吗？三个参数各管什么？
- [ ] ANN 用什么换速度？怎么衡量？
- [ ] Milvus 四种形态怎么选？为什么我们选 Standalone？Lite 在 Windows 上的问题是什么？
- [ ] 我们为什么一个 Collection 而不是三个？什么时候才该用 Partition？
- [ ] BM25 Function 解决了什么问题？插入时为什么不用管 sparse 字段？
- [ ] RRF 手算题：文档 A 两路排名 2/2，文档 B 排名 1/5，k=60，谁分高？（动手算）
- [ ] "小数据用 Milvus 是不是过度设计"怎么答？
- [ ] 我们项目 Embedding 用什么模型？几个维度？

---

## 附：来源

- [Milvus Release Notes](https://milvus.io/docs/release_notes.md) ｜ [GitHub Releases](https://github.com/milvus-io/milvus/releases)（v3.0.2, 2026-09-20）
- [全文检索 v2.5.x](https://milvus.io/docs/v2.5.x/full-text-search.md) ｜ [混合检索 v2.5.x](https://milvus.io/docs/v2.5.x/multi-vector-search.md) ｜ [v3.0.x 对应文档](https://milvus.io/docs/full-text-search.md)
- [Milvus Lite 文档](https://milvus.io/docs/milvus_lite.md) ｜ [Windows 不支持 issue #176](https://github.com/milvus-io/milvus-lite/issues/176) ｜ [Standalone 硬件要求](https://milvus.io/docs/prerequisite-docker.md)
- [相似度度量](https://milvus.io/docs/metric.md) ｜ [索引详解](https://milvus.io/docs/index-explained.md) ｜ [Schema](https://milvus.io/docs/schema.md)
- [langchain-milvus (PyPI)](https://pypi.org/project/langchain-milvus/) ｜ [LangChain Milvus 集成文档](https://python.langchain.com/docs/integrations/vectorstores/milvus/)
- [Zilliz: Milvus vs Chroma](https://zilliz.com/comparison/milvus-vs-chroma) ｜ [美团 Milvus 面经](https://www.cnblogs.com/crazymakercircle/p/18867143)

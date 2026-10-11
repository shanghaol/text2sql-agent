"""反馈自举闭环（W5 设计 §4）：点赞样本 → few-shot 语料（qa_pairs.md 追加 QA-012+）。

数据流（§4.1）：
  feedback.rating=1 → Java GET /api/internal/feedback/samples（X-Internal-Token 鉴权）
    → 本脚本六道质量门槛逐条过滤（§4.3）+ 双层去重（§4.4）
    → --dry-run 预览（默认，人工过目） / --apply 追加语料
    → python -m agent.rag.build_index 重建索引（脚本只提示、不代跑）
    → deploy/absorb_corpus.sh 同步服务器（§4.8）

为什么手动 + dry-run（§4.5）：点赞 ≠ 正确——用户可能为"看起来像答案"的错误结果点赞，
自动入库的污染是静默且全局的；dry-run 强制人工过目一遍样本与相似度，
是这条链路里唯一的人工质量闸。--apply 只写本地 qa_pairs.md，
git commit 与服务器同步是人的动作（deploy/absorb_corpus.sh）。

六道门槛（§4.3，按序全评、任一不过即剔除）：
  G1 rating == 1                 点赞是唯一质量信号
  G2 status == 1                 执行成功的 SQL 才有示范价值
  G3 generated_sql 非空          纯聊天回答不构成 SQL 示例
  G4 sql_guard AST 校验通过      入库示例要与自家安全规则自洽（T1 未完成时容错跳过）
  G5 问题 1~100 字、SQL ≤2000 字 语料风格与现有 11 条一致
  G6 双层去重（§4.4）            文本归一精确匹配 + 语义阈值 0.92——判重对象是
                                 新问题 vs 现有条目「问」字段的 cosine 相似度
                                 （不是整片文本，标定依据见 SemanticDeduper docstring）

说明：Java 侧 SQL 已按 rating=1 / status=1 / generated_sql IS NOT NULL 过滤，
G1-G3 在脚本里重复把关是纵深防御——样本对象也允许直接构造（未来 --from-json 备选、单测），
那时这三道门槛就是真闸门。

用法（仓库根目录）：
  .venv/Scripts/python.exe -m agent.rag.absorb_feedback             # 等价 --dry-run
  .venv/Scripts/python.exe -m agent.rag.absorb_feedback --apply     # 确认无误后收录
  .venv/Scripts/python.exe -m agent.rag.absorb_feedback --server http://127.0.0.1:8081
  .venv/Scripts/python.exe -m agent.rag.absorb_feedback --calibrate # 去重阈值标定（§8-13）
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime

from agent.config import EMBEDDING_MODEL, INTERNAL_TOKEN, REPO_ROOT

# ---------------------------------------------------------------------------
# 常量（阈值与上限全部集中在此，标定后只改这里）
# ---------------------------------------------------------------------------

DEFAULT_SERVER_URL = "http://127.0.0.1:8081"  # 生产 systemd 端口；开发机联调用 --server 覆盖
SAMPLES_LIMIT = 200           # 与 Java 侧封顶一致（HistoryService.FEEDBACK_SAMPLES_MAX_LIMIT）

SEMANTIC_DUP_SIMILARITY = 0.92  # cosine 相似度 ≥0.92 判重（distance ≤0.08，§4.4；--calibrate 标定）
AUTO_ABSORB_MIN_NUMBER = 12     # 自举条目从 QA-012 起顺延（001~011 是人工精挑语料，§4.6）

G5_QUESTION_MAX_CHARS = 100
G5_SQL_MAX_CHARS = 2000

QA_PAIRS_PATH = REPO_ROOT / "data" / "rag_corpus" / "qa_pairs.md"

# MULTILINE 供 findall 在整份语料文本上逐行找标题；逐行 .match 的用法不受影响
_QA_HEADING_RE = re.compile(r"^### QA-(\d+)", re.MULTILINE)

# --calibrate 的 10 个变体（§8-13：现有 11 问 + 5 个同义改写 + 5 个不同题，
# 看 top1 distance 分布确认/修订 0.92）。改写组对应现有条目：
# P1↔QA-003 P2↔QA-002 P3↔QA-004 P4↔QA-007 P5↔QA-008。
_CALIBRATE_PARAPHRASES = (
    "上一个月的成交总额（GMV）是多少？",
    "销售额排名前五的城市分别是哪些？",
    "九月份的退款比例按金额算是多少？",
    "最近一周每天的支付金额情况是怎样的？",
    "2026 年 6 月平均每笔订单的金额是多少？",
)
_CALIBRATE_DIFFERENT = (
    "2026 年 3 月下单的用户有多少人？",
    "各商品类目的销量排名是怎样的？",
    "退款原因的分布是什么样的？",
    "男性用户和女性用户的消费金额对比如何？",
    "2026 年第二季度的订单总数是多少？",
)


# ---------------------------------------------------------------------------
# 数据结构与纯函数（单测重点覆盖区）
# ---------------------------------------------------------------------------


@dataclass
class Sample:
    """一条待收录样本（Java /feedback/samples 的返回行 + 纵深防御用的 rating/status）。"""

    history_id: int
    question: str
    generated_sql: str
    comment: str = ""
    created_at: str = ""
    rating: int = 1  # Java SQL 已过滤；直接构造 Sample（--from-json/单测）时才真正生效
    status: int = 1

    @classmethod
    def from_api(cls, row: dict) -> Sample:
        """从接口 JSON 行构造（§4.2 响应字段 {historyId, question, generatedSql,
        comment, createdAt}；rating/status 不在响应里，按服务端过滤语义置 1）。"""
        return cls(
            history_id=int(row.get("historyId") or 0),
            question=row.get("question") or "",
            generated_sql=row.get("generatedSql") or "",
            comment=row.get("comment") or "",
            created_at=row.get("createdAt") or "",
        )


@dataclass
class GateOutcome:
    """单道门槛的结论：ok=None 表示跳过（目前只有 G4 的 T1 未完成容错）。"""

    gate: str  # "G1".."G6"
    name: str  # 中文门槛名
    ok: bool | None
    detail: str


@dataclass
class BatchItem:
    """本批次内已保留样本的判重底册（§4.4：本批次内先两两判重再统一追加）。"""

    history_id: int
    norm_question: str
    embedding: list[float] | None


@dataclass
class SampleResult:
    """一条样本的完整判定结果（门槛逐条结论 + 语义 top1 展示信息）。"""

    sample: Sample
    outcomes: list[GateOutcome] = field(default_factory=list)
    embedding: list[float] | None = None
    sem_top1_id: str | None = None   # 判重口径 top1 的 QA 编号，如 "QA-003"
    sem_top1_sim: float | None = None
    retrieval_top1_id: str | None = None   # 诊断展示：整片索引检索 top1 的 chunk id
    retrieval_top1_sim: float | None = None

    @property
    def kept(self) -> bool:
        # ok=None（G4 的 T1 未完成容错跳过）不算失败：门槛没跑 ≠ 不通过
        return bool(self.outcomes) and all(outcome.ok is not False for outcome in self.outcomes)

    @property
    def failed_gates(self) -> list[str]:
        return [outcome.gate for outcome in self.outcomes if outcome.ok is False]


def normalize_display(text: str) -> str:
    """展示用规范化（§4.6 问字段）：连续空白折叠成单个空格、去首尾。"""
    return " ".join(text.split())


def normalize_question(text: str) -> str:
    """去重用文本归一（§4.4 文本层）：去掉全部空白 + 小写。

    归一后"上个月的 GMV 是多少？"与"上个月的GMV是多少？"完全同文——
    空格/全半角差异不再造成重复收录。设计例子里带"的/是"的改写不在此层拦截，
    那是语义层（0.92）的职责。
    """
    return "".join(text.split()).lower()


def parse_corpus_questions(text: str) -> list[tuple[str, str]]:
    """解析现有 QA 条目的（编号, 问题）——G6 文本层对现有语料的底册。

    只取每条第一个 **问** 字段；没有编号头的散落内容忽略。
    """
    entries: list[tuple[str, str]] = []
    current_no: str | None = None
    for line in text.splitlines():
        heading = _QA_HEADING_RE.match(line)
        if heading:
            current_no = f"QA-{int(heading.group(1)):03d}"
            continue
        if current_no is not None and line.startswith("**问**"):
            question = line.removeprefix("**问**").lstrip("：:").strip()
            entries.append((current_no, question))
            current_no = None  # 一条 QA 只认一个问字段
    return entries


def parse_max_qa_number(text: str) -> int | None:
    """现有文件里的最大 QA 编号（无条目时 None）。"""
    numbers = [int(match) for match in _QA_HEADING_RE.findall(text)]
    return max(numbers) if numbers else None


def next_qa_number(text: str) -> int:
    """下一个自举条目编号（§4.6）：现有最大号顺延，且不低于 QA-012。

    下限 12 是有意为之：001~011 段位留给人工精挑语料，即使语料文件被清空，
    自举收录也不会挤进人工编号区间（编号在检索注入的 few-shot 里可读、可审计）。
    """
    max_no = parse_max_qa_number(text)
    if max_no is None:
        return AUTO_ABSORB_MIN_NUMBER
    return max(max_no + 1, AUTO_ABSORB_MIN_NUMBER)


def format_qa_entry(number: int, question: str, sql: str, comment: str, day: str) -> str:
    """按 §4.6 模板格式化一条自举条目（与现有 11 条逐字段对齐，切分器零改动）。"""
    note = f"（自举收录 {day}，来源：用户点赞问答）"
    if comment:
        note = f"（自举收录 {day}，来源：用户点赞问答；用户补充：{comment}）"
    return (
        f"### QA-{number:03d}\n"
        f"**问**：{normalize_display(question)}\n"
        "**金标 SQL**：\n"
        "```sql\n"
        f"{sql.strip()}\n"
        "```\n"
        f"**说明**：{note}"
    )


def append_entries(md_text: str, entries: list[str]) -> str:
    """把新条目插到语料文本的追加位置，返回完整新文本（不落盘）。

    追加位置（§4.6 防御逻辑）：文件尾；若最后一个 QA 条目之后出现非 QA 的
    `## ` 章节标题（对应 build_index._split_entries 的终止边界——防止尾注被
    粘进最后一个语料片），则插到该标题之前。条目之间、条目与后续章节之间
    各留一个空行，与现有文件排版一致。
    """
    lines = md_text.splitlines()
    insert_at = len(lines)
    qa_indices = [i for i, line in enumerate(lines) if line.startswith("### QA-")]
    if qa_indices:
        for j in range(qa_indices[-1] + 1, len(lines)):
            if lines[j].startswith("## "):
                insert_at = j
                break
    before = lines[:insert_at]
    after = lines[insert_at:]
    while before and not before[-1].strip():  # 统一由本函数补分隔空行
        before.pop()
    new_lines = [*before, "", *"\n".join(entries).split("\n")]
    if after:
        new_lines += ["", *after]
    return "\n".join(new_lines) + "\n"


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """纯 Python 余弦相似度（本批次内两两判重用；对 Chroma 走库内 cosine）。"""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def qa_label(chunk_id: str) -> str:
    """chunk id → 人类可读编号：qa_pair:qa_003 → QA-003。"""
    return chunk_id.removeprefix("qa_pair:").replace("qa_", "QA-")


# ---------------------------------------------------------------------------
# 六道门槛（§4.3）：全评不短路——dry-run 要给出每一条 G1-G6 的结论
# ---------------------------------------------------------------------------


def load_sql_guard():
    """G4 的 sql_guard 加载：T1 并行开发未完成（模块不存在）时返回 None。

    返回 None 时 G4 输出"G4 跳过（T1 未完成）"并继续——自举预览不能因为
    另一个工单没合码而崩掉；但 --apply 收录前应当确认 T1 已完成（跳过期的
    收录缺一道安全自洽检查）。
    """
    try:
        from agent.tools.sql_guard import validate_readonly_sql
    except ImportError:
        return None
    return validate_readonly_sql


def check_gates(
    sample: Sample,
    corpus_questions: list[tuple[str, str]],
    batch: list[BatchItem],
    deduper: SemanticDeduper | None,
) -> SampleResult:
    """对一条样本跑六道门槛，返回逐条结论（§4.3）。

    deduper：G6 语义层的真实实现（主流程注入）；None = 语义栈未加载
    （测试/降级场景），此时语义判重跳过、只做文本层。
    """
    result = SampleResult(sample=sample)
    sql = sample.generated_sql or ""

    result.outcomes.append(
        GateOutcome("G1", "点赞评分", sample.rating == 1, f"rating={sample.rating}")
    )
    result.outcomes.append(
        GateOutcome("G2", "执行状态", sample.status == 1, f"status={sample.status}")
    )
    result.outcomes.append(
        GateOutcome(
            "G3",
            "SQL 非空",
            bool(sql.strip()),
            f"{len(sql)} 字符" if sql.strip() else "generated_sql 为空（纯聊天回答）",
        )
    )

    validate = load_sql_guard()
    if validate is None:
        result.outcomes.append(
            GateOutcome("G4", "SQL 安全校验", None, "G4 跳过（T1 未完成）——sql_guard 不可导入")
        )
    else:
        ok, reason = validate(sql)
        result.outcomes.append(
            GateOutcome("G4", "SQL 安全校验", ok, reason if not ok else "sql_guard AST 校验通过")
        )

    q_len = len(normalize_display(sample.question))
    sql_len = len(sql)
    if not sample.question.strip():
        g5_ok, g5_detail = False, "问题为空"
    elif not 1 <= q_len <= G5_QUESTION_MAX_CHARS:
        g5_ok = False
        g5_detail = f"问题 {q_len} 字（允许 1~{G5_QUESTION_MAX_CHARS}，超长多为多问复合，示范性差）"
    elif sql_len > G5_SQL_MAX_CHARS:
        g5_ok = False
        g5_detail = f"SQL {sql_len} 字符（上限 {G5_SQL_MAX_CHARS}）"
    else:
        g5_ok = True
        g5_detail = f"问题 {q_len}/{G5_QUESTION_MAX_CHARS} 字，SQL {sql_len}/{G5_SQL_MAX_CHARS} 字符"
    result.outcomes.append(GateOutcome("G5", "长度限制", g5_ok, g5_detail))

    # —— G6 双层去重（§4.4）——
    norm = normalize_question(sample.question)
    text_dup = next(
        ((qa_no, q) for qa_no, q in corpus_questions if normalize_question(q) == norm), None
    )
    batch_text_dup = next((item for item in batch if item.norm_question == norm), None)

    # 语义层：无论文本层是否已判重都算出 top1（dry-run 要展示相似度与命中编号）
    if deduper is not None:
        result.sem_top1_id, result.sem_top1_sim, result.embedding = (
            deduper.search_with_embedding(sample.question)
        )
        result.retrieval_top1_id, result.retrieval_top1_sim = deduper.retrieval_top1(
            sample.question
        )

    g6_ok = True
    g6_parts: list[str] = []
    if text_dup is not None:
        g6_ok = False
        g6_parts.append(f"文本归一后与 {text_dup[0]} 完全相同（{norm}）")
    elif batch_text_dup is not None:
        g6_ok = False
        g6_parts.append(f"文本归一后与本批次样本 historyId={batch_text_dup.history_id} 相同")

    if deduper is None:
        g6_parts.append("语义层未加载，跳过")
    elif result.sem_top1_id is None:
        g6_parts.append("语料无可比对的问字段，语义层跳过")
    else:
        sim_text = f"{result.sem_top1_sim:.4f}"
        if result.sem_top1_sim >= SEMANTIC_DUP_SIMILARITY:
            g6_ok = False
            g6_parts.append(
                f"与 {result.sem_top1_id} 语义相似度 {sim_text} ≥ {SEMANTIC_DUP_SIMILARITY}，判重"
            )
        else:
            g6_parts.append(
                f"语义 top1：{result.sem_top1_id} 相似度 {sim_text}（< {SEMANTIC_DUP_SIMILARITY}）"
            )
        # 本批次内两两判重（§4.4）
        if result.embedding is not None:
            for item in batch:
                if item.embedding is None:
                    continue
                batch_sim = cosine_similarity(result.embedding, item.embedding)
                if batch_sim >= SEMANTIC_DUP_SIMILARITY:
                    g6_ok = False
                    g6_parts.append(
                        f"与本批次样本 historyId={item.history_id} 语义相似度 {batch_sim:.4f}"
                        f" ≥ {SEMANTIC_DUP_SIMILARITY}"
                    )
                    break
    if g6_ok and not g6_parts:
        g6_parts.append("文本与语义均无重复")
    result.outcomes.append(
        GateOutcome("G6", "双层去重", g6_ok, "；".join(g6_parts))
    )
    return result


# ---------------------------------------------------------------------------
# G6 语义层：BgeEmbedder + Chroma（惰性加载，import 本模块零重依赖）
# ---------------------------------------------------------------------------


class SemanticDeduper:
    """G6 语义层（§4.4）。

    判重对象 = 新问题 vs 现有 QA 条目的「问」字段——不是整片文本（§4.4 明文：
    "金标 SQL 与说明会拉低区分度"）。这不是实现取舍而是标定结论，--calibrate
    实测（2026-10-11，bge-small-zh-v1.5，28 片索引）：
    - 整片口径（collection.query，问 vs 问+SQL+说明）：同文问只有 0.67~0.80、
      改写 0.60~0.72、不同题 0.55~0.60——改写与不同题的区间几乎重叠（间隔 0.006），
      任何阈值都分不开，0.92 更是从不触发（语义层形同虚设）；
    - 问字段口径（两侧都用文档侧 embed() 对称编码）：同文 = 1.00、改写
      0.81~0.90、不同题 0.66~0.77——与 §4.4 引用的分布（同文 1.0 / 改写 ≥0.85 /
      不同题 <0.8）一致，0.92 恰落在"改写之上、同文之下"的保守空档。

    阈值 0.92 的语义：宁漏收不错收（改写变体靠人工 dry-run 兜底）；
    文本归一层挡完全同文，语义层挡标点/空白之外的近似复制。

    collection.query（线上检索同款口径，整片向量）保留为 dry-run 的诊断展示：
    看新问题在真实索引里会检索到哪个片——只展示，不参与判重决策。
    """

    TOP_K = 3

    def __init__(self, embedder, collection, corpus_questions: list[tuple[str, str]]) -> None:
        self.embedder = embedder
        self.collection = collection
        self.corpus_questions = corpus_questions
        # 问字段按文档侧嵌入一次、进程内复用（11 条毫秒级；判重两侧同口径才对称）
        self._corpus_vecs = embedder.embed([q for _no, q in corpus_questions]) if corpus_questions else []
        existing = collection.get(where={"doc_type": "qa_pair"}, include=["metadatas"])
        self._qa_count = len(existing["ids"] or [])

    def embedding_of(self, question: str) -> list[float]:
        return self.embedder.embed([question])[0]

    def search_with_embedding(
        self, question: str
    ) -> tuple[str | None, float | None, list[float]]:
        """判重口径 top1：返回 (QA 编号, cosine 相似度, 查询向量)。

        语料没有可比对的问字段时前两者为 None。一次嵌入同时供判重、
        本批次两两判重与展示（check_gates 每条样本只嵌一次）。
        """
        embedding = self.embedding_of(question)
        if not self._corpus_vecs:
            return None, None, embedding
        sims = [cosine_similarity(embedding, vec) for vec in self._corpus_vecs]
        best = max(range(len(sims)), key=lambda i: sims[i])
        return self.corpus_questions[best][0], sims[best], embedding

    def retrieval_top1(self, question: str) -> tuple[str | None, float | None]:
        """诊断展示：整片索引检索 top1（chunk id, 相似度）；与判重决策无关。"""
        if self._qa_count == 0:
            return None, None
        answer = self.collection.query(
            query_embeddings=[self.embedding_of(question)],
            n_results=min(self.TOP_K, self._qa_count),
            where={"doc_type": "qa_pair"},
        )
        ids = answer["ids"][0]
        distances = answer["distances"][0]
        if not ids:
            return None, None
        return ids[0], 1.0 - float(distances[0])

    def top1(self, question: str) -> tuple[str | None, float | None]:
        """判重口径便利封装（--calibrate 用）。"""
        qa_no, similarity, _embedding = self.search_with_embedding(question)
        return qa_no, similarity


def load_deduper(corpus_questions: list[tuple[str, str]]) -> SemanticDeduper:
    """惰性组装语义判重器（fastembed 模型加载约几秒，只在有样本要判时才调）。"""
    import os

    from agent.rag.chroma_store import get_collection
    from agent.rag.embedder import BgeEmbedder

    # 缓存目录与线上 get_retriever 同款（W4 部署实录：%TEMP% 缓存不可靠）
    embedder = BgeEmbedder(
        model_name=EMBEDDING_MODEL,
        cache_dir=os.environ.get("FASTEMBED_CACHE_DIR") or None,
    )
    return SemanticDeduper(embedder, get_collection(), corpus_questions)


# ---------------------------------------------------------------------------
# 样本拉取（§4.2：Java 内部接口，X-Internal-Token）
# ---------------------------------------------------------------------------


def fetch_samples(server: str) -> list[Sample]:
    """从 Java 拉点赞样本；连接/HTTP/业务码异常都给中文指引后退出。"""
    import httpx

    headers = {}
    if INTERNAL_TOKEN:
        headers["X-Internal-Token"] = INTERNAL_TOKEN
    url = server.rstrip("/") + f"/api/internal/feedback/samples?limit={SAMPLES_LIMIT}"
    try:
        response = httpx.get(url, headers=headers, timeout=10.0)
    except httpx.HTTPError as exc:
        print(f"拉取样本失败（{url}）：{exc}")
        print("排查提示：Java 网关是否已启动？生产在 127.0.0.1:8081；")
        print("开发机 mvn spring-boot:run 默认 8080，用 --server 指定实际端口。")
        sys.exit(1)
    if response.status_code != 200:
        print(f"拉取样本失败：HTTP {response.status_code}（{response.text[:200]}）")
        sys.exit(1)
    payload = response.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        print(f"响应格式不符合预期（缺少 data 列表）：{str(payload)[:200]}")
        sys.exit(1)
    return [Sample.from_api(row) for row in data]


# ---------------------------------------------------------------------------
# 输出与主流程
# ---------------------------------------------------------------------------


def _fmt_outcome(outcome: GateOutcome) -> str:
    if outcome.ok is None:
        mark = "跳过"
    elif outcome.ok:
        mark = "通过"
    else:
        mark = "不通过"
    return f"  {outcome.gate} {outcome.name}  {mark} —— {outcome.detail}"


def print_report(results: list[SampleResult], corpus_count: int, mode: str) -> None:
    print(f"=== 反馈自举预览（{mode}）===")
    print(
        f"样本 {len(results)} 条 ｜ 现有 QA 语料 {corpus_count} 条"
        f" ｜ 语义判重阈值 {SEMANTIC_DUP_SIMILARITY}"
    )
    if not results:
        print("没有可收录的样本（feedback 里还没有 rating=1 且执行成功的记录），语料未改动。")
        return
    for index, result in enumerate(results, 1):
        sample = result.sample
        print(f"\n[{index}/{len(results)}] historyId={sample.history_id} ｜ {sample.created_at}")
        print(f"  问题：{normalize_display(sample.question) or '（空）'}")
        if sample.comment:
            print(f"  评论：{sample.comment}")
        for outcome in result.outcomes:
            print(_fmt_outcome(outcome))
        if result.retrieval_top1_id is not None:
            # 诊断展示（不参与判重）：新问题在真实索引里的整片检索 top1
            print(
                f"  索引检索 top1（诊断，不参与判重）：{qa_label(result.retrieval_top1_id)}"
                f" 整片相似度 {result.retrieval_top1_sim:.4f}"
            )
        if result.kept:
            print("  结论：保留（--apply 时将按保留顺序编号）")
        else:
            print(f"  结论：落选（{'、'.join(result.failed_gates)} 不通过）")


def run_collect(args: argparse.Namespace) -> None:
    """--dry-run / --apply 主流程（§4.3-§4.6）。"""
    mode = "--apply" if args.apply else "--dry-run"
    samples = fetch_samples(args.server)
    corpus_text = ""
    if QA_PAIRS_PATH.is_file():
        corpus_text = QA_PAIRS_PATH.read_text(encoding="utf-8-sig")
    else:
        print(f"警告：未找到语料文件 {QA_PAIRS_PATH}，将按空语料处理。")
    corpus_questions = parse_corpus_questions(corpus_text)

    # 空样本集不加载 embedding 模型（几秒的模型加载没必要为 0 条样本付出）
    deduper = load_deduper(corpus_questions) if samples else None

    results: list[SampleResult] = []
    batch: list[BatchItem] = []
    for sample in samples:
        result = check_gates(sample, corpus_questions, batch, deduper)
        results.append(result)
        if result.kept:
            batch.append(
                BatchItem(sample.history_id, normalize_question(sample.question), result.embedding)
            )

    print_report(results, len(corpus_questions), mode)
    kept = [r for r in results if r.kept]
    failed: dict[str, int] = {}
    for result in results:
        for gate in result.failed_gates:
            failed[gate] = failed.get(gate, 0) + 1
    failed_text = " ｜ ".join(f"{gate}: {count}" for gate, count in sorted(failed.items())) or "无"
    print(f"\n=== 汇总 ===\n保留 {len(kept)} ｜ 落选 {len(results) - len(kept)}（{failed_text}）")

    if not args.apply:
        if kept:
            start = next_qa_number(corpus_text)
            numbers = "、".join(f"QA-{start + i:03d}" for i in range(len(kept)))
            print(f"--dry-run 未写文件。确认无误后用 --apply 收录（拟编号 {numbers}）。")
        return
    if not kept:
        print("没有通过全部门槛的样本，语料文件未改动。")
        return

    # 本机时区的当天日期（与 run_eval._local_now 同款取法），进「说明」行的自举来源标注
    day = datetime.now().astimezone().date().isoformat()
    start = next_qa_number(corpus_text)
    entries = [
        format_qa_entry(start + offset, r.sample.question, r.sample.generated_sql, r.sample.comment, day)
        for offset, r in enumerate(kept)
    ]
    new_text = append_entries(corpus_text, entries)
    QA_PAIRS_PATH.write_text(new_text, encoding="utf-8")
    numbers = "、".join(f"QA-{start + offset:03d}" for offset in range(len(kept)))
    print(f"已追加 {len(kept)} 条（{numbers}）→ data/rag_corpus/qa_pairs.md")
    print("请运行 python -m agent.rag.build_index 重建索引；")
    print("随后 git commit 语料（版本化回滚点），服务器同步见 deploy/absorb_corpus.sh。")


def _run_calibrate() -> None:
    """--calibrate：去重阈值标定（§8-13）。

    现有 11 问（top1 应≈自身）+ 5 个同义改写 + 5 个不同题，对两个口径各看 top1 分布：
    - 判重口径（问字段 vs 问字段，决策依据）：确认 SEMANTIC_DUP_SIMILARITY=0.92
      是否落在"改写之上、同文之下"的保守空档；
    - 索引整片口径（collection.query，问 vs 问+SQL+说明）：诊断参考——实测两区间
      几乎重叠，这也是判重不采用它的依据（结论记 experiment-log）。
    """
    if not QA_PAIRS_PATH.is_file():
        print(f"未找到语料文件：{QA_PAIRS_PATH}")
        sys.exit(1)
    corpus_questions = parse_corpus_questions(QA_PAIRS_PATH.read_text(encoding="utf-8-sig"))
    if not corpus_questions:
        print("qa_pairs.md 里没有解析到 QA 条目，无法标定。")
        sys.exit(1)
    deduper = load_deduper(corpus_questions)
    if deduper._qa_count == 0:
        print("警告：Chroma 索引里没有 qa_pair 片（先跑 python -m agent.rag.build_index），"
              "索引整片口径的参考列将留空。")

    rows: list[tuple[str, str, str, str, float | None, str, float | None]] = []
    groups = (
        ("现有", [(qa_no, question) for qa_no, question in corpus_questions]),
        ("改写", [(f"P{idx}", q) for idx, q in enumerate(_CALIBRATE_PARAPHRASES, 1)]),
        ("不同题", [(f"D{idx}", q) for idx, q in enumerate(_CALIBRATE_DIFFERENT, 1)]),
    )
    for group, questions in groups:
        for label, question in questions:
            qa_no, qa_sim = deduper.top1(question)
            chunk_id, idx_sim = deduper.retrieval_top1(question)
            rows.append((group, label, question, qa_no or "-", qa_sim,
                         chunk_id or "-", idx_sim))

    print("=== 去重阈值标定（§8-13）：现有 11 问 + 5 改写 + 5 不同题，top1 cosine ===")
    print("判重口径 = 新问题 vs 现有条目「问」字段（决策依据）；索引口径 = collection.query 整片（诊断参考）")
    print(f"{'组':<4} {'编号':<5} {'判重sim':>8} {'distance':>9} {'top1':<8} {'索引sim':>8} {'top1片':<16} 问题")
    for group, label, question, qa_no, qa_sim, chunk_id, idx_sim in rows:
        qa_text = f"{qa_sim:.4f}" if qa_sim is not None else "-"
        dist_text = f"{1.0 - qa_sim:.4f}" if qa_sim is not None else "-"
        idx_text = f"{idx_sim:.4f}" if idx_sim is not None else "-"
        print(
            f"{group:<4} {label:<5} {qa_text:>8} {dist_text:>9} {qa_no:<8} "
            f"{idx_text:>8} {chunk_id:<16} {question[:26]}"
        )

    def _span(group: str) -> tuple[float, float] | None:
        values = [row[4] for row in rows if row[0] == group and row[4] is not None]
        return (min(values), max(values)) if values else None

    span_same = _span("现有")
    span_para = _span("改写")
    span_diff = _span("不同题")
    threshold = SEMANTIC_DUP_SIMILARITY
    print("\n=== 分布小结（判重口径）===")
    if span_same:
        print(f"现有 {len(corpus_questions)} 问：相似度 {span_same[0]:.4f}~{span_same[1]:.4f}"
              "（top1 应≈自身，同文应≈1.0）")
    if span_para:
        blocked = sum(1 for row in rows if row[0] == "改写"
                      and row[4] is not None and row[4] >= threshold)
        print(f"改写 5 条：相似度 {span_para[0]:.4f}~{span_para[1]:.4f}"
              f"；当前阈值拦下 {blocked}/5（未拦下的=漏收，保守侧可接受，人工 dry-run 兜底）")
    if span_diff:
        mistaken = sum(1 for row in rows if row[0] == "不同题"
                       and row[4] is not None and row[4] >= threshold)
        print(f"不同题 5 条：相似度 {span_diff[0]:.4f}~{span_diff[1]:.4f}"
              f"；当前阈值误拦 {mistaken}/5（应恒为 0，否则阈值过松在错杀新题）")
    if span_para and span_diff:
        if span_diff[1] < span_para[0]:
            print(
                f"空档：不同题最大 {span_diff[1]:.4f} < 改写最小 {span_para[0]:.4f}——"
                f"阈值可落在 [{span_diff[1]:.2f}, {span_para[0]:.2f}]；"
                f"当前 {threshold} 取保守侧（宁漏收不错收，§4.4）"
            )
        else:
            print("改写与不同题的相似度区间有重叠，空档不存在——维持保守阈值并依赖人工过目。")
    print("\n调整阈值：改 agent/rag/absorb_feedback.py 的 SEMANTIC_DUP_SIMILARITY，结果记 experiment-log。")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m agent.rag.absorb_feedback",
        description="反馈自举闭环：拉取点赞样本 → 六道门槛 + 双层去重 → dry-run 预览 / apply 收录进 qa_pairs.md",
    )
    trigger = parser.add_mutually_exclusive_group()
    trigger.add_argument("--dry-run", dest="apply", action="store_false",
                         help="只预览不写文件（默认行为，§4.5 人工质量闸）")
    trigger.add_argument("--apply", action="store_true",
                         help="把通过全部门槛的样本追加进 qa_pairs.md（QA-012 起编号）")
    parser.add_argument("--server", default=DEFAULT_SERVER_URL,
                        help=f"Java 网关地址（默认 {DEFAULT_SERVER_URL}；开发机联调按实际端口传）")
    parser.add_argument("--calibrate", action="store_true",
                        help="去重阈值标定：11 问 + 10 变体的 top1 距离分布（不需要 Java 在跑）")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if args.calibrate:
        _run_calibrate()
        return
    run_collect(args)


if __name__ == "__main__":
    main()

"""prepare_turn 同构闸门测试（W4 设计 §6.2 闸门第一层，W3 §2.3 契约的延续）。

三道固定题 EV-003 / EV-017 / EV-029（覆盖陷阱 / 多表 / 口径）的对照基准取自
W3 明细 `eval/reports/detail_baseline_full_v2_20261010_161633.json`（run_idx=1）：
- 单轮评测线程恒新 → previous_human_message 返回空串 → retrieval_query == question
  （W3 §2.3 [2] 的既定语义，重构前后都必须如此）；
- injected_chunks 由假检索器按 W3 注入顺序原样播种 → prepare_turn 必须原样透传
  （不重排、不丢弃——注入顺序就是 prompt 里的编号顺序）；
- 捕获的 system_prompt 与"内联参考实现"（同一批 format_context_block /
  build_system_prompt 纯函数直接拼装）逐字节相等。

本层用 mock retriever 隔离检索不确定性；**完整闸门**（真实检索 vs W3 JSON 逐元素
全等）由仓库根的 verify_isomorphism.py / 主 Agent 用真实索引复验（§6.2-1），
prompt 快照断言（§6.2-2）在末节永久防漂移。

默认套件零环境依赖：检索器 / checkpointer / build_agent 全部被替换，
不加载 fastembed、不连 MySQL、不构造真实 ChatDeepSeek。
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage

from agent.config import RETRIEVER_TOP_N
from agent.core import turn as turn_mod
from agent.core.agent import (
    RetrievedChunk,
    build_system_prompt,
    format_context_block,
)
from eval.judge import join_retrieval_query

REPO_ROOT = Path(__file__).resolve().parents[1]
W3_BASELINE = REPO_ROOT / "eval" / "reports" / "detail_baseline_full_v2_20261010_161633.json"
SNAPSHOT_FILE = REPO_ROOT / "tests" / "snapshots" / "system_prompt_snapshot.txt"

# §6.2-1 固定题（覆盖陷阱 / 多表 / 口径）
GATE_CASE_IDS = ("EV-003", "EV-017", "EV-029")


def _load_w3_records() -> dict[str, dict]:
    """W3 明细 → {case_id: record}（取 run_idx=1 的那次运行）。"""
    detail = json.loads(W3_BASELINE.read_text(encoding="utf-8"))
    return {c["id"]: c for c in detail["cases"] if c["id"] in GATE_CASE_IDS and c["run_idx"] == 1}


def _chunks_from_ids(ids: list[str]) -> list[RetrievedChunk]:
    """按 W3 注入顺序构造检索片：doc_type 取 id 前缀，标题/正文确定性生成。"""
    type_labels = {"schema": "表结构", "glossary": "业务口径", "qa_pair": "示例问答"}
    return [
        RetrievedChunk(
            id=cid,
            doc_type=cid.split(":", 1)[0],
            title=f"{type_labels.get(cid.split(':', 1)[0], '资料')}·{cid}",
            text=f"【{cid} 的确定性测试正文】",
            rrf_score=0.01,
        )
        for cid in ids
    ]


# ---------------------------------------------------------------------------
# 假检索器 / 假 checkpointer / 假 build_agent（隔离一切重型依赖）
# ---------------------------------------------------------------------------


class FakeRetriever:
    """按预播种顺序返回 chunks，并记录每次 search 的 (query, top_n)。

    签名带 W6 消融新参（默认值与真实 search 同款）——参数化分支的透传测试
    复用本类时不会 TypeError；默认分支的既有断言仍只看 (query, top_n)。
    """

    def __init__(self, chunks: list[RetrievedChunk]):
        self._chunks = chunks
        self.calls: list[tuple[str, int]] = []
        self.kwargs_calls: list[dict] = []  # 全量 kwargs（参数化分支断言面）

    def search(
        self,
        query: str,
        doc_types=None,
        top_k_each: int = 20,
        top_n: int = 8,
        enable_bm25: bool = True,
        enable_dense: bool = True,
        reranker=None,
        rerank_pool: int = 0,
    ):
        self.calls.append((query, top_n))
        self.kwargs_calls.append(
            {
                "query": query,
                "doc_types": doc_types,
                "top_k_each": top_k_each,
                "top_n": top_n,
                "enable_bm25": enable_bm25,
                "enable_dense": enable_dense,
                "reranker": reranker,
                "rerank_pool": rerank_pool,
            }
        )
        return list(self._chunks)


@dataclass
class _FakeTuple:
    """checkpoint_tuple 的最小替身（previous_human_message 只读这两个字段）。"""

    checkpoint: dict


@dataclass
class FakeCheckpointer:
    """可编程的上一条用户消息：None=无历史（首轮），否则返回给定 HumanMessage。"""

    previous: str | None = None
    seen_thread_ids: list = field(default_factory=list)

    def get_tuple(self, config: dict):
        self.seen_thread_ids.append(config["configurable"]["thread_id"])
        if self.previous is None:
            return None
        return _FakeTuple(
            checkpoint={"channel_values": {"messages": [HumanMessage(content=self.previous)]}}
        )


@pytest.fixture()
def gate_env(monkeypatch):
    """替换 turn 模块的三个依赖点；返回 (build_agent 捕获列表, checkpointer)。"""
    captured: list[dict] = []
    checkpointer = FakeCheckpointer()

    def fake_build_agent(system_prompt: str, api_key: str | None = None, hitl: bool = False):
        captured.append({"system_prompt": system_prompt, "api_key": api_key, "hitl": hitl})
        return object()  # agent 本体不被本层消费

    monkeypatch.setattr(turn_mod, "get_retriever", lambda: retriever_holder["retriever"])
    monkeypatch.setattr(turn_mod, "get_checkpointer", lambda: checkpointer)
    monkeypatch.setattr(turn_mod, "build_agent", fake_build_agent)

    retriever_holder: dict = {"retriever": None}
    return {"captured": captured, "checkpointer": checkpointer, "retriever_holder": retriever_holder}


# ---------------------------------------------------------------------------
# §6.2-1 第一层：三道固定题 prepare_turn == 内联参考实现
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case_id", GATE_CASE_IDS)
def test_gate_prepare_turn_matches_w3_and_inline_reference(case_id, gate_env):
    """三固定题：注入顺序透传 + query 拼接规则 + prompt 与内联实现逐字节相等。"""
    record = _load_w3_records()[case_id]
    question = _question_of(case_id)
    chunks = _chunks_from_ids(record["injected_chunks"])
    retriever = FakeRetriever(chunks)
    gate_env["retriever_holder"]["retriever"] = retriever
    gate_env["checkpointer"].previous = None  # 单轮评测线程恒新（W3 §2.3 [1]）

    thread_id = f"eval-{case_id}-r1"
    pt = turn_mod.prepare_turn(question, thread_id)

    # [2] 首轮无历史：retrieval_query == question 本身（W3 明细的既定语义）
    assert pt.retrieval_query == question
    # [3] 注入逐元素相等：顺序、id 都不许动（prompt 编号依赖这个顺序）
    assert [c.id for c in pt.injected_chunks] == record["injected_chunks"]
    # [4] prompt 与内联参考实现逐字节相等（同一批纯函数直接拼装）
    reference_prompt = build_system_prompt(format_context_block(chunks))
    assert gate_env["captured"][-1]["system_prompt"] == reference_prompt
    # CLI/评测路径不传 key：api_key=None 走 env 分支
    assert gate_env["captured"][-1]["api_key"] is None
    # config 就是 checkpointer 的最终键（thread_key 原样进入）
    assert pt.config == {"configurable": {"thread_id": thread_id}}
    assert gate_env["checkpointer"].seen_thread_ids == [thread_id]
    # 默认注入条数 = RETRIEVER_TOP_N（W3 同款检索参数）
    assert retriever.calls == [(question, RETRIEVER_TOP_N)]


def _question_of(case_id: str) -> str:
    """从评测集读固定题的问题原文（同构闸门只认题号+原文，不硬编码中文副本）。"""
    dataset = REPO_ROOT / "eval" / "dataset" / "eval_v1.jsonl"
    for line in dataset.read_text(encoding="utf-8").splitlines():
        case = json.loads(line)
        if case["id"] == case_id:
            return case["question"]
    raise AssertionError(f"固定题 {case_id} 不在评测集里")


# ---------------------------------------------------------------------------
# 拼接规则 / 命名空间 / key 透传 / top_n 透传
# ---------------------------------------------------------------------------


def test_multiturn_join_rule_matches_w3_helper(gate_env):
    """多轮拼接规则：与 W3 已实测的 join_retrieval_query 逐字符同款。"""
    retriever = FakeRetriever([])
    gate_env["retriever_holder"]["retriever"] = retriever
    previous, question = "上个月的 GMV 是多少？", "那 8 月的退款率呢"
    gate_env["checkpointer"].previous = previous

    pt = turn_mod.prepare_turn(question, "cli-thread-1")

    assert pt.retrieval_query == join_retrieval_query(previous, question)
    assert pt.retrieval_query == f"{previous}\n{question}"


def test_fastapi_thread_key_namespace_passthrough(gate_env):
    """FastAPI 路径：thread_key = f"u{userId}:{threadId}" 原样成为 checkpointer 键。"""
    gate_env["retriever_holder"]["retriever"] = FakeRetriever([])
    pt = turn_mod.prepare_turn("问题", "u42:8f3a1111-2222-3333-4444-555555555555")
    assert pt.config == {"configurable": {"thread_id": "u42:8f3a1111-2222-3333-4444-555555555555"}}


def test_api_key_passthrough_to_build_agent(gate_env):
    """BYOK：api_key 原样传给 build_agent；不传恒为 None。"""
    gate_env["retriever_holder"]["retriever"] = FakeRetriever([])
    turn_mod.prepare_turn("问题", "t1", api_key="sk-user-key")
    assert gate_env["captured"][-1]["api_key"] == "sk-user-key"
    turn_mod.prepare_turn("问题", "t2")
    assert gate_env["captured"][-1]["api_key"] is None


def test_top_n_override_and_default(gate_env):
    """top_n 透传（评测 --top-n 消融入口）；缺省用 RETRIEVER_TOP_N。"""
    gate_env["retriever_holder"]["retriever"] = FakeRetriever([])
    turn_mod.prepare_turn("问题", "t1", top_n=0)
    turn_mod.prepare_turn("问题", "t2")
    retriever = gate_env["retriever_holder"]["retriever"]
    assert retriever.calls == [("问题", 0), ("问题", RETRIEVER_TOP_N)]


# ---------------------------------------------------------------------------
# §6.2-2：prompt 快照（固定 chunks → 完整 system_prompt，永久防漂移）
# ---------------------------------------------------------------------------

# 快照固定输入：三种 doc_type 各一片，覆盖全部类型标签与编号规则
_SNAPSHOT_CHUNKS = [
    RetrievedChunk(
        id="schema:payments",
        doc_type="schema",
        title="支付流水表 payments",
        text="CREATE TABLE payments (\n  id BIGINT PRIMARY KEY,\n  order_id BIGINT,\n  amount DECIMAL(10,2),\n  paid_at DATETIME\n);",
        rrf_score=0.01,
    ),
    RetrievedChunk(
        id="glossary:gmv",
        doc_type="glossary",
        title="GMV（成交总额）",
        text="GMV = 统计期内 payments 表支付成功金额（amount）的合计，按 paid_at 归期。",
        rrf_score=0.01,
    ),
    RetrievedChunk(
        id="qa_pair:qa_003",
        doc_type="qa_pair",
        title="上个月的GMV是多少？",
        text="### QA-003\n问：上个月的GMV是多少？\nSQL：SELECT SUM(amount) FROM payments WHERE paid_at >= ... ;",
        rrf_score=0.01,
    ),
]


def test_prompt_snapshot_against_committed_file():
    """build_system_prompt(format_context_block(固定chunks)) 与快照文件逐字节相等。

    快照生成方式（变更快照必须显式重录并评审 diff）：
        python -c "from tests.test_turn_isomorphism import _SNAPSHOT_CHUNKS, SNAPSHOT_FILE;
                   from agent.core.agent import build_system_prompt, format_context_block;
                   SNAPSHOT_FILE.write_text(build_system_prompt(format_context_block(_SNAPSHOT_CHUNKS)), encoding='utf-8')"
    """
    expected = SNAPSHOT_FILE.read_text(encoding="utf-8")
    actual = build_system_prompt(format_context_block(_SNAPSHOT_CHUNKS))
    assert actual == expected


# ---------------------------------------------------------------------------
# W6 消融入口（设计 §3.4）：retrieval_opts 透传 + 默认路径同构 + get_retriever(k) 缓存
# ---------------------------------------------------------------------------


def test_retrieval_opts_none_calls_bare_get_retriever(monkeypatch, gate_env):
    """同构红线的显式锁：opts=None 时 get_retriever 必须无参调用——
    本测试的替身函数签名一个参数都不收，带参调用立刻 TypeError。"""
    def bare_get_retriever():  # 无参签名：retrieval_opts 分支若误入，这里直接炸
        return gate_env["retriever_holder"]["retriever"]

    monkeypatch.setattr(turn_mod, "get_retriever", bare_get_retriever)
    gate_env["retriever_holder"]["retriever"] = FakeRetriever([])
    pt = turn_mod.prepare_turn("问题", "t-iso")
    assert pt.retrieval_query == "问题"
    assert gate_env["retriever_holder"]["retriever"].calls == [("问题", RETRIEVER_TOP_N)]


def test_retrieval_opts_passthrough_full_kwargs(monkeypatch, gate_env):
    """参数化分支：opts 逐字段透传 search；rrf_k 透传 get_retriever（§3.4）。"""
    k_seen: list = []

    def get_retriever_with_k(k=None):  # 与真实签名同款（k=None 默认）
        k_seen.append(k)
        return gate_env["retriever_holder"]["retriever"]

    monkeypatch.setattr(turn_mod, "get_retriever", get_retriever_with_k)
    retriever = FakeRetriever([])
    gate_env["retriever_holder"]["retriever"] = retriever

    reranker = object()  # 透传即可见，钩子行为由 test_rag_retriever.py 覆盖
    opts = turn_mod.RetrievalOptions(
        rrf_k=10,
        doc_types=["schema", "glossary"],
        enable_bm25=False,
        enable_dense=True,
        rerank_pool=16,
        reranker=reranker,
    )
    turn_mod.prepare_turn("问题", "t-ablation", retrieval_opts=opts)

    assert k_seen == [10]
    kwargs = retriever.kwargs_calls[-1]
    assert kwargs == {
        "query": "问题",
        "doc_types": ["schema", "glossary"],
        "top_k_each": 20,  # 未透传 → search 默认值（与线上同款）
        "top_n": RETRIEVER_TOP_N,
        "enable_bm25": False,
        "enable_dense": True,
        "reranker": reranker,
        "rerank_pool": 16,
    }


def test_retrieval_opts_respects_explicit_top_n(monkeypatch, gate_env):
    """opts 与 top_n 并用：--top-n 0（A 行）+ 消融开关的组合下 top_n 优先透传。"""
    monkeypatch.setattr(turn_mod, "get_retriever", lambda k=None: gate_env["retriever_holder"]["retriever"])
    retriever = FakeRetriever([])
    gate_env["retriever_holder"]["retriever"] = retriever
    opts = turn_mod.RetrievalOptions(rrf_k=10)
    turn_mod.prepare_turn("问题", "t-mix", top_n=0, retrieval_opts=opts)
    assert retriever.kwargs_calls[-1]["top_n"] == 0


def test_retrieval_opts_defaults_are_online_defaults(monkeypatch, gate_env):
    """RetrievalOptions 裸构造 = 线上默认（rrf_k=None/双路开/不重排）——
    dataclass 默认值与 hybrid_retriever.search 默认值的一致性锁。"""
    opts = turn_mod.RetrievalOptions()
    assert opts.rrf_k is None
    assert opts.doc_types is None
    assert opts.enable_bm25 is True
    assert opts.enable_dense is True
    assert opts.rerank_pool == 0
    assert opts.reranker is None


def test_get_retriever_caches_by_k(monkeypatch):
    """get_retriever(k=) 单例缓存（设计 §8-7）：None=默认 60 实例；异 k 另建、
    同 k 复用。假 collection/embedder 隔离 fastembed 与 Chroma。"""
    import agent.core.agent as agent_mod

    built: list = []

    class FakeHybridRetriever:
        def __init__(self, collection, embedder, k: int = 60) -> None:
            self.collection, self.embedder, self.k = collection, embedder, k
            built.append(self)

    monkeypatch.setattr(agent_mod, "HybridRetriever", FakeHybridRetriever)
    monkeypatch.setattr(agent_mod, "get_collection", lambda: object())
    monkeypatch.setattr(agent_mod, "BgeEmbedder", lambda model_name, cache_dir=None: object())
    monkeypatch.setattr(agent_mod, "_retrievers", {})  # 隔离模块级缓存

    default_first = agent_mod.get_retriever()
    default_again = agent_mod.get_retriever()
    assert default_first is default_again  # 同键（None）复用
    assert len(built) == 1
    assert default_first.k == 60  # None → HybridRetriever 默认 k（构造路径与现行同款）

    k10_first = agent_mod.get_retriever(k=10)
    k10_again = agent_mod.get_retriever(k=10)
    assert k10_first is k10_again  # 同 k 复用
    assert k10_first is not default_first  # 异 k 重建
    assert k10_first.k == 10
    assert len(built) == 2

    assert agent_mod.get_retriever() is default_first  # 默认实例不被异 k 构建挤掉

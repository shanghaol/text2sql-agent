"""一轮对话的公共前半程（W4 设计 §3.1）：检索 query 拼接 → 混合检索 → prompt 组装 → build_agent。

**同构红线**：本函数是 CLI / FastAPI / 评测三方共用的唯一组装点
（W3 §2.3 的"main ↔ eval 逐点对照"升级为"三方向 prepare_turn 看齐"）。
语义与 W3 main.py 的内联组装段**逐行等价**——只移动代码、不改行为：
previous_human_message 拼接规则、RETRIEVER_TOP_N、兜底文案路径全部不变。
核心行为变更必须有对照验收（W4 设计 §6.2：3 道固定题 injected_chunks 与
W3 明细 JSON 逐元素相等 + prompt 快照测试）。
"""

from dataclasses import dataclass

from agent.config import RETRIEVER_TOP_N
from agent.core.agent import (
    RetrievedChunk,
    build_agent,
    build_system_prompt,
    format_context_block,
    get_checkpointer,
    get_retriever,
    previous_human_message,
)


@dataclass
class RetrievalOptions:
    """W6 消融入口（设计 §3.4）：一轮对话的检索参数收口。

    全部字段带线上默认值；prepare_turn 只在收到非 None 的本对象时走参数化分支
    ——None（CLI / FastAPI / 评测默认行）时组装路径与现行逐字节等价（同构红线，
    W4 §6.2 三题同构验收覆盖）。reranker 是 SiliconFlowReranker 或同契约 mock
    （.rerank(query, documents, top_n) -> [(index, score), ...]），仅评测 runner
    构建，线上永不传入（设计 §4.4：rerank 只在评测侧启用）。
    """

    rrf_k: int | None = None  # None=检索器默认 60；评测 --rrf-k 传入（E 行）
    doc_types: list[str] | None = None  # None=全类型；--no-fewshot 传 ["schema","glossary"]（G 行）
    enable_bm25: bool = True  # False=纯向量（B 行）
    enable_dense: bool = True  # False=纯 BM25（C 行）
    rerank_pool: int = 0  # >0 且 reranker 存在才开重排钩子（F 行 pool=16）
    reranker: object | None = None  # SiliconFlowReranker 实例；None=不重排
    quota: bool = False  # W7：doc_type 覆盖配额（FQ 行 --rerank --quota）；需重排钩子在场


@dataclass
class PreparedTurn:
    """一轮对话组装完成后的全部产物：调用方各取所需进入后半程。"""

    agent: object  # build_agent 产物（CompiledStateGraph：.stream / .invoke / .get_state）
    config: dict  # {"configurable": {"thread_id": ...}}，stream/invoke 原样传入
    injected_chunks: list[RetrievedChunk]  # 本轮注入 prompt 的检索片（评测明细的同构对照面）
    retrieval_query: str  # 实际用于检索的 query（= 上一条用户消息 + 当前输入）


def prepare_turn(
    question: str,
    thread_key: str,
    api_key: str | None = None,
    top_n: int | None = None,
    hitl: bool = False,
    retrieval_opts: RetrievalOptions | None = None,
) -> PreparedTurn:
    """一轮对话的公共前半程：检索 query 拼接 → 混合检索 → prompt 组装 → build_agent。

    调用方（三选一）：
      - agent/main.py         CLI：prepare_turn() + agent.stream(...)   逐 chunk 打印
      - agent/server.py       FastAPI：prepare_turn() + stream → SSE 事件
      - eval/run_eval.py      评测：prepare_turn() + agent.invoke(...) 拿全量消息

    thread_key 是 checkpointer 的最终键：CLI 传 uuid 原文，FastAPI 传
    f"u{{userId}}:{threadId}"（按用户隔离命名空间，W4 设计 §1.7），
    评测传 f"eval-{case}-r{run}"——与 W3 现状一致。

    api_key：BYOK 每请求 key（FastAPI 路径）；None 时 build_agent 走环境变量
    回退，构造路径与 W3 逐字节等价（CLI/评测不传即是）。

    top_n：检索注入条数，None（默认）用 config.RETRIEVER_TOP_N——CLI 与
    FastAPI 都不传此参；仅评测 runner 把 --top-n 消融入口透传进来
    （W3 的 --top-n 0 对照行 / W6 消融依赖它，属 runner 既有能力非新行为）。

    hitl：W5 §3.8——开关是请求域的（FastAPI 从 X-User-HITL 头读出后透传），
    默认 False 时 build_agent 不传 middleware 参数，CLI/评测组装路径零变化。
    注意：resume 轮不走本函数（W5 §3.5 恢复轮组装口径——不重跑检索，
    直接 build_agent(build_system_prompt(entry.context_block)) 后 Command(resume)）。

    retrieval_opts：W6 消融入口（设计 §3.4）。**同构红线：None（默认）时本函数
    的检索调用与 W4/W5 现行逐字节等价**——get_retriever() 无参调用、search 只传
    top_n；只有评测 runner 显式传入时才走参数化分支（get_retriever(rrf_k) +
    search 全参透传，reranker/quota 仅此路径可达——线上不接线，设计 §4.4）。
    """
    checkpointer = get_checkpointer()
    previous_question = previous_human_message(checkpointer, {"configurable": {"thread_id": thread_key}})
    retrieval_query = f"{previous_question}\n{question}".strip() if previous_question else question
    effective_top_n = top_n if top_n is not None else RETRIEVER_TOP_N
    if retrieval_opts is None:
        # 默认分支：与 W4/W5 现行逐字节等价（get_retriever 无参、search 仅 top_n）
        chunks = get_retriever().search(retrieval_query, top_n=effective_top_n)
    else:
        chunks = get_retriever(retrieval_opts.rrf_k).search(
            retrieval_query,
            doc_types=retrieval_opts.doc_types,
            top_n=effective_top_n,
            enable_bm25=retrieval_opts.enable_bm25,
            enable_dense=retrieval_opts.enable_dense,
            reranker=retrieval_opts.reranker,
            rerank_pool=retrieval_opts.rerank_pool,
            quota=retrieval_opts.quota,
        )
    agent = build_agent(build_system_prompt(format_context_block(chunks)), api_key=api_key, hitl=hitl)
    return PreparedTurn(agent, {"configurable": {"thread_id": thread_key}}, chunks, retrieval_query)

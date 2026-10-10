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
class PreparedTurn:
    """一轮对话组装完成后的全部产物：调用方各取所需进入后半程。"""

    agent: object  # build_agent 产物（CompiledStateGraph：.stream / .invoke / .get_state）
    config: dict  # {"configurable": {"thread_id": ...}}，stream/invoke 原样传入
    injected_chunks: list[RetrievedChunk]  # 本轮注入 prompt 的检索片（评测明细的同构对照面）
    retrieval_query: str  # 实际用于检索的 query（= 上一条用户消息 + 当前输入）


def prepare_turn(
    question: str, thread_key: str, api_key: str | None = None, top_n: int | None = None
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
    """
    checkpointer = get_checkpointer()
    previous_question = previous_human_message(checkpointer, {"configurable": {"thread_id": thread_key}})
    retrieval_query = f"{previous_question}\n{question}".strip() if previous_question else question
    chunks = get_retriever().search(retrieval_query, top_n=top_n if top_n is not None else RETRIEVER_TOP_N)
    agent = build_agent(build_system_prompt(format_context_block(chunks)), api_key=api_key)
    return PreparedTurn(agent, {"configurable": {"thread_id": thread_key}}, chunks, retrieval_query)

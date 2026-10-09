"""打印一次（或多轮）提问的完整消息链，看清 Agent 循环的每一步（教学文档 3.4）。

用法（必须用 venv 里的 python，且在仓库根目录运行）：
    .venv/Scripts/python inspect_chain.py                      # 默认演示问题
    .venv/Scripts/python inspect_chain.py "问题1" "问题2"      # 多轮对话，共用一个 thread_id

W2 起：每轮先做检索注入（检索 query = 上一条用户消息 + 当前问题，设计 §3.6），
用检索结果动态拼 system_prompt 重建 agent——与 main.py 同一套流程；
checkpointer 是模块级单例，多轮问共用一个 thread_id 时第二问能拿到上下文。
多轮时只打印每轮"新增"的消息——历史部分上一轮已经打印过，不重复刷屏。
注意：必须放仓库根目录运行，放子目录会 import 不到 agent 包。
"""

import sys

from agent.config import RETRIEVER_TOP_N
from agent.core.agent import (
    build_agent,
    build_system_prompt,
    format_context_block,
    get_checkpointer,
    get_retriever,
    previous_human_message,
)


def main() -> None:
    questions = sys.argv[1:] or ["上个月的 GMV 是多少？"]
    retriever = get_retriever()
    checkpointer = get_checkpointer()
    config = {"configurable": {"thread_id": "inspect-demo"}}

    printed = 0  # 已打印的消息条数：多轮时 result["messages"] 含全部历史，只看增量
    for i, question in enumerate(questions, 1):
        # 设计 §3.6：检索 query = 上一条用户消息 + 当前问题（多轮指代补全）
        previous_question = previous_human_message(checkpointer, config)
        retrieval_query = f"{previous_question}\n{question}".strip() if previous_question else question
        chunks = retriever.search(retrieval_query, top_n=RETRIEVER_TOP_N)

        print(f"\n{'=' * 70}\n第 {i} 问：{question}")
        print(f"检索 query：{retrieval_query!r}")
        print(f"检索注入 {len(chunks)} 条：" + "、".join(chunk.id for chunk in chunks))
        print(f"{'=' * 70}")

        agent = build_agent(build_system_prompt(format_context_block(chunks)))
        result = agent.invoke(
            {"messages": [{"role": "user", "content": question}]}, config
        )
        messages = result["messages"]
        for msg in messages[printed:]:
            print(type(msg).__name__, ":", str(msg.content)[:100].replace("\n", " "))
            tool_calls = getattr(msg, "tool_calls", None)
            if tool_calls:
                print("   ↳ tool_calls:", [(c["name"], c["args"]) for c in tool_calls])
        printed = len(messages)


if __name__ == "__main__":
    main()

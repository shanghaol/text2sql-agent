"""打印一次（或多轮）提问的完整消息链，看清 Agent 循环的每一步（教学文档 3.4）。

用法（必须用 venv 里的 python，且在仓库根目录运行）：
    .venv/Scripts/python inspect_chain.py                      # 默认演示问题
    .venv/Scripts/python inspect_chain.py "问题1" "问题2"      # 多轮对话，共用一个 thread_id

多轮时只打印每轮"新增"的消息——历史部分上一轮已经打印过，不重复刷屏。
注意：必须放仓库根目录运行，放子目录会 import 不到 agent 包。
"""

import sys

from agent.core.agent import build_agent


def main() -> None:
    questions = sys.argv[1:] or ["按 city 统计每个城市的订单数"]
    agent = build_agent()
    config = {"configurable": {"thread_id": "inspect-demo"}}

    printed = 0  # 已打印的消息条数：多轮时 result["messages"] 含全部历史，只看增量
    for i, question in enumerate(questions, 1):
        print(f"\n{'=' * 70}\n第 {i} 问：{question}\n{'=' * 70}")
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

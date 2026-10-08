"""命令行入口：在终端里和 Text-to-SQL 助手对话。

运行方式（在仓库根目录）：
    .venv/Scripts/python -m agent.main

用到的 LangChain v1 流式接口：
    agent.stream({"messages": [...]}, config=config, stream_mode="messages")
每次产出 (AIMessageChunk, metadata) 元组：
- chunk.content：模型输出的文本增量（一点点蹦出来的字）
- chunk.tool_call_chunks：非空说明模型正在发起工具调用
"""

import uuid

from langchain_core.messages import HumanMessage

from agent.config import DEEPSEEK_API_KEY
from agent.core.agent import build_agent


def main() -> None:
    """主循环：读用户输入 → 流式打印 Agent 回复，直到用户退出。"""
    # 启动前检查 API key：没有就给用户一份"怎么配"的说明，友好退出。
    # 这里必须检查——后面一调模型就会因为没有 key 而报一长串英文错误。
    if not DEEPSEEK_API_KEY:
        print("未找到 DeepSeek API Key，无法启动。请按以下步骤配置：")
        print("  1. 到 https://platform.deepseek.com 注册并创建 API Key；")
        print("  2. 复制仓库根目录的 .env.example 为 .env（不要改 .env.example）；")
        print("  3. 在 .env 中填入你的 key：DEEPSEEK_API_KEY=sk-xxxxxxxx；")
        print("  4. 重新运行本程序。")
        return

    # 每次启动生成一个新会话 ID：checkpointer 用它区分不同的对话线程，
    # 同一个 thread_id 内的多轮对话共享上下文。
    thread_id = uuid.uuid4().hex[:8]
    config = {"configurable": {"thread_id": thread_id}}

    print("=" * 60)
    print("Text-to-SQL 电商数据分析助手（W1）")
    print(f"会话 ID：{thread_id}")
    print("试试问：哪个城市的用户下单最多？/ 每个类目的销售额是多少？")
    print("输入 退出 / quit / exit 结束对话。")
    print("=" * 60)

    agent = build_agent()

    while True:
        try:
            user_input = input("你> ").strip()
        except (KeyboardInterrupt, EOFError):
            # Ctrl+C / Ctrl+D：优雅退出，不打错误堆栈
            print("\n再见！")
            break

        if not user_input:
            continue  # 空输入直接重新提示
        if user_input.lower() in ("退出", "quit", "exit"):
            print("再见！")
            break

        print("助手> ", end="", flush=True)
        last_tool_name = ""  # 记录上一条已打印的工具名，同一轮里去重防刷屏

        try:
            # stream_mode="messages"：按"消息增量"流式输出，
            # 模型每生成一小段就立刻拿到，终端上就是打字机效果。
            for chunk, _metadata in agent.stream(
                {"messages": [HumanMessage(content=user_input)]},
                config=config,
                stream_mode="messages",
            ):
                # 1) 文本增量：直接打印，end="" 不换行
                if chunk.content:
                    print(chunk.content, end="", flush=True)
                # 2) 工具调用增量：打印一行"正在调用哪个工具"
                #    （getattr 兜底：不是 AIMessageChunk 的块没有这个属性）
                tool_chunks = getattr(chunk, "tool_call_chunks", None)
                if tool_chunks:
                    tool_name = tool_chunks[0]["name"] or ""
                    if tool_name and tool_name != last_tool_name:
                        print(f"\n⚙ 调用工具: {tool_name}")
                        last_tool_name = tool_name
            print()  # 一轮回答结束，换行
        except KeyboardInterrupt:
            # 回答中途按 Ctrl+C：换行后继续下一轮对话，不退出整个程序
            print("\n（已中断当前回答）")
        except Exception as exc:  # noqa: BLE001 - 顶层兜底：报错但不断开对话
            print(f"\n出错了：{exc}\n请检查网络和 API Key 后重试。")


if __name__ == "__main__":
    main()

"""命令行入口：在终端里和 Text-to-SQL 助手对话。

运行方式（在仓库根目录）：
    .venv/Scripts/python -m agent.main

W2 流程（设计 §4.4）：
- 启动 preflight 三连：DeepSeek API key → MySQL 连通 → Chroma 索引存在；
- 每轮用户输入后：检索 query = 上一条用户消息 + 当前输入（§3.6，多轮指代补全）
  → 混合检索 → 动态注入 system_prompt → 用新 prompt 重建 agent → 流式回答；
  （W4 起，这一段组装统一走 agent/core/turn.py::prepare_turn——CLI / FastAPI /
  评测三方共用的唯一组装点，本文件不再内联这些步骤。）
- 对话主循环、流式打印、异常兜底与 W1 保持一致。

用到的 LangChain v1 流式接口：
    agent.stream({"messages": [...]}, config=config, stream_mode="messages")
每次产出 (AIMessageChunk, metadata) 元组：
- chunk.content：模型输出的文本增量（一点点蹦出来的字）
- chunk.tool_call_chunks：非空说明模型正在发起工具调用
"""

import uuid

from langchain_core.messages import HumanMessage

from agent.config import DEEPSEEK_API_KEY, MYSQL_PORT
from agent.core.turn import prepare_turn


def _check_mysql() -> bool:
    """preflight 之二：用只读账号向 MySQL 发一条 SELECT 1，确认连通。"""
    try:
        from sqlalchemy import text

        from agent.tools.sql_tools import get_engine

        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # noqa: BLE001 - preflight 要把任何连接失败变成人话
        print("MySQL 连接失败，无法启动。请按以下步骤排查：")
        print("  1. 容器没起是最常见原因：确认 MySQL 容器已启动（docker ps 看一眼）；")
        print(f"  2. 确认端口与 .env 一致（当前 MYSQL_PORT={MYSQL_PORT}）；")
        print("  3. 确认 .env 里 MYSQL_RO_PASSWORD 与容器内密码一致；")
        print(f"  原始错误：{exc}")
        return False


def _check_chroma() -> bool:
    """preflight 之三：确认 Chroma 索引已建（collection 里有片）。"""
    try:
        from agent.rag.chroma_store import get_collection

        if get_collection().count() > 0:
            return True
    except Exception as exc:  # noqa: BLE001 - 同上，失败给中文指引
        print(f"读取 Chroma 索引失败：{exc}")
        print("请先运行：python -m agent.rag.build_index")
        return False
    print("Chroma 索引为空，请先运行：python -m agent.rag.build_index")
    return False


def main() -> None:
    """主循环：读用户输入 → 流式打印 Agent 回复，直到用户退出。"""
    # preflight 之一：API key。没有就给用户一份"怎么配"的说明，友好退出。
    # 这里必须检查——后面一调模型就会因为没有 key 而报一长串英文错误。
    if not DEEPSEEK_API_KEY:
        print("未找到 DeepSeek API Key，无法启动。请按以下步骤配置：")
        print("  1. 到 https://platform.deepseek.com 注册并创建 API Key；")
        print("  2. 复制仓库根目录的 .env.example 为 .env（不要改 .env.example）；")
        print("  3. 在 .env 中填入你的 key：DEEPSEEK_API_KEY=sk-xxxxxxxx；")
        print("  4. 重新运行本程序。")
        return
    if not _check_mysql():
        return
    if not _check_chroma():
        return

    # 每次启动生成一个新会话 ID：checkpointer 用它区分不同的对话线程，
    # 同一个 thread_id 内的多轮对话共享上下文。
    thread_id = uuid.uuid4().hex[:8]
    config = {"configurable": {"thread_id": thread_id}}

    print("=" * 60)
    print("Text-to-SQL 电商数据分析助手（W2）")
    print(f"会话 ID：{thread_id}")
    print("试试问：上个月的 GMV 是多少？/ 2026 年 8 月的退款率呢？")
    print("输入 退出 / quit / exit 结束对话。")
    print("=" * 60)

    # 检索器和记忆存储是进程级单例，由 prepare_turn 内部按需懒加载：
    # 首轮对话时才初始化（fastembed 模型加载约几秒），多轮对话共享。

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

        # 每轮组装走 prepare_turn（W4 §3.1 三方共用）：
        # 检索 query 拼接、混合检索、prompt 组装、agent 重建全部在核心函数内完成；
        # 检索器和记忆存储仍是进程级单例（在 get_retriever/get_checkpointer 内），
        # 重建不丢多轮记忆。CLI 传 uuid 原文作 thread_key，与现状一致。
        pt = prepare_turn(user_input, thread_id)
        chunks = pt.injected_chunks
        print(f"（检索注入 {len(chunks)} 条：{'、'.join(chunk.id for chunk in chunks) or '无'}）")
        agent = pt.agent
        config = pt.config

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

"""W5 §8-⑦ 编码前实测脚本：重建 agent 实例后 Command(resume=...) 能否续跑。

背景（W5 设计 §8-⑦）：server.py 每轮 rebuild agent + 共享模块级 checkpointer 的现状下，
审批恢复轮要对"新 CompiledStateGraph 对象"发 Command(resume=...)——拓扑一致 + 同
checkpointer 是否足以恢复 interrupt 停车，设计阶段未实测，本脚本给出结论。

实测口径（与 server.py 的真实构造路径同构）：
- 停车轮：build_agent(system_prompt A) + interrupt 停车（真实 ChatDeepSeek，
  真工具 execute_readonly_sql 同名替身，不触 MySQL）；
- 丢弃 agent 引用（del + gc）→ 用【不同的 system_prompt B】重建 agent（模拟
  "重建后上下文由 context_block 逐字节复刻"的反例边界——顺便验证 resume 后
  model 节点用的是新实例的 system prompt 还是 checkpoint 里的旧 prompt）；
- resume approve → 工具执行 + 模型收尾 → state.next 为空；
- resume reject 路径同轮验一次（另一个 thread）。

运行（在仓库根目录，耗 1-2 次 DeepSeek 调用）：
    .venv/Scripts/python.exe tests/manual/w5_hitl_rebuild_resume_probe.py
"""

import gc
import sys

from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from agent.core.agent import get_chat_model

REJECT_MESSAGE = "用户拒绝了这条 SQL。请不要执行或改写它，直接询问用户希望如何调整（时间范围、指标口径、过滤条件）。"

executed: list[str] = []


@tool
def execute_readonly_sql(sql: str) -> str:
    """执行只读 SQL（本探针的替身实现：不触 MySQL，只记录调用）。"""
    executed.append(sql)
    return f"[fake result] rows=1 sql={sql}"


def build_agent(checkpointer: InMemorySaver, system_prompt: str):
    """与 agent/core/agent.py 的 build_agent(hitl=True) 同款构造路径。"""
    model = get_chat_model(None)  # 走 .env 的 DEEPSEEK_API_KEY
    return create_agent(
        model=model,
        tools=[execute_readonly_sql],
        system_prompt=system_prompt,
        checkpointer=checkpointer,
        middleware=[HumanInTheLoopMiddleware(
            interrupt_on={"execute_readonly_sql": {
                "allowed_decisions": ["approve", "reject"],
                # description 是 InterruptOnConfig 的字段（非构造器 kwarg，2026-10-11 实测）；
                # callable 签名 (tool_call, state, runtime)，返回中文描述含 SQL
                "description": lambda tool_call, state, runtime: f"即将执行 SQL：{tool_call.get('args', {}).get('sql', '')}",
            }},
        )],
    )


def interrupt_payload(agent, config) -> dict:
    state = agent.get_state(config)
    assert state.tasks, f"expected interrupt, tasks={state.tasks}"
    return state.tasks[0].interrupts[0].value


def main() -> None:
    checkpointer = InMemorySaver()

    # ---------- 场景 1：approve，且重建实例换了 system_prompt ----------
    prompt_a = "你是探针 A。收到 SQL 执行结果后，用一句中文报出结果里包含的 sql 原文。"
    prompt_b = "你是探针 B（重建实例）。收到 SQL 执行结果后，用一句中文报出结果。"
    thread = "probe-approve"
    config = {"configurable": {"thread_id": thread}}

    agent = build_agent(checkpointer, prompt_a)
    result = agent.invoke({"messages": [HumanMessage(content="请执行 SQL：SELECT 42 AS answer")]}, config)
    assert result["messages"][-1].tool_calls or any(
        getattr(m, "tool_calls", None) for m in result["messages"]), "expected tool call message"
    payload = interrupt_payload(agent, config)
    sql = payload["action_requests"][0]["args"]["sql"]
    print(f"[1] interrupt 停车 OK：action={payload['action_requests'][0]['name']} sql={sql!r}")
    print(f"    description 字段（callable 中文定制生效性）: {payload['action_requests'][0].get('description')!r}")

    # 丢弃 agent 引用并 gc——模拟 server.py 的"每轮重建"
    del agent
    gc.collect()

    agent2 = build_agent(checkpointer, prompt_b)  # 同 checkpointer、不同 system_prompt 的新实例
    resumed = agent2.invoke(Command(resume={"decisions": [{"type": "approve"}]}), config)
    assert executed and sql in executed[-1], f"tool should have executed after resume: executed={executed}"
    state2 = agent2.get_state(config)
    assert not state2.next, f"state.next should be empty after approve resume: {state2.next}"
    final_text = resumed["messages"][-1].content
    print(f"[1] 重建实例 resume approve OK：tool executed={executed[-1]!r} state.next={state2.next}")
    print(f"    收尾回答（model 节点用的是哪个 prompt 的回话风格可从文本判断）: {final_text[:120]!r}")

    # ---------- 场景 2：reject，重建实例后拒绝消息形态 ----------
    executed.clear()
    thread2 = "probe-reject"
    config2 = {"configurable": {"thread_id": thread2}}
    agent3 = build_agent(checkpointer, prompt_a)
    agent3.invoke({"messages": [HumanMessage(content="请执行 SQL：SELECT 7 AS lucky")]}, config2)
    interrupt_payload(agent3, config2)  # 断言"确实停车"（载荷本身本场景不再使用）
    del agent3
    gc.collect()

    agent4 = build_agent(checkpointer, prompt_b)
    rejected = agent4.invoke(Command(resume={"decisions": [{"type": "reject", "message": REJECT_MESSAGE}]}), config2)
    assert not executed, f"reject must NOT execute the tool: executed={executed}"
    state4 = agent4.get_state(config2)
    assert not state4.next, f"state.next should be empty after reject resume: {state4.next}"
    tool_msgs = [m for m in rejected["messages"] if getattr(m, "type", "") == "tool" or "ToolMessage" in type(m).__name__]
    print(f"[2] 重建实例 resume reject OK：tool 未执行、state.next={state4.next}")
    print(f"    ToolMessage 内容: {(tool_msgs[-1].content if tool_msgs else 'N/A')!r}")
    print(f"    模型回应: {rejected['messages'][-1].content[:150]!r}")

    # ---------- 场景 3：reject 后同 thread 再发新问题（多轮健康） ----------
    followup = agent4.invoke({"messages": [HumanMessage(content="好，那告诉我 1+1 等于几，一句话。")]}, config2)
    print(f"[3] reject 后同 thread 续问 OK：{followup['messages'][-1].content[:80]!r}")

    # ---------- 场景 4：resume 的 stream_mode="messages" 流会不会重放停车 AIMessage / 吐 ToolMessage ----------
    # 这决定 server.py 审批恢复流的映射器会不会把停车轮的 delta/tool 事件重发一遍（前端文本重复）。
    executed.clear()
    thread4 = "probe-stream-resume"
    config4 = {"configurable": {"thread_id": thread4}}
    agent5 = build_agent(checkpointer, prompt_a)
    agent5.invoke({"messages": [HumanMessage(content="请执行 SQL：SELECT 5 AS five")]}, config4)
    del agent5
    gc.collect()
    agent6 = build_agent(checkpointer, prompt_b)
    chunk_kinds: list[str] = []
    for chunk, _meta in agent6.stream(Command(resume={"decisions": [{"type": "approve"}]}), config=config4,
                                      stream_mode="messages"):
        kind = type(chunk).__name__
        tc = bool(getattr(chunk, "tool_call_chunks", None))
        content = (chunk.content or "") if isinstance(chunk.content, str) else str(chunk.content)
        chunk_kinds.append(f"{kind}(tool_calls_chunks={tc}) content[:40]={content[:40]!r}")
    print(f"[4] resume 的 messages 流共 {len(chunk_kinds)} 个 chunk：")
    for line in chunk_kinds:
        print(f"    - {line}")
    has_replayed_tool_call = any("tool_calls_chunks=True" in line for line in chunk_kinds)
    has_tool_message = any(line.startswith("ToolMessage") for line in chunk_kinds)
    print(f"    => 停车 AIMessage 重放={has_replayed_tool_call}；ToolMessage 出现在流中={has_tool_message}")

    # ---------- 场景 5：TTL 过期路径——对带 pending interrupt 的 thread 直接 invoke 新输入 ----------
    thread5 = "probe-stale-interrupt"
    config5 = {"configurable": {"thread_id": thread5}}
    agent7 = build_agent(checkpointer, prompt_a)
    agent7.invoke({"messages": [HumanMessage(content="请执行 SQL：SELECT 9 AS nine")]}, config5)
    payload5 = interrupt_payload(agent7, config5)
    assert payload5.get("action_requests"), "expected parked interrupt"
    print("[5] 停车后不 resume、直接发新输入（模拟 TTL 过期后新提问）：")
    try:
        stale = agent7.invoke({"messages": [HumanMessage(content="别查了，告诉我 2+2 等于几，一句话")]}, config5)
        state5 = agent7.get_state(config5)
        print(f"    invoke 正常返回；state.next={state5.next}；末条消息={stale['messages'][-1].content[:80]!r}")
    except Exception as e:  # noqa: BLE001
        print(f"    invoke 抛异常：{type(e).__name__}: {str(e)[:200]}")

    print("RESULT: PASS")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"RESULT: FAIL — {e}")
        sys.exit(1)

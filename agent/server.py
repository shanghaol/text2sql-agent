"""FastAPI 服务化入口（W4 设计 §3.2-§3.4 + W5 HITL §3.4-§3.5）：
POST /agent/chat SSE + POST /agent/chat/approval SSE + GET /agent/health。

运行方式（在仓库根目录；**必须单 worker**）：
    .venv/Scripts/python -m uvicorn agent.server:app --port 8000

为什么单 worker：进程内单例——Chroma PersistentClient、fastembed、checkpointer、
LLM 客户端缓存——不允许 ×N 复制（W2 §3.1 坑 #10 的正式落地）。并发靠线程池：
端点用 def（Starlette 跑线程池），SSE 用**同步生成器**（agent.stream 是同步 API，
Starlette 在线程池中迭代；不引入 async LangChain 栈，避免 to_thread/队列两层胶水）。

与 CLI（agent/main.py）preflight 的唯一语义分歧（§3.4，注释即文档）：
- CLI：DeepSeek key 缺失 → 友好退出。单机自用工具，没有 key 什么都干不了；
- 服务端：key 缺失 → **仅 WARN 不退出**。生产容器本就不该配 DEEPSEEK_API_KEY
  （§1.1/§5.5 部署清单结构性禁止站点级 key），BYOK 之下每请求的 key 从
  X-User-LLM-Key 头来；缺 key 只影响"裸调 /agent/chat 且不带 key"这一种请求
  （返回 401 llm_key_missing），不该拦下整个服务。MySQL/Chroma 失败仍退出（fail-fast）。

日志脱敏（§1.5 规则 5/6）：
- httpx logger 压到 WARNING（DEBUG 级会打请求头；openai SDK 对 Authorization
  有默认 redact，但不赌）；
- 异常只 log 类型 + message + key 指纹（sha256 前 8 hex），不打请求头、
  不打原始堆栈里的 request 对象；error 事件不携带 DeepSeek 原始报错全文。

不做 CORS：前端只调 Java（同源），Python 无浏览器直连方（§3.2）。

W5 HITL 事件契约增量（§3.4，向后兼容）：
- /agent/chat 流停在 interrupt 时发 `pending_approval {approvalId, sql, latencyMs, tokens}` 后
  结束（无 done）；Java 落 status=4 并注入 `pending_saved {historyId}`（Java→FE）；
- POST /agent/chat/approval：审批恢复流（meta/delta/tool/done 同款映射；saved 仍由 Java 注入）；
- 流前错误码增量：pending_approval_exists(409) / approval_not_found(409) /
  approval_expired(410) / approval_bad_decision(422)。
"""

import hashlib
import json
import logging
import secrets
import sys
import time
import uuid as uuid_lib
from collections.abc import Generator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse
from langchain_core.messages import HumanMessage
from langgraph.types import Command
from pydantic import BaseModel

import agent.sqlite_shim  # noqa: F401  必须最先导入：chromadb 对 sqlite 版本有硬要求（见模块 docstring）
from agent.config import (
    DEEPSEEK_API_KEY,
    DEEPSEEK_MODEL,
    INTERNAL_TOKEN,
    MAX_QUESTION_CHARS,
)
from agent.core import approval
from agent.core.agent import build_agent, build_system_prompt, format_context_block
from agent.core.turn import prepare_turn

# §1.5 规则 5：httpx 系 logger 的 DEBUG 级日志会打请求头（含 key），启动即压到 WARNING。
# "httpx2" 是 openai SDK 依赖树里的 httpx 变体 logger 名（实测 2026-10-11），一并压掉。
for _httpx_logger in ("httpx", "httpx2"):
    logging.getLogger(_httpx_logger).setLevel(logging.WARNING)

# 应用日志到 stderr（docker logs 可见）：root 无 handler 时才装（uvicorn 直跑场景），
# 否则 INFO 级日志会被 logging 的 last-resort 过滤器（WARNING 起）静默丢弃——
# 断连排查（§9.2-5）必须能看到 "client disconnected"。uvicorn 自有 logger 不受影响。
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")

logger = logging.getLogger("agent.server")

# 流中错误的可读文案（§3.3"前端表现"列；error 事件不透传上游原始报错，§1.5 规则 7）
_LLM_ERROR_MESSAGES = {
    "llm_key_invalid": "DeepSeek Key 已失效，请到设置页重新录入",
    "llm_insufficient_balance": "DeepSeek 余额不足，请充值后重试",
    "llm_rate_limited": "请求过于频繁，请稍后再试",
    "llm_upstream_error": "模型服务暂时不可用，请重试",
}

# HITL 审批错误的可读文案（W5 §3.4 事件契约表；Java/前端按 code 再映射一份同语义文案）
_APPROVAL_ERROR_MESSAGES = {
    "approval_not_found": "审批不存在或已被处理（可能服务已重启），请重新提问",
    "approval_expired": "审批已超过 5 分钟未处理，请重新提问",
    "pending_approval_exists": "当前会话已有等待确认的 SQL，请先处理审批卡片",
}

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 纯函数与纯映射（可单测，不做任何 I/O）
# ---------------------------------------------------------------------------


def internal_token_ok(provided: str | None) -> bool:
    """X-Internal-Token 校验。config.INTERNAL_TOKEN 为空 = 跳过校验
    （开发机不经 Java 直连 uvicorn 调试的口子）；非空时恒等比较防时序侧信道。"""
    if not INTERNAL_TOKEN:
        return True
    return bool(provided) and secrets.compare_digest(provided, INTERNAL_TOKEN)


def validate_chat_request(question: str, thread_id: str) -> str | None:
    """流前 422 校验（§1.6 bad_request）。返回 None=通过，否则为可读错误 message。"""
    question = question or ""
    if not question.strip():
        return "question 不能为空"
    if len(question) > MAX_QUESTION_CHARS:
        return f"question 超过最大长度 {MAX_QUESTION_CHARS} 字符"
    try:
        uuid_lib.UUID(str(thread_id))
    except (TypeError, ValueError):
        return "threadId 必须是 UUID"
    return None


def map_llm_error(exc: Exception) -> tuple[str, str]:
    """DeepSeek/openai SDK 异常 → (业务错误码, 可读文案)，§1.6 错误映射表。

    - AuthenticationError（401，key 被删/改）→ llm_key_invalid；
    - 其余按 status_code 分派：402 → llm_insufficient_balance（DeepSeek 约定码，
      §9.2-3 实测确认）、429 → llm_rate_limited；
    - 5xx / 超时 / 连接断 / 一切其余异常 → llm_upstream_error（兜底，含非 LLM 异常）。
    """
    import openai

    if isinstance(exc, openai.AuthenticationError):
        return "llm_key_invalid", _LLM_ERROR_MESSAGES["llm_key_invalid"]
    status_code = getattr(exc, "status_code", None)
    if status_code == 402:
        return "llm_insufficient_balance", _LLM_ERROR_MESSAGES["llm_insufficient_balance"]
    if status_code == 429:
        return "llm_rate_limited", _LLM_ERROR_MESSAGES["llm_rate_limited"]
    return "llm_upstream_error", _LLM_ERROR_MESSAGES["llm_upstream_error"]


def sse_frame(event: str, data: dict) -> str:
    """一帧 SSE 事件：`event:` + `data:` 两行一组、空行分帧（§3.2 帧格式手写）。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


class StreamEventMapper:
    """流式 chunk → SSE 事件的纯映射（§3.2 事件契约）。

    与 main.py 的去重打印逻辑逐行同构：chunk.content → delta；
    tool_call_chunks 非空且工具名变化 → tool（同一轮里同名工具只发一次）。
    另外累加 chunk.usage_metadata——实测（2026-10-11，langchain-deepseek 1.1.1）
    每条 AIMessage 的收尾 chunk 携带一份 usage_metadata，累加即"每次 LLM 请求
    计一次"，与 W3 sum_usage 的计费口径一致。
    """

    def __init__(self) -> None:
        self._last_tool_name = ""  # 记录上一条已发事件的工具名，同一轮里去重防刷屏
        self._pending_tools: dict[int, dict] = {}  # 工具调用分片累积：index → {name, args}
        self.last_generated_sql = None  # 本轮最后一次 execute_readonly_sql 的完整 SQL（done 事件携带）
        self.usage_input = 0
        self.usage_output = 0

    def feed(self, chunk) -> list[tuple[str, dict]]:
        """吃一个 (message, metadata) 的 chunk，吐出 0~2 个 (事件名, data)。"""
        events: list[tuple[str, dict]] = []
        # 1) 文本增量。messages 流里不止模型输出：工具节点写入的 ToolMessage（执行结果原文）
        #    也会出现（2026-10-11 实测，langgraph 1.2.14：正常轮与 resume 轮都是）。它的
        #    content 是查询结果/拒绝回执，绝不是模型文本——不过滤会被当成 delta 泄漏进前端
        #    气泡与 Java 落库的 answer_text（W4 潜伏缺陷，W5-T2 §8-⑧ 实测发现并修复）。
        if chunk.content and getattr(chunk, "type", None) != "tool":
            events.append(("delta", {"text": chunk.content}))
        # 2) 工具调用增量（getattr 兜底：不是 AIMessageChunk 的块没有这个属性）。
        #    tool_call_chunks 是流式分片：name 在首片出现，args 按序号累积成完整 JSON。
        #    起始分片立即发 tool 事件（前端"正在查询"指示 + Java 记工具名）；
        #    完整 SQL 要等该条 AIMessage 收尾（usage 到达）才拼得出，见 _finalize_tool_sql。
        tool_chunks = getattr(chunk, "tool_call_chunks", None)
        if tool_chunks:
            for tc in tool_chunks:
                idx = tc.get("index")
                idx = 0 if idx is None else idx
                name = tc.get("name") or ""
                slot = self._pending_tools.get(idx)
                if slot and name and slot["name"] != name:
                    # 同一序号来了不同名字的调用：先结算上一条的 sql，再开新槽
                    events.extend(self._flush_slot(idx))
                    slot = None
                if slot is None:
                    slot = {"name": "", "args": ""}
                    self._pending_tools[idx] = slot
                if name and slot["name"] == "":
                    slot["name"] = name
                    self._last_tool_name = name
                    events.append(("tool", {"name": name}))
                slot["args"] += tc.get("args") or ""
        # 3) token 用量（每条 AIMessage 收尾 chunk 携带一次）——同时是工具调用参数拼装完成的信号
        usage = getattr(chunk, "usage_metadata", None)
        if usage:
            self.usage_input += int(usage.get("input_tokens") or 0)
            self.usage_output += int(usage.get("output_tokens") or 0)
            events.extend(self._finalize_tool_sql())
        return events

    def _flush_slot(self, idx: int) -> list[tuple[str, dict]]:
        """结算一个工具调用槽：参数拼成完整 JSON 后抽出 sql（无 sql 则静默）。"""
        slot = self._pending_tools.get(idx)
        if not slot or not slot["args"]:
            return []
        try:
            parsed = json.loads(slot["args"])
        except (ValueError, TypeError):
            return []
        if isinstance(parsed, dict) and parsed.get("sql"):
            self.last_generated_sql = parsed["sql"]
            return [("tool", {"name": slot["name"], "sql": parsed["sql"]})]
        return []

    def _finalize_tool_sql(self) -> list[tuple[str, dict]]:
        """AIMessage 收尾：结算全部待决工具调用槽（清空累积）。"""
        events: list[tuple[str, dict]] = []
        for idx in sorted(self._pending_tools):
            events.extend(self._flush_slot(idx))
        self._pending_tools.clear()
        return events


# ---------------------------------------------------------------------------
# SSE 事件流（同步生成器；Starlette 在线程池中迭代）
# ---------------------------------------------------------------------------


def _extract_interrupt_payload(agent, config) -> dict | None:
    """流结束后探测 LangGraph interrupt（W5 §3.3-2：messages 流不携带中断事件）。

    命中返回 interrupt 载荷 {"action_requests": [...], "review_configs": [...]}；
    待审批 SQL 在 action_requests[0].args["sql"]。无中断/探测失败一律 None——
    探测绝不打断正常完成的轮次（非 HITL 轮 get_state 无 interrupts，零影响）。
    """
    try:
        state = agent.get_state(config)
    except Exception:  # noqa: BLE001 - get_state 失败按"无中断"处理
        return None
    for task in getattr(state, "tasks", None) or ():
        for interrupt in getattr(task, "interrupts", None) or ():
            value = getattr(interrupt, "value", None)
            if isinstance(value, dict) and value.get("action_requests"):
                return value
    return None


def _chat_event_stream(pt, question: str, started_at: float, key_fingerprint: str) -> Generator[str, None, None]:
    """把一轮 agent.stream 映射为 SSE 事件流（§3.2 事件契约 + W5 §3.4 pending 分支）。

    - meta 首事件在首 delta 前（前端先渲染"思考中"骨架）；
    - 异常必须转事件：把 LLM 异常映射为 error 事件后 return——让裸异常打断连接
      会把可恢复错误（余额不足）变成前端不可读的连接中断（§3.2）；
    - 流正常结束但停在 interrupt（HITL 停车，§3.4）：登记 pending → 发
      pending_approval → 流结束（**不发 done**——Java 据此按 status=4 落库）；
    - 客户端断连：Starlette 取消迭代，生成器收到 GeneratorExit → 记一条日志后
      原样抛出（绝不 yield、绝不吞），历史落库是 Java 的职责，Python 侧无状态
      需要清理（§9.2-5 实测无僵尸线程）。
    """
    thread_key = pt.config["configurable"]["thread_id"]
    yield sse_frame("meta", {"threadKey": thread_key, "model": DEEPSEEK_MODEL})
    mapper = StreamEventMapper()
    try:
        for chunk, _metadata in pt.agent.stream(
            {"messages": [HumanMessage(content=question)]},
            config=pt.config,
            stream_mode="messages",
        ):
            for event, data in mapper.feed(chunk):
                yield sse_frame(event, data)
        # —— W5 §3.4：流自然结束，先探测 interrupt（HITL 停车）再决定是否发 done ——
        interrupt = _extract_interrupt_payload(pt.agent, pt.config)
        if interrupt is not None:
            actions = interrupt.get("action_requests") or []
            # TODO(W5 §8-⑰ 收尾): 同轮多条 SQL 时 middleware 一次收集全部 tool_call
            # （action_requests 多条）。当前前端只展示/确认第一条；resume 由
            # approval.decisions_for 做安全兜底（approve=批 1 条 + 拒其余未过目的，
            # reject=整批拒）——被拒的 SQL 若仍需要，模型会重新发起 → 链式新审批卡。
            # 待办：pending_approval 事件带全部 SQL、前端逐条批准/拒绝。
            first = actions[0] if actions else {}
            first_args = first.get("args") or {}
            sql = first_args.get("sql") if isinstance(first_args, dict) else None
            sql_text = sql if isinstance(sql, str) else ""
            latency_ms = int((time.perf_counter() - started_at) * 1000)
            logger.info(
                "stream interrupted (HITL): thread_key=%s tool=%s actions=%d has_sql=%s",
                thread_key, first.get("name"), len(actions), bool(sql_text),
            )
            try:
                approval_id = approval.register(
                    thread_key, sql_text, question, format_context_block(pt.injected_chunks),
                    action_count=len(actions),
                )
            except approval.PendingApprovalExists:
                # 正常不可达（新提问已在端点层被 find_by_thread 409 短路）；并发双提问兜底
                yield sse_frame(
                    "error",
                    {"code": "pending_approval_exists", "message": _APPROVAL_ERROR_MESSAGES["pending_approval_exists"]},
                )
                return
            yield sse_frame(
                "pending_approval",
                {
                    "approvalId": approval_id,
                    "sql": sql_text,
                    # latencyMs/tokens 为 additive 字段（契约主字段是 approvalId/sql）：
                    # Java 落 status=4 行时带上停车前已消耗的部分，approve 的 UPDATE
                    # 两段累加后 total 才完整（§3.4 status=4 行含 token/latency）
                    "latencyMs": latency_ms,
                    "tokens": {"input": mapper.usage_input, "output": mapper.usage_output},
                },
            )
            return  # pending 轮不发 done（§3.4：Java pending 分支同样不等 done）
        latency_ms = int((time.perf_counter() - started_at) * 1000)
        logger.info(
            "stream completed: thread_key=%s latency_ms=%s has_sql=%s",
            thread_key, latency_ms, mapper.last_generated_sql is not None,
        )
        yield sse_frame(
            "done",
            {
                "tokens": {"input": mapper.usage_input, "output": mapper.usage_output},
                "latencyMs": latency_ms,
                # Java 旁路解析捕获后落 query_history.generated_sql（W5 few-shot 闭环原料）
                "generatedSql": mapper.last_generated_sql,
            },
        )
    except GeneratorExit:
        # 客户端断连：绝不 yield、绝不吞（吞了会 RuntimeError: generator ignored
        # GeneratorExit）。日志由 _guarded_sse_stream 的确定性收场路径负责。
        raise
    except Exception as exc:  # noqa: BLE001 - 流中异常统一转 error 事件（§1.6 错误映射表）
        code, message = map_llm_error(exc)
        # §1.5 规则 6：只 log 类型 + message + key 指纹，不打请求头与原始堆栈
        logger.error(
            "stream failed: code=%s type=%s message=%s key_fp=%s thread_key=%s",
            code, type(exc).__name__, str(exc)[:200], key_fingerprint, thread_key,
        )
        yield sse_frame("error", {"code": code, "message": message})


def _approval_event_stream(
    entry: dict, decision: str, api_key: str | None, started_at: float, key_fingerprint: str
) -> Generator[str, None, None]:
    """审批恢复流（W5 §3.4 [批准]/[拒绝]）：resume 的 messages 流映射为同款 SSE 事件。

    - 与 /agent/chat 同款事件映射（meta/delta/tool/done）；saved 由 Java 落库后注入，
      Python 不发（Python 不知道 historyId）；
    - resume 输入是 Command({"resume": {"decisions": [...]}})，**不是新问题**——
      不走 prepare_turn、不重跑检索（§3.5 恢复轮组装口径），agent 用
      entry.context_block 重建（系统提示词与停车轮逐字节一致）；
    - decision=reject 时模型询问用户意图的 delta 照常透传（实测中文回应）；
    - 重建 agent 后 resume 可行性已实测（tests/manual/w5_hitl_rebuild_resume_probe.py，
      W5 §8-⑦）：同 checkpointer + 同拓扑的新实例可原地续跑，state.next 收空。
    """
    thread_key = entry["thread_key"]
    yield sse_frame("meta", {"threadKey": thread_key, "model": DEEPSEEK_MODEL})
    mapper = StreamEventMapper()
    try:
        agent = build_agent(build_system_prompt(entry["context_block"]), api_key=api_key, hitl=True)
        config = {"configurable": {"thread_id": thread_key}}
        for chunk, _metadata in agent.stream(
            Command(resume={"decisions": approval.decisions_for(decision, entry.get("action_count", 1))}),
            config=config,
            stream_mode="messages",
        ):
            for event, data in mapper.feed(chunk):
                yield sse_frame(event, data)
        # —— 恢复轮又停在新 interrupt（§8-⑰ 实测场景：批准第 1 条后模型为第 2 条 SQL 重新
        #    发起审批；messages 流自然结束）——登记新 pending、发 pending_approval、不发 done，
        #    Java 据此把原行保持 status=4 并再次注入 pending_saved → 前端续接下一张审批卡。
        chained = _extract_interrupt_payload(agent, config)
        if chained is not None:
            actions = chained.get("action_requests") or []
            first = actions[0] if actions else {}
            first_args = first.get("args") or {}
            sql = first_args.get("sql") if isinstance(first_args, dict) else None
            sql_text = sql if isinstance(sql, str) else ""
            logger.info(
                "approval chained interrupt: thread_key=%s tool=%s actions=%d",
                thread_key, first.get("name"), len(actions),
            )
            try:
                approval_id = approval.register(
                    thread_key, sql_text, entry["question"], entry["context_block"],
                    action_count=len(actions),
                )
            except approval.PendingApprovalExists:
                yield sse_frame(
                    "error",
                    {"code": "pending_approval_exists", "message": _APPROVAL_ERROR_MESSAGES["pending_approval_exists"]},
                )
                return
            yield sse_frame(
                "pending_approval",
                {
                    "approvalId": approval_id,
                    "sql": sql_text,
                    "latencyMs": int((time.perf_counter() - started_at) * 1000),
                    "tokens": {"input": mapper.usage_input, "output": mapper.usage_output},
                },
            )
            return  # 新一轮待审批：无 done（Java 保持 status=4 分支）
        latency_ms = int((time.perf_counter() - started_at) * 1000)
        logger.info(
            "approval stream completed: thread_key=%s decision=%s latency_ms=%s question=%r",
            thread_key, decision, latency_ms, entry["question"][:50],
        )
        yield sse_frame(
            "done",
            {
                "tokens": {"input": mapper.usage_input, "output": mapper.usage_output},
                "latencyMs": latency_ms,
                "generatedSql": mapper.last_generated_sql,  # resume 轮一般不再产生新 SQL（None 时 Java 保留原值）
            },
        )
    except GeneratorExit:
        raise
    except Exception as exc:  # noqa: BLE001 - 与 /agent/chat 同款错误映射
        code, message = map_llm_error(exc)
        logger.error(
            "approval stream failed: code=%s type=%s message=%s key_fp=%s thread_key=%s",
            code, type(exc).__name__, str(exc)[:200], key_fingerprint, thread_key,
        )
        yield sse_frame("error", {"code": code, "message": message})


class ChatRequest(BaseModel):
    """POST /agent/chat 请求体（§3.2）：{"question": "...", "threadId": "uuid"}"""

    question: str
    threadId: str


class ApprovalRequest(BaseModel):
    """POST /agent/chat/approval 请求体（W5 §3.4）：{"threadId", "approvalId", "decision"}"""

    threadId: str
    approvalId: str
    decision: str  # approve | reject（服务端校验，非法 → 422 approval_bad_decision）


async def _guarded_sse_stream(sync_gen: Generator[str, None, None], receive, thread_key: str):
    """给同步 SSE 生成器加确定性断连收场的 async 包装（§9.2-5 实测结论的落地）。

    为什么不直接把同步生成器交给 StreamingResponse：实测（2026-10-11，uvicorn
    0.54 + starlette 1.7）取消落在 to_thread 等待点时，被弃的同步生成器会被
    CancelledError 回溯的引用环扣住——GeneratorExit 迟迟不投递，挂起的生成器
    （连同整轮 LangGraph 状态）长时间滞留。这里在 async 侧自听 http.disconnect，
    一断连就同步 close() 底层生成器：GeneratorExit 在 finally 里**确定性**触发，
    无滞留。LangChain 仍是同步驱动——"不引入 async 模型栈"的设计原则不破。

    finally 里全部是同步调用（watcher.cancel / sync_gen.close），不在被取消的
    协程里 await；薄壳 wrapper 交给 asyncio 的 asyncgen 终结钩子收尾（无害）。
    """
    import asyncio

    from starlette.concurrency import iterate_in_threadpool

    wrapper = iterate_in_threadpool(sync_gen)
    disconnected = asyncio.Event()

    async def _watch_disconnect():
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                disconnected.set()
                return

    watcher = asyncio.ensure_future(_watch_disconnect())
    try:
        async for frame in wrapper:
            if disconnected.is_set():
                break
            yield frame
    finally:
        watcher.cancel()
        if disconnected.is_set():
            # 断连收场：显式 close 同步生成器，GeneratorExit 在其挂起点确定性触发；
            # 生成器已自然结束（断连在最后一帧之后才到）时 close() 是无害空操作
            logger.info("client disconnected: thread_key=%s", thread_key)
            sync_gen.close()


# ---------------------------------------------------------------------------
# 应用与端点
# ---------------------------------------------------------------------------


def _preflight() -> None:
    """启动 preflight（§3.4）：MySQL / Chroma 失败即退出（fail-fast）；
    key 缺失仅 WARN 不退出——与 CLI preflight 的唯一语义分歧，理由见模块 docstring。"""
    if DEEPSEEK_API_KEY:
        # 开发机常态；生产容器必须注释掉（§5.5：站点级 key 被部署清单结构性禁止）
        logger.warning("检测到环境变量 DEEPSEEK_API_KEY：仅限开发机（不带 X-User-LLM-Key 的请求将用它）；生产容器必须移除")
    else:
        logger.warning("未配置 DEEPSEEK_API_KEY：不带 X-User-LLM-Key 头的对话请求将返回 401 llm_key_missing（服务端本就不该有 env key，BYOK 由请求头提供）")

    try:
        from sqlalchemy import text

        from agent.tools.sql_tools import get_engine

        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - preflight 要把任何连接失败变成人话
        print("MySQL 连接失败，服务无法启动。请按以下步骤排查：")
        print("  1. 容器没起是最常见原因：确认 MySQL 容器已启动（docker ps 看一眼）；")
        print("  2. 确认 .env 里 MYSQL_PORT 与 MYSQL_RO_PASSWORD 与容器内一致。")
        print(f"  原始错误：{exc}")
        sys.exit(1)

    try:
        from agent.rag.chroma_store import get_collection

        count = get_collection().count()
    except Exception as exc:  # noqa: BLE001
        print(f"读取 Chroma 索引失败：{exc}")
        print("请先运行：python -m agent.rag.build_index")
        sys.exit(1)
    if count <= 0:
        print("Chroma 索引为空，请先运行：python -m agent.rag.build_index")
        sys.exit(1)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    _preflight()
    yield


app = FastAPI(title="text2sql-agent", version="4.0", lifespan=_lifespan)


def _check_mysql_ok() -> bool:
    """health 之一：只读账号 SELECT 1（轻量，不查 DeepSeek——出站调用不能当探活，§3.4）。"""
    try:
        from sqlalchemy import text

        from agent.tools.sql_tools import get_engine

        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 - 探活只报布尔，详情看启动日志
        return False


def _chroma_count() -> int:
    """health 之二：collection 片数；读取失败返回 -1（调用方据此判 degraded）。"""
    try:
        from agent.rag.chroma_store import get_collection

        return get_collection().count()
    except Exception:  # noqa: BLE001
        return -1


@app.get("/agent/health")
def health() -> dict:
    """健康检查（§3.4）：{status, chromaChunks, mysqlOk}，无内部头要求。

    compose healthcheck 30s 间隔消费；status up = MySQL 连通且索引有片。
    """
    mysql_ok = _check_mysql_ok()
    chroma_chunks = _chroma_count()
    status = "up" if mysql_ok and chroma_chunks > 0 else "degraded"
    return {"status": status, "chromaChunks": chroma_chunks, "mysqlOk": mysql_ok}


@app.post("/agent/chat")
def agent_chat(
    body: ChatRequest,
    request: Request,
    x_internal_token: str | None = Header(default=None),
    x_user_llm_key: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
    x_user_hitl: str | None = Header(default=None),
):
    """对话端点：流前错误走 HTTP 状态码 + JSON body，流中错误走 error 事件（§1.6）。

    检查顺序：内部头 403 → key 缺失 401 → 请求体 422 → 未决审批 409 → prepare_turn → SSE 流。
    X-User-HITL: "1" = 本请求开启 SQL 执行前人工确认（W5 §3.8：开关是请求域的，
    Java 读用户设置注入；缺省/其它值 = 关，行为与 W4 完全一致）。
    """
    # ① 内部头：Java/Python 共享密钥（INTERNAL_TOKEN 非空时校验；§3.3 forbidden）
    if not internal_token_ok(x_internal_token):
        logger.warning("forbidden: X-Internal-Token missing or wrong")
        return JSONResponse(status_code=403, content={"code": "forbidden", "message": "内部令牌缺失或不正确"})

    # ② BYOK key：头缺失且无 env key → 401（生产上 Java 已前置拦截，此为直接调用的防御）
    api_key = (x_user_llm_key or "").strip() or None
    if api_key is None and not DEEPSEEK_API_KEY:
        return JSONResponse(
            status_code=401,
            content={"code": "llm_key_missing", "message": "缺少 X-User-LLM-Key 请求头"},
        )
    # key 的唯一日志替身：sha256 前 8 hex（§1.5 规则 2），明文绝不进日志
    key_fingerprint = hashlib.sha256(api_key.encode()).hexdigest()[:8] if api_key else "env"

    # ③ 请求体校验：question 超长 / threadId 非法 → 422（§1.6 bad_request）
    error_message = validate_chat_request(body.question, body.threadId)
    if error_message:
        return JSONResponse(status_code=422, content={"code": "bad_request", "message": error_message})

    # ④ 组装走三方共用核心（§3.1）。X-User-Id 由 Java 注入；开发机直连无此头时
    #    退回 "dev" 命名空间，与 INTERNAL_TOKEN 空串同属直连调试语义（生产必有头）。
    user_id = (x_user_id or "").strip() or "dev"
    thread_key = f"u{user_id}:{body.threadId}"

    # ④.5 HITL 未决检查（W5 §3.10）：该 thread 已有未决审批 → 409，引导先处理审批卡。
    #     不选"自动拒绝旧审批再答新问题"：resume reject 会触发一次模型回应（额外 token +
    #     混入上一问的答非所问文本）；409 语义最清晰。过期项已在 find_by_thread 惰性清理。
    if approval.find_by_thread(thread_key) is not None:
        return JSONResponse(
            status_code=409,
            content={"code": "pending_approval_exists", "message": _APPROVAL_ERROR_MESSAGES["pending_approval_exists"]},
        )

    started_at = time.perf_counter()  # 计时口径与 W3 一致：组装（检索）之前起
    hitl = (x_user_hitl or "").strip() == "1"
    try:
        pt = prepare_turn(body.question, thread_key, api_key=api_key, hitl=hitl)
    except Exception as exc:  # noqa: BLE001 - 组装失败（检索/组装层）走流前 500
        logger.error("prepare_turn failed: type=%s message=%s", type(exc).__name__, str(exc)[:300])
        return JSONResponse(
            status_code=500,
            content={"code": "internal_error", "message": "服务内部错误，请稍后重试"},
        )

    # ⑤ SSE 流：text/event-stream + 禁代理缓冲双保险（X-Accel-Buffering 是
    #   nginx 系约定头，§2.5 同款；Caddy 侧另有 flush_interval -1）。
    #   外面套断连守卫（_guarded_sse_stream）：GeneratorExit 确定性触发，§9.2-5。
    return StreamingResponse(
        _guarded_sse_stream(
            _chat_event_stream(pt, body.question, started_at, key_fingerprint),
            request.receive,
            thread_key,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/agent/chat/approval")
def agent_chat_approval(
    body: ApprovalRequest,
    request: Request,
    x_internal_token: str | None = Header(default=None),
    x_user_llm_key: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
):
    """审批恢复端点（W5 §3.4 [批准]/[拒绝]）：pop → 重建 agent → Command(resume) → SSE。

    检查顺序：内部头 403 → key 缺失 401 → 参数 422（threadId/decision）→
    pop 失败 409/410 → 跨用户防御 409 → SSE 流。resume 轮不走 prepare_turn
    （§3.5：不重跑检索，context_block 从注册表原样复用）。
    """
    # ① 内部头（与 /agent/chat 同款）
    if not internal_token_ok(x_internal_token):
        logger.warning("forbidden: X-Internal-Token missing or wrong (approval)")
        return JSONResponse(status_code=403, content={"code": "forbidden", "message": "内部令牌缺失或不正确"})

    # ② BYOK key（与 /agent/chat 同款；审批请求必须带 key——恢复轮还要调模型）
    api_key = (x_user_llm_key or "").strip() or None
    if api_key is None and not DEEPSEEK_API_KEY:
        return JSONResponse(
            status_code=401,
            content={"code": "llm_key_missing", "message": "缺少 X-User-LLM-Key 请求头"},
        )
    key_fingerprint = hashlib.sha256(api_key.encode()).hexdigest()[:8] if api_key else "env"

    # ③ 参数校验：threadId 必须 UUID（与 /agent/chat 同口径）；decision 白名单
    try:
        uuid_lib.UUID(str(body.threadId))
    except (TypeError, ValueError):
        return JSONResponse(status_code=422, content={"code": "bad_request", "message": "threadId 必须是 UUID"})
    if body.decision not in ("approve", "reject"):
        return JSONResponse(
            status_code=422,
            content={"code": "approval_bad_decision", "message": "decision 仅支持 approve / reject"},
        )
    if not body.approvalId.strip():
        return JSONResponse(status_code=422, content={"code": "bad_request", "message": "approvalId 不能为空"})

    # ④ pop：原子取出（锁内，双击第二次必 NotFound）。NotFound→409 / Expired→410。
    try:
        entry = approval.pop(body.approvalId)
    except approval.ApprovalError as exc:
        return JSONResponse(
            status_code=exc.http_status,
            content={"code": exc.code, "message": _APPROVAL_ERROR_MESSAGES.get(exc.code, exc.code)},
        )

    # ⑤ 跨用户防御：注册表里的 thread_key 必须与请求者一致（approvalId 是 uuid4 难枚举，
    #    但归属校验零成本——不匹配按 not_found 报，不泄露存在性）
    user_id = (x_user_id or "").strip() or "dev"
    thread_key = f"u{user_id}:{body.threadId}"
    if entry["thread_key"] != thread_key:
        logger.warning("approval thread mismatch: approval_owner=%s requester=%s", entry["thread_key"], thread_key)
        return JSONResponse(
            status_code=409,
            content={"code": "approval_not_found", "message": _APPROVAL_ERROR_MESSAGES["approval_not_found"]},
        )

    started_at = time.perf_counter()
    # ⑥ SSE 恢复流（断连守卫与 /agent/chat 同款）
    return StreamingResponse(
        _guarded_sse_stream(
            _approval_event_stream(entry, body.decision, api_key, started_at, key_fingerprint),
            request.receive,
            thread_key,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    # 直跑仅为便利；正式启动用 uvicorn agent.server:app --port 8000（单 worker，§3.2）
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000, workers=1)

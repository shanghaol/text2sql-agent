"""Agent 组装模块：把模型、工具、系统提示词、记忆拼成一个完整 Agent。

LangChain v1 的写法：用 create_agent 一次性组装，不再使用
AgentExecutor / initialize_agent / ConversationBufferMemory 等废弃 API。

W2 改造（设计 §3.5 / §4.3）：
- 系统提示词从"W1 全静态内嵌三表 DDL"改为"静态骨架 + 动态参考资料"：
  build_system_prompt(context_block) 拼装，检索层每轮注入相关表结构/口径/示例；
- build_agent(system_prompt) 参数化，每轮对话用新 prompt 重建 agent
  （组装成本微秒级，模型客户端连接可复用）；
- checkpointer 提为模块级单例：多次 build_agent 共享同一个记忆存储，
  多轮对话的记忆才能跨请求（跨轮重建）保留；
- get_retriever() 懒加载混合检索器（fastembed 模型加载约几秒，进程内只做一次）。

W4 改造（设计 §1.6）：build_agent 增加 api_key 参数（BYOK 每请求 key），
get_chat_model 按 sha256(key)[:16]+模型名 LRU 缓存 ChatDeepSeek 实例——
每个实例内含 openai SDK client（httpx 连接池），不缓存则每轮付 TCP+TLS 握手税。
**绝不改为"全局单例 client、每请求换 api_key"**：openai SDK 的 api_key 在
client 构造时绑定，并发下换 key 是数据竞争，两个用户的请求可能带上对方的
key——那是安全事故不是性能问题。

W5 改造（HITL 设计 §3.3）：build_agent 增加 hitl 参数（默认 False）。hitl=True 时
注入 HumanInTheLoopMiddleware——只有 execute_readonly_sql 需要审批
（interrupt_on 名单外如 list_tables 自动放行），停车点在工具执行前，
resume 用 Command({"resume": {"decisions": [...]}}) 原地续跑（§3.5）。
hitl=False 时**不传 middleware 参数**，构造路径与 W4 逐字节等价
（CLI / 评测 / W4 行为回归零变化，条件传参模式与 api_key 同款）。
"""

import hashlib
import threading
from collections import OrderedDict
from collections.abc import Sequence

from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain_core.messages import HumanMessage
from langchain_deepseek import ChatDeepSeek
from langgraph.checkpoint.memory import InMemorySaver

from agent.config import DEEPSEEK_MODEL, EMBEDDING_MODEL, LLM_CLIENT_CACHE_SIZE
from agent.rag.chroma_store import get_collection
from agent.rag.embedder import BgeEmbedder
from agent.rag.hybrid_retriever import HybridRetriever, RetrievedChunk
from agent.tools.sql_tools import execute_readonly_sql, list_tables

# ---------------------------------------------------------------------------
# 系统提示词：静态骨架（数据库 + 行为规则）+ 动态参考资料占位。
# 静态部分只保留"检索失灵时的兜底"知识（6 行表一览 + 数据锚点），
# 详细的表结构 / 业务口径 / 示例问答由检索层按当前问题动态注入。
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT_TEMPLATE = """你是一名电商数据分析助手，帮助用户用自然语言查询和分析电商数据。

【数据库】
- MySQL 8.0，库名 mart，通过只读账号访问。SQL 必须使用 MySQL 方言：
  日期格式化用 DATE_FORMAT(列, '%Y-%m')，当前日期用 CURDATE()，日期回退用
  DATE_SUB(日期, INTERVAL n DAY)，字符串拼接用 CONCAT()；
  禁止使用 SQLite 的 strftime() 和 date('now')。
- 库中数据表一览（检索失灵时的兜底；写 SQL 前仍应以【参考资料】里的列说明为准）：
  - users：用户表，约 2 万行（ID、姓名、性别、城市/省份、注册时间）
  - products：商品表，约 2 千行（ID、名称、类目、品牌、原价元、上架日期）
  - orders：订单主表，约 10 万行（订单号、用户、状态、金额、件数、下单时间）
  - order_items：订单明细表，约 15 万行（一行 = 订单里的一种商品）
  - payments：支付流水表，约 8.9 万行（一笔成功支付恰一条记录）
  - refunds：退款记录表，约 9 千行（退款金额、原因、完成时间；一单至多一条）
- 数据范围为 2025-10-01 至 2026-09-30 的静态快照：用户问“今天/本月/近 N 天”
  等相对时间时，一律以 2026-09-30 为“今天”计算。

【参考资料】
{context_section}

【行为规则】
1. 写 SQL 前先依据【参考资料】中的表结构和业务口径；资料未覆盖时用 list_tables 工具探查。
2. 只生成 MySQL 方言的只读 SQL（SELECT / WITH 开头），任何写操作都会被拒绝。
3. 所有金额单位是元。回答口径类问题时说明所用口径（如“按支付成功金额计”）。
4. 回答必须基于工具返回的真实结果；查不到就如实说明，不要编造。
5. 用中文回答，关键数字说明来源（基于哪次查询的结果）。
"""

# 检索一条没命中时的兜底文案：提示模型走 list_tables 探查
_NO_CONTEXT_SECTION = "（本次没有检索到相关资料。请先用 list_tables 工具了解库结构，再写 SQL 查询。）"

# 检索片的 doc_type → 注入标题里的类型名（设计 §3.5 的 `--- [N] 类型 · 标题 ---`）
_DOC_TYPE_LABELS = {
    "schema": "表结构",
    "glossary": "业务口径",
    "qa_pair": "示例问答",
}


def format_context_block(chunks: Sequence[RetrievedChunk]) -> str:
    """把检索结果格式化成注入 prompt 的【参考资料】文本块。

    每片一个标题行 `--- [序号] 类型 · 标题 ---` + chunk 全文；
    没有检索结果时返回空字符串（由 build_system_prompt 换成兜底文案）。
    """
    sections: list[str] = []
    for i, chunk in enumerate(chunks, 1):
        label = _DOC_TYPE_LABELS.get(chunk.doc_type, "资料")
        sections.append(f"--- [{i}] {label} · {chunk.title} ---\n{chunk.text}")
    return "\n\n".join(sections)


def build_system_prompt(context_block: str) -> str:
    """拼装系统提示词：静态骨架 + 动态【参考资料】块。

    context_block 是 format_context_block 的产物；为空时用兜底文案，
    引导模型改走 list_tables 工具探查。
    """
    context_section = context_block.strip() if context_block and context_block.strip() else _NO_CONTEXT_SECTION
    return _SYSTEM_PROMPT_TEMPLATE.format(context_section=context_section)


# ---------------------------------------------------------------------------
# 模块级单例：checkpointer（多轮记忆）与检索器（embedding 模型）
# 都只在进程内初始化一次，多次 build_agent / 多轮对话共享。
# ---------------------------------------------------------------------------

_checkpointer: InMemorySaver | None = None


def get_checkpointer() -> InMemorySaver:
    """会话记忆单例：每轮重建 agent 时传入同一个 checkpointer，多轮记忆不丢。"""
    global _checkpointer
    if _checkpointer is None:
        _checkpointer = InMemorySaver()
    return _checkpointer


_retriever: HybridRetriever | None = None


def get_retriever() -> HybridRetriever:
    """混合检索器懒加载单例：Chroma collection + bge embedding（首次约几秒）。"""
    global _retriever
    if _retriever is None:
        collection = get_collection()
        # 生产由 FASTEMBED_CACHE_DIR 指到稳定缓存路径（%TEMP% 默认在服务器不可靠，
        # W4 部署实录：缓存缺失 → 运行时向 HuggingFace 下载 → 国内网络挂死）
        import os

        embedder = BgeEmbedder(
            model_name=EMBEDDING_MODEL,
            cache_dir=os.environ.get("FASTEMBED_CACHE_DIR") or None,
        )
        _retriever = HybridRetriever(collection, embedder)
    return _retriever


def previous_human_message(checkpointer: InMemorySaver, config: dict) -> str:
    """从 checkpointer 取该 thread 的上一条用户消息（设计 §3.6）。

    多轮问题的检索 query = 上一条用户消息 + 当前输入——否则“那 10 月呢？”
    这种指代式短问在检索层什么都召不回。首轮没有历史时返回空字符串。
    """
    checkpoint_tuple = checkpointer.get_tuple(config)
    if checkpoint_tuple is None:
        return ""
    messages = (checkpoint_tuple.checkpoint.get("channel_values") or {}).get("messages") or []
    for message in reversed(list(messages)):
        if isinstance(message, HumanMessage):
            content = message.content
            return content if isinstance(content, str) else str(content)
    return ""


# ---------------------------------------------------------------------------
# LLM 客户端缓存（W4 设计 §1.6）：按 key 指纹复用 ChatDeepSeek 实例
# ---------------------------------------------------------------------------

# 缓存键 = (sha256(key)[:16] 或 "__env__", 模型名)。不缓存明文 key 做键
# （避免明文常驻 dict 内存），哈希前 16 位碰撞概率在此规模可忽略；
# 不同用户不同实例，auth 隔离天然正确。
_LLM_CACHE: OrderedDict[tuple[str, str], ChatDeepSeek] = OrderedDict()
_LLM_CACHE_LOCK = threading.Lock()


def get_chat_model(api_key: str | None) -> ChatDeepSeek:
    """取（或建）一个 ChatDeepSeek 实例：同一 key 复用同一实例（LRU 缓存）。

    - 同 key 同实例：命中即移到队尾（LRU 语义），复用其 httpx 连接池；
    - 不同 key 不同实例：并发请求互不串号（见模块 docstring 的反面教材）；
    - api_key=None：走环境变量回退，构造参数与 W3 现状逐字节等价——
      条件传参，None 时绝不传 api_key 参数（CLI 与评测路径零变化）；
    - 容量 LLM_CLIENT_CACHE_SIZE（默认 16），超出逐出最旧条目
      （丢弃引用由 GC 回收 httpx 连接池；openai SDK 未提供逐实例显式
      close 的公开途径，§9.2-4 已确认靠 GC，无连接泄漏告警）。
    """
    cache_key = (hashlib.sha256(api_key.encode()).hexdigest()[:16] if api_key else "__env__", DEEPSEEK_MODEL)
    with _LLM_CACHE_LOCK:
        if cache_key in _LLM_CACHE:
            _LLM_CACHE.move_to_end(cache_key)
            return _LLM_CACHE[cache_key]
    kwargs: dict = {"model": DEEPSEEK_MODEL, "api_base": "https://api.deepseek.com", "temperature": 0}
    if api_key:
        kwargs["api_key"] = api_key  # 只有显式给了才传——None 时绝不多传参数
    model = ChatDeepSeek(**kwargs)
    with _LLM_CACHE_LOCK:
        _LLM_CACHE[cache_key] = model
        while len(_LLM_CACHE) > LLM_CLIENT_CACHE_SIZE:  # LRU 逐出（线程锁保护）
            _LLM_CACHE.popitem(last=False)
    return model


def _hitl_sql_description(tool_call: dict, state, runtime) -> str:
    """interrupt 载荷里 action_request 的 description（前端审批卡/审计用）。

    2026-10-11 实测（langchain 1.4.3）：description 是 interrupt_on 每工具配置里的字段
    （str 或 callable），callable 签名 (tool_call, state, runtime)——不是 middleware
    构造器的 kwarg（构造器只有 description_prefix）。
    """
    args = tool_call.get("args") or {}
    sql = args.get("sql") if isinstance(args, dict) else None
    return f"Agent 请求执行 SQL，等待人工确认：\n{sql if isinstance(sql, str) and sql else '（未提供 SQL）'}"


def build_agent(system_prompt: str, api_key: str | None = None, hitl: bool = False):
    """组装并返回一个带会话记忆的 Text-to-SQL Agent。

    组成部分：
    - model：ChatDeepSeek（get_chat_model 按 key 缓存复用），temperature=0
      让输出更稳定（适合生成 SQL）；api_key=None 时与 W3 现状逐字节等价
      （环境变量回退），显式传 key 时该请求用用户自己的 key（BYOK）
    - tools：两个 SQL 工具（查表、执行只读查询）
    - system_prompt：调用方传入（静态骨架 + 本轮动态检索注入）
    - checkpointer：模块级单例，保存对话历史让 Agent 记住上下文；
      调用时通过 config={"configurable": {"thread_id": ...}} 区分不同会话
    - middleware（hitl=True 才传，W5 §3.3）：HumanInTheLoopMiddleware 在
      execute_readonly_sql 执行前中断等人工批准/拒绝；list_tables 不在名单
      自动放行。不给 edit/respond 决策——编辑 SQL 超出 W5 范围（§9-1 备选）。
      hitl=False 时绝不传 middleware 参数（与 W4 构造路径逐字节等价）。
    """
    model = get_chat_model(api_key)
    kwargs: dict = {}
    if hitl:
        kwargs["middleware"] = [
            HumanInTheLoopMiddleware(
                interrupt_on={
                    "execute_readonly_sql": {
                        # allowed_decisions 必须非空（middleware 构造器校验，空列表 ValueError）
                        "allowed_decisions": ["approve", "reject"],
                        "description": _hitl_sql_description,
                    }
                }
            )
        ]
    agent = create_agent(
        model=model,
        tools=[execute_readonly_sql, list_tables],
        system_prompt=system_prompt,
        checkpointer=get_checkpointer(),
        **kwargs,
    )
    return agent

"""Agent 组装模块：把模型、工具、系统提示词、记忆拼成一个完整 Agent。

LangChain v1 的写法：用 create_agent 一次性组装，不再使用
AgentExecutor / initialize_agent / ConversationBufferMemory 等废弃 API。
"""

from langchain.agents import create_agent
from langchain_deepseek import ChatDeepSeek
from langgraph.checkpoint.memory import InMemorySaver

from agent.config import DEEPSEEK_MODEL
from agent.tools.sql_tools import execute_readonly_sql, list_tables

# ---------------------------------------------------------------------------
# 系统提示词：Agent 的"岗位说明书"。
# 一份好的系统提示词 = 角色定位 + 领域知识（表结构）+ 行为规则。
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """你是一名电商数据分析助手，帮助用户用自然语言查询和分析电商数据。

【表结构】
你只可以查询以下三张表（SQLite 数据库）：

CREATE TABLE users (
    id INTEGER PRIMARY KEY,   -- 用户 ID
    name TEXT,                -- 用户姓名
    city TEXT,                -- 所在城市
    registered_at TEXT        -- 注册日期（YYYY-MM-DD）
);

CREATE TABLE products (
    id INTEGER PRIMARY KEY,   -- 商品 ID
    name TEXT,                -- 商品名称
    category TEXT,            -- 类目（数码/家居/服饰）
    price REAL                -- 单价（元）
);

CREATE TABLE orders (
    id INTEGER PRIMARY KEY,   -- 订单 ID
    user_id INTEGER,          -- 下单用户，对应 users.id
    product_id INTEGER,       -- 购买商品，对应 products.id
    quantity INTEGER,         -- 购买数量
    amount REAL,              -- 订单金额（元）
    status TEXT,              -- 订单状态：paid=已支付 / refunded=已退款 / pending=待支付
    created_at TEXT           -- 下单时间（YYYY-MM-DD HH:MM:SS）
);

（说明：W1 直接把表结构写死在提示词里；W2 起这段会改为 RAG 动态检索注入。）

【行为规则】
1. 需要数据时，先用 list_tables 了解库结构，再写 SQL 调用 execute_readonly_sql 查询；
   绝对不要凭空编造数据。
2. 只生成 SQLite 方言的只读 SQL（SELECT / WITH 开头），任何写操作都会被工具拒绝。
3. 所有金额的单位都是元。
4. 回答必须基于工具返回的真实结果；如果查询不到数据，就如实说明查询不到，
   不要猜测或编造。
5. 用中文回答。回答中的关键数字要说明来源（基于哪次查询的结果）。
"""


def build_agent():
    """组装并返回一个带会话记忆的 Text-to-SQL Agent。

    组成部分：
    - model：ChatDeepSeek，temperature=0 让输出更稳定（适合生成 SQL）
    - tools：两个 SQL 工具（查表、执行只读查询）
    - system_prompt：上面定义的角色与规则
    - checkpointer：InMemorySaver 保存对话历史，让 Agent 记住上下文；
      调用时通过 config={"configurable": {"thread_id": ...}} 区分不同会话
    """
    model = ChatDeepSeek(
        model=DEEPSEEK_MODEL,
        api_base="https://api.deepseek.com",
        temperature=0,  # 温度 0：SQL 生成要的是确定性，不是创造力
    )
    agent = create_agent(
        model=model,
        tools=[execute_readonly_sql, list_tables],
        system_prompt=SYSTEM_PROMPT,
        checkpointer=InMemorySaver(),
    )
    return agent

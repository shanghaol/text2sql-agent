# Text-to-SQL 电商数据分析助手（W2）

用自然语言问数据，单 Agent（LangChain v1 `create_agent` + DeepSeek）通过 **混合检索**（BM25 + 向量 + 自实现 RRF）获取表结构与业务口径，生成 **MySQL 只读 SQL** 查询电商数据集并回答。架构与决策依据见《开发需求Prompt-Text-to-SQL分析助手.md》与《W2设计-数据与检索层.md》。

## 运行步骤

前置：Docker Desktop（跑 MySQL 8.0）；Python 3.10+。

```bash
# 1. 虚拟环境 + 依赖
python -m venv .venv
source .venv/Scripts/activate
pip install -r requirements.txt

# 2. 配置：复制 .env.example 为 .env，填入
#    DEEPSEEK_API_KEY（platform.deepseek.com 申请）
#    MYSQL_ADMIN_PASSWORD / MYSQL_RO_PASSWORD（自定，仅存本机）
cp .env.example .env

# 3. 启动 MySQL 容器（本机 3306 被占时映射 3307，.env 的 MYSQL_PORT 要一致）
docker run -d --name text2sql-mysql -p 3307:3306 \
  -e MYSQL_ROOT_PASSWORD=<与 .env 的 MYSQL_ADMIN_PASSWORD 一致> \
  -v text2sql_mysql_data:/var/lib/mysql \
  mysql:8.0 --character-set-server=utf8mb4 --collation-server=utf8mb4_0900_ai_ci

# 4. 生成数据集并导入（建 app/mart 双库 + 6 表 + 只读账号，约 37 万行，幂等可重跑）
.venv/Scripts/python data/generate_dataset.py --seed 42 --load

# 5. 构建 RAG 索引（解析 data/rag_corpus → Chroma，28 片；语料改动后重跑）
.venv/Scripts/python -m agent.rag.build_index

# 6. 启动对话
.venv/Scripts/python -m agent.main
```

问两句试试：`上个月 GMV 多少？` → `环比呢`（多轮追问会自动带上下文）。

## 运行测试

```bash
.venv/Scripts/python -m pytest tests/ -q   # 有 MySQL 配置时门控用例自动启用
.venv/Scripts/python -m ruff check agent/ tests/
```

默认套件不依赖 MySQL / API Key / 模型下载，裸机可跑全绿。

## 目录速览

```
agent/            # Python：Agent 核心 + 检索层 + SQL 工具
  rag/            # 混合检索（Chroma + BM25 + RRF）与索引构建
  tools/          # execute_readonly_sql / list_tables（三层只读防线）
  core/           # create_agent 组装与系统提示词
data/
  schema.sql          # mart 库建表 DDL
  generate_dataset.py # 数据集生成 + 建库 + 导入（Faker，固定种子可复现）
  rag_corpus/         # RAG 语料：6 表文档 + 11 条口径 + 11 条金标 QA
backend/ frontend/ eval/   # W4 起逐步启用
docs/             # 需求、设计、教学材料、实验记录
```

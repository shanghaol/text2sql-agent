# Text-to-SQL 电商数据分析助手（W1）

用自然语言问数据，Agent 自动生成只读 SQL、查询本地 SQLite 演示库并回答。
基于 LangChain v1（`create_agent` + `@tool` + checkpointer + 流式输出）。

## W1 运行步骤

```bash
# 1. 创建并激活虚拟环境（Windows / Git Bash）
python -m venv .venv
source .venv/Scripts/activate

# 2. 安装依赖
pip install -r requirements.txt

# 3. 配置密钥：复制 .env.example 为 .env，填入你的 DeepSeek API Key
cp .env.example .env
# 然后编辑 .env，把 DEEPSEEK_API_KEY 换成你在 https://platform.deepseek.com 申请的 key

# 4. 生成演示数据库（data/demo.db，10 用户 / 8 商品 / 30 订单）
.venv/Scripts/python -m agent.db.seed

# 5. 启动对话
.venv/Scripts/python -m agent.main
```

## 运行测试

测试不依赖 API Key，可随时运行：

```bash
.venv/Scripts/python -m pytest tests/ -v
```

## 代码检查

```bash
.venv/Scripts/python -m ruff check agent/ tests/
```

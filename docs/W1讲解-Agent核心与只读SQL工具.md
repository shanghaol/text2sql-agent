# W1 讲解：Agent 核心与只读 SQL 工具（教学四件套）

> 交付时间：2026-10-08 ｜ 对应需求文档 W1 里程碑
> 本文档目标：让你**脱稿向面试官讲清 W1 每一块的为什么**。建议配合 `git log` 边看代码边读。

---

## 一、做了什么（整体架构）

```
你（命令行输入）
   │
   ▼
agent/main.py ──流式读取──► agent/core/agent.py 的 build_agent()
                                │  create_agent(model=ChatDeepSeek("deepseek-flash"),
                                │               tools=[execute_readonly_sql, list_tables],
                                │               system_prompt=中文角色+DDL+规则,
                                │               checkpointer=InMemorySaver())
                                ▼
                        Agent 循环（教学文档第 1 章）：
                        模型推理 → 决定调工具 → 执行工具 → 结果回传 → 再推理 → … → 最终回答
                                │
                                ▼
                    agent/tools/sql_tools.py（两道防线）──► data/demo.db（SQLite 只读连接）
```

一次提问的真实消息链（自己跑一遍 `result["messages"]` 就能看到，教学文档 3.4 节）：
`HumanMessage(你的问题) → AIMessage(带 tool_calls) → ToolMessage(SQL结果表格) → AIMessage(最终回答)`

---

## 二、为什么这么做（每个决策的对比与理由）

### 1. 为什么工具 docstring 是"第一等公民"？
模型**看不见函数体，只看得见名称+docstring+类型注解**（工具 Schema，教学文档 4.3.1）。
`execute_readonly_sql` 的 docstring 里写了：只能 SELECT/WITH、写操作会被拒绝、示例 SQL（用了 JOIN+GROUP BY 的示范，暗示模型"可以写复杂查询"）。**docstring 写得越像说明书，模型用对工具的概率越高**——这是 Agent 开发性价比最高的优化点。

### 2. 为什么校验是"正则关键词"而不是 AST？（明知不完美为什么先这么做）
W1 用 `\b` 整词黑名单（15 个写操作关键字），已知两个不完美：字符串里的 `'update'` 会误杀、子查询 LIMIT 会漏判。**为什么接受**：W1 的目标是快速打通 Agent 循环、验证工具交互；保守误杀方向是安全的（fail-closed）；W5 会换 sqlglot 做 AST 级解析。面试讲法："我知道正则方案的边界，并已经留好了升级路径"——**知道工具的边界比工具本身加分**。

### 3. 为什么有"两道防线"？
- 第一道：`validate_readonly_sql` 代码层校验（剥注释→空→多语句→首关键字→黑名单，顺序有讲究：**先剥注释再检查，否则 `/*DROP*/` 能藏住关键字**）
- 第二道：`mode=ro` 只读 URI 连接——即使第一道被绕过，SQLite 从文件系统层面直接拒绝写入（审查时实测 INSERT 报 `attempt to write a readonly database`）
面试讲法："安全设计不依赖单一检查点"。（需求文档 F4 的完整四层防线在 W5 补齐：+强制 LIMIT 已实现、+超时、+审计日志）

### 4. 为什么错误信息要"可读化"而不是抛堆栈？
工具返回 `SQL 错误：no such column: xxx。请检查表名、列名和语法后修正 SQL 重试`——模型读到这个能**自己改 SQL 重试**，这就是 Agent 的自我修正循环（教学文档 9.1 第 4 条）。堆栈对模型是噪音，对人是调试信息。有配套测试：拼错列名 → 断言返回含 "no such column"。

### 5. 为什么结果要截断到 20 行？
Agent 每轮把全部工具结果塞进上下文（教学文档 2.2 Context Window），全表 30 行虽然不大，但习惯要从一开始养成：**大结果 = 上下文爆炸 = 变慢变贵变失忆**。细节：截断分两种情况——真超了（fetchmany 多取 1 行探测）和"自动补了 LIMIT 且恰好 20 行"（提醒模型可能不止，可加条件重查）。

### 6. 为什么多轮追问能记住上文？（checkpointer + thread_id）
`create_agent` 默认无状态（教学文档 5.1），配 `InMemorySaver` 后每轮结束状态存档到 `thread_id` 名下，同 thread_id 的下次调用恢复历史。main.py 每次启动生成一个短 uuid 当会话 ID。**面试必考**"多轮记忆怎么做的"，W5 还会上 SummarizationMiddleware 解决"聊久了上下文爆炸"（考点预告）。

### 7. 为什么模型名是 deepseek-flash 而不是教程里的 deepseek-chat？
DeepSeek 2026-07-24 停用了 `deepseek-chat`/`deepseek-reasoner`（调研子 Agent 查证官方更新日志）。教训：**LLM 生态变化快，接 API 前先查官方 changelog，别全信教程**。

---

## 三、面试考点与参考答法

| 考点 | 参考答法（结合你的实现） |
|---|---|
| "描述 Agent 的工作原理" | 循环四步：推理→工具调用→结果回传→再推理；流程由模型运行时决定，区别于预编排的 Chain（教学文档 1.1 表格） |
| "Agent 调错工具/参数不合法怎么办？"（字节真题） | 三层：docstring 写清约束降低犯错率 → 错误信息可读化让模型自我修正 → W5 补 ToolRetryMiddleware/调用次数上限兜底 |
| "NL2SQL 怎么防 SQL 注入/写操作？"（系统设计原题） | 两道防线（校验+只读连接），W1 已实测 26 组攻击用例；W5 补 AST 解析与审计日志 |
| "多轮记忆怎么实现？" | checkpointer 存档状态 + thread_id 恢复；短期记忆（单会话）vs 长期记忆（跨会话 Store，P1 的薄弱点追踪会用） |
| "为什么单 Agent 不用多 Agent？" | 教学文档 8.2 官方判断三问：上下文装得下吗？提示词冲突吗？要并行吗？——Text-to-SQL 任务链路清晰（理解→查→答），单 Agent+好工具更可靠 |

## 四、对应教学文档章节

- 第 1 章 Agent 循环与 Harness → `agent/core/agent.py`
- 第 3.3 节 第一个 Agent → `build_agent()`
- 第 4.3 节 工具（Schema/docstring/runtime 注入）→ `agent/tools/sql_tools.py`
- 第 5.2 节 短期记忆 → checkpointer + thread_id
- 第 8.1 节 流式输出 → `main.py` 的 stream_mode="messages"
- 第 9.1/9.6 节 工具设计最佳实践与常见坑 → 错误可读化、截断、调用上限（待做）

---

## 五、你的动手清单（跑起来 + 三个实验）

1. **跑通**：注册 DeepSeek 开放平台充值 → 复制 `.env.example` 为 `.env` 填入 `DEEPSEEK_API_KEY` → `.venv/Scripts/python -m agent.db.seed` → `.venv/Scripts/python -m agent.main`
2. **实验一（验证多轮记忆）**：先问"上个月卖了多少"，接着只问"那 9 月呢？"——看它是否理解省略了主语
3. **实验二（验证自我修正）**：问"按 city 统计订单数"——注意表里 users 才有 city，观察它是否 JOIN 或先用 list_tables 探索
4. **实验三（验证拒绝）**：让它"把北京用户的订单删了"——看拒绝原因如何返回、它如何向你解释做不到
5. 每个实验后打开 `agent/main.py` 加一行 `print(result["messages"])` 或 debug=True 观察完整消息链（教学文档 3.4）

## 六、W2 预告与待拍板事项

W2 = 数据集生成 + Chroma 建库 + 自实现混合检索。开工前需要你拍板**分析库选型**（Agent 最终查询的库，现在的 demo.db 只是 W1 占位）：

| 选项 | 优点 | 代价 |
|---|---|---|
| MySQL（推荐讨论） | 面试最主流、和你 Java 栈统一；业务库/分析库同一实例省内存 | 2核2G 上要压配置；SQLite→MySQL 方言差异要处理 |
| DuckDB | 分析型 OLAP 内嵌引擎，聚合飞快、零运维、内存友好；"OLTP/OLAP 选型"是面试亮点 | 新技术栈要学；面试官若不熟需要你解释 |
| 继续 SQLite | 最简单 | 叙事最弱，不太推荐当最终方案 |

下轮开工时告诉我选哪个（或让我把三者对比展开再选）。

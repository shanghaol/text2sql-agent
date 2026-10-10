"""全局配置模块：统一管理密钥、模型名、MySQL / 检索层配置。

为什么单独抽一个 config.py？
- 代码里不应该到处硬编码路径和密钥；
- .env 文件只在本机存在（已加入 .gitignore），密钥不会误提交到 git。

使用方式：其他模块 `from agent.config import MYSQL_HOST` 等即可拿到配置好的值。
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# 仓库根目录：config.py 位于 <根>/agent/config.py，
# parents[1] 即向上两级，得到仓库根目录。
# （parents[0] 是 agent/ 目录本身，parents[1] 才是仓库根。）
REPO_ROOT = Path(__file__).resolve().parents[1]

# 启动时加载仓库根目录下的 .env 文件（如果存在）。
# load_dotenv 会把 .env 里的 KEY=VALUE 写入 os.environ，
# 之后用 os.environ.get 就能读到；文件不存在也不会报错。
load_dotenv(REPO_ROOT / ".env")

# DeepSeek 的 API 密钥：从环境变量读取。
# 注意：这里缺失时**不报错**——只有真正调用模型的地方（main.py）才需要它，
# 纯本地功能（如测试、建库）不该因为没配 key 而失败。
DEEPSEEK_API_KEY: str = os.environ.get("DEEPSEEK_API_KEY", "")

# 模型名：默认 deepseek-flash，允许通过环境变量 DEEPSEEK_MODEL 覆盖，
# 方便以后换模型时不用改代码。
DEEPSEEK_MODEL: str = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")

# ---------------------------------------------------------------------------
# W2 新增：MySQL 连接配置（设计 §4.2）
# Agent 用只读账号 text2sql_ro（仅 SELECT ON mart.*）；
# 管理员账号只给 data/generate_dataset.py 建库/导入用，Agent 代码不碰它。
# ---------------------------------------------------------------------------

MYSQL_HOST: str = os.environ.get("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT: int = int(os.environ.get("MYSQL_PORT", "3306"))
# 凭据变量名按消费者拆分（W4 教训：共享 MYSQL_PASSWORD 会让 ro 用户配上 app 密码 → 1045）
# Java 用 MYSQL_APP_*（app 库读写）；Python 用 MYSQL_RO_*（mart 库只读）
MYSQL_USER: str = os.environ.get("MYSQL_RO_USER", "text2sql_ro")
# 密码变量名映射：优先只读专用名 MYSQL_RO_PASSWORD（生产 env.prod 的名字）；
# 本地 .env 也只维护这一个名字。MYSQL_PASSWORD 不再作为回退（生产它属于 app 账号）。
MYSQL_PASSWORD: str = os.environ.get("MYSQL_RO_PASSWORD", "")
MYSQL_DB: str = os.environ.get("MYSQL_DB", "mart")
# 仅数据脚本（generate_dataset.py）使用，Agent 运行链路不读这两个值
MYSQL_ADMIN_USER: str = os.environ.get("MYSQL_ADMIN_USER", "root")
MYSQL_ADMIN_PASSWORD: str = os.environ.get("MYSQL_ADMIN_PASSWORD", "")

# ---------------------------------------------------------------------------
# W2 新增：检索层配置（Chroma 索引位置 / Embedding 模型 / 注入条数）
# ---------------------------------------------------------------------------

# Chroma 持久化目录：绝对化（坑清单 #1：相对路径换启动目录即"数据丢失"）。
# 允许环境变量覆盖；传相对路径时按仓库根拼接，传绝对路径时 pathlib 直接采用。
_chroma_dir = Path(os.environ.get("CHROMA_PERSIST_DIR", "data/chroma_db"))
CHROMA_PERSIST_DIR: Path = _chroma_dir if _chroma_dir.is_absolute() else REPO_ROOT / _chroma_dir

# Embedding 模型：bge-small-zh-v1.5（512 维），fastembed 本地 ONNX 加载
EMBEDDING_MODEL: str = os.environ.get("EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5")

# 每轮注入 prompt 的检索条数上限（W6 消融调参入口，设计 §3.4 建议 8）
RETRIEVER_TOP_N: int = int(os.environ.get("RETRIEVER_TOP_N", "8"))

# ---------------------------------------------------------------------------
# W4 新增：服务化配置（W4 设计 §3.4）
# ---------------------------------------------------------------------------

# Java/Python 共享密钥：非空时 server.py 校验请求头 X-Internal-Token，
# 空串（默认）= 跳过校验，供开发机不经 Java 直连 uvicorn 调试。
# 生产必配（compose 的 .env.prod 注入，§5.5 清单核对）。
INTERNAL_TOKEN: str = os.environ.get("INTERNAL_TOKEN", "")

# 单个 question 的最大字符数：超长直接 422 bad_request，不进检索/模型（§1.6 流前错误）
MAX_QUESTION_CHARS: int = int(os.environ.get("MAX_QUESTION_CHARS", "2000"))

# LLM 客户端缓存容量（§1.6）：按 key 指纹缓存的 ChatDeepSeek 实例数上限，
# LRU 逐出。演示规模并发个位数，16 个 httpx 连接池在 2G 预算内可忽略。
LLM_CLIENT_CACHE_SIZE: int = int(os.environ.get("LLM_CLIENT_CACHE_SIZE", "16"))

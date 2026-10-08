"""全局配置模块：统一管理密钥、模型名、数据库路径。

为什么单独抽一个 config.py？
- 代码里不应该到处硬编码路径和密钥；
- .env 文件只在本机存在（已加入 .gitignore），密钥不会误提交到 git。

使用方式：其他模块 `from agent.config import DB_PATH` 即可拿到配置好的值。
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

# 演示数据库路径：默认仓库根目录下的 data/demo.db。
# 允许用环境变量 DEMO_DB_PATH 覆盖——这是给测试用的钩子：
# 测试时把库指到 pytest 的临时目录，不会污染真实数据文件。
DB_PATH: Path = Path(os.environ.get("DEMO_DB_PATH", str(REPO_ROOT / "data" / "demo.db")))

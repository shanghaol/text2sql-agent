"""W3 评测包：判定纯函数层（judge）+ 评测 runner（run_eval）。

eval/ 带本 __init__.py 成为常规包，导入方式统一为：
- 测试 / 内部引用：`from eval.judge import ...`（在仓库根目录运行 pytest）；
- 命令行：`.venv/Scripts/python.exe -m eval.run_eval ...`（在仓库根目录运行）。
"""

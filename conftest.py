"""Pytest 引导：确保项目根目录在 sys.path 上。

仓库按 README 使用 ``python3 -m unittest discover -s tests`` 运行；
此文件只在 pytest 收集时生效，对 unittest 无副作用。
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

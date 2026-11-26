"""常用入口别名：``python3 main.py`` 等价于 ``python3 -m yardflow``。"""
from yardflow.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())

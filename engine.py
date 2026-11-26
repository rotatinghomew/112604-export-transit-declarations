"""兼容性入口，规范实现见 :mod:`yardflow.service`。"""
from yardflow.service import *  # noqa: F401,F403
from yardflow import *  # noqa: F401,F403
import yardflow.service as _service  # noqa: F401

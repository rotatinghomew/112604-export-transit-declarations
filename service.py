"""兼容入口：``from service import Service`` 与 ``from yardflow import Service`` 等价。

直接运行 ``python3 service.py`` 等价于 ``python3 -m yardflow``。
"""

from yardflow.service import *  # noqa: F401,F403
from yardflow.service import (  # noqa: F401
    DISPOSITIONS,
    FLEET_ACTIONS,
    GROUP_FIELDS,
    ISSUE_TEXT,
    App,
    CustomsYardService,
    Engine,
    Ledger,
    Project,
    ReleaseService,
    RuleViolation,
    Service,
    YardEngine,
    YardService,
    load_project,
    parse_ts,
    ts_text,
)

if __name__ == "__main__":
    from yardflow.__main__ import main

    raise SystemExit(main())

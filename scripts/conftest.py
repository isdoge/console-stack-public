# -*- coding: utf-8 -*-
"""把 console/backend 的 PG16 隔离夹具暴露给 scripts/ 测试（PG-03 测试基础设施）。

不复制底座逻辑，只 re-export；容器名/端口/库仍由 `pg_test_support` 随机化，
生产闸门（`assert_not_production`）在所有连接前生效。
"""
import pathlib
import sys

BACKEND_DIR = pathlib.Path(__file__).resolve().parent.parent / "console" / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from pg_test_support import (  # noqa: E402,F401
    pg_admin,
    pg_conn,
    pg_conn_two,
    pg_container,
    pg_dsn,
)

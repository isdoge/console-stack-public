# -*- coding: utf-8 -*-
"""公共 pytest 夹具入口。

PG-00 起，真正 PG16 的隔离底座定义在 `pg_test_support.py`；这里只把夹具**暴露**给
pytest 的 fixture 查找，本身不含逻辑。PG-01–04 的测试直接写 `def test_x(pg_dsn): ...`
即可，不必自己起容器。

夹具只在被请求时才启动容器：没有 PG 测试的文件不会因为本文件而变慢或依赖 docker。

**存储引擎声明**（2026-09-10，G06）：`app.py` 的在线入口要求**显式声明**
`CONSOLE_STORAGE_ENGINE`，且 `sqlite` 只与 `CONSOLE_INSTANCE_ROLE=test` 共存
（§11.3.2 G06 / D3 / D7：在线入口强制 PG，SQLite 只作**明确声明**的离线/测试路径）。
21 个 `import app` 的测试因此必须**明说**自己跑在 SQLite 离线底座上——在收集任何
测试模块之前注入，`import app` 时才不会 fail closed。用 `setdefault` 而非直接赋值：
`CONSOLE_PG_TEST_DSN` 等显式指了 PG 的调用方不被覆盖；测试函数内
`monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", ...)` 的用例也不受影响——闸门只在
`import app` 那一刻判定一次。
"""
import os
import pathlib
import sys

os.environ.setdefault("CONSOLE_STORAGE_ENGINE", "sqlite")
os.environ.setdefault("CONSOLE_INSTANCE_ROLE", "test")

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from pg_test_support import (  # noqa: E402,F401
    pg_admin,
    pg_conn,
    pg_conn_two,
    pg_container,
    pg_dsn,
    pg_online,
)

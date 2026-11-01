# -*- coding: utf-8 -*-
"""基础模块的架构门禁（提案 §8.5）：不 import app、import 期不拨号、不建表、不起线程。"""
import pathlib
import subprocess
import sys

BACKEND_DIR = pathlib.Path(__file__).resolve().parent

FOUNDATION_MODULES = (
    "pgdb",
    "pg_locks",
    "pg_contracts",
    "pg_migrations",
    "money",
    "runtime_mode",
    "sqlite_legacy",
)


def _run(source):
    proc = subprocess.run(
        [sys.executable, "-c", source],
        capture_output=True,
        text=True,
        cwd=str(BACKEND_DIR),
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def test_import_db_does_not_pull_in_psycopg_or_pgdb():
    """`import db` 必须保持轻量：没装 psycopg 的环境也能导入 legacy 部分。"""
    out = _run(
        "import sys; import db; "
        "print('pgdb' in sys.modules, 'psycopg' in sys.modules)"
    )
    assert out == "False False"


def test_foundation_modules_do_not_import_app():
    source = (
        "import sys; "
        + "".join(f"import {name}; " for name in FOUNDATION_MODULES)
        + "print('app' in sys.modules)"
    )
    assert _run(source) == "False"


def test_foundation_imports_open_no_sockets():
    """import 期不得拨号：socket.connect 在导入过程中一次都不应被调用。"""
    source = """
import socket
calls = []
_orig = socket.socket.connect
def spy(self, address):
    calls.append(address)
    return _orig(self, address)
socket.socket.connect = spy
import pgdb, pg_locks, pg_contracts, pg_migrations, money, runtime_mode, sqlite_legacy
print(len(calls))
"""
    assert _run(source) == "0"


def test_foundation_modules_expose_no_thread_start_at_import():
    source = """
import threading
started = []
_orig = threading.Thread.start
def spy(self):
    started.append(self.name)
    return _orig(self)
threading.Thread.start = spy
import pgdb, pg_locks, pg_contracts, pg_migrations, money, runtime_mode, sqlite_legacy
print(len(started))
"""
    assert _run(source) == "0"


def test_pg_migrations_does_not_touch_database_on_import():
    source = """
import pg_migrations
print(pg_migrations.PG_SCHEMA_VERSION, len(pg_migrations.SCHEMA_TABLES))
"""
    assert _run(source) == "12 25"


def test_db_exposes_pg_facade_without_connecting():
    out = _run(
        "import db; "
        "print(callable(db.pg_transaction), callable(db.pg_connection), "
        "callable(db.pg_healthcheck), callable(db.pg_close))"
    )
    assert out == "True True True True"

# -*- coding: utf-8 -*-
"""真正 PG16 的隔离测试底座（提案 §3.4/§8.5；契约版本 `pg00-1`）。

给 PG-01–04 的公共夹具，三条设计约束：

1. **真连 PG16**——不用 SQLite 或纯 mock 冒充绿灯。
2. **绝不连生产**——`assert_not_production()` 在建立任何连接前拒绝 NAS 生产 host/库/账号；
   测试账号必须由测试自己创建。
3. **缺 PG 就失败**——`pytest.fail` 而不是 `pytest.skip`。伪 skip 会让「没跑」看起来像「通过」，
   这正是提案 §3.4 明令禁止的。

发现顺序：`CONSOLE_PG_TEST_DSN` → 本机 docker 起 `postgres:16-alpine`（随机容器名/随机端口）
→ 都没有则失败并打印可操作指引。

每个任务用**独立**容器名/端口/库名/文件目录；不要共享 DSN 或数据卷（提案 §8.5）。
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pytest

DEFAULT_IMAGE = os.environ.get("CONSOLE_PG_TEST_IMAGE", "postgres:16-alpine")
ADMIN_USER = "console_test_admin"
ADMIN_PASSWORD = "console_test_admin_pw"
TEST_DB = "console_test"

# `apply_env` 会写的全部键；夹具用它做保存/还原（见 `pg_online`）。
_ENV_KEYS = (
    "CONSOLE_PG_HOST", "CONSOLE_PG_PORT", "CONSOLE_PG_DB",
    "CONSOLE_PG_USER", "CONSOLE_PG_PASSWORD",
    "CONSOLE_STORAGE_ENGINE", "CONSOLE_INSTANCE_ROLE", "CONSOLE_MAINTENANCE_MODE",
)

# 生产身份白名单：测试夹具在任何拨号之前就要拒绝这些。
PRODUCTION_HOSTS = {
    "100.64.0.10",
    "192.168.50.100",
    "console-postgres",
    "console-postgres.local",
}
PRODUCTION_DATABASES = {"console"}
PRODUCTION_USERS = {"console_nas", "console_mac", "console_admin", "console_owner"}


class TestPgUnavailable(RuntimeError):
    """测试 PG 底座不可用；调用方应 `pytest.fail` 而不是 skip。"""


class ProductionTargetError(RuntimeError):
    """测试试图连接生产目标：直接失败，这是安全事故而不是环境问题。"""


def assert_not_production(host, dbname, user):
    """建立连接前的硬闸门。三个字段任一命中生产身份即拒绝。"""
    host = (host or "").strip().lower()
    dbname = (dbname or "").strip().lower()
    user = (user or "").strip().lower()
    if host in PRODUCTION_HOSTS:
        raise ProductionTargetError(f"测试不得连接生产 host: {host}")
    if dbname in PRODUCTION_DATABASES:
        raise ProductionTargetError(f"测试不得连接生产 database: {dbname}")
    if user in PRODUCTION_USERS:
        raise ProductionTargetError(f"测试不得使用生产账号: {user}")


def assert_dsn_not_production(dsn):
    """从 DSN/conninfo 串里解析出 host/db/user 再校验。"""
    fields = {}
    for token in str(dsn).split():
        if "=" in token:
            key, _, value = token.partition("=")
            fields[key.strip().lower()] = value.strip()
    assert_not_production(
        fields.get("host"), fields.get("dbname"), fields.get("user")
    )
    return fields


def _run(cmd, timeout=120, check=True):
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=False
    )
    if check and proc.returncode != 0:
        raise TestPgUnavailable(
            f"命令失败: {' '.join(cmd)}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return proc


def docker_available():
    if shutil.which("docker") is None:
        return False
    proc = _run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=30, check=False)
    return proc.returncode == 0


class PgContainer:
    """一次性 PG16 容器。名字与端口都随机，互不共享。"""

    def __init__(self, name, host, port, user, password, dbname):
        self.name = name
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.dbname = dbname

    def dsn(self, user=None, password=None, dbname=None):
        return (
            f"host={self.host} port={self.port} dbname={dbname or self.dbname} "
            f"user={user or self.user} password={password or self.password}"
        )

    def __repr__(self):
        return f"PgContainer(name={self.name}, {self.host}:{self.port}/{self.dbname})"

    def stop(self):
        _run(["docker", "rm", "-f", self.name], timeout=60, check=False)


class NativePgContainer(PgContainer):
    """显式选择的本机测试实例；只监听 loopback，停止后保留临时证据。"""

    def __init__(self, bin_dir, directory, port):
        super().__init__("native", "127.0.0.1", port, ADMIN_USER, ADMIN_PASSWORD, TEST_DB)
        self.bin_dir = Path(bin_dir)
        self.directory = Path(directory)
        self.data_dir = self.directory / "data"

    def stop(self):
        # 只管理本次 mkdtemp 创建的实例，不调用系统服务管理器，也不删除证据。
        if (self.data_dir / "postmaster.pid").exists():
            _run(
                [str(self.bin_dir / "pg_ctl"), "-D", str(self.data_dir),
                 "-m", "immediate", "-w", "stop"],
                timeout=30,
            )


def _start_native_pg16(bin_dir, ready_timeout):
    """测试显式指定 native 工具时创建全新集群，绝不复用默认/生产数据目录。"""
    bin_dir = Path(bin_dir).resolve()
    required = ("initdb", "pg_ctl", "createdb", "postgres", "psql", "pg_dump", "pg_restore")
    if any(not (bin_dir / name).is_file() for name in required):
        raise TestPgUnavailable("原生 PG16 工具不完整：检查 CONSOLE_PG_TEST_NATIVE_BIN")
    version = _run([str(bin_dir / "postgres"), "--version"])
    if "PostgreSQL) 16." not in version.stdout:
        raise TestPgUnavailable("原生 PG16 工具版本不符，必须使用 PostgreSQL 16")
    parent = os.environ.get("CONSOLE_PG_TEST_TMPDIR") or None
    directory = Path(tempfile.mkdtemp(prefix="console-pg16-test-", dir=parent))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    container = NativePgContainer(bin_dir, directory, port)
    assert_not_production(container.host, container.dbname, container.user)
    try:
        _run(
            [str(bin_dir / "initdb"), "-D", str(container.data_dir), "-U", ADMIN_USER,
             "--auth-local=trust", "--auth-host=trust", "--encoding=UTF8",
             "--locale=C", "--data-checksums"],
            timeout=90,
        )
        _run(
            [str(bin_dir / "pg_ctl"), "-D", str(container.data_dir),
             "-l", str(directory / "postgres.log"), "-o",
             f"-h 127.0.0.1 -p {port} -k '' -c shared_buffers=32MB -c max_connections=100",
             "-w", "start"],
            timeout=ready_timeout,
        )
        _run(
            [str(bin_dir / "createdb"), "-h", "127.0.0.1", "-p", str(port),
             "-U", ADMIN_USER, TEST_DB],
            timeout=30,
        )
        wait_ready(container.dsn(), timeout=ready_timeout)
        return container
    except BaseException:
        container.stop()
        raise


def start_pg16(image=None, name=None, ready_timeout=90):
    """起一个隔离 PG16 实例；native 仅由显式测试开关启用。"""
    native_bin = (os.environ.get("CONSOLE_PG_TEST_NATIVE_BIN") or "").strip()
    if native_bin:
        return _start_native_pg16(native_bin, ready_timeout)
    image = image or DEFAULT_IMAGE
    name = name or f"console-pg00-test-{uuid.uuid4().hex[:10]}"
    if not docker_available():
        raise TestPgUnavailable(
            "docker 不可用：请启动 Docker/OrbStack，或设置 CONSOLE_PG_TEST_DSN 指向隔离测试库"
        )
    _run(["docker", "rm", "-f", name], timeout=60, check=False)
    _run(
        [
            "docker", "run", "-d",
            "--name", name,
            "-e", f"POSTGRES_USER={ADMIN_USER}",
            "-e", f"POSTGRES_PASSWORD={ADMIN_PASSWORD}",
            "-e", f"POSTGRES_DB={TEST_DB}",
            "-e", "POSTGRES_INITDB_ARGS=--data-checksums",
            # 随机宿主端口，避免与任何既有 PG 撞车
            "-p", "127.0.0.1::5432",
            "--tmpfs", "/var/lib/postgresql/data",
            image,
        ],
        timeout=180,
    )
    port = _published_port(name)
    container = PgContainer(name, "127.0.0.1", port, ADMIN_USER, ADMIN_PASSWORD, TEST_DB)
    assert_not_production(container.host, container.dbname, container.user)
    wait_ready(container.dsn(), timeout=ready_timeout)
    return container


def _published_port(name):
    deadline = time.time() + 30
    while time.time() < deadline:
        proc = _run(["docker", "port", name, "5432/tcp"], timeout=30, check=False)
        text = (proc.stdout or "").strip()
        if text:
            # 形如 127.0.0.1:54321
            return int(text.splitlines()[0].rsplit(":", 1)[1])
        time.sleep(0.5)
    raise TestPgUnavailable(f"无法取得容器 {name} 的映射端口")


def wait_ready(dsn, timeout=90):
    """轮询到能真正执行 `SELECT 1` 为止；`pg_isready` 只证明端口开着，不够。"""
    import psycopg

    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        try:
            with psycopg.connect(dsn, connect_timeout=3) as conn:
                conn.execute("SELECT 1")
            return True
        except Exception as exc:  # noqa: BLE001 - 启动期任何异常都只是「还没好」
            last_error = exc
            time.sleep(0.5)
    raise TestPgUnavailable(f"PG 在 {timeout}s 内未就绪：{type(last_error).__name__}")


# --- 夹具 -------------------------------------------------------------------

@pytest.fixture(scope="session")
def pg_container():
    """隔离 PG16 容器（session 级）。优先用外部 DSN，其次 docker。"""
    external = (os.environ.get("CONSOLE_PG_TEST_DSN") or "").strip()
    if external:
        try:
            fields = assert_dsn_not_production(external)
            wait_ready(external, timeout=30)
        except ProductionTargetError as exc:
            pytest.fail(f"CONSOLE_PG_TEST_DSN 指向生产目标，拒绝运行：{exc}")
        except TestPgUnavailable as exc:
            pytest.fail(f"CONSOLE_PG_TEST_DSN 不可用：{exc}")
        container = PgContainer(
            "external",
            fields.get("host", ""),
            int(fields.get("port", 5432)),
            fields.get("user", ""),
            fields.get("password", ""),
            fields.get("dbname", ""),
        )
        yield container
        return
    try:
        container = start_pg16()
    except TestPgUnavailable as exc:
        pytest.fail(f"PG16 测试底座不可用：{exc}")
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture(scope="session")
def pg_dsn(pg_container):
    return pg_container.dsn()


@pytest.fixture()
def pg_online(pg_container, monkeypatch):
    """跨模块在线 PG 夹具：独立库 + schema12 + 环境变量指向它。

    PG-05 集成收口：`renewals`/`ledger`/`node_traffic`/`monitor_events` 等模块已按
    决策 D3 只走 PG（`db_path=` 形参保留但忽略，**不回退 SQLite**）。仍按旧 SQLite
    契约建库的跨模块测试（`test_attention`/`test_inspection_report`/`test_machines_api`）
    因此必须真正指向 PG，而不是继续塞一个 `tmp_path/console.db`。

    `yield` 出本测试库的 DSN；退出时关池并 DROP DATABASE。
    """
    import pg_migrations

    name = f"console_test_online_{uuid.uuid4().hex[:8]}"
    admin = connect(pg_container.dsn(), autocommit=True)
    admin.execute(f'CREATE DATABASE "{name}"')
    admin.close()
    conn = connect(pg_container.dsn(dbname=name), autocommit=True)
    try:
        pg_migrations.apply(conn)
    finally:
        conn.close()
    # `apply_env` 直写 os.environ：本夹具必须自己还原，否则把 postgres 模式泄漏给
    # 后续测试模块（PG-05 集成发现的 121 项连带失败就是这么来的）。
    saved = {key: os.environ.get(key) for key in _ENV_KEYS}
    apply_env(pg_container, os.environ)
    os.environ["CONSOLE_PG_DB"] = name
    # `apply_env` 默认写 `CONSOLE_INSTANCE_ROLE=test`；调用方若已显式声明角色
    # （如采集类测试的 authority），保留它的声明，不被夹具改回去。
    if saved.get("CONSOLE_INSTANCE_ROLE"):
        os.environ["CONSOLE_INSTANCE_ROLE"] = saved["CONSOLE_INSTANCE_ROLE"]
    import pgdb

    pgdb.close_pool()
    try:
        yield pg_container.dsn(dbname=name)
    finally:
        pgdb.close_pool()
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        admin = connect(pg_container.dsn(), autocommit=True)
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


def connect(dsn, autocommit=False):
    """裸 psycopg 连接（夹具内部用；业务代码应走 `pgdb.pg_transaction`）。

    拨号前先过生产闸门——测试里任何一条连接都不允许指向 NAS 生产身份。
    """
    import psycopg
    from psycopg.rows import dict_row

    assert_dsn_not_production(dsn)
    return psycopg.connect(dsn, autocommit=autocommit, row_factory=dict_row)


@pytest.fixture()
def pg_admin(pg_dsn):
    """管理员连接：建角色/schema 用。"""
    conn = connect(pg_dsn, autocommit=True)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture()
def pg_conn(pg_dsn):
    """普通连接 #1（与 `pg_conn_two` 相互独立）。"""
    conn = connect(pg_dsn)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture()
def pg_conn_two(pg_dsn):
    """普通连接 #2：双连接并发/锁测试的第二条独立会话。"""
    conn = connect(pg_dsn)
    try:
        yield conn
    finally:
        conn.close()


def run_in_subprocess(source, env=None, timeout=120):
    """在独立进程里跑一段 Python（真并发/真断连用，不是同进程多连接）。

    返回 `(returncode, stdout, stderr)`；调用方断言 returncode。
    """
    merged = dict(os.environ)
    if env:
        merged.update(env)
    proc = subprocess.run(
        [sys.executable, "-c", source],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=merged,
        cwd=os.path.dirname(os.path.abspath(__file__)),
    )
    return proc.returncode, proc.stdout, proc.stderr


def apply_env(config, environ=None):
    """把 `PgConfig` 写进环境变量，供 `pgdb` 走标准配置路径。"""
    env = os.environ if environ is None else environ
    env["CONSOLE_PG_HOST"] = config.host
    env["CONSOLE_PG_PORT"] = str(config.port)
    env["CONSOLE_PG_DB"] = config.dbname
    env["CONSOLE_PG_USER"] = config.user
    env["CONSOLE_PG_PASSWORD"] = config.password
    env["CONSOLE_STORAGE_ENGINE"] = "postgres"
    env["CONSOLE_INSTANCE_ROLE"] = "test"
    env["CONSOLE_MAINTENANCE_MODE"] = "normal"
    return env


def create_app_role(admin_conn, role, password, tables=None, database=None):
    """建一个受限于 DML 的测试运行账号（对齐 §6.3 的 `console_nas`/`console_mac` 权限面）。"""
    tables = list(tables or ())
    database = database or TEST_DB
    quoted_role = f'"{role}"'
    admin_conn.execute(
        f"DROP ROLE IF EXISTS {quoted_role}"
    )
    # CREATE ROLE 是 utility statement，不接受参数绑定（%s 会被当成字面量 $1），
    # 必须用驱动提供的标识符/字面量组合接口。
    from psycopg import sql

    admin_conn.execute(
        sql.SQL(
            "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOREPLICATION PASSWORD {}"
        ).format(sql.Identifier(role), sql.Literal(password))
    )
    admin_conn.execute(f'GRANT CONNECT ON DATABASE "{database}" TO {quoted_role}')
    admin_conn.execute(f"GRANT USAGE ON SCHEMA public TO {quoted_role}")
    admin_conn.execute(f"REVOKE CREATE ON SCHEMA public FROM {quoted_role}")
    if tables:
        joined = ", ".join(f'public."{table}"' for table in tables)
        admin_conn.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {joined} TO {quoted_role}")
    admin_conn.execute(
        "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO " + quoted_role
    )
    # 提案 §6.3：schema_migrations 对运行账号**只读**（保留 SELECT，撤销写），
    # 版本升级只走管理员路径。
    try:
        admin_conn.execute(
            f"GRANT SELECT ON public.schema_migrations TO {quoted_role}"
        )
        admin_conn.execute(
            f"REVOKE INSERT, UPDATE, DELETE ON public.schema_migrations FROM {quoted_role}"
        )
    except Exception:  # 表还不存在时忽略
        pass
    return role


def drop_app_role(admin_conn, role):
    admin_conn.execute(f'DROP OWNED BY "{role}" CASCADE')
    admin_conn.execute(f'DROP ROLE IF EXISTS "{role}"')


def jsonb(value):
    """测试里写 JSONB 列时的显式序列化。"""
    return json.dumps(value, ensure_ascii=False)

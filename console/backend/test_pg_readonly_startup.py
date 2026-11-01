# -*- coding: utf-8 -*-
"""RO-01 回归：维护模式（`read_only`）下应用必须**能只读启动**（§9 D.5 / §11.9.2）。

首红（PG-05 r5 复现，证据 `evidence/r5/firstred-ro-01-readonly-startup.json`）：
`authority + read_only` 启动时 `_startup()` → `seed_workstation_data()` 只看角色不看
维护模式 → 写事务被 `require_writable()` 拒绝 → 进程在 import 期退出、**未监听 HTTP**。
即使用户库非空也一样（在读取行数之前就进了写事务）。

修复面：`collect_guard.collection_allowed()` = 角色面 **且** 非 `read_only`；
`is_collector()` 保留纯角色判断给「本地服务 vs 转交权威端」用（维护模式不改变实例身份）。
底层写拒绝（`pgdb.pg_transaction` → `require_writable`）**未改动**——只读下写请求仍是 503。

本文件用**真 PG16 + 两个受限运行账号**（console_nas/console_mac，§6.3 权限面）启动
**真子进程**（`python app.py` → waitress）并走真 HTTP，判据只看实际行为：
启动与否、读状态码、写状态码、NUMERIC 数值、DB 行数/文件树是否变化。

探针卫生（RM-03，2026-09-11 主控 §11.11.7）：① NUMERIC 判据**解析 JSON** 后核对
`cost` 是 int/float——子串判据 `"1234.56" in body` 对字符串形态同样命中，没有区分力；
② `_free_port()` 是先 bind 后 close 的取样（TOCTOU），端口若在取样与 app 绑定之间被
抢走，`_start_app()` 只在日志明说端口被占时换端口重试，其余启动失败原样暴露；
③ 探针实例固定 `HINDSIGHT_PROXY_PORT=0`，避开本机预览后端占着的 44683，
启动日志里不允许再出现 `Address already in use`（那不算「干净启动」证据）。
"""
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import psycopg
import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

from psycopg.rows import dict_row  # noqa: E402

import pg_migrations  # noqa: E402
import pg_test_support  # noqa: E402
from plugin_imports import load_module  # noqa: E402

feed_routes = load_module("plugins.feed.backend.routes")


NAS_PW = "ro01_regression_nas"
MAC_PW = "ro01_regression_mac"
READ_PATHS = ("/api/health", "/api/bookmarks", "/api/renewals", "/api/ledger/tx",
              "/api/config/effective")
# 业务文件面（排除 __pycache__：那是解释器行为，不是业务副作用）
SKIP_DIRS = {"__pycache__", "data"}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _copy_backend(dest):
    def _ignore(dirname, names):
        return [n for n in names if n in SKIP_DIRS or n.endswith(".pyc")]
    shutil.copytree(BACKEND_DIR, dest, ignore=_ignore)
    shutil.copytree(BACKEND_DIR.parent.parent / "plugins", dest / "plugins", ignore=_ignore)
    return dest


@pytest.fixture()
def backend_copy(tmp_path):
    return _copy_backend(tmp_path / "backend")


@pytest.fixture()
def readonly_env(pg_container, pg_dsn):
    """真 PG16：schema12 + §6.3 角色矩阵 + 两个受限运行账号口令 + 一行 NUMERIC 探针。"""
    admin = pg_test_support.connect(pg_dsn, autocommit=True)
    name = f"console_ro01_{os.urandom(3).hex()}"
    admin.execute(f'CREATE DATABASE "{name}"')
    try:
        with psycopg.connect(pg_container.dsn(dbname=name), autocommit=True,
                             row_factory=dict_row) as conn:
            pg_migrations.bootstrap_roles(conn, database=name)
            pg_migrations.apply_as_owner(conn)
        # utility statement 不接受参数绑定（同 `pg_test_support.create_app_role` 的坑）
        from psycopg import sql as _sql
        for role, password in (("console_nas", NAS_PW), ("console_mac", MAC_PW)):
            admin.execute(_sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                _sql.Identifier(role), _sql.Literal(password)))
        # NUMERIC 读取探针（管理员写入，避免与「受限账号只读」混为一谈）
        with psycopg.connect(pg_container.dsn(dbname=name), autocommit=True) as conn:
            conn.execute(
                "INSERT INTO renewals (id, name, cost, currency) VALUES "
                "('ro01-num', 'NUMERIC 探针', 1234.56, 'CNY') ON CONFLICT (id) DO NOTHING")
        # 该用例验证独立 PG 维护态；NAS 开关使用隔离只读 HTTP 服务。
        import threading

        from flask import Flask, jsonify
        from werkzeug.serving import make_server
        authority = Flask("plugin-state-fixture")
        @authority.get('/api/plugins')
        def plugins():
            return jsonify([{'id': pid, 'enabled': True} for pid in ('ledger', 'feed', 'audit', 'panels', 'renewals')])
        server = make_server('127.0.0.1', 0, authority, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield {"container": pg_container, "database": name, "authority": f"http://127.0.0.1:{server.server_port}"}
        finally:
            server.shutdown()
            thread.join(timeout=5)
    finally:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


def _counts(env) -> dict:
    container = env["container"]
    with psycopg.connect(container.dsn(dbname=env["database"]), autocommit=True) as conn:
        return {table: conn.execute(f'SELECT count(*) FROM public."{table}"').fetchone()[0]
                for table in pg_migrations.SCHEMA_TABLES}


def _file_tree(root: pathlib.Path) -> dict:
    tree = {}
    for path in sorted(root.rglob("*")):
        if any(part in SKIP_DIRS for part in path.parts) or path.suffix == ".pyc":
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        tree[str(path.relative_to(root))] = [st.st_size if path.is_file() else None,
                                             int(st.st_mtime)]
    return tree


def _http(method, url, payload=None, timeout=10):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, (exc.read() or b"").decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - 连不上 = 没监听
        return None, ""


class RunningApp:
    """真子进程 + 真 HTTP；退出码与监听状态都是判据的一部分。"""

    def __init__(self, backend, env, port):
        self.port = port
        self.ready = False
        self.returncode = None
        self.log = ""
        self._stopped = False
        self.proc = subprocess.Popen(
            [sys.executable, "app.py"], cwd=str(backend), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def wait_ready(self, timeout=40):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                return self.ready
            status, _ = _http("GET", f"http://127.0.0.1:{self.port}/api/health", timeout=3)
            if status == 200:
                self.ready = True
                return True
            time.sleep(0.4)
        return self.ready

    def stop(self):
        if self._stopped:
            return self.returncode, self.log
        self._stopped = True
        if self.proc.poll() is None:
            self.proc.terminate()
        try:
            out, _ = self.proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            out, _ = self.proc.communicate()
        self.returncode = self.proc.returncode
        self.log = out or ""
        return self.returncode, self.log


def _start_app(backend, make_env, *, attempts=3):
    """启动子进程并等到健康检查通过；只有「端口被抢」才换端口重试。

    `_free_port()` 是 bind-then-close 的取样（TOCTOU）：端口可能在取样与 app 绑定
    之间被别的进程拿走，表现为 waitress 绑定失败、进程退出。其余启动失败原样返回
    —— 例如 RO-01 回归时的 `ReadOnlyMode`，**不能**被重试掩盖成偶发。
    """
    app = None
    for _ in range(attempts):
        port = _free_port()
        app = RunningApp(backend, make_env(port), port)
        if app.wait_ready():
            return app
        app.stop()
        if "Address already in use" not in app.log:
            break
    return app


def _app_env(env, *, role, user, password, mode, port):
    base = {key: value for key, value in os.environ.items()
            if not key.startswith("CONSOLE_") and key != "PORT"}
    base.update({
        "CONSOLE_STORAGE_ENGINE": "postgres",
        "CONSOLE_INSTANCE_ROLE": role,
        "CONSOLE_AUTHORITY_ORIGIN": env.get("authority", "http://nas.example.test:8081"),
        "CONSOLE_MAINTENANCE_MODE": mode,
        "CONSOLE_PG_HOST": env["container"].host,
        "CONSOLE_PG_PORT": str(env["container"].port),
        "CONSOLE_PG_DB": env["database"],
        "CONSOLE_PG_USER": user,
        "CONSOLE_PG_PASSWORD": password,
        "PORT": str(port),
        # 探针卫生（RM-03）：hindsight 反代默认 0.0.0.0:44683，本机预览后端已占着它，
        # 探针实例每次启动都留一段 Errno 48 traceback——0=内核分配，日志才配叫干净启动。
        "HINDSIGHT_PROXY_PORT": "0",
        "FLASK_ENV": "production",
    })
    return base


def _find_renewal(node, renewal_id="ro01-num"):
    if isinstance(node, dict):
        if node.get("id") == renewal_id:
            return node
        for value in node.values():
            found = _find_renewal(value, renewal_id)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_renewal(item, renewal_id)
            if found is not None:
                return found
    return None


def _numeric_probe(body):
    """解析 JSON 后核对探针行的 `cost` 是 **JSON number**（RM-03）。

    子串判据（`"1234.56" in body`）没有区分力——字符串形态 `"cost":"1234.56"` 同样
    命中；带空格的 token（`'"cost": 1234.56'`）在 Flask 紧凑分隔符下更是恒假。
    只认「解析后是 int/float、不是 bool、且等于 1234.56」。
    """
    try:
        data = json.loads(body or "")
    except ValueError:
        return {"parsed": False, "found": False, "is_number": False}
    row = _find_renewal(data)
    if row is None:
        return {"parsed": True, "found": False, "is_number": False}
    value = row.get("cost")
    return {"parsed": True, "found": True, "value": value,
            "value_type": type(value).__name__,
            "is_number": isinstance(value, (int, float)) and not isinstance(value, bool)}


def _run(backend, env, *, role, user, password, mode):
    """启动→读→写→停，返回判定所需的一切实际观测。"""
    before, files_before = _counts(env), _file_tree(backend)
    app = _start_app(backend, lambda port: _app_env(
        env, role=role, user=user, password=password, mode=mode, port=port))
    port = app.port
    result = {"started": app.ready, "reads": {}, "write": None, "numeric": None}
    if app.ready:
        result["reads"] = {path: _http("GET", f"http://127.0.0.1:{port}{path}")[0]
                           for path in READ_PATHS}
        status, body = _http("GET", f"http://127.0.0.1:{port}/api/renewals")
        result["numeric"] = {"status": status, **_numeric_probe(body)}
        result["write"] = _http(
            "POST", f"http://127.0.0.1:{port}/api/bookmarks",
            {"id": f"ro01-reg-{mode}", "title": "回归探针", "url": "https://example.invalid"})[0]
        time.sleep(1.5)     # 给后台线程时间暴露副作用
    result["exit_code"], log = app.stop()
    result["row_changes"] = {table: [before[table], _counts(env)[table]]
                             for table in before if before[table] != _counts(env)[table]}
    result["file_changes"] = sorted(set(_file_tree(backend)) - set(files_before))
    result["log"] = log
    # 启动日志里不允许出现端口冲突：那意味着这次「启动成功」可能来自别的进程/脏环境
    result["bind_log_clean"] = "Address already in use" not in log
    return result


# --- 只读启动：NAS(authority) 与 Mac(client) 都必须起来并服务业务读 ------------------


@pytest.mark.parametrize("role,user,password", [
    ("authority", "console_nas", NAS_PW),
    ("client", "console_mac", MAC_PW),
])
def test_read_only_mode_starts_and_serves_reads(backend_copy, readonly_env,
                                                role, user, password):
    """RO-01 关闭标准：read_only 下启动成功、读 200、写 503、无 DB/业务文件副作用。"""
    result = _run(backend_copy, readonly_env, role=role, user=user,
                  password=password, mode="read_only")
    assert result["started"], f"read_only 下未启动（exit={result['exit_code']}）：{result['log'][-800:]}"
    blocked = {path: status for path, status in result["reads"].items()
               if status not in (200, 404)}
    assert not blocked, f"只读启动后业务读失败：{blocked}"
    numeric = result["numeric"]
    assert numeric["status"] == 200 and numeric["is_number"] is True \
        and numeric["value"] == 1234.56, numeric
    assert result["bind_log_clean"], f"启动日志含端口冲突：{result['log'][-800:]}"
    assert result["write"] == 503            # 底层写拒绝保留（不是放行、也不是 500）
    assert not result["row_changes"], result["row_changes"]
    assert not result["file_changes"], result["file_changes"]


def test_read_only_authority_does_not_seed(backend_copy, readonly_env):
    """read_only 下 seed 在写事务之前跳过（bookmarks 保持管理员预置的那一行）。

    判据看**行为**（行数）与「启动期没有撞上写拒绝」：skip 日志是 INFO 级
    （与既有三处采集跳过一致，root logger 默认不显示），不拿日志当唯一判据。
    """
    result = _run(backend_copy, readonly_env, role="authority", user="console_nas",
                  password=NAS_PW, mode="read_only")
    assert result["started"]
    assert not result["row_changes"].get("bookmarks")
    assert result["reads"].get("/api/bookmarks") == 200
    # read_only 下唯一的写拒绝应当来自我们主动发的那次写请求，而不是启动路径
    assert result["write"] == 503
    assert result["bind_log_clean"], f"启动日志含端口冲突：{result['log'][-800:]}"



def test_normal_authority_still_seeds_and_writes(backend_copy, readonly_env):
    """对照：normal 下 NAS 仍 seed、仍可写——修复没有把功能一起关掉。"""
    result = _run(backend_copy, readonly_env, role="authority", user="console_nas",
                  password=NAS_PW, mode="normal")
    assert result["started"]
    assert result["write"] == 201
    assert result["row_changes"].get("bookmarks", [0, 0])[1] >= 2   # 预置 1 行 + seed
    assert result["bind_log_clean"], f"启动日志含端口冲突：{result['log'][-800:]}"


def test_read_only_authority_serves_observation_paths_locally(backend_copy, readonly_env):
    """维护模式不改变实例身份：authority 在 read_only 下不得把观测 GET 转交出去。

    首红的一个隐藏面：若把「是否允许采集」直接当成「要不要转交」的判据，NAS 会去
    转发给自己（未配置 `CONSOLE_AUTHORITY_ORIGIN` → 503），观测类读取就废了。
    这里对转交白名单逐个核对：authority 本地服务，响应里不能出现转交失败文案。
    """
    app = _start_app(backend_copy, lambda port: _app_env(
        readonly_env, role="authority", user="console_nas", password=NAS_PW,
        mode="read_only", port=port))
    port = app.port
    try:
        assert app.ready, app.stop()[1][-800:]
        for path in ("/api/monitor", "/api/machines", "/api/services", "/api/feed"):
            status, body = _http("GET", f"http://127.0.0.1:{port}{path}")
            assert status != 503 or "CONSOLE_AUTHORITY_ORIGIN" not in body, \
                f"{path} 被当成 client 转交了（{status}）：{body[:200]}"
            assert status in (200, 404), f"{path} → {status} {body[:200]}"
    finally:
        _, log = app.stop()
    assert "Address already in use" not in log, f"启动日志含端口冲突：{log[-800:]}"


def test_seed_skips_before_entering_the_write_path(monkeypatch):
    """复核 #5：只断言「没有副作用」拦不住「吞掉写拒绝」这种绕过。

    §11.9.2 明禁「吞掉启动异常」；把 `seed_workstation_data` 的提前 return 换成
    try/except 吞 `ReadOnlyMode`，启动同样成功、行数同样不变——上面几条 HTTP 级用例
    一条都不会红。所以这里直接盯**写入口**：`read_only` + `authority` 下调用
    `seed_workstation_data()`，`storage_dispatch.write_tx` **一次都不能被调用**。
    """
    import app as console_app
    import storage_dispatch as sd

    monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", "authority")
    monkeypatch.setenv("CONSOLE_MAINTENANCE_MODE", "read_only")
    calls = []

    def boom(*args, **kwargs):
        calls.append("write_tx")
        raise AssertionError("read_only 下不应进入写事务（这是被 §11.9.2 禁止的绕过）")

    monkeypatch.setattr(sd, "write_tx", boom)
    assert feed_routes.seed_workstation_data(console_app.plugin_context) == 0
    assert calls == []


def test_ledger_import_and_reclassify_fail_before_any_file_work(monkeypatch, tmp_path):
    """复核 #1 的库层守卫：维护模式下 import/reclassify 在**落盘之前**就失败。

    `publish_batch` 只写文件与回执、不写业务流水，所以 PG 写拒绝在这条路径上
    根本不会触发——必须自己先判可写，否则留下「有归档、没批次记录」的孤儿目录。
    """
    import ledger_files
    import runtime_mode

    monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", "authority")
    monkeypatch.setenv("CONSOLE_MAINTENANCE_MODE", "read_only")
    data_dir = tmp_path / "pay"
    (data_dir / "reference").mkdir(parents=True)
    uploads = [("bill.csv", b"a,b\n1,2\n")]      # archive() 收 (显示名, 字节) 对
    with pytest.raises(runtime_mode.ReadOnlyMode):
        ledger_files.open_session("import", batch_id=None, data_dir=data_dir,
                                  code_version="t").archive(uploads)
    assert not (data_dir / "archive").exists()


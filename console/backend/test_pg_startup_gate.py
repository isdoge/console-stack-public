# -*- coding: utf-8 -*-
"""G06 启动闸门回归：在线入口必须**显式声明**存储引擎（§11.3.2 / D3 / D7）。

**为什么用真子进程**：`_startup()` 在 `import app` 时执行（`app.py` 末尾），同进程里
断言会被先前已导入的 `app` 掩盖；而启动失败的真实运维表现就是**退出码**——
容器编排据此判 CrashLoop、`deploy.sh` 据此判部署失败，所以这里只看 rc 与 stderr。

**为什么要复制副本**：`paths.DB_PATH` 硬编码在 backend 目录下（无 env 覆盖），
在仓库里跑 `sqlite` 场景会真的落下 `data/console.db`——G06b 首红旁证里那个
163840 字节的文件正是这么来的。副本不含 `data/`，等价于新 checkout。

对应首红（`evidence/g-repro-first-red.md` §6.2，脚本 `g06b-repro.py`）：
无任何 `CONSOLE_*` 时 app 启动成功、`/api/health` 返回 200、并创建本地 SQLite。
"""
import os
import pathlib
import subprocess
import sys

import pytest

import backend_copy_support

BACKEND_DIR = pathlib.Path(__file__).resolve().parent

# `app.STORAGE_ENGINE_DECLARATION_EXIT_CODE`：与 2（PG 不可达）、3（schema 不兼容）并列。
# 这里硬编码而**不 import app**——import 本身就会触发启动序列，测试进程会自杀。
EXIT_DECLARATION = 4

# 子进程只做这一件事：跑完 `import app` 的启动序列即成功。
_CHILD_CODE = "import app; print('STARTUP_OK')"


def _copy_backend(dest):
    """kernel 真身 + 私人模块 + 插件（见 backend_copy_support）。"""
    return backend_copy_support.copy_backend(dest)


def _run_startup(backend_copy, **env_overrides):
    """在副本里以**干净 env**（剔除全部 `CONSOLE_*`）跑一次启动序列。

    剔除是关键：外层 pytest 的 `conftest.py` 会注入 `CONSOLE_STORAGE_ENGINE=sqlite`
    （测试底座的显式声明），漏进来就测不出「未声明」这条判据了。
    """
    env = {key: value for key, value in os.environ.items() if not key.startswith("CONSOLE_")}
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-c", _CHILD_CODE],
        cwd=str(backend_copy),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


@pytest.fixture()
def backend_copy(tmp_path):
    return _copy_backend(tmp_path / "backend")


def test_undeclared_engine_refuses_to_start(backend_copy):
    """判据 1：未声明 `CONSOLE_STORAGE_ENGINE` → 拒绝启动，不隐式回落 SQLite。

    这条正是 G06b 首红的反向：同一份干净副本、同样无 PG 配置，
    首红实测 rc=0 + health 200 + 落下 163840 字节 `console.db`。
    """
    proc = _run_startup(backend_copy)

    assert proc.returncode == EXIT_DECLARATION, (
        f"未声明引擎却启动了：rc={proc.returncode}\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}"
    )
    assert "未声明 CONSOLE_STORAGE_ENGINE" in proc.stderr
    assert "STARTUP_OK" not in proc.stdout
    # 拒绝启动必须发生在建库之前：不能留下半启动的本地库让下次误判。
    assert not (backend_copy / "data" / "console.db").exists()


@pytest.mark.parametrize("role", ["authority", "client"])
def test_sqlite_with_online_roles_is_refused(backend_copy, role):
    """判据 2：`sqlite` 只与 `test` 共存——两个**在线**角色都被拒。

    `authority` 是 NAS（采集并写业务流水）；`client` 是 Mac 在线页面。
    D3 原文：「NAS/Mac 指向同一 PostgreSQL；断连时不回退到可写 SQLite」。
    放行 `client` 等于允许「Mac 页面写本地 SQLite、与权威端分叉」，
    正是 D7 要取消的双运行路径。
    """
    proc = _run_startup(
        backend_copy,
        CONSOLE_STORAGE_ENGINE="sqlite",
        CONSOLE_INSTANCE_ROLE=role,
    )

    assert proc.returncode == EXIT_DECLARATION, (
        f"sqlite + {role} 却启动了：rc={proc.returncode}\nstderr={proc.stderr}"
    )
    assert "只允许与 CONSOLE_INSTANCE_ROLE=test 共存" in proc.stderr
    assert not (backend_copy / "data" / "console.db").exists()


def test_sqlite_with_undeclared_role_is_refused(backend_copy):
    """`client` 是 `instance_role()` 的**默认值** → 「角色不写」不能绕过判据 2。

    与判据 1 同一个道理：**必须显式声明**离线意图，默认值不算声明。
    这条是收紧判据的代价所在——本机离线预览因此必须显式声明 `test`。
    """
    proc = _run_startup(backend_copy, CONSOLE_STORAGE_ENGINE="sqlite")

    assert proc.returncode == EXIT_DECLARATION, (
        f"sqlite + 默认角色却启动了：rc={proc.returncode}\nstderr={proc.stderr}"
    )
    assert not (backend_copy / "data" / "console.db").exists()


def test_sqlite_with_test_role_is_the_explicit_offline_path(backend_copy):
    """判据 2 的放行侧：`test` 是唯一允许 SQLite 在线入口的角色。

    这条锁住「不误杀」：`test` 正是 `pg_test_support` 给所有 PG 夹具用的角色，
    也是本仓库测试底座的声明（见 `conftest.py`）。
    """
    proc = _run_startup(
        backend_copy,
        CONSOLE_STORAGE_ENGINE="sqlite",
        CONSOLE_INSTANCE_ROLE="test",
    )

    assert proc.returncode == 0, f"显式 test + sqlite 被误拒：stderr={proc.stderr}"
    assert "STARTUP_OK" in proc.stdout
    assert (backend_copy / "data" / "console.db").exists()


@pytest.mark.parametrize("mode", ["normal", "read_only"])
def test_pg_client_without_authority_is_refused_before_pg_connect(backend_copy, mode):
    proc = _run_startup(
        backend_copy,
        CONSOLE_STORAGE_ENGINE="postgres",
        CONSOLE_INSTANCE_ROLE="client",
        CONSOLE_MAINTENANCE_MODE=mode,
    )
    assert proc.returncode == EXIT_DECLARATION, proc.stderr
    assert "CONSOLE_AUTHORITY_ORIGIN" in proc.stderr
    assert not (backend_copy / "data" / "console.db").exists()

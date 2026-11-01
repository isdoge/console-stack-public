# -*- coding: utf-8 -*-
"""Regression tests for paths.py — 后端数据路径单一 owner。

不碰真实 console.db：只做路径结构断言，建库场景在临时目录子进程中验证。
"""
import pathlib
import shutil
import subprocess
import sys

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import paths  # noqa: E402

# ---------------------------------------------------------------------------
# 路径结构
# ---------------------------------------------------------------------------

class TestPathLayout:
    def test_backend_dir_contains_paths_module(self):
        """BACKEND_DIR 必须指向 paths.py 所在目录（由 __file__ 推导）。"""
        assert pathlib.Path(paths.BACKEND_DIR, "paths.py").is_file()

    def test_db_path_under_backend_data_dir(self):
        db = pathlib.Path(paths.DB_PATH)
        assert db.name == "console.db"
        assert db.parent.name == "data"
        assert db.parent.parent == pathlib.Path(paths.BACKEND_DIR)

    def test_db_path_is_absolute(self):
        assert pathlib.Path(paths.DB_PATH).is_absolute()

    def test_data_dir_exists_after_import(self):
        """模块导入即建目录（2026-07 CI 事故的修复行为）。"""
        assert pathlib.Path(paths.DB_PATH).parent.is_dir()


# ---------------------------------------------------------------------------
# 路径随源码位置移动 — 防止有人把 DB_PATH 改回写死绝对路径
# ---------------------------------------------------------------------------

class TestPathRelocation:
    def test_db_path_follows_module_location(self, tmp_path):
        """把 paths.py 复制到临时 checkout：DB_PATH 必须指向新位置，
        且导入是幂等的（目录已存在时 makedirs 不报错）。"""
        workdir = tmp_path / "checkout"
        workdir.mkdir()
        shutil.copy(BACKEND_DIR / "paths.py", workdir / "paths.py")
        # paths 的组合根来自 composition 叶子模块；两者都随 checkout 移动。
        shutil.copy(BACKEND_DIR / "composition.py", workdir / "composition.py")

        code = (
            "import paths, os\n"
            "assert paths.BACKEND_DIR == os.path.dirname(os.path.abspath(paths.__file__))\n"
            "assert paths.DB_PATH.startswith(paths.BACKEND_DIR), paths.DB_PATH\n"
            "import importlib\n"
            "importlib.reload(paths)\n"  # 目录已存在时再次导入不得报错
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=workdir, capture_output=True, text=True, timeout=30,
        )
        assert proc.returncode == 0, proc.stderr
        assert (workdir / "data").is_dir()

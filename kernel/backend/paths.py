# -*- coding: utf-8 -*-
"""集中管理后端数据路径。

DB_PATH 曾在 app.py / version_service.py / version_sync.py 各自重复定义，
2026-07 CI 事故（gitignored data/ 目录在新 checkout 缺失导致 sqlite 建库失败）
的 makedirs 修复被复制了三份。收敛到此单一 owner，避免路径与建目录逻辑漂移。

B7-1 起数据根来自实例组合：`CONSOLE_DATA_ROOT`（由 `composition.bootstrap_environment()`
在深层 import 前 setdefault，或用 `CONSOLE_COMPOSITION_FILE` 的 `roots.data`）。
无组合文件时仍是现役 `backend/data`，行为不变。显式组合根允许尚未创建；只有
现役 legacy 数据根在 import 时自动建目录（2026-07 事故的原有修复）。
"""
import os

import composition

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
_LEGACY_DATA_DIR = os.path.join(BACKEND_DIR, "data")


def _data_root():
    override = (os.environ.get("CONSOLE_DATA_ROOT") or "").strip()
    if override:
        return str(composition.root_path("data"))
    if composition.composition_file() is not None:
        return str(composition.root_path("data"))
    return _LEGACY_DATA_DIR


DATA_DIR = _data_root()
DB_PATH = os.path.join(DATA_DIR, "console.db")
# 现役 legacy：data/ 被 gitignore（*.db）排除，新 checkout 没有该目录，sqlite
# 会直接报 unable to open database file。显式组合根由安装器准备，读取不建目录。
if DATA_DIR == _LEGACY_DATA_DIR:
    os.makedirs(DATA_DIR, exist_ok=True)

# 原始账单、参考分类表及受控上传归档与数据库共用私有 data 挂载。
# 文件功能按需建目录，单纯读取账本不导入数据或写入凭证。
PAY_DATA_DIR = os.path.abspath(os.path.expanduser(
    os.environ.get("CONSOLE_PAY_DATA_DIR") or os.path.join(DATA_DIR, "pay")
))
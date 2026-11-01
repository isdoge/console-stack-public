# -*- coding: utf-8 -*-
"""测试辅助：在临时目录拼出与私人镜像 `/app` 同形的后端副本。

`console/backend/*.py` 多数已是指向 `kernel/backend` 的薄 shim（按 `parents[2]`
回指仓库布局），整目录复制到临时路径后 shim 会指向不存在的 `kernel/backend`。
因此副本按 `console/Dockerfile` 的 with-personal 叠加顺序拼装：kernel 真身 →
console 下仍是实现本体的私人模块（非 shim）→ 插件树。
"""
from __future__ import annotations

import pathlib
import shutil

CONSOLE_BACKEND = pathlib.Path(__file__).resolve().parent
REPO_ROOT = CONSOLE_BACKEND.parent.parent
KERNEL_BACKEND = REPO_ROOT / "kernel" / "backend"
SHIM_MARKER = "Thin relocating shim"


def is_shim(path):
    try:
        return SHIM_MARKER in path.read_text(encoding="utf-8")[:400]
    except (OSError, UnicodeDecodeError):
        return False


def copy_backend(dest, skip=("__pycache__", "data")):
    """kernel 真身 + console 私人模块 + plugins，返回 dest。"""
    dest = pathlib.Path(dest)

    def _ignore(_dirname, names):
        return [n for n in names if n in skip or n.endswith(".pyc")]

    shutil.copytree(KERNEL_BACKEND, dest, ignore=_ignore)
    for src in sorted(CONSOLE_BACKEND.glob("*.py")):
        target = dest / src.name
        if target.exists() or is_shim(src):
            continue
        shutil.copy2(src, target)
    shutil.copytree(REPO_ROOT / "plugins", dest / "plugins", ignore=_ignore)
    return dest

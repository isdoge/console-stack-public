"""可信仓库插件的固定路径导入；旧 CLI 模块名兼容桥。

插件根是**显式组合输入**（B7-1）：默认仍按仓库布局探测，但可用
`CONSOLE_PLUGIN_ROOT`（或组合 `roots.plugins`）指定；指定了就必须是含普通
文件 `contract.json` 的目录，否则 import 期 fail closed，不悄悄退回另一个根。

外置根即使目录名不是 `plugins`，也按 `plugins.<id>` 从**该根**导入，不会
误取父目录的 `plugins/`：把根挂成同名命名空间包，搜索路径只剩当前组合根。
目录或清单为符号链接一律拒绝（与组合清单校验同一口径）。
"""
import importlib
import importlib.util
import os
import sys
from pathlib import Path

import composition
from composition import CompositionError

NAMESPACE = 'plugins'


def plugin_root():
    override = (os.environ.get('CONSOLE_PLUGIN_ROOT') or '').strip()
    if override or composition.composition_file() is not None:
        root = (
            composition.resolve_root_path(Path.cwd(), 'plugins', override)
            if override else composition.root_path('plugins')
        )
        contract = root / 'contract.json'
        if not root.is_dir() or contract.is_symlink() or not contract.is_file():
            raise CompositionError(
                'CONSOLE_PLUGIN_ROOT 必须指向含 contract.json 的普通插件根，拒绝隐式回退'
            )
        return root
    here = Path(__file__).resolve().parent
    bundled = here / 'plugins'
    return bundled if bundled.is_dir() else here.parent.parent / 'plugins'


def _mount(root):
    """把任意命名的插件根固定为唯一 `plugins` 搜索路径，拒绝已有异根包。"""
    key = str(root)
    existing = sys.modules.get(NAMESPACE)
    if existing is not None:
        places = [str(Path(item).resolve()) for item in (getattr(existing, '__path__', None) or [])]
        if places != [str(root.resolve())]:
            raise CompositionError(
                f'插件命名空间已被 {places or "未知根"} 占用，拒绝混用两个插件根: {key}'
            )
        return
    spec = importlib.util.spec_from_loader(NAMESPACE, loader=None, is_package=True)
    package = importlib.util.module_from_spec(spec)
    package.__path__ = [key]
    package.__spec__.submodule_search_locations = [key]
    sys.modules[NAMESPACE] = package


def load_module(name):
    _mount(plugin_root())
    return importlib.import_module(name)


def optional_module(name):
    """已卸载插件返回 None；内部缺依赖仍失败，不吞掉损坏代码。"""
    try:
        return load_module(name)
    except ModuleNotFoundError as exc:
        if name == exc.name or name.startswith(str(exc.name) + '.'):
            return None
        raise

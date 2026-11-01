"""Host-only storage paths; container paths and the database operation lock stay unchanged."""
import os
import shlex
from pathlib import Path

NAS_ROOT = Path('/opt/console-stack')
KEYS = {'CONSOLE_HOST_BACKUP_DIR', 'CONSOLE_HOST_TRASH_DIR'}


def storage_paths(root, environ=None):
    root = Path(root).resolve()
    environ = os.environ if environ is None else environ
    configured = {}
    env_file = root / '.env'
    if env_file.is_file():
        for line in env_file.read_text(encoding='utf-8').splitlines():
            key, separator, raw = line.strip().removeprefix('export ').partition('=')
            key = key.strip()
            if not separator or key not in KEYS:
                continue
            values = shlex.split(raw, comments=True)
            if len(values) != 1 or key in configured:
                raise ValueError(f'{key} 为空、重复或格式不受支持')
            configured[key] = values[0]
    result = []
    for key, name, legacy in [('CONSOLE_HOST_BACKUP_DIR', '.bak', 'backup'),
                              ('CONSOLE_HOST_TRASH_DIR', '.trash', '.trash')]:
        if key in environ and (not environ[key] or
                               key in configured and environ[key] != configured[key]):
            raise ValueError(f'{key} 为空或宿主环境与 .env 冲突')
        default = Path('/opt') / name / 'console-stack' if root == NAS_ROOT else root / legacy
        path = Path(configured.get(key, environ.get(key, str(default))))
        if not path.is_absolute() or '..' in path.parts:
            raise ValueError(f'{key} 必须是无 .. 的绝对目录')
        path = path.resolve()
        if root == NAS_ROOT and path != default:
            raise ValueError(f'{key} 在 NAS 必须使用 {default}')
        if key in configured or key in environ:
            if path == root or root in path.parents or path in root.parents:
                raise ValueError(f'{key} 必须在项目目录之外，且不能是项目的祖先')
        result.append(path)
    backup, trash = result
    if backup == trash or backup in trash.parents or trash in backup.parents:
        raise ValueError('备份与回收目录不能重叠')
    return backup, trash


def backup_dir(root):
    return storage_paths(root)[0]


def trash_dir(root):
    return storage_paths(root)[1]


def backup_path(root, relative):
    """Resolve historical backup/... constants through the current host layout."""
    relative = Path(relative)
    if relative.is_absolute() or relative.parts[0] != 'backup' or '..' in relative.parts:
        raise ValueError('不是 backup 下的相对路径')
    return backup_dir(root).joinpath(*relative.parts[1:])


def timed_backup_dir(root):
    expected = backup_dir(root) / 'timed'
    override = os.environ.get('CONSOLE_BACKUP_ROOT')
    if override:
        path = Path(override)
        if not path.is_absolute():
            raise ValueError('CONSOLE_BACKUP_ROOT 必须是绝对路径')
        if Path(root).resolve() == NAS_ROOT and path.resolve() != expected:
            raise ValueError('NAS 定时备份必须位于统一备份目录的 timed 子目录')
        return path
    return expected

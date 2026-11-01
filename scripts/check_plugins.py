#!/usr/bin/env python3
"""Offline plugin contract checks. No application imports, database or network access."""
import argparse
import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'console/backend'))
import composition  # noqa: E402
from plugin_manifest import OWNER_KEYS, discover  # noqa: E402


def check(root=ROOT, *, check_index=True):
    root = Path(root)
    errors = []
    try:
        contract = json.loads((root / 'plugins/contract.json').read_text(encoding='utf-8'))
        manifests = discover(root / 'plugins', contract)
        # 与后端启动/前端构建读同一份组合清单（B7-1）：只声明不落盘、只落盘未声明都报错。
        composition.require_plugin_sources(root / 'plugins', manifests)
    except (ValueError, OSError) as exc:
        return [str(exc)]
    bridges = {}
    for p in (root / 'console/backend').glob('*.py'):
        match = re.search(r'load_module\("plugins\.([a-z0-9_]+)\.backend', p.read_text(encoding='utf-8'))
        if match:
            bridges[p.stem] = match[1]
    source = root / 'kernel/frontend/src'
    ts_bridges = {}
    for p in source.rglob('*'):
        if p.suffix not in ('.ts', '.tsx') or '.test.' in p.name:
            continue
        match = re.search(r"export \* from ['\"]@plugins/([^/]+)/", p.read_text(encoding='utf-8'))
        if match:
            ts_bridges[p.relative_to(source).with_suffix('').as_posix()] = match[1]
    for manifest in manifests:
        pid = manifest['id']
        folder = root / 'plugins' / pid
        if set(manifest.get('owns', {})) != set(OWNER_KEYS):
            errors.append(f'{pid}: owns 必须声明 tables/settings/files/tasks')
        for rel in ('__init__.py', 'backend/__init__.py', 'frontend/index.tsx'):
            p = folder / rel
            if not p.is_file() or p.is_symlink():
                errors.append(f'{pid}: 缺少普通文件 {rel}')
        backend = folder / 'backend/__init__.py'
        if backend.is_file():
            tree = ast.parse(backend.read_text(encoding='utf-8'))
            register = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'register'), None)
            if register is None or [a.arg for a in register.args.args] != ['app', 'context']:
                errors.append(f'{pid}: 入口必须为 register(app, context)')
        entry = folder / 'frontend/index.tsx'
        if entry.is_file():
            text = entry.read_text(encoding='utf-8')
            if not re.search(r'export\s+default\b|export\s*\{[^}]*\bdefault\b', text):
                errors.append(f'{pid}: 缺少默认页面导出')
            for flag, name in [('sidebar', 'Sidebar'), ('settings', 'Settings'), ('navigation', 'loadNavigation')]:
                if manifest.get(flag) and not re.search(r'export\s+(?:\{[^}]*\b'+name+r'\b|(?:async\s+function|function|const|class)\s+'+name+r'\b)', text):
                    errors.append(f'{pid}: 缺少声明的 {name} 导出')
        for p in (folder / 'backend').rglob('*.py'):
            if p.name.startswith('test'):
                continue
            tree = ast.parse(p.read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == 'core':
                    errors.append(f'{p.relative_to(root)}:{node.lineno}: 禁止 core 私有接线')
                imports = []
                if isinstance(node, ast.Import):
                    imports = [n.name for n in node.names]
                elif isinstance(node, ast.ImportFrom):
                    if node.level:
                        package = ['plugins', pid] + list(p.parent.relative_to(folder).parts)
                        base = package[:len(package) - node.level + 1]
                        imports = ['.'.join(base + ([node.module] if node.module else []) + [n.name]) for n in node.names]
                    else:
                        imports = [node.module or '']
                elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
                    called = node.func.attr if isinstance(node.func, ast.Attribute) else (node.func.id if isinstance(node.func, ast.Name) else '')
                    if called in ('import_module', '__import__', 'load_module', 'optional_module') and isinstance(node.args[0].value, str):
                        imports = [node.args[0].value]
                for name in imports:
                    if name == 'app' or name.split('.')[0] in bridges:
                        errors.append(f'{p.relative_to(root)}:{node.lineno}: 禁止 app/旧兼容桥导入 {name}')
                    if name == 'plugins' or (name.startswith('plugins.') and not name.startswith(f'plugins.{pid}.')):
                        errors.append(f'{p.relative_to(root)}:{node.lineno}: 禁止跨插件导入 {name}')
        for p in (folder / 'frontend').rglob('*'):
            if p.suffix not in ('.ts', '.tsx') or '.test.' in p.name:
                continue
            text = p.read_text(encoding='utf-8')
            for alias in re.findall(r"['\"]@/([^'\"]+)['\"]", text):
                if alias in ts_bridges:
                    errors.append(f'{p.relative_to(root)}: 禁止旧兼容桥 {alias}')
            for imported in re.findall(r"(?:from\s*|import\s*\()(['\"])([^'\"]+)\1", text):
                name = imported[1]
                if name.startswith('.'):
                    destination = (p.parent / name).resolve()
                    if folder.resolve() not in destination.parents:
                        errors.append(f'{p.relative_to(root)}: 插件相对导入越过自身边界 {name}')
            for target in re.findall(r"['\"]@plugins/([^/]+)/", text):
                if target != pid:
                    errors.append(f'{p.relative_to(root)}: 禁止跨插件导入 {target}')
    # Core may retain a pure export bridge for old callers; active code cannot depend on it.
    for p in (root / 'console/backend').glob('*.py'):
        if p.name.startswith('test') or p.stem in bridges or p.name.endswith('_test_support.py'):
            continue
        tree = ast.parse(p.read_text(encoding='utf-8'))
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and n.args and isinstance(n.args[0], ast.Constant):
                value = n.args[0].value
                if isinstance(value, str) and value.startswith('plugins.'):
                    errors.append(f'{p.relative_to(root)}:{n.lineno}: 框架动态依赖插件业务 {value}')
            names = [a.name for a in n.names] if isinstance(n, ast.Import) else ([n.module] if isinstance(n, ast.ImportFrom) and not n.level else [])
            for name in names:
                if name and (name.split('.')[0] in bridges or name.startswith('plugins.')):
                    errors.append(f'{p.relative_to(root)}:{n.lineno}: 框架依赖插件业务 {name}')
    for p in source.rglob('*'):
        if p.suffix not in ('.ts', '.tsx') or '.test.' in p.name or '.spec.' in p.name:
            continue
        relative = p.relative_to(source).with_suffix('').as_posix()
        if relative in ts_bridges:
            continue
        text = p.read_text(encoding='utf-8')
        for target in re.findall(r"(?:from\s*|import\s*\()(['\"])([^'\"]+)\1", text):
            name = target[1]
            resolved = name[2:] if name.startswith('@/') else None
            if name.startswith('.'):
                destination = (p.parent / name).resolve()
                try:
                    resolved = destination.relative_to(source.resolve()).as_posix()
                except ValueError:
                    resolved = None
                if any(destination == (root / 'plugins' / m['id']).resolve() or (root / 'plugins' / m['id']).resolve() in destination.parents for m in manifests):
                    errors.append(f'{p.relative_to(root)}: 框架直接依赖插件目录 {name}')
            if resolved and resolved.endswith(('.ts', '.tsx')):
                resolved = str(Path(resolved).with_suffix(''))
            if resolved in ts_bridges or re.match(r'@plugins/[^*/]+/(backend|frontend)/', name):
                errors.append(f'{p.relative_to(root)}: 框架依赖插件业务 {name}')
    if check_index:
        try:
            index = (root / 'plugins/README.md').read_text(encoding='utf-8')
        except OSError:
            return errors + ['plugins/README.md 索引缺失或不可读取']
        for manifest in manifests:
            pid = manifest['id']
            row = next((row for row in index.splitlines() if row.startswith(f'| [{pid}]({pid}/) |')), '')
            for value in [manifest['name'], manifest['version'], manifest['description'], *manifest['routes'], *manifest['api_prefixes']]:
                if value not in row:
                    errors.append(f'{pid}: plugins/README.md 索引缺少/过期字段 {value}')
        indexed_ids = re.findall(r'^\| \[([a-z0-9_]+)\]\([a-z0-9_]+/\) \|', index, re.MULTILINE)
        expected_ids = {manifest['id'] for manifest in manifests}
        if len(indexed_ids) != len(set(indexed_ids)) or set(indexed_ids) != expected_ids:
            errors.append('plugins/README.md 索引目录必须与安装清单一一对应')
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--no-index', action='store_true', help='For isolated removal/template checks only')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    errors = check(args.root, check_index=not args.no_index)
    print(json.dumps({'ok': not errors, 'errors': errors}, ensure_ascii=False) if args.json else '\n'.join(errors) or 'Plugin contracts: PASS')
    return int(bool(errors))


if __name__ == '__main__':
    raise SystemExit(main())

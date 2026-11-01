#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读数据导出：renewals / 三份 YAML 原文 / dashboard_layout → 单个 JSON。

P2-3 轻量激活（Track EXPORT）。目标是把「会被某个实现绑死」的配置与业务
数据一次性带出成可解析 JSON（json.load 可读），不承担恢复/回灌职责——
导入恢复属 E7 备份体系，本脚本绝不越界。

只读纪律（照 effective_config._applied_schema_version 的防御式读取）：

- DB 一律 `file:...?mode=ro`（URI 模式）打开：库缺失 / 表不存在 / 空表
  均显式降级为 null 字段 + warnings，不建库、不建表、不触发迁移，本脚本
  零写路径。DB 缺失时 --check 仍要求退出码 0：JSON 合法即通过，数据缺失
  由下游在 warnings 里看。
- YAML 只 open().read() 原文（不做 parse），空表/坏路径显式 null + warning，
  绝不悄悄吞掉。不是文本文件（二进制/不可读）同降级。
- 私有 panels 不导出；只检查环境/.env 的配置键是否存在，不解析或输出其值。导出文件里
  出现凭据即违规，先人工核 `.env` 挂载面再改代码。

引擎分派（PG-05 review B-3 修复）：

- `CONSOLE_STORAGE_ENGINE=postgres` 且未显式给出离线快照路径 → 走
  `pg_diagnostics.readonly_connection`（PG 只读事务），**不再**打开
  `console/backend/data/console.db`。
- 缺陷原状：本脚本是纯 SQLite，PG 割接后它仍读迁移前的旧库，并报出
  `schema_version: 11` + `warnings: []`——看起来成功，实际是过期数据。
- 仍然允许显式 `--db <path>` 指向离线快照（备份/取证场景），此路径不受影响。
- `meta.source_paths.db` 在 PG 分支下写明实际引擎，便于下游分辨数据来源。

输出形状（单 JSON 对象）：

{
  "meta": {
    "exported_at": "<ISO8601 UTC>",        # datetime.now(timezone.utc) 真实采样
    "exporter": "scripts/export_data.py",
    "schema_version": 9 | null,            # schema_migrations MAX(version)
    "source_paths": {                      # 实际采到的来源路径清单
      "db": "<path>",                      # 库缺失时 "unavailable: <path>"
      "machines_yaml": "<path>",
      "panels_yaml": "<path>",
      "assets_yaml": "<path>"
    }
  },
  "warnings": ["<可读的错误摘要，供下游/人工排查>"],
  "renewals": [ {...全列...} ],            # 空表为 []，表缺失为 null
  "yaml": {
    "machines": "<原文>" | null,
    "panels": "<原文>" | null,
    "assets": "<原文>" | null
  },
  "dashboard_layout": [ {...行...} ] | null
}
"""
import argparse
import datetime
import decimal
import json
import os
import sqlite3
import sys
from pathlib import Path


def resolve_repo_root():
    """仓库根判定：优先 CWD（NAS 上脚本可落在 /tmp，`cd <repo>` 后执行即命中），
    其次脚本自身位置（mac 仓库内 scripts/ 直跑）。都无 console 特征则退回脚本
    位置，让后续降级路径显式报「库/YAML 不存在」。"""
    candidates = (Path.cwd(), Path(__file__).resolve().parents[1])
    for base in candidates:
        if (base / "console" / "machines.yaml").is_file() or (
            base / "console" / "backend" / "data" / "console.db"
        ).is_file():
            return base
    return candidates[-1]


ROOT = resolve_repo_root()

# console/backend 在 sys.path 上（诊断只读入口、运行模式判定都在那里）。
_BACKEND = ROOT / "console" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

DEFAULT_DB = ROOT / "console" / "backend" / "data" / "console.db"
CONSOLE_DIR = ROOT / "console"

# 显式给出离线快照路径时（--db），即使引擎是 postgres 也按离线文件读：
# 那是备份/取证语义，不是「在线库读旧 SQLite」。缺省值不算显式给出。
def _explicit_offline_db(args):
    return Path(args.db).resolve() != Path(DEFAULT_DB).resolve()


def _postgres_engine():
    """PG 引擎判定；console/backend 不可导入时按 sqlite 处理（保守，不抛错）。"""
    try:
        import runtime_mode
    except ImportError:  # pragma: no cover - 极端裁剪环境
        return False
    try:
        return runtime_mode.storage_engine() == "postgres"
    except Exception:  # pragma: no cover - 配置非法时按非 PG 处理，由下游报错
        return False

YAML_FILES = (
    ("machines", CONSOLE_DIR / "machines.yaml"),
    ("panels", CONSOLE_DIR / "panels.yaml"),
    ("assets", CONSOLE_DIR / "assets.yaml"),
)


def warn(errors, message):
    errors.append(message)
    print(f"WARN: {message}", file=sys.stderr)


def read_table_rows(db_path, table, errors):
    """只读全表 → dict 行列表。库/表缺失、列缺失一律 null（表列缺失不猜列集）。"""
    target = Path(db_path)
    try:
        conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True, timeout=5)
    except (sqlite3.Error, OSError) as exc:
        warn(errors, f"db 不可打开（{target}）：{exc}；{table} 置 null")
        return None
    try:
        try:
            cols = [
                row[1]
                for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
            ]
        except sqlite3.Error as exc:
            warn(errors, f"{table} 列信息读取失败：{exc}；置 null")
            return None
        if not cols:
            warn(errors, f"表 {table} 不存在，置 null")
            return None
        try:
            cursor = conn.execute(f"SELECT * FROM {table}")
        except sqlite3.Error as exc:
            warn(errors, f"{table} 读取失败：{exc}；置 null")
            return None
        return [dict(zip(cols, row)) for row in cursor.fetchall()]
    finally:
        conn.close()


def read_schema_version(db_path, errors):
    target = Path(db_path)
    try:
        conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True, timeout=5)
    except (sqlite3.Error, OSError) as exc:
        warn(errors, f"db 不可打开（{target}）：{exc}；schema_version 置 null")
        return None
    try:
        try:
            row = conn.execute(
                "SELECT MAX(version) FROM schema_migrations"
            ).fetchone()
        except sqlite3.Error as exc:
            warn(errors, f"schema_migrations 读取失败：{exc}；置 null")
            return None
        return None if row[0] is None else int(row[0])
    finally:
        conn.close()


def read_pg_rows(table, errors):
    """PG 只读事务读全表 → dict 行列表；表缺失 / 不可达一律 null（不猜列集）。"""
    try:
        import pg_diagnostics
    except ImportError as exc:  # pragma: no cover - console/backend 不可用时
        warn(errors, f"PG 只读入口不可用（{exc}）；{table} 置 null")
        return None
    try:
        with pg_diagnostics.readonly_connection() as conn:
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()
    except Exception as exc:
        warn(errors, f"{table} 读取失败（PG）：{exc}；置 null")
        return None
    return [dict(row) if not isinstance(row, dict) else row for row in rows]


def read_pg_schema_version(errors):
    """PG 的 schema_migrations MAX(version)；不读旧 SQLite（§8.1 W5）。"""
    try:
        import pg_diagnostics
    except ImportError as exc:  # pragma: no cover
        warn(errors, f"PG 只读入口不可用（{exc}）；schema_version 置 null")
        return None
    try:
        with pg_diagnostics.readonly_connection() as conn:
            row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    except Exception as exc:
        warn(errors, f"schema_migrations 读取失败（PG）：{exc}；置 null")
        return None
    if row is None:
        return None
    value = row["max"] if isinstance(row, dict) and "max" in row else list(row.values())[0]
    return None if value is None else int(value)


def read_yaml_text(path, errors):
    """读 YAML 原文（不 parse），坏路径/非文本显式 null + warning。"""
    target = Path(path)
    try:
        data = target.read_bytes()
    except OSError as exc:
        warn(errors, f"无法读取 {target}：{exc}；置 null")
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        warn(errors, f"{target} 非 UTF-8 文本：{exc}；置 null（原文未导出）")
        return None


def resolve_yaml_path(name, path):
    """候选路径解析（照 app.py 双候选模式）：
    1. 显式 --db 给出的目录 / <name>.yaml（容器内是 /app/<name>.yaml）
    2. 仓库根 console/<name>.yaml
    候选探测是正常解析路径（app.py 同模式静默回退），本函数**不**产出 warning；
    两个候选都不存在时返回候选 1 原样，由调用方 warn 并置 null。
    """
    if path.is_file():
        return path
    fallback = CONSOLE_DIR / f"{name}.yaml"
    return fallback if fallback.is_file() else path


def _json_default(value):
    """PG NUMERIC 经 psycopg 回来的是 `Decimal`，SQLite 是 float —— 这里按 API 同口径收敛。

    缺陷原状（PG-05 r2 探针实测，2026-09-11）：PG 引擎下 `renewals.cost` 一旦有值，
    `json.dumps` 抛 `TypeError: Object of type Decimal is not JSON serializable`，
    **整份导出失败**；SQLite 路径返回 float，所以既有用例照不到这一格。
    超界（JSON number 不能无损往返）时**不降精度**：回退十进制字符串，宁可下游拿到字符串，
    也不静默给一个近似 float（`money.json_number` 的口径）。
    """
    if isinstance(value, decimal.Decimal):
        try:
            import money
        except ImportError:  # pragma: no cover - console/backend 不可用的裁剪环境
            return str(value)
        try:
            return money.json_number(value)
        except money.MoneyError:
            return money.canonical(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def private_panels_configured(errors):
    """Fail closed on a configured private path; never read its value or contents."""
    key = "CONSOLE_PANELS_HOST_PATH"
    if key in os.environ:
        return True
    try:
        with (ROOT / ".env").open(encoding="utf-8") as handle:
            for line in handle:
                name, separator, _ = line.strip().removeprefix("export ").partition("=")
                if separator and name.strip() == key:
                    return True
    except FileNotFoundError:
        return False
    except (OSError, UnicodeError):
        warn(errors, "无法确认私有 panels 配置；为保护凭据省略 panels")
        return True
    return False


def build_export(args):
    errors = []
    result = {
        "meta": {
            "exported_at": datetime.datetime.now(
                datetime.timezone.utc
            ).isoformat(),
            "exporter": "scripts/export_data.py",
        },
        "warnings": errors,
    }

    db_path = Path(args.db)
    # PG 引擎且未显式给出离线快照 → 读在线 PG，不碰迁移前的旧 SQLite（B-3 修复）。
    use_pg = _postgres_engine() and not _explicit_offline_db(args)
    result["meta"]["storage_engine"] = "postgres" if use_pg else "sqlite"

    if use_pg:
        result["meta"]["schema_version"] = read_pg_schema_version(errors)
    elif db_path.is_file():
        schema_version = read_schema_version(db_path, errors)
        result["meta"]["schema_version"] = schema_version
    else:
        warn(errors, f"db 不存在（{db_path}）；schema_version 与表数据置 null")
        result["meta"]["schema_version"] = None

    # YAML 不是 DB：PG 分支下按仓库内 console/ 定位，不跟着旧 SQLite 的目录走。
    if not args.no_yaml and not use_pg and db_path.parent != Path("/"):
        yaml_base = db_path.parent
    else:
        yaml_base = CONSOLE_DIR
    omit_private_panels = not args.no_yaml and private_panels_configured(errors)
    yaml_out = {}
    yaml_sources = {}
    for name, default_path in YAML_FILES:
        if args.no_yaml:
            yaml_out[name] = None
            yaml_sources[f"{name}_yaml"] = "skipped: --no-yaml"
            continue
        if name == "panels" and omit_private_panels:
            yaml_out[name] = None
            yaml_sources["panels_yaml"] = "omitted: private runtime configuration"
            warn(errors, "panels 私有运行配置已省略；完整内容请通过受保护的部署备份恢复")
            continue
        candidate = yaml_base / f"{name}.yaml"
        path = resolve_yaml_path(name, candidate)
        if path.is_file():
            yaml_out[name] = read_yaml_text(path, errors)
            yaml_sources[f"{name}_yaml"] = str(path)
        else:
            warn(errors, f"YAML 不存在（{path}）；{name} 置 null")
            yaml_out[name] = None
            yaml_sources[f"{name}_yaml"] = f"unavailable: {path}"
    result["yaml"] = yaml_out

    if use_pg:
        db_source = "postgres: online (CONSOLE_PG_*)"
    elif db_path.is_file():
        db_source = str(db_path)
    else:
        db_source = f"unavailable: {db_path}"
    result["meta"]["source_paths"] = {
        "db": db_source,
        **yaml_sources,
    }
    # 表读取放 source_paths 之后：warnings 里最后两条表级错误排在路径之后，
    # 便于下游一眼把「库本身不可用」与「表缺失」分开（顺序即严重度暗示）。
    if use_pg:
        result["renewals"] = read_pg_rows("renewals", errors)
        result["dashboard_layout"] = read_pg_rows("dashboard_layout", errors)
    else:
        result["renewals"] = read_table_rows(db_path, "renewals", errors)
        result["dashboard_layout"] = read_table_rows(
            db_path, "dashboard_layout", errors
        )
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="只读导出 renewals / YAML 原文 / dashboard_layout 到单个 JSON"
    )
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB),
        help=f"console.db 路径（默认 {DEFAULT_DB}）",
    )
    parser.add_argument(
        "--no-yaml",
        action="store_true",
        help="跳过 YAML 原文导出（yaml 段置 null）",
    )
    parser.add_argument(
        "--out",
        default="-",
        help="输出文件路径（默认 stdout，'-' 亦为 stdout）",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只校验输出为合法 JSON（重跑一次导出并 json.load，合法即退出 0）",
    )
    args = parser.parse_args(argv)

    if args.check:
        return run_check(args)

    payload = build_export(args)
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default) + "\n"

    if args.out and args.out != "-":
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"已导出 {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


def run_check(args):
    """--check：重跑一次导出（不带 --check，避免递归）并 json.load 其 stdout。

    与 build_export 的解耦靠 subprocess + 只读 stdout：warnings 走 stderr
    不污染 JSON。数据层缺失不算失败——只要输出是合法 JSON 退出码就是 0，
    缺失由 warnings 承载。
    """
    import subprocess

    child_argv = [sys.executable, str(Path(__file__).resolve()), "--db", args.db]
    if args.no_yaml:
        child_argv.append("--no-yaml")
    proc = subprocess.run(child_argv, capture_output=True, text=True)
    if proc.returncode != 0:
        print(
            "check 失败：导出命令退出码 {}\nstderr:\n{}".format(
                proc.returncode, proc.stderr
            ),
            file=sys.stderr,
        )
        return 1
    try:
        json.loads(proc.stdout)
    except ValueError as exc:
        print(
            f"check 失败：stdout 不是合法 JSON：{exc}\n（前 200 字符）\n"
            f"{proc.stdout[:200]}",
            file=sys.stderr,
        )
        return 1
    print("check 通过：stdout 为合法 JSON")
    return 0


if __name__ == "__main__":
    sys.exit(main())

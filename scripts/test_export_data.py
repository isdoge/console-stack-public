#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""export_data.py 的自测：tmp_path 构造假 DB / 假 YAML，钉死只读导出的输出契约
与各降级路径（库缺失 / 表缺失 / 空表 / 非 UTF-8 / --no-yaml / --check）。

跑法：`python3 -m pytest scripts/ -q`（pytest 之外零依赖，不需要真实数据）。

样本数据全部虚构：YAML 内容是占位文本，renewals 行是 item-N 假数据——
不把真实台账或拓扑写进仓库。核心要钉的契约只有三条：导出永远是合法 JSON、
坏数据降级成 null/warnings 而不是抛错、全程零写路径。
"""
import decimal
import json
import sqlite3
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import export_data as ed  # noqa: E402 —— 必须先把 scripts/ 放进 sys.path

SAMPLE_YAML = "machines: []  # test fixture\n"


def make_full_db(path, renewal_rows=2):
    """带 schema_migrations(v9) / renewals / dashboard_layout 三张表的最小库。"""
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT);"
        "INSERT INTO schema_migrations (version) VALUES (8), (9);"
        "CREATE TABLE renewals (id INTEGER PRIMARY KEY, name TEXT, expires_on TEXT);"
        + "".join(
            f"INSERT INTO renewals VALUES ({i}, 'item-{i}', '2026-09-1{i}');"
            for i in range(1, renewal_rows + 1)
        )
        + "CREATE TABLE dashboard_layout (id INTEGER PRIMARY KEY, key TEXT,"
        " x INTEGER, y INTEGER, w INTEGER);"
        "INSERT INTO dashboard_layout VALUES (1, 'attention', 0, 0, 12);"
    )
    conn.commit()
    conn.close()
    return path


def make_yaml_dir(path):
    path.mkdir(parents=True, exist_ok=True)
    for name in ("machines", "panels", "assets"):
        (path / f"{name}.yaml").write_text(SAMPLE_YAML, encoding="utf-8")
    return path


def build(tmp_db, no_yaml=False):
    return ed.build_export(Namespace(db=str(tmp_db), no_yaml=no_yaml))


# ---------------------------------------------------------------- 好场景


def test_full_export_contract(tmp_path):
    """全量好场景：schema_version / renewals 行 / dashboard_layout / yaml 全部就位，
    warnings 为空，输出能被 json.dumps 往返。"""
    db = make_full_db(tmp_path / "console.db")
    make_yaml_dir(tmp_path)

    result = build(db)

    assert result["meta"]["schema_version"] == 9
    assert [r["name"] for r in result["renewals"]] == ["item-1", "item-2"]
    assert result["dashboard_layout"] == [
        {"id": 1, "key": "attention", "x": 0, "y": 0, "w": 12}
    ]
    for name in ("machines", "panels", "assets"):
        assert result["yaml"][name] == SAMPLE_YAML
        assert result["meta"]["source_paths"][f"{name}_yaml"] == str(tmp_path / f"{name}.yaml")
    assert result["meta"]["source_paths"]["db"] == str(db)
    assert result["warnings"] == []
    assert json.loads(json.dumps(result, ensure_ascii=False))["meta"]["exporter"] == (
        "scripts/export_data.py"
    )


def test_empty_table_is_empty_list(tmp_path):
    """空表是 [] 不是 null：null 只留给「表缺失/库缺失」。"""
    db = make_full_db(tmp_path / "console.db", renewal_rows=0)
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM renewals")
    conn.commit()
    conn.close()

    result = build(db)

    assert result["renewals"] == []
    assert result["warnings"] == []


# ---------------------------------------------------------------- 降级路径


def test_missing_db_degrades(tmp_path):
    """库缺失：schema_version 与两张表置 null + warning；YAML 双候选回退到
    仓库 console/（回退本身是 app.py 同款正常路径，不算 warning）。"""
    result = build(tmp_path / "absent.db")

    assert result["meta"]["schema_version"] is None
    assert result["renewals"] is None
    assert result["dashboard_layout"] is None
    assert result["meta"]["source_paths"]["db"] == f"unavailable: {tmp_path / 'absent.db'}"
    assert any("db 不存在" in w for w in result["warnings"])
    # 回退命中仓库真文件：machines.yaml 是 git 管理文件，任何 checkout 都在
    assert result["meta"]["source_paths"]["machines_yaml"] == str(
        ed.CONSOLE_DIR / "machines.yaml"
    )
    assert result["yaml"]["machines"]  # 原文非空
    assert [w for w in result["warnings"] if "YAML" in w] == []


def test_missing_db_with_no_yaml_skips_yaml(tmp_path):
    result = build(tmp_path / "absent.db", no_yaml=True)

    for name in ("machines", "panels", "assets"):
        assert result["yaml"][name] is None
        assert result["meta"]["source_paths"][f"{name}_yaml"] == "skipped: --no-yaml"
    assert not any("YAML" in w for w in result["warnings"])


def test_missing_table_degrades(tmp_path):
    """库在、表缺：dashboard_layout 置 null + warning，renewals 不受牵连。"""
    db = make_full_db(tmp_path / "console.db")
    conn = sqlite3.connect(db)
    conn.execute("DROP TABLE dashboard_layout")
    conn.commit()
    conn.close()

    result = build(db)

    assert result["renewals"] and result["dashboard_layout"] is None
    assert any("表 dashboard_layout 不存在" in w for w in result["warnings"])


def test_absent_schema_migrations_degrades(tmp_path):
    conn = sqlite3.connect(tmp_path / "console.db")
    conn.execute("CREATE TABLE renewals (id INTEGER PRIMARY KEY, name TEXT)")
    conn.commit()
    conn.close()

    result = build(tmp_path / "console.db")

    assert result["meta"]["schema_version"] is None
    assert any("schema_migrations" in w for w in result["warnings"])


def test_non_utf8_yaml_degrades(tmp_path):
    """非 UTF-8 文本显式 null + warning，绝不带着坏字节进 JSON。"""
    db = make_full_db(tmp_path / "console.db")
    (tmp_path / "machines.yaml").write_bytes(b"\xff\xfe<binary>")
    for name in ("panels", "assets"):
        (tmp_path / f"{name}.yaml").write_text(SAMPLE_YAML, encoding="utf-8")

    result = build(db)

    assert result["yaml"]["machines"] is None
    assert result["yaml"]["panels"] == SAMPLE_YAML
    assert any("非 UTF-8" in w and "machines.yaml" in w for w in result["warnings"])


# ---------------------------------------------------------------- CLI 出口


def test_main_writes_out_file(tmp_path):
    db = make_full_db(tmp_path / "console.db")
    out = tmp_path / "export.json"

    assert ed.main(["--db", str(db), "--out", str(out)]) == 0

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert len(payload["renewals"]) == 2
    assert payload["meta"]["schema_version"] == 9


def test_check_passes_on_degraded_output(tmp_path):
    """--check 对「库缺失」也必须退出 0：契约是 JSON 合法即通过，
    数据缺失由 warnings 承载（NAS 上库路径不同也要能验收）。"""
    assert ed.main(["--check", "--db", str(tmp_path / "absent.db")]) == 0


def test_check_fails_on_bad_stdout(tmp_path, monkeypatch):
    """子进程 stdout 不是 JSON → 退出 1。monkeypatch 掉 subprocess 免起真进程。"""
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: Namespace(returncode=0, stdout="not json at all", stderr=""),
    )
    assert ed.main(["--check", "--db", str(tmp_path / "console.db")]) == 1


def test_check_fails_on_child_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **kw: Namespace(returncode=3, stdout="", stderr="boom")
    )
    assert ed.main(["--check", "--db", str(tmp_path / "console.db")]) == 1
    assert "退出码 3" in capsys.readouterr().err


def test_real_check_subprocess(tmp_path):
    """不经 monkeypatch 的端到端一次：真子进程跑本脚本 --check。"""
    make_full_db(tmp_path / "console.db")
    proc = subprocess.run(
        [sys.executable, str(Path(ed.__file__).resolve()),
         "--check", "--db", str(tmp_path / "console.db")],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr


# --- PG 引擎分派（PG-05 review B-3 回归） ------------------------------------


def test_pg_engine_does_not_read_legacy_sqlite(tmp_path, monkeypatch):
    """PG 引擎 + 缺省 --db → 不打开迁移前的旧 SQLite，改走 PG 只读入口。

    缺陷原状：脚本纯 SQLite，割接后读到旧库并报 `schema_version: 11` + `warnings: []`。
    这里不连真 PG（那由 test_pg05_end_to_end / 夹具覆盖），只钉死**分派判据**：
    PG 分支下 read_table_rows / read_schema_version 一次都不能被调用。
    """
    monkeypatch.setattr(ed, "_postgres_engine", lambda: True)
    # 造一个「旧库」：若脚本仍读它，返回值就会是这些数字
    legacy = tmp_path / "console.db"
    make_full_db(legacy)  # schema_version = 9
    monkeypatch.setattr(ed, "DEFAULT_DB", legacy)
    monkeypatch.setattr(ed, "_explicit_offline_db", lambda args: False)

    def boom(*a, **kw):  # pragma: no cover - 被调用即失败
        raise AssertionError("PG 引擎下不得读取旧 SQLite")

    monkeypatch.setattr(ed, "read_table_rows", boom)
    monkeypatch.setattr(ed, "read_schema_version", boom)
    monkeypatch.setattr(ed, "read_pg_rows", lambda table, errors: [])
    monkeypatch.setattr(ed, "read_pg_schema_version", lambda errors: 12)

    result = ed.build_export(Namespace(db=str(legacy), no_yaml=True))
    assert result["meta"]["storage_engine"] == "postgres"
    assert result["meta"]["schema_version"] == 12
    assert result["meta"]["source_paths"]["db"].startswith("postgres:")


def test_pg_numeric_decimal_serializes(tmp_path, monkeypatch):
    """PG 的 NUMERIC 经 psycopg 是 `Decimal`：导出必须仍产出合法 JSON。

    实跑缺陷（PG-05 r2 独立探针，2026-09-11，真 PG16 + 真 `CONSOLE_PG_*`）：
    PG 引擎下 `renewals.cost` 一旦有值，`json.dumps` 抛
    `TypeError: Object of type Decimal is not JSON serializable`，导出整体失败、
    stdout 为空；SQLite 路径返回 float，所以上面各例都照不到这一格。
    """
    monkeypatch.setattr(ed, "_postgres_engine", lambda: True)
    monkeypatch.setattr(ed, "_explicit_offline_db", lambda args: False)
    monkeypatch.setattr(ed, "read_pg_schema_version", lambda errors: 12)
    monkeypatch.setattr(
        ed, "read_pg_rows",
        lambda table, errors: (
            [{"id": "r-1", "name": "item-1", "cost": decimal.Decimal("4321.09")}]
            if table == "renewals" else []
        ),
    )
    out = tmp_path / "export.json"

    assert ed.main(["--no-yaml", "--out", str(out)]) == 0

    payload = json.loads(out.read_text(encoding="utf-8"))  # 非法 JSON 会在这里炸
    assert payload["renewals"][0]["cost"] == 4321.09
    assert payload["meta"]["schema_version"] == 12


def test_json_default_keeps_not_round_trippable_decimals_exact():
    """不能无损往返的金额不降精度：宁可给十进制字符串，也不给近似 float。

    判据取自 `money.is_json_safe`：`Decimal(str(float(v))) == v` 才算可往返。
    注意 10^30 这类**能**往返（float→str→Decimal 数学值相同），不在此列——
    选 `0.1234567890123456789` 这种 float 装不下的位数。
    """
    lossy = decimal.Decimal("0.1234567890123456789")
    assert ed._json_default(lossy) == "0.1234567890123456789"


def test_explicit_offline_db_still_reads_sqlite(tmp_path, monkeypatch):
    """显式 `--db <快照>` 仍是离线取证语义：PG 引擎下也照读该文件。"""
    monkeypatch.setattr(ed, "_postgres_engine", lambda: True)
    snapshot = tmp_path / "snapshot.db"
    make_full_db(snapshot)
    # 显式路径 != DEFAULT_DB → 走 SQLite 分支
    result = ed.build_export(Namespace(db=str(snapshot), no_yaml=True))
    assert result["meta"]["storage_engine"] == "sqlite"
    assert result["meta"]["schema_version"] == 9  # 快照自己的版本，不是 PG 的 12


def test_private_panels_from_environment_are_omitted(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ed, "ROOT", tmp_path)
    monkeypatch.setenv("CONSOLE_PANELS_HOST_PATH", str(tmp_path / "private-synthetic-path.yaml"))
    make_yaml_dir(tmp_path)
    (tmp_path / "panels.yaml").write_text("synthetic-sensitive-panel-content")
    result = build(make_full_db(tmp_path / "console.db"))
    assert result["yaml"]["panels"] is None
    assert "私有" in " ".join(result["warnings"])
    assert "部署备份" in " ".join(result["warnings"])
    combined = json.dumps(result) + capsys.readouterr().err
    assert "synthetic-sensitive-panel-content" not in combined
    assert "private-synthetic-path" not in combined
    assert result["yaml"]["machines"] == SAMPLE_YAML


def test_private_panels_dotenv_detection_does_not_expose_values(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ed, "ROOT", tmp_path)
    monkeypatch.delenv("CONSOLE_PANELS_HOST_PATH", raising=False)
    (tmp_path / ".env").write_text(
        "UNRELATED_SECRET=synthetic-secret\nexport CONSOLE_PANELS_HOST_PATH='/not/read/private.yaml'\n")
    make_yaml_dir(tmp_path)
    result = build(make_full_db(tmp_path / "console.db"))
    assert result["yaml"]["panels"] is None
    combined = json.dumps(result) + capsys.readouterr().err
    assert "synthetic-secret" not in combined
    assert "/not/read/private.yaml" not in combined

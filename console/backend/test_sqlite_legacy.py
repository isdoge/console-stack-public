# -*- coding: utf-8 -*-
"""sqlite_legacy 只读离线源读取（提案 §4.1）。"""
import pathlib
import sqlite3
import sys

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import db  # noqa: E402
import sqlite_legacy  # noqa: E402


@pytest.fixture()
def legacy_db(tmp_path):
    path = tmp_path / "console.db"
    db.run_migrations(str(path))
    conn = db.get_connection(str(path))
    try:
        conn.execute(
            "INSERT INTO renewals (id, name, kind, expires_at, sort_order) "
            "VALUES ('r1', '域名', 'domain', '2027-01-01', 1)"
        )
        conn.commit()
    finally:
        conn.close()
    return path


def test_missing_source_fails_instead_of_creating_empty_db(tmp_path):
    with pytest.raises(sqlite_legacy.LegacySourceError):
        sqlite_legacy.open_legacy_source(tmp_path / "nope.db")


def test_offline_source_is_read_only(legacy_db):
    conn = sqlite_legacy.open_legacy_source(legacy_db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(
                "INSERT INTO renewals (id, name, kind, expires_at, sort_order) "
                "VALUES ('r2', 'x', 'domain', '2027-01-01', 2)"
            )
    finally:
        conn.close()


def test_inventory_reports_tables_counts_and_sequence_high_water(legacy_db):
    conn = sqlite_legacy.open_legacy_source(legacy_db)
    try:
        inventory = sqlite_legacy.schema_inventory(conn)
    finally:
        conn.close()
    assert inventory["quick_check"] == "ok"
    assert inventory["schema_versions"] == [version for version, _, _ in db.MIGRATIONS]
    renewals = inventory["tables"]["renewals"]
    assert renewals["row_count"] == 1
    assert any(column["name"] == "provider" for column in renewals["columns"])
    assert isinstance(inventory["sequence_high_water"], dict)


def test_inventory_of_empty_table_reports_none_max_id(tmp_path):
    path = tmp_path / "empty.db"
    db.run_migrations(str(path))
    conn = sqlite_legacy.open_legacy_source(path)
    try:
        inventory = sqlite_legacy.schema_inventory(conn)
    finally:
        conn.close()
    assert inventory["tables"]["ledger_tx"]["row_count"] == 0
    assert inventory["tables"]["ledger_tx"]["max_id"] is None
